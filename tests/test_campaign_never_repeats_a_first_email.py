"""The campaign never sends a second first email.

The retry loop has refused to repeat a delivered first email since an
earlier fix; the campaign loop never asked. Its batch can be hours old, so
a Direct Send, a retry or a status write that failed after a real send left
a prospect in the batch who had already received the email - and the
campaign sent it again. The shared send check now asks the email log for the
campaign too. A live pass also repairs the stale status, so the backlog stops
offering the contact; a dry run changes nothing.
"""

import pytest

from agent import OutreachAgent


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
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    a.sent = []
    monkeypatch.setattr(
        a, "_process_contact", lambda c, *x, **k: a.sent.append(c["email"]) or True
    )
    a.sid = a.db.log_search("q")
    return a


def _contact(a, email, status="pending", delivered=None):
    cid = a.db.add_contact(a.sid, email=email, organization="Brand")
    a.db.update_contact_status(cid, status)
    if delivered:
        a.db.log_email(cid, email, "s", delivery_status=delivered)
    return a.db.get_contact_by_id(cid)


def _already_emailed_rows(a):
    return [
        r
        for r in a.db.get_recent_activity(limit=200)
        if r["action"] == "ALREADY_EMAILED"
    ]


@pytest.mark.parametrize("delivered", ["sent", "sent_unconfirmed"])
def test_a_contact_already_emailed_is_not_emailed_again(agent, delivered):
    a = agent
    batch = [_contact(a, "done@brand.de", delivered=delivered)]
    a._email_contacts(batch)
    assert a.sent == []


def test_the_stale_status_is_repaired_and_recorded(agent):
    a = agent
    c = _contact(a, "done@brand.de", delivered="sent")
    a._email_contacts([c])
    assert a.db.get_contact_by_id(c["id"])["status"] == "emailed"
    assert len(_already_emailed_rows(a)) == 1


def test_a_dry_run_changes_nothing(agent):
    a = agent
    c = _contact(a, "done@brand.de", delivered="sent")
    a._email_contacts([c], dry_run=True)
    assert a.sent == []
    assert a.db.get_contact_by_id(c["id"])["status"] == "pending"
    assert _already_emailed_rows(a) == []


def test_a_failed_first_attempt_does_not_count_as_emailed(agent):
    """The control: only a delivered email closes the door."""
    a = agent
    batch = [_contact(a, "fresh@brand.de", delivered="failed")]
    a._email_contacts(batch)
    assert a.sent == ["fresh@brand.de"]
