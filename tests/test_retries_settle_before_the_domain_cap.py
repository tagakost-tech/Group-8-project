"""A retry that can never be sent is settled even when its domain is at cap.

The retry loop checked the per-domain daily cap before it asked whether the
contact had replied, become a client, opted out or been emailed by another
route. A retry for a closed contact at a capped domain was left queued
instead of cancelled, and came back the next loop to be skipped the same way
until the cap reset. The cap is a "not today"; a closed contact is a "never",
and the never is now decided first.
"""

import pytest

from agent import OutreachAgent


def _agent(settings_factory, tmp_path):
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
    return a


def _due_retry(a):
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="buyer@brand.de", organization="Brand")
    log_id = a.db.log_email(cid, "buyer@brand.de", "s", delivery_status="failed")
    a.retry_queue.add_to_retry(log_id, cid, "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    return cid


def _retry_row(a):
    return a.db.conn.execute("SELECT status, last_error FROM retry_queue").fetchone()


@pytest.fixture
def capped(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path)
    assert a.domain_throttle is not None
    monkeypatch.setattr(a.domain_throttle, "can_send_to_domain", lambda d: False)
    a.sent = []
    monkeypatch.setattr(
        a, "_process_contact", lambda c, *x, **k: a.sent.append(c) or True
    )
    return a


def test_a_closed_contact_at_a_capped_domain_is_cancelled(capped):
    a = capped
    cid = _due_retry(a)
    a.db.update_contact_status(cid, "responded")
    a._process_retries()
    assert a.sent == []
    assert _retry_row(a)["status"] == "cancelled"


def test_an_already_emailed_contact_at_a_capped_domain_is_cancelled(capped):
    a = capped
    cid = _due_retry(a)
    a.db.log_email(cid, "buyer@brand.de", "s", delivery_status="sent")
    a._process_retries()
    assert a.sent == []
    assert _retry_row(a)["status"] == "cancelled"


def test_an_open_contact_at_a_capped_domain_waits(capped):
    """The control: the cap still defers a retry that can be sent later."""
    a = capped
    _due_retry(a)
    a._process_retries()
    assert a.sent == []
    assert _retry_row(a)["status"] == "pending"
