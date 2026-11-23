"""Direct Send applies the address screens and records its legal basis.

Direct Send bypasses discovery and scoring by design - the operator chose the
recipient. It also bypassed two screens that are about the ADDRESS, not the
choice:

- A PEC (certified mail) address is legal correspondence with the force of a
  registered letter, never a marketing channel.
- A personal mailbox (gmail, libero, gmx...) belongs to a natural person.
  B2B legitimate interest does not cover it; Art. 130 Codice Privacy
  requires prior consent.

And it wrote no consent-log row, so the one path where a human hand-picked
the recipient was the one path with no recorded legal basis.
"""

import pytest

from agent import OutreachAgent


@pytest.fixture
def agent(settings_factory, tmp_path, monkeypatch):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
        )
    )
    a.sent = []
    monkeypatch.setattr(
        a, "_process_contact", lambda c, *x, **k: a.sent.append(c) or True
    )
    return a


@pytest.mark.parametrize("email", ["ufficio@pec.brand.it", "brand@legalmail.it"])
def test_a_pec_address_is_refused(agent, email):
    ok, message = agent.send_single_email(email, dry_run=True)
    assert ok is False
    assert "PEC" in message
    assert agent.sent == []


def test_a_personal_mailbox_is_refused(agent):
    ok, message = agent.send_single_email("mario.rossi@gmail.com", dry_run=True)
    assert ok is False
    assert "personal" in message
    assert agent.sent == []


def test_a_business_address_goes_through(agent):
    ok, _ = agent.send_single_email("info@brand.de", dry_run=True)
    assert ok is True
    assert len(agent.sent) == 1


def test_the_legal_basis_goes_with_the_send(agent):
    # The send is stubbed here, so check what it is handed: the basis of the
    # operator's channel. The record itself is written once a live send
    # succeeds (tests/test_each_send_records_its_notice_and_basis.py); it was
    # written before the send, so a failed send left one too.
    agent.send_single_email("info@brand.de", channel="trade_fair")
    assert agent.sent[0]["send_channel"] == "trade_fair"
    assert agent.sent[0]["basis"] == "legitimate_interest"
