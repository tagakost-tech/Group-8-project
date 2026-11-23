"""Guards on the follow-up sender.

_process_sequences re-sends to people we already contacted, so it needs the
same protections as a first email - and one more: a contact who replied or
opted out between step 1 and step 2 must fall out of the sequence. None of
these guards were covered, so every one of them could be deleted with the
suite still green.
"""

import pytest


def _agent(settings_factory, tmp_path, **overrides):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        delay_between_emails=0.0,
        ai_humanize_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        sequences_enabled=True,
        **overrides,
    )
    return OutreachAgent(s)


@pytest.fixture
def seq_agent(settings_factory, tmp_path, monkeypatch):
    """An agent with one contact due a follow-up, and sends captured."""
    a = _agent(settings_factory, tmp_path)
    if not a.sequences:
        pytest.skip("sequences disabled in this build")

    sid = a.db.log_search("q")
    contact_id = a.db.add_contact(sid, email="buyer@brand.de", organization="Brand")
    a.db.update_contact_status(contact_id, "emailed")

    sent = []
    monkeypatch.setattr(
        a, "_process_contact", lambda contact, *args, **kw: sent.append(contact) or True
    )

    cancelled = []
    monkeypatch.setattr(
        a.sequences,
        "cancel_enrollment",
        lambda eid, reason: cancelled.append(reason),
    )
    monkeypatch.setattr(a.sequences, "advance_enrollment", lambda eid, step=None: None)
    monkeypatch.setattr(
        a.sequences,
        "get_due_followups",
        lambda: [
            {
                "enrollment_id": 1,
                "contact_id": contact_id,
                "subject": "Following up",
                "template_name": None,
            }
        ],
    )
    a._interruptible_sleep = lambda *_a, **_kw: None
    a._running = True  # the loop is entered only while the agent is running
    a.__dict__["_test"] = {"sent": sent, "cancelled": cancelled, "id": contact_id}
    return a


def _state(a):
    return a.__dict__["_test"]


def test_followup_is_sent_when_nothing_blocks_it(seq_agent):
    seq_agent._process_sequences()
    assert len(_state(seq_agent)["sent"]) == 1


def test_suppressed_contact_gets_no_followup(seq_agent):
    """Suppression is the GDPR stop signal. A follow-up sequence that ignores
    it keeps mailing someone who already objected."""
    seq_agent.db.add_to_suppression("buyer@brand.de", "unsubscribe")
    seq_agent._process_sequences()
    assert _state(seq_agent)["sent"] == []
    assert _state(seq_agent)["cancelled"]


def test_contact_who_replied_is_dropped_from_the_sequence(seq_agent):
    """Chasing someone who already answered is the most visible way for this
    agent to look like a robot."""
    seq_agent.db.update_contact_status(_state(seq_agent)["id"], "responded")
    seq_agent._process_sequences()
    assert _state(seq_agent)["sent"] == []
    assert _state(seq_agent)["cancelled"]


def test_bounce_guard_stops_followups_and_pauses_sending(seq_agent, monkeypatch):
    monkeypatch.setattr(seq_agent, "_bounce_guard_tripped", lambda: True)
    seq_agent._process_sequences()
    assert _state(seq_agent)["sent"] == []
    assert seq_agent._sending_paused is True
    assert seq_agent._pause_reason == "bounce"


def test_blacklist_guard_stops_followups(seq_agent, monkeypatch):
    monkeypatch.setattr(seq_agent, "_blacklist_guard_tripped", lambda: True)
    seq_agent._process_sequences()
    assert _state(seq_agent)["sent"] == []
    assert seq_agent._pause_reason == "blacklist"


def test_closed_send_window_defers_followups(seq_agent, monkeypatch):
    monkeypatch.setattr(seq_agent, "_within_send_window", lambda: False)
    seq_agent._process_sequences()
    assert _state(seq_agent)["sent"] == []
    # Deferred, NOT cancelled - the enrollment must survive to tomorrow.
    assert _state(seq_agent)["cancelled"] == []


def test_daily_cap_defers_followups(seq_agent, monkeypatch):
    """Follow-ups share the warm-up budget with first emails; if they didn't,
    a busy day would blow straight through the daily cap."""
    monkeypatch.setattr(seq_agent, "_pick_sender", lambda: None)
    seq_agent._process_sequences()
    assert _state(seq_agent)["sent"] == []
    assert _state(seq_agent)["cancelled"] == []


def test_stopping_the_agent_halts_followups(seq_agent):
    seq_agent._running = False
    seq_agent._process_sequences()
    assert _state(seq_agent)["sent"] == []


def test_missing_contact_cancels_the_enrollment(seq_agent, monkeypatch):
    monkeypatch.setattr(seq_agent.db, "get_contact_by_id", lambda cid: None)
    seq_agent._process_sequences()
    assert _state(seq_agent)["sent"] == []
    assert _state(seq_agent)["cancelled"]
