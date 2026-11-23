"""A sending mailbox is picked only for a contact that will actually be sent.

Every send loop picked the next mailbox first and asked about the contact
afterwards. A contact then skipped - its domain at today's cap, a retry for
someone who has since replied - had already advanced the round-robin and run
the warm-up count query for nothing, so the rotation drifted away from the
mailboxes that actually sent. The three loops now share one send check that
picks the mailbox last, after every reason not to send has been ruled out.
"""

from unittest.mock import MagicMock

import pytest

from agent import OutreachAgent


def _sender():
    s = MagicMock()
    s.auth_failed = False
    return s


@pytest.fixture
def agent(settings_factory, tmp_path, monkeypatch):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            sequences_enabled=True,
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    a.senders = [("acct1", "A", _sender()), ("acct2", "B", _sender())]
    a._rr = 0
    a.capacity_checks = []
    monkeypatch.setattr(
        a.warmup,
        "can_send",
        lambda account=None: a.capacity_checks.append(account) or True,
    )
    monkeypatch.setattr(
        a.domain_throttle, "can_send_to_domain", lambda d: d != "full.example"
    )
    a.sent_via = []
    monkeypatch.setattr(
        a,
        "_process_contact",
        lambda c, *x, **k: a.sent_via.append((c["email"], k["account_key"])) or True,
    )
    a.sid = a.db.log_search("q")
    return a


def _contact(a, email):
    cid = a.db.add_contact(a.sid, email=email, organization="Brand")
    return a.db.get_contact_by_id(cid)


def test_a_capped_contact_does_not_turn_the_campaign_rotation(agent):
    a = agent
    batch = [_contact(a, "x@full.example"), _contact(a, "y@open.example")]
    a._email_contacts(batch)
    assert a.sent_via == [("y@open.example", "acct1")]
    assert len(a.capacity_checks) == 1


def test_a_capped_follow_up_does_not_turn_the_rotation(agent, monkeypatch):
    a = agent
    due = [
        {"enrollment_id": 1, "contact_id": c["id"], "contact": c, "subject": "s"}
        for c in (_contact(a, "x@full.example"), _contact(a, "y@open.example"))
    ]
    for c in due:
        a.db.update_contact_status(c["contact_id"], "emailed")
        c["contact"] = a.db.get_contact_by_id(c["contact_id"])
    monkeypatch.setattr(a.sequences, "get_due_followups", lambda: due)
    monkeypatch.setattr(a.sequences, "advance_enrollment", lambda eid, step=None: None)
    a._process_sequences(dry_run=False)
    assert a.sent_via == [("y@open.example", "acct1")]
    assert len(a.capacity_checks) == 1


def test_a_retry_for_someone_who_replied_checks_no_mailbox(agent):
    a = agent
    c = _contact(a, "buyer@brand.de")
    log_id = a.db.log_email(c["id"], c["email"], "s", delivery_status="failed")
    a.retry_queue.add_to_retry(log_id, c["id"], "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    a.db.update_contact_status(c["id"], "responded")
    a._process_retries()
    assert a.sent_via == []
    assert a.capacity_checks == []
    assert a._rr == 0
