"""The send loops judge each contact on its current row, not the batch snapshot.

The campaign loop works through a list read when the pass began, and sleeps
the send delay (two minutes by default) after every contact, so the last
rows of a long batch can be hours old by the time they are reached. It
checked none of them again: a prospect who replied, was blocked from the
dashboard, or was erased in the meantime still got the cold email. The
follow-up loop had the same gap with the rows it read at the start of its
pass. Both now re-read each contact just before deciding.
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
            sequences_enabled=True,
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


def _snapshot(a, email, status="qualified"):
    cid = a.db.add_contact(a.sid, email=email, organization="Brand")
    a.db.update_contact_status(cid, status)
    return a.db.get_contact_by_id(cid)


@pytest.mark.parametrize("status", ["responded", "blocked", "unsubscribed"])
def test_the_campaign_skips_a_contact_closed_since_the_batch_was_read(agent, status):
    a = agent
    batch = [_snapshot(a, "late@brand.de"), _snapshot(a, "open@brand.de")]
    a.db.update_contact_status(batch[0]["id"], status)
    a._email_contacts(batch)
    assert a.sent == ["open@brand.de"]


def test_the_campaign_skips_a_contact_erased_since_the_batch_was_read(agent):
    a = agent
    batch = [_snapshot(a, "gone@brand.de"), _snapshot(a, "open@brand.de")]
    a.db.conn.execute("DELETE FROM contacts WHERE id = ?", (batch[0]["id"],))
    a.db.conn.commit()
    a._email_contacts(batch)
    assert a.sent == ["open@brand.de"]


def test_a_follow_up_is_stopped_by_a_reply_that_arrived_during_the_pass(
    agent, monkeypatch
):
    a = agent
    c = _snapshot(a, "buyer@brand.de", status="emailed")
    due = [{"enrollment_id": 7, "contact_id": c["id"], "contact": c, "subject": "s"}]
    monkeypatch.setattr(a.sequences, "get_due_followups", lambda: due)
    monkeypatch.setattr(a.sequences, "advance_enrollment", lambda eid, step=None: None)
    cancelled = []
    monkeypatch.setattr(
        a.sequences, "cancel_enrollment", lambda eid, why: cancelled.append(eid)
    )
    a.db.update_contact_status(c["id"], "responded")
    a._process_sequences(dry_run=False)
    assert a.sent == []
    assert cancelled == [7]
