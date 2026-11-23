"""A follow-up that can no longer go out does not keep its contact.

Retention erases a contact who never replied RETENTION_DAYS_CONTACTED after
the last email, the period the footer promises ("we erase them 365 days
after our last email"), but it skipped anyone with an active follow-up
enrolment, however old. Two things leave an enrolment active for good:

- the country lock refusing the follow-up on every pass, once counsel
  narrows ENABLED_COUNTRIES or switches UNKNOWN_COUNTRY_ALLOWED off after the
  first email. The refusal came at send time and left the contact open, so
  the enrolment stayed and the follow-up was tried again, for ever;
- follow-ups switched off (SEQUENCES_ENABLED=false), after which nothing
  reads the enrolments at all.

Either way the contact was kept indefinitely, and nothing said so. A
follow-up the country lock refuses now ends its enrolment, with the reason,
and an enrolment holds its contact only until the last email is older than
the no-reply period plus the longest step delay. A follow-up still to come
keeps it (test_retention).
"""

from datetime import timedelta

import pytest

from agent import OutreachAgent
from modules import clock
from modules.export import erasure_token
from modules.retention import apply_retention

MARIE = "marie@maison.fr"


def _agent(settings_factory, tmp_path, monkeypatch, **kw):
    kw.setdefault("sequences_enabled", True)
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            **kw,
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    a.dispatched = []
    monkeypatch.setattr(
        a.email_sender,
        "dispatch_to_contact",
        lambda **k: a.dispatched.append(k) or 1,
    )
    return a


def _emailed_and_enrolled(a, days_ago, email=MARIE):
    """Emailed once `days_ago` days ago and enrolled then: the first follow-up
    is due."""
    db = a.db
    cid = db.add_contact(
        db.log_search("q"), email, name="Marie Dubois", organization="Maison"
    )
    db.log_email(cid, email, "Hello", delivery_status="sent")
    db.update_contact_status(cid, "emailed")
    a.sequences.enroll_contact(cid, a.sequence_id)
    then = clock.now() - timedelta(days=days_ago)
    db.conn.execute(
        "UPDATE email_log SET timestamp = ?", (then.strftime("%Y-%m-%d %H:%M:%S"),)
    )
    db.conn.execute(
        "UPDATE sequence_enrollments SET enrolled_at = ?", (then.isoformat(),)
    )
    db.conn.execute(
        "UPDATE contacts SET timestamp = ?", (then.strftime("%Y-%m-%d %H:%M:%S"),)
    )
    db.conn.commit()
    return cid


def _enrolment(a):
    return a.db.conn.execute(
        "SELECT status, current_step, cancel_reason FROM sequence_enrollments"
    ).fetchone()


def test_a_follow_up_the_country_lock_refuses_ends_the_enrolment(
    settings_factory, tmp_path, monkeypatch
):
    """France was switched off after Marie's first email."""
    a = _agent(settings_factory, tmp_path, monkeypatch, ENABLED_COUNTRIES="DE,IT")
    _emailed_and_enrolled(a, days_ago=10)
    a._process_sequences(dry_run=False)
    assert a.dispatched == []
    row = _enrolment(a)
    assert row["status"] == "cancelled", "the refused follow-up stays due for ever"
    assert "FR is not an enabled country" in (row["cancel_reason"] or "")


def test_a_follow_up_to_no_country_ends_once_those_addresses_are_refused(
    settings_factory, tmp_path, monkeypatch
):
    """UNKNOWN_COUNTRY_ALLOWED was switched off after the first email."""
    a = _agent(settings_factory, tmp_path, monkeypatch, UNKNOWN_COUNTRY_ALLOWED=False)
    _emailed_and_enrolled(a, days_ago=10, email="info@maison.com")
    a._process_sequences(dry_run=False)
    assert a.dispatched == []
    row = _enrolment(a)
    assert row["status"] == "cancelled", "the refused follow-up stays due for ever"
    assert "UNKNOWN_COUNTRY_ALLOWED" in (row["cancel_reason"] or "")


@pytest.mark.parametrize("days_ago", [366, 400])
def test_the_contact_is_then_erased_after_the_no_reply_period(
    settings_factory, tmp_path, monkeypatch, days_ago
):
    """The refusal ended the enrolment, so at 366 days nothing holds her: not
    even the step delay an enrolment still open gets."""
    a = _agent(settings_factory, tmp_path, monkeypatch, ENABLED_COUNTRIES="DE,IT")
    cid = _emailed_and_enrolled(a, days_ago=days_ago)
    for _ in range(3):
        a._process_sequences(dry_run=False)
    counts = apply_retention(a.db, a.settings)
    assert a.dispatched == []
    assert counts["erased_no_reply"] == 1
    row = a.db.get_contact_by_id(cid)
    assert row["status"] == "erased", f"kept {days_ago} days after her last email"
    assert row["email"] == erasure_token(MARIE)
    assert row["name"] != "Marie Dubois"


