"""No live email goes out until the sender's legal identity is configured.

The footer renders a blank legal field as "[set LEGAL_ENTITY_NAME]". That is
fine in a dry-run preview and unlawful in a prospect's inbox: D.Lgs. 70/2003
Art. 8 and Cod. Civ. 2250 require the identity in the email itself. So a
blank field refuses every live send, on every path, and says why once - while
dry runs keep working, because they are how the operator checks the result.

Refused, not failed: the contact stays pending and a queued retry keeps its
attempts, so filling in .env and restarting loses nothing.
"""

import pytest

from agent import OutreachAgent

BLANK = {"legal_entity_name": ""}


def _agent(settings_factory, tmp_path, **kw):
    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        ai_humanize_emails=False,
        **kw,
    )
    return OutreachAgent(s)


@pytest.fixture
def blank_agent(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, **BLANK)
    a.dispatched = []
    monkeypatch.setattr(
        a.email_sender,
        "dispatch_to_contact",
        lambda **k: a.dispatched.append(k) or 1,
    )
    return a


def _contact(a, email="info@brand.de"):
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email=email, organization="Brand GmbH")
    return a.db.get_contact_by_id(cid)


def _activity(a, action):
    return [r for r in a.db.get_recent_activity(50) if r["action"] == action]


def test_a_live_send_is_refused_without_it(blank_agent):
    a = blank_agent
    c = _contact(a)
    assert a._process_contact(dict(c), dry_run=False) is False
    assert a.dispatched == [], "an email without the legal identity was sent"
    assert a.db.get_contact_by_id(c["id"])["status"] == "pending"


def test_the_refusal_is_logged_once_not_per_contact(blank_agent):
    """One clear line on the dashboard, not one per prospect in the backlog."""
    a = blank_agent
    for i in range(3):
        a._process_contact(dict(_contact(a, f"p{i}@brand{i}.de")), dry_run=False)
    rows = _activity(a, "LEGAL_IDENTITY_MISSING")
    assert len(rows) == 1
    assert "LEGAL_ENTITY_NAME" in rows[0]["details"]


def test_a_dry_run_still_works_without_it(blank_agent):
    """The control: previews are how the operator sees the footer."""
    a = blank_agent
    assert a._process_contact(dict(_contact(a)), dry_run=True)
    assert len(a.dispatched) == 1


def test_direct_send_says_why(blank_agent):
    a = blank_agent
    ok, message = a.send_single_email("info@brand.de", dry_run=False)
    assert ok is False
    assert "LEGAL_ENTITY_NAME" in message
    assert a.dispatched == []


def test_the_campaign_loop_stops_instead_of_walking_the_backlog(blank_agent):
    a = blank_agent
    a._running = True
    contacts = [dict(_contact(a, f"p{i}@brand{i}.de")) for i in range(3)]
    seen = []
    orig = a._process_contact
    a._process_contact = lambda c, *x, **k: seen.append(c) or orig(c, *x, **k)
    a._email_contacts(contacts, dry_run=False)
    assert seen == [], "the loop went contact by contact into the same refusal"


def test_the_followup_loop_stops_too(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, sequences_enabled=True, **BLANK)
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="buyer@brand.de", organization="Brand")
    a.db.update_contact_status(cid, "emailed")
    monkeypatch.setattr(
        a.sequences,
        "get_due_followups",
        lambda: [{"enrollment_id": 1, "contact_id": cid, "subject": "s"}],
    )
    seen = []
    monkeypatch.setattr(a, "_process_contact", lambda c, *x, **k: seen.append(c))
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    a._process_sequences(dry_run=False)
    assert seen == []


def test_retries_are_not_spent_while_blocked(blank_agent):
    """get_due_retries marks rows in_flight, and a refused send then counts
    as a failed attempt. Three refusals and a lead is gone for good."""
    a = blank_agent
    a._running = True
    c = _contact(a)
    log_id = a.db.log_email(c["id"], c["email"], "s", delivery_status="failed")
    a.retry_queue.add_to_retry(log_id, c["id"], "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    a._process_retries()
    row = a.db.conn.execute("SELECT status, attempt FROM retry_queue").fetchone()
    assert tuple(row) == ("pending", 0)
    assert a.dispatched == []
    # Nothing claimed and nothing announced: while blocked, the dashboard
    # must not report "N retries due" every loop for work that cannot run.
    assert not _activity(a, "RETRY_PROCESSING")


def test_doctor_fails_until_it_is_set(settings_factory, tmp_path):
    from main import run_doctor_checks

    def legal(checks):
        return next(c for c in checks if c.label.startswith("Legal identity"))

    blank = settings_factory(db_path=str(tmp_path / "d.db"), **BLANK)
    check = legal(run_doctor_checks(blank, network=False))
    assert check.ok is False and check.blocking
    assert "LEGAL_ENTITY_NAME" in check.detail

    full = settings_factory(db_path=str(tmp_path / "d.db"))
    assert legal(run_doctor_checks(full, network=False)).ok is True


def test_doctor_checks_every_template_carries_the_footer(
    settings_factory, tmp_path, monkeypatch
):
    import main

    tpl = tmp_path / "templates"
    tpl.mkdir()
    (tpl / "email_template.html").write_text("{{unsubscribe_url}} no footer")
    (tpl / "followup_1.html").write_text("{{legal_footer}} {{unsubscribe_url}}")
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    checks = main.run_doctor_checks(
        settings_factory(db_path=str(tmp_path / "d.db")), network=False
    )
    check = next(c for c in checks if c.label.startswith("Legal footer"))
    assert check.ok is False and check.blocking
    assert "email_template.html" in check.detail
