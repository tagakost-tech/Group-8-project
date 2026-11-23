"""A named competitor is dropped at qualification, not at the last moment.

The competitor list was checked only at the send chokepoint. The client list,
its twin, is also checked in qualification - "before spending a
verification, an AI call or a send allowance". A rival therefore still cost
an MX lookup, an AI classification and a consent-log row recording a
"legitimate interest" in emailing them, before being refused at send time.
"""

from agent import OutreachAgent
from modules.clients import ClientRegister


def test_a_named_competitor_is_dropped_before_qualification_spends_anything(
    settings_factory, tmp_path
):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            lead_scoring_enabled=False,
        )
    )
    a._running = True
    a.competitor_register = ClientRegister(["Naturacare"])
    sid = a.db.log_search("q")
    email = "info@naturacare.com"
    cid = a.db.add_contact(sid, email=email, organization="Naturacare")

    qualified = a._qualify_contacts(
        [{"id": cid, "email": email, "organization": "Naturacare"}]
    )

    assert qualified == []
    assert a.db.conn.execute("SELECT COUNT(*) FROM consent_log").fetchone()[0] == 0
    contact = a.db.get_contact_by_id(cid)
    assert contact["status"] == "irrelevant"
    assert "Naturacare" in (contact["discard_reason"] or "")


def test_an_ordinary_prospect_still_qualifies(settings_factory, tmp_path):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            lead_scoring_enabled=False,
        )
    )
    a._running = True
    a.competitor_register = ClientRegister(["Naturacare"])
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="info@brand.de", organization="Brand GmbH")
    qualified = a._qualify_contacts(
        [{"id": cid, "email": "info@brand.de", "organization": "Brand GmbH"}]
    )
    assert len(qualified) == 1
