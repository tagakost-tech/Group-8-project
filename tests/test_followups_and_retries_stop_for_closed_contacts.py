"""Follow-ups and retries stop for anyone the agent must no longer contact.

Two loops each kept their own list of "stop" statuses, and both were short:

- The follow-up loop stopped for responded / unsubscribed / blocked only. A
  contact later found to be a client, a competitor, erased or bounced stayed
  enrolled. The send chokepoint refused each step, but nothing ended the
  enrolment, so every pass re-logged the refusal, took a sending slot and
  slept - forever.
- The retry loop checked no status at all. A prospect who REPLIED after a
  failed first send was sent that first email again.

One shared definition now answers "is this contact closed?" for both.
"""

import pytest

from agent import OutreachAgent
from modules.clients import ClientRegister


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
            sequences_enabled=True,
            **kw,
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    return a


@pytest.fixture
def followup(settings_factory, tmp_path, monkeypatch):
    """One contact, emailed once and due a follow-up. Sends and cancels are
    recorded, and the real _process_contact is kept unless a test swaps it."""
    a = _agent(settings_factory, tmp_path)
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="buyer@brand.de", organization="Brand")
    a.db.update_contact_status(cid, "emailed")
    a.cid = cid
    a.dispatched, a.cancelled = [], []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: a.dispatched.append(k) or 1
    )
    monkeypatch.setattr(
        a.sequences, "cancel_enrollment", lambda eid, why: a.cancelled.append(why)
    )
    monkeypatch.setattr(a.sequences, "advance_enrollment", lambda eid, step=None: None)
    monkeypatch.setattr(
        a.sequences,
        "get_due_followups",
        lambda: [{"enrollment_id": 1, "contact_id": cid, "subject": "s"}],
    )
    return a


@pytest.mark.parametrize(
    "status",
    [
        "active_client",
        "erased",
        "bounced",
        "irrelevant",
        "ai_irrelevant",
        "low_score",
        "unverified",
    ],
)
def test_no_followup_for_a_closed_or_discarded_contact(followup, status):
    a = followup
    a.db.update_contact_status(a.cid, status)
    a._process_sequences(dry_run=False)
    assert a.dispatched == []
    assert a.cancelled, f"a {status!r} contact stayed enrolled"


def test_a_competitor_refused_at_send_time_leaves_the_sequence(followup):
    """The chokepoint knows what the loop does not. When it refuses, the
    enrolment must end, or the refusal repeats every pass."""
    a = followup
    a.competitor_register = ClientRegister(["Brand"])
    a._process_sequences(dry_run=False)
    assert a.dispatched == []
    assert a.cancelled


def test_a_client_refused_at_send_time_is_marked_and_leaves(followup):
    a = followup
    a.client_register = ClientRegister(["Brand"])
    a._process_sequences(dry_run=False)
    assert a.dispatched == []
    assert a.db.get_contact_by_id(a.cid)["status"] == "active_client"
    assert a.cancelled


def test_an_ordinary_contact_still_gets_the_followup(followup):
    """The control: stopping everyone would also pass the tests above."""
    followup._process_sequences(dry_run=False)
    assert len(followup.dispatched) == 1
    assert followup.cancelled == []


def _due_retry(a, status):
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="buyer@brand.de", organization="Brand")
    log_id = a.db.log_email(cid, "buyer@brand.de", "s", delivery_status="failed")
    a.retry_queue.add_to_retry(log_id, cid, "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    a.db.update_contact_status(cid, status)
    return cid


def test_a_retry_to_someone_who_replied_is_cancelled_not_sent(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path)
    _due_retry(a, "responded")
    sent = []
    monkeypatch.setattr(a, "_process_contact", lambda c, *x, **k: sent.append(c))
    a._process_retries()
    assert sent == [], "the first email was re-sent to someone who had replied"
    status = a.db.conn.execute("SELECT status FROM retry_queue").fetchone()[0]
    assert status == "cancelled"


def test_an_ordinary_retry_still_runs(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path)
    _due_retry(a, "failed")
    sent = []
    monkeypatch.setattr(
        a, "_process_contact", lambda c, *x, **k: sent.append(c) or True
    )
    a._process_retries()
    assert len(sent) == 1
