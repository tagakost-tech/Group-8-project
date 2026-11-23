"""A Direct Send preview of a new address stores nothing the agent acts on.

The dry run ("Preview") stored the typed address as a new contact - status
'pending', lead score 50, tier A - so it could log its simulated send against
it, and recorded a legal basis for contacting the person. 'pending' is
exactly what the campaign backlog selects, so the next loop qualified the
previewed address and emailed it for real: the operator looked at a draft,
decided not to send, and the agent sent the cold pitch anyway. A consent
record for an email that was never sent was false as well.

A preview of an address the agent does not hold now stores nothing: no
contact, no consent row, no email-log row. The EMAIL_DRY_RUN activity row
still says what would have been sent. A preview of a stored contact changes
nothing about it, as before.
"""

import pytest

from agent import OutreachAgent


@pytest.fixture
def agent(settings_factory, tmp_path):
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
    return a


def _consent_rows(a):
    return a.db.conn.execute("SELECT COUNT(*) FROM consent_log").fetchone()[0]


def test_a_preview_of_a_new_address_stores_no_contact(agent):
    ok, msg = agent.send_single_email(
        "buyer@nordicwellness.se", organization="Nordic Wellness", dry_run=True
    )
    assert ok, msg
    assert agent.db.get_contact_by_email("buyer@nordicwellness.se") is None
    assert agent.db.get_pending_contacts() == [], (
        "the previewed address is in the backlog the campaign emails"
    )


def test_a_preview_records_no_legal_basis(agent):
    agent.send_single_email("buyer@nordicwellness.se", dry_run=True)
    assert _consent_rows(agent) == 0


def test_the_preview_is_still_in_the_activity_log(agent):
    agent.send_single_email("buyer@nordicwellness.se", dry_run=True)
    actions = [r["action"] for r in agent.db.get_recent_activity(limit=50)]
    assert "EMAIL_DRY_RUN" in actions


def test_a_preview_of_a_stored_contact_leaves_it_as_it_was(agent):
    sid = agent.db.log_search("q")
    cid = agent.db.add_contact(sid, "buyer@brand.de", organization="Brand")
    ok, msg = agent.send_single_email("buyer@brand.de", dry_run=True)
    assert ok, msg
    assert agent.db.get_contact_by_id(cid)["status"] == "pending"