def test_follow_ups_switched_off_do_not_keep_an_enrolled_contact(
    settings_factory, tmp_path, monkeypatch
):
    """Enrolled while follow-ups were on; the agent then runs with them off,
    so nothing reads or ends the enrolment."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _emailed_and_enrolled(a, days_ago=400)
    off = _agent(settings_factory, tmp_path, monkeypatch, sequences_enabled=False)
    assert off.sequences is None
    off._process_sequences(dry_run=False)
    assert _enrolment(off)["status"] == "active"
    counts = apply_retention(off.db, off.settings)
    assert counts["erased_no_reply"] == 1
    assert off.db.get_contact_by_id(cid)["email"] == erasure_token(MARIE)


@pytest.mark.parametrize("days_ago,erased", [(368, 0), (370, 1)])
def test_an_enrolment_holds_its_contact_for_the_longest_step_delay_and_no_longer(
    settings_factory, tmp_path, monkeypatch, days_ago, erased
):
    """365 days plus 4, the longest step of the default sequence."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    _emailed_and_enrolled(a, days_ago=days_ago)
    assert apply_retention(a.db, a.settings)["erased_no_reply"] == erased


def test_a_follow_up_failing_on_every_pass_does_not_keep_the_contact(
    settings_factory, tmp_path, monkeypatch
):
    """Only an email that went out counts: today's failed attempt leaves
    Marie's last email 400 days old."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _emailed_and_enrolled(a, days_ago=400)
    a.db.log_email(cid, MARIE, "Following up", delivery_status="failed")
    counts = apply_retention(a.db, a.settings)
    assert counts["erased_no_reply"] == 1


def _pending_retry(a, cid):
    log_id = a.db.log_email(cid, MARIE, "Following up", delivery_status="failed")
    a.db.conn.execute(
        "INSERT INTO retry_queue (email_log_id, contact_id, status, next_retry_at) "
        "VALUES (?, ?, 'pending', ?)",
        (log_id, cid, clock.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    a.db.conn.commit()


@pytest.mark.parametrize("days_ago,erased", [(368, 0), (400, 1)])
def test_a_retry_holds_its_contact_no_longer_than_a_follow_up(
    settings_factory, tmp_path, monkeypatch, days_ago, erased
):
    """A retry left pending, by retries switched off or a pass that never
    reaches it, held its contact for ever, as an enrolment did."""
    a = _agent(settings_factory, tmp_path, monkeypatch, retry_enabled=False)
    cid = _emailed_and_enrolled(a, days_ago=days_ago)
    a.db.conn.execute("DELETE FROM sequence_enrollments")
    a.db.conn.commit()
    _pending_retry(a, cid)
    assert apply_retention(a.db, a.settings)["erased_no_reply"] == erased


def test_the_enrolment_an_erasure_leaves_active_sends_nothing(
    settings_factory, tmp_path, monkeypatch
):
    """Follow-ups come back on after the contact was erased with them off: the
    due step finds the contact erased, and the enrolment ends there."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    _emailed_and_enrolled(a, days_ago=400)
    off = _agent(settings_factory, tmp_path, monkeypatch, sequences_enabled=False)
    assert apply_retention(off.db, off.settings)["erased_no_reply"] == 1
    a._process_sequences(dry_run=False)
    assert a.dispatched == []
    assert _enrolment(a)["status"] == "cancelled"


def test_a_malformed_step_delay_does_not_stop_the_retention_job(
    settings_factory, tmp_path, monkeypatch
):
    """A stored delay that is not a number (process_sequences allows for one)
    counts as no delay. Added to the period it would raise, and the job would
    erase nobody at all."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    _emailed_and_enrolled(a, days_ago=400)
    a.db.conn.execute(
        "INSERT INTO sequence_steps (sequence_id, step_number, delay_days, subject) "
        "VALUES (?, 99, 'soon', 'Following up')",
        (a.sequence_id,),
    )
    a.db.conn.commit()
    assert apply_retention(a.db, a.settings)["erased_no_reply"] == 1


def test_a_follow_up_to_an_enabled_country_still_goes_out(
    settings_factory, tmp_path, monkeypatch
):
    """The control: ending every enrolment would also pass the tests above."""
    a = _agent(settings_factory, tmp_path, monkeypatch, ENABLED_COUNTRIES="DE,IT,FR")
    _emailed_and_enrolled(a, days_ago=10)
    a._process_sequences(dry_run=False)
    assert [k["contact"]["email"] for k in a.dispatched] == [MARIE]
    row = _enrolment(a)
    assert (row["status"], row["current_step"]) == ("active", 1)


def test_no_country_enabled_stops_the_pass_and_ends_nothing(
    settings_factory, tmp_path, monkeypatch
):
    """A blank ENABLED_COUNTRIES, the default, refuses every live send: the
    pass stops there, before any follow-up is ended for its country."""
    a = _agent(settings_factory, tmp_path, monkeypatch, ENABLED_COUNTRIES="")
    _emailed_and_enrolled(a, days_ago=10)
    a._process_sequences(dry_run=False)
    assert a.dispatched == []
    assert _enrolment(a)["status"] == "active"


def test_a_dry_run_previews_the_refused_follow_up_and_ends_nothing(
    settings_factory, tmp_path, monkeypatch
):
    """Dry runs preview every country, as the send itself does: a preview
    must not end anyone's follow-ups."""
    a = _agent(settings_factory, tmp_path, monkeypatch, ENABLED_COUNTRIES="DE,IT")
    _emailed_and_enrolled(a, days_ago=10)
    a._process_sequences(dry_run=True)
    assert [k["dry_run"] for k in a.dispatched] == [True]
    assert _enrolment(a)["status"] == "active"
