"""A dead SMTP login stops every send path, not just the campaign loop.

The circuit breaker exists because each send opens a fresh LOGIN: after the
credentials break, carrying on means hundreds of failed logins in minutes -
enough for Microsoft 365 to lock the mailbox. It guarded _email_contacts
only. Follow-ups, retries and Direct Send kept logging in, so the lockout the
breaker was built to prevent could still happen, just more slowly.
"""

from agent import OutreachAgent


def _agent(settings_factory, tmp_path, **kw):
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
    for _k, _n, es in a.senders:
        es.auth_failed = True
    return a


def _contact(a, email="buyer@brand.de"):
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email=email, organization="Brand")
    return a.db.get_contact_by_id(cid)


def test_followups_stop(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, sequences_enabled=True)
    c = _contact(a)
    a.db.update_contact_status(c["id"], "emailed")
    monkeypatch.setattr(
        a.sequences,
        "get_due_followups",
        lambda: [{"enrollment_id": 1, "contact_id": c["id"], "subject": "s"}],
    )
    seen = []
    monkeypatch.setattr(a, "_process_contact", lambda ct, *x, **k: seen.append(ct))
    a._process_sequences(dry_run=False)
    assert seen == [], "a follow-up tried to log in after auth had failed"


def test_retries_stop_and_keep_their_attempts(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path)
    c = _contact(a)
    log_id = a.db.log_email(c["id"], c["email"], "s", delivery_status="failed")
    a.retry_queue.add_to_retry(log_id, c["id"], "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    seen = []
    monkeypatch.setattr(a, "_process_contact", lambda ct, *x, **k: seen.append(ct))
    a._process_retries()
    assert seen == []
    row = a.db.conn.execute("SELECT status, attempt FROM retry_queue").fetchone()
    assert tuple(row) == ("pending", 0)


def test_direct_send_refuses_and_says_why(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path)
    seen = []
    monkeypatch.setattr(a, "_process_contact", lambda ct, *x, **k: seen.append(ct))
    ok, message = a.send_single_email("buyer@brand.de", dry_run=False)
    assert ok is False
    assert "authentication" in message.lower()
    assert seen == []


def test_the_halt_is_logged_once(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    for _ in range(3):
        a._send_blocker(False)
    rows = [r for r in a.db.get_recent_activity(20) if r["action"] == "AGENT_HALTED"]
    assert len(rows) == 1


def test_an_auth_failure_mid_batch_does_not_spend_the_other_retries(
    settings_factory, tmp_path, monkeypatch
):
    """The blocker was checked once, before the loop. The password can die
    on the FIRST retry of the batch - and every retry after it was then
    refused at the chokepoint and recorded as a failed attempt, dropping
    leads that were never tried with working credentials."""
    a = _agent(settings_factory, tmp_path)
    for _k, _n, es in a.senders:
        es.auth_failed = False
    ids = []
    for i in range(3):
        c = _contact(a, f"p{i}@brand{i}.de")
        log_id = a.db.log_email(c["id"], c["email"], "s", delivery_status="failed")
        ids.append(a.retry_queue.add_to_retry(log_id, c["id"], "Initial send failed"))
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()

    calls = []

    def first_login_fails(contact, *x, **k):
        calls.append(contact["email"])
        for _k, _n, es in a.senders:
            es.auth_failed = True
        return False

    monkeypatch.setattr(a, "_process_contact", first_login_fails)
    a._process_retries()

    assert len(calls) == 1, "retries kept going after the login died"
    rows = a.db.conn.execute(
        "SELECT status, attempt FROM retry_queue ORDER BY id"
    ).fetchall()
    untried = [tuple(r) for r in rows if tuple(r) != ("pending", 1)]
    assert untried.count(("pending", 0)) == 2, f"untried retries were spent: {rows}"
