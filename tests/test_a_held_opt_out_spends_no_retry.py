"""A retry for someone whose opt-out is held is neither spent nor sent.

The held check sits in the send itself, after the retry loop's own checks,
so a retry refused there was recorded as a failed attempt (found by the
review of step G20): re-queued with its attempt spent when the opt-out was
applied during the send, or exhausted with "Gave up" while it stayed held.
Now a retry whose contact the send closed is cancelled, and one whose
opt-out is still held waits, unspent.
"""

import sqlite3

from agent import OutreachAgent
from modules import optout_spool
from modules.database import Database

EMAIL = "buyer@brand.de"


def _agent(settings_factory, tmp_path, monkeypatch):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    a.dispatched = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: a.dispatched.append(k) or 1
    )
    cid = a.db.add_contact(a.db.log_search("q"), email=EMAIL, organization="Brand")
    log_id = a.db.log_email(cid, EMAIL, "s", delivery_status="failed")
    a.retry_queue.add_to_retry(log_id, cid, "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    a.db.update_contact_status(cid, "failed")
    return a


def _retry(a):
    return tuple(
        a.db.conn.execute("SELECT status, attempt FROM retry_queue").fetchone()
    )


def test_a_retry_whose_held_opt_out_the_send_applies_is_cancelled(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    optout_spool.spool_optout(a.db.db_path, EMAIL, "link")
    a._process_retries()
    assert a.dispatched == []
    assert _retry(a) == ("cancelled", 0)


def test_a_retry_waits_unspent_while_its_opt_out_stays_held(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    optout_spool.spool_optout(a.db.db_path, EMAIL, "link")

    def busy(*x, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "record_opt_out", busy)
    a._process_retries()
    assert a.dispatched == []
    assert _retry(a) == ("pending", 0)


def test_an_ordinary_failed_retry_is_still_spent(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    monkeypatch.setattr(a, "_process_contact", lambda *x, **k: False)
    a._process_retries()
    assert _retry(a) == ("pending", 1)
