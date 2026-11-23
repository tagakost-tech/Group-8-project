"""The backlog is qualified only when a send can follow.

Each loop qualified every unqualified backlog contact - AI classification,
MX and SMTP probes - before asking whether anything could be sent. At night,
while sending was paused, or once every mailbox had used today's allowance,
that work bought nothing: the contacts waited for hours, the mailbox verdict
went stale before the send, and a contact erased by retention meanwhile had
been classified for nothing. Qualification now waits for a moment when a send
can follow. The contacts stay pending, so nothing is lost.
"""

import pytest

from agent import OutreachAgent


@pytest.fixture
def agent(settings_factory, tmp_path, monkeypatch):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    a._running = True
    a._interruptible_sleep = lambda *_a, **_k: None
    a._within_send_window = lambda: True
    a.capacity = True
    monkeypatch.setattr(a.warmup, "can_send", lambda account=None: a.capacity)
    a.qualified = []
    a._qualify_contacts = lambda cs: a.qualified.extend(cs) or []
    sid = a.db.log_search("q")
    a.cid = a.db.add_contact(sid, "buyer@brand.de", organization="Brand")
    return a


def test_nothing_is_qualified_outside_the_send_window(agent):
    agent._within_send_window = lambda: False
    agent._process_pending_contacts()
    assert agent.qualified == []
    assert agent.db.get_contact_by_id(agent.cid)["status"] == "pending"


def test_nothing_is_qualified_once_every_mailbox_is_capped(agent):
    agent.capacity = False
    agent._process_pending_contacts()
    assert agent.qualified == []


def test_nothing_is_qualified_while_sending_is_paused(agent):
    """A one-off run reaches the backlog without the loop's pause check."""
    agent._sending_paused, agent._pause_reason = True, "bounce"
    agent._process_pending_contacts()
    assert agent.qualified == []


def test_the_backlog_is_qualified_when_a_send_can_follow(agent):
    """The control."""
    agent._process_pending_contacts()
    assert [c["id"] for c in agent.qualified] == [agent.cid]
