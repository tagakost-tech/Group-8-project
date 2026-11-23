"""A Direct Send that trips a reputation guard pauses the running campaign.

The dashboard runs Direct Send on the campaign agent when one is running.
The send loops latch the pause when the bounce or blocklist guard trips, so
the campaign stops sending until the operator resolves it; Direct Send only
refused, and left the campaign to discover the same trip on its own next
contact. It now latches too - but only on a running agent. The dashboard's
standalone Direct Send agent never runs the loop that releases a pause, so
latching there would refuse every later Direct Send, even after recovery.
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
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    a.dispatched = []
    monkeypatch.setattr(
        a.email_sender,
        "dispatch_to_contact",
        lambda **k: a.dispatched.append(k["contact"]["email"]) or 1,
    )
    return a


@pytest.mark.parametrize("guard", ["bounce", "blacklist"])
def test_a_running_campaign_is_paused(agent, guard):
    a = agent
    a._running = True
    setattr(a, f"_{guard}_guard_tripped", lambda: True)
    ok, _ = a.send_single_email("buyer@brand.de")
    assert ok is False
    assert a._sending_paused is True
    assert a._pause_reason == guard


@pytest.mark.parametrize("guard", ["bounce", "blacklist"])
def test_a_standalone_send_agent_is_not_left_paused(agent, guard):
    a = agent
    a._running = False
    setattr(a, f"_{guard}_guard_tripped", lambda: True)
    assert a.send_single_email("buyer@brand.de")[0] is False
    assert a._sending_paused is False
    # Once the guard recovers, the next Direct Send goes.
    setattr(a, f"_{guard}_guard_tripped", lambda: False)
    ok, msg = a.send_single_email("buyer@brand.de")
    assert ok is True, msg
