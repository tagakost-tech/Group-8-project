"""Personal data is kept only as long as it serves outreach (GDPR Art. 5(1)(e)).

Nothing ever deleted or anonymised anything by age: a contact scraped and
never emailed stayed forever, and so did one emailed once, years ago, who
never answered. Art. 14(2)(a) also requires telling the recipient how long
their data is kept - impossible while the answer was "indefinitely".

Once a day the agent now erases, with the same routine GDPR erasure uses:
- contacts not emailed within FIRST_CONTACT_MAX_AGE_DAYS (default 30);
- contacts emailed who never replied, RETENTION_DAYS_CONTACTED (default 365)
  after the last email - unless a follow-up or retry is still pending.

Replies, leads, clients and objections have their own periods
(test_every_contact_has_a_retention_period). The suppression list is never
touched - it is what keeps an opt-out honoured.
"""

import pytest

from agent import OutreachAgent
from modules.analytics import Analytics
from modules.export import erasure_token
from modules.retention import apply_retention


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "r.db"))


@pytest.fixture
def db(s):
    from modules.database import Database

    return Database(s.get_abs_db_path())


def _contact(db, email, *, age_days, status="pending", emailed_days_ago=None):
    sid = db.log_search("q")
    cid = db.add_contact(sid, email, name="Anna", organization="Brand")
    db.update_contact_status(cid, status)
    db.conn.execute(
        "UPDATE contacts SET timestamp = datetime('now','localtime',?) WHERE id = ?",
        (f"-{age_days} days", cid),
    )
    db.conn.commit()  # log_email writes through its own connection
    if emailed_days_ago is not None:
        log_id = db.log_email(cid, email, "Hello", delivery_status="sent")
        db.conn.execute(
            "UPDATE email_log SET timestamp = datetime('now','localtime',?) WHERE id = ?",
            (f"-{emailed_days_ago} days", log_id),
        )
    db.conn.commit()
    return cid


def _erased(db, cid):
    return db.get_contact_by_id(cid)["status"] == "erased"


def test_an_old_never_emailed_contact_is_erased(db, s):
    cid = _contact(db, "old@brand.de", age_days=100)
    apply_retention(db, s)
    assert _erased(db, cid)
    assert db.get_contact_by_id(cid)["email"] == erasure_token("old@brand.de")


def test_an_old_contact_who_never_replied_is_erased(db, s):
    cid = _contact(
        db, "quiet@brand.de", age_days=500, status="emailed", emailed_days_ago=400
    )
    apply_retention(db, s)
    assert _erased(db, cid)


def test_a_reply_outlasts_the_no_reply_period(db, s):
    """A client was kept here too, forever; it is now erased like any
    contact (decision L5.1)."""
    cid = _contact(
        db, "kept@brand.de", age_days=500, status="responded", emailed_days_ago=400
    )
    apply_retention(db, s)
    assert db.get_contact_by_id(cid)["status"] == "responded"


def test_a_lead_is_kept(db, s):
    cid = _contact(
        db, "lead@brand.de", age_days=500, status="emailed", emailed_days_ago=400
    )
    db.record_lead_alert("lead@brand.de", notified_to="ops@co.it", reply_excerpt="Yes")
    apply_retention(db, s)
    assert not _erased(db, cid)


def test_recent_contacts_are_kept(db, s):
    new = _contact(db, "new@brand.de", age_days=10)
    recent = _contact(
        db, "recent@brand.de", age_days=500, status="emailed", emailed_days_ago=100
    )
    apply_retention(db, s)
    assert not _erased(db, new)
    assert not _erased(db, recent), "erased 100 days after the last email, not 365"


