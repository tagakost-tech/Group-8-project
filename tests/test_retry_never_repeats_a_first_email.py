"""A retry never sends the first email to someone who already has it.

Review finding. A first send that failed on an SMTP-auth error left the
contact 'pending' (by design: the backlog picks it up once credentials are
fixed) AND queued a retry. After the fix and a restart, the backlog emailed
the contact; then the retry came due and sent the identical cold email a
second time. _contact_is_closed does not treat 'emailed' as closed - it is
not closed, it just must not get the FIRST email again.
"""

from agent import OutreachAgent


def _agent(settings_factory, tmp_path):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    return a


def test_an_auth_failure_queues_no_retry(settings_factory, tmp_path, monkeypatch):
    """The contact stays pending for the backlog; a retry is a second route to
    the same first email."""
    a = _agent(settings_factory, tmp_path)
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="x@brand.de", organization="Brand")

    def auth_dies(**_k):
        # As dispatch_to_contact does: the row is logged before sending.
        a.db.log_email(cid, "x@brand.de", "s", delivery_status="failed")
        a.email_sender.auth_failed = True
        return False

    monkeypatch.setattr(a.email_sender, "dispatch_to_contact", auth_dies)
    a._process_contact({"id": cid, "email": "x@brand.de"}, dry_run=False)
    n = a.db.conn.execute("SELECT COUNT(*) FROM retry_queue").fetchone()[0]
    assert n == 0


def test_a_retry_for_someone_already_emailed_is_cancelled(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path)
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="x@brand.de", organization="Brand")
    failed = a.db.log_email(cid, "x@brand.de", "s", delivery_status="failed")
    a.retry_queue.add_to_retry(failed, cid, "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    # Meanwhile the backlog delivered the first email.
    a.db.log_email(cid, "x@brand.de", "s", delivery_status="sent")
    a.db.update_contact_status(cid, "emailed")

    sent = []
    monkeypatch.setattr(a, "_process_contact", lambda c, *x, **k: sent.append(c))
    a._process_retries()

    assert sent == [], "the first email was sent twice"
    status = a.db.conn.execute("SELECT status FROM retry_queue").fetchone()[0]
    assert status == "cancelled"
