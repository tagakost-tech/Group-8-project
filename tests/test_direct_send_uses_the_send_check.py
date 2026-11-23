"""Direct Send runs the shared send check and says why it refused.

Direct Send kept a fourth copy of the delivery guards. It screened personal
and PEC addresses itself but left rivals and clients to the send chokepoint,
which refuses silently - so the operator who typed a client's address was
told only "Could not send ... (check the logs)". It now asks the same check
as the three send loops and turns the answer into a message that names the
match.
"""

import pytest

from agent import OutreachAgent
from modules.clients import ClientRegister
from modules.dispatch_pipeline import SendVerdict


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
    a.dispatched = []
    monkeypatch.setattr(
        a.email_sender,
        "dispatch_to_contact",
        lambda **k: a.dispatched.append(k["contact"]["email"]) or 1,
    )
    return a


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_rival_is_refused_by_name(agent, dry_run):
    a = agent
    a.competitor_register = ClientRegister(["Rival Labs"])
    ok, msg = a.send_single_email(
        "buyer@rival.it", organization="Rival Labs S.r.l.", dry_run=dry_run
    )
    assert ok is False
    assert a.dispatched == []
    assert "competitor" in msg and "Rival Labs" in msg, msg


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_client_is_refused_by_name(agent, dry_run):
    a = agent
    a.client_register = ClientRegister(["Client Foods"])
    ok, msg = a.send_single_email(
        "buyer@client.it", organization="Client Foods", dry_run=dry_run
    )
    assert ok is False
    assert a.dispatched == []
    assert "client" in msg and "Client Foods" in msg, msg


def test_direct_send_asks_the_shared_check(agent):
    a = agent
    asked = []

    def _verdict(contact, *, purpose, dry_run):
        asked.append(purpose)
        return SendVerdict("batch", "blocked", "a test refusal")

    a._send_verdict = _verdict
    ok, msg = a.send_single_email("buyer@brand.de", organization="Brand")
    assert asked == ["manual"]
    assert ok is False and "a test refusal" in msg
    assert a.dispatched == []


def test_an_ordinary_direct_send_still_goes(agent):
    """The control."""
    ok, msg = agent.send_single_email("buyer@brand.de", organization="Brand")
    assert ok is True, msg
    assert agent.dispatched == ["buyer@brand.de"]
