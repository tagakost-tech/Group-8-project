"""Direct Send refuses a closed contact, as the dashboard's Send button does.

Direct Send checked only the suppression list. A contact who had replied, was
recorded as a client, or was blocked from the dashboard - with nothing on the
suppression list - could be sent the cold pitch by typing their address. The
Send button already refuses all of these; Direct Send now does too, in dry
runs as well, since "would send" is the wrong answer about such a contact.
A filter verdict (low score, irrelevant, unverified) is still the operator's
to override.
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
    a.dispatched = []
    monkeypatch.setattr(
        a.email_sender,
        "dispatch_to_contact",
        lambda **k: a.dispatched.append(k["contact"]["email"]) or 1,
    )
    a.sid = a.db.log_search("q")
    return a


def _stored(a, status):
    cid = a.db.add_contact(a.sid, email="buyer@brand.de", organization="Brand")
    a.db.update_contact_status(cid, status)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "status",
    ["responded", "active_client", "unsubscribed", "erased", "bounced", "blocked"],
)
def test_a_closed_contact_is_refused(agent, status, dry_run):
    _stored(agent, status)
    ok, msg = agent.send_single_email("buyer@brand.de", dry_run=dry_run)
    assert ok is False, msg
    assert agent.dispatched == []
    assert "buyer@brand.de" in msg


@pytest.mark.parametrize(
    "status", ["irrelevant", "ai_irrelevant", "low_score", "unverified"]
)
def test_a_filter_verdict_can_still_be_overridden(agent, status):
    _stored(agent, status)
    ok, msg = agent.send_single_email("buyer@brand.de")
    assert ok is True, msg
    assert agent.dispatched == ["buyer@brand.de"]
