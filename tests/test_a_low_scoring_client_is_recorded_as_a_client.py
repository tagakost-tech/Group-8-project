"""A client or a competitor is recorded as such, whatever its lead score.

Qualification scored each contact before the client and competitor screens.
A client whose lead scored under MIN_LEAD_SCORE_TO_EMAIL - an info@ address,
no role - was stored as 'low_score' with "Score 5 below threshold 15". It was
still never emailed, but the dashboard, its "why" panel and the run's stats
all said the lead was weak, not that it already buys from SUPPLECO. Lowering
the threshold and re-queuing low scorers would put the client back in the
backlog, caught again only at the send. The screens now run first.
"""

import pytest

from agent import OutreachAgent
from modules.clients import ClientRegister


class _LowScorer:
    def score_contact(self, contact):
        return {"score": 5, "breakdown": {}, "tier": "C"}


@pytest.fixture
def agent(settings_factory, tmp_path):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            min_lead_score_to_email=15,
        )
    )
    a._running = True
    a.client_register = ClientRegister(["Menarini"])
    a.competitor_register = ClientRegister(["Naturacare"])
    a.relevance_filter = None
    a.lead_scorer = _LowScorer()
    a.sid = a.db.log_search("q")
    return a


def _qualify(a, email, organization):
    cid = a.db.add_contact(a.sid, email=email, organization=organization)
    qualified = a._qualify_contacts(
        [{"id": cid, "email": email, "organization": organization}]
    )
    return qualified, a.db.get_contact_by_id(cid)


def test_a_low_scoring_client_is_stored_as_a_client(agent):
    qualified, contact = _qualify(agent, "info@menarini.it", "Menarini")
    assert qualified == []
    assert contact["status"] == "active_client"
    assert "MENARINI" in (contact["discard_reason"] or "").upper()
    assert agent._stats["low_score_skipped"] == 0
    assert agent._stats.get("client_skipped") == 1


def test_a_low_scoring_competitor_is_stored_as_a_competitor(agent):
    qualified, contact = _qualify(agent, "info@naturacare.com", "Naturacare")
    assert qualified == []
    assert contact["status"] == "irrelevant"
    assert "Naturacare" in (contact["discard_reason"] or "")


def test_a_low_scoring_prospect_is_still_a_low_score(agent):
    """Control: the screens moving first must not swallow the score check."""
    qualified, contact = _qualify(agent, "info@brand.de", "Brand GmbH")
    assert qualified == []
    assert contact["status"] == "low_score"