def test_a_contact_with_a_follow_up_still_due_is_kept(db, settings_factory, tmp_path):
    """Its follow-up, due 45 days after the email, is still to come when the
    30-day period has passed: it would be the last email. An enrolment that
    can no longer progress holds nothing: see
    tests/test_a_stuck_follow_up_does_not_outlast_retention.py."""
    s = settings_factory(db_path=str(tmp_path / "r.db"), retention_days_contacted=30)
    cid = _contact(
        db, "seq@brand.de", age_days=50, status="emailed", emailed_days_ago=40
    )
    db.conn.execute("INSERT INTO sequences (name) VALUES ('S')")
    db.conn.executemany(
        "INSERT INTO sequence_steps (sequence_id, step_number, delay_days, subject) "
        "VALUES (1, ?, ?, 'Following up')",
        [(0, 45), (1, 3)],
    )
    db.conn.execute(
        "INSERT INTO sequence_enrollments (contact_id, sequence_id, status) VALUES (?, 1, 'active')",
        (cid,),
    )
    db.conn.commit()
    apply_retention(db, s)
    assert not _erased(db, cid)


def test_the_suppression_list_is_never_touched(db, s):
    cid = _contact(db, "gone@brand.de", age_days=100)
    db.add_to_suppression("gone@brand.de", "unsubscribe")
    apply_retention(db, s)
    assert _erased(db, cid)
    assert db.is_suppressed("gone@brand.de"), "an opt-out was forgotten"


def test_old_open_tracking_device_data_is_cleared(db, s):
    cid = _contact(db, "o@brand.de", age_days=10, status="emailed", emailed_days_ago=40)
    log_id = db.conn.execute(
        "SELECT id FROM email_log WHERE contact_id=?", (cid,)
    ).fetchone()[0]
    Analytics(db).generate_tracking_id(log_id, "t1")
    Analytics(db).record_open("t1", ip_address="203.0.113.5", user_agent="UA/1")
    db.conn.execute(
        "UPDATE open_tracking SET opened_at = datetime('now','localtime','-40 days')"
    )
    db.conn.commit()
    apply_retention(db, s)
    row = db.conn.execute("SELECT ip_address, user_agent FROM open_tracking").fetchone()
    assert tuple(row) == (None, None)


def test_an_erased_address_is_not_rediscovered(db, s):
    """Without this, a prospect erased for never replying is re-scraped and
    emailed again a year later - retention would reset the clock, not end it."""
    _contact(db, "quiet@brand.de", age_days=100)
    apply_retention(db, s)
    assert db.add_contact(db.log_search("q"), "quiet@brand.de") is None


def test_retention_days_below_thirty_are_refused(settings_factory):
    """At one day the job would erase the backlog before it could be emailed."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings_factory(retention_days_contacted=5)


def test_the_footer_states_the_retention_period(settings_factory):
    from modules.compliance import render_legal_footer

    html = render_legal_footer(settings_factory(), {}, "mailto:a@b.it")
    assert "365 days" in html


def test_the_footer_says_nothing_when_retention_is_off(settings_factory):
    """Stating a period that is not enforced would be a false statement."""
    from modules.compliance import render_legal_footer

    html = render_legal_footer(settings_factory(retention_enabled=False), {}, "m")
    assert "days" not in html


def test_doctor_warns_when_retention_is_off(settings_factory, tmp_path):
    from main import run_doctor_checks

    def check(**kw):
        s = settings_factory(db_path=str(tmp_path / "d.db"), **kw)
        return next(
            c
            for c in run_doctor_checks(s, network=False)
            if c.label == "Data retention"
        )

    assert check(retention_enabled=False).ok is False
    assert check().ok is True


def _agent(settings_factory, tmp_path, **kw):
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            heuristic_discovery_enabled=False,
            target_queries="",
            **kw,
        )
    )


def test_the_job_runs_once_a_day_and_never_in_a_dry_run(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path)
    calls = []
    monkeypatch.setattr(
        "modules.retention.apply_retention", lambda db, s: calls.append(1) or {}
    )
    a._maybe_apply_retention(dry_run=True)
    assert calls == []
    a._maybe_apply_retention(dry_run=False)
    a._maybe_apply_retention(dry_run=False)
    assert calls == [1]


def test_the_continuous_loop_runs_it(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path)
    calls = []
    monkeypatch.setattr(
        "modules.retention.apply_retention", lambda db, s: calls.append(1) or {}
    )
    a._interruptible_sleep = lambda *_a, **_k: setattr(a, "_running", False)
    a.run_continuous(dry_run=False, loop_delay=60)
    assert calls == [1]
