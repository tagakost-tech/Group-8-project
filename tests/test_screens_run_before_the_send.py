"""Address, competitor and client screens run before a mailbox is picked.

The screens lived only inside the per-contact send, which runs after the
loop has picked a mailbox and is followed by the send delay. A backlog full
of personal mailboxes, rivals or clients therefore turned the rotation, ran
a warm-up count query and then slept two minutes per contact - for contacts
that were never going to be emailed. A retry for such a contact spent an
attempt on the refusal instead of being cancelled. The shared send check now
runs the screens before the domain cap and the mailbox; the send itself still
runs them as the final guarantee.
"""

import pytest

from agent import OutreachAgent
from modules.clients import ClientRegister


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
    a._running = True
    a.sleeps = []
    a._interruptible_sleep = lambda s: a.sleeps.append(s)
    a.capacity_checks = []
    monkeypatch.setattr(
        a.warmup,
        "can_send",
        lambda account=None: a.capacity_checks.append(account) or True,
    )
    a.dispatched = []
    monkeypatch.setattr(
        a.email_sender,
        "dispatch_to_contact",
        lambda **k: a.dispatched.append(k["contact"]["email"]) or 1,
    )
    a.sid = a.db.log_search("q")
    return a


def _contact(a, email, org="Brand", status="pending"):
    cid = a.db.add_contact(a.sid, email=email, organization=org)
    a.db.update_contact_status(cid, status)
    return a.db.get_contact_by_id(cid)


@pytest.mark.parametrize(
    "email, org, register",
    [
        ("mario.rossi@gmail.com", "Brand", None),
        ("buyer@rival.it", "Rival Labs", "competitor"),
        ("buyer@client.it", "Client Foods", "client"),
    ],
)
def test_a_screened_contact_costs_no_mailbox_and_no_sleep(agent, email, org, register):
    a = agent
    if register == "competitor":
        a.competitor_register = ClientRegister([org])
    if register == "client":
        a.client_register = ClientRegister([org])
    batch = [_contact(a, email, org), _contact(a, "buyer@brand.de")]
    a._email_contacts(batch)
    assert a.dispatched == ["buyer@brand.de"]
    assert len(a.capacity_checks) == 1, "the screened contact checked a mailbox"
    assert len(a.sleeps) == 1, "the loop slept after a contact it never sent"


def test_a_retry_for_a_competitor_is_cancelled_not_spent(agent):
    a = agent
    a.competitor_register = ClientRegister(["Rival Labs"])
    c = _contact(a, "buyer@rival.it", "Rival Labs", status="failed")
    log_id = a.db.log_email(c["id"], c["email"], "s", delivery_status="failed")
    a.retry_queue.add_to_retry(log_id, c["id"], "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    reached = []
    send = a._process_contact
    a._process_contact = lambda c, *x, **k: (
        reached.append(c["email"]) or send(c, *x, **k)
    )
    a._process_retries()
    assert a.dispatched == []
    assert reached == [], "the retry reached the send before its screen"
    status = a.db.conn.execute("SELECT status FROM retry_queue").fetchone()[0]
    assert status == "cancelled"


def test_a_follow_up_hard_bounced_at_send_time_leaves_the_sequence(agent, monkeypatch):
    """The refusal the send-time path still owns: the server rejects the
    address while the follow-up is being sent, which closes the contact."""
    a = agent
    c = _contact(a, "buyer@brand.de", status="emailed")

    def _rejected(**k):
        a.db.add_to_suppression(c["email"], "bounce")
        a.db.update_contact_status(c["id"], "bounced")
        return 0

    monkeypatch.setattr(a.email_sender, "dispatch_to_contact", _rejected)
    due = [{"enrollment_id": 3, "contact_id": c["id"], "contact": c, "subject": "s"}]
    monkeypatch.setattr(a.sequences, "get_due_followups", lambda: due)
    cancelled = []
    monkeypatch.setattr(
        a.sequences, "cancel_enrollment", lambda eid, why: cancelled.append(eid)
    )
    a._process_sequences(dry_run=False)
    assert cancelled == [3]


@pytest.mark.parametrize(
    "email, org, register",
    [
        ("mario.rossi@gmail.com", "Brand", None),
        ("buyer@rival.it", "Rival Labs", "competitor"),
        ("buyer@client.it", "Client Foods", "client"),
    ],
)
def test_the_send_itself_still_refuses_a_screened_contact(agent, email, org, register):
    """Every send path now screens in the send check first, so the send's own
    screens are the guarantee for any path added later that forgets to."""
    a = agent
    if register == "competitor":
        a.competitor_register = ClientRegister([org])
    if register == "client":
        a.client_register = ClientRegister([org])
    assert a._process_contact(_contact(a, email, org)) is False
    assert a.dispatched == []
