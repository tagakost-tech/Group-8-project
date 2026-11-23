"""Tests for A/B testing: subject variants, DB stats, and agent variant recording."""


def test_email_subjects_fallback(settings_factory):
    s = settings_factory(email_subject="Hello {company_name}",
                         company_name="SUPPLECO", email_subjects="")
    assert s.get_email_subjects() == ["Hello SUPPLECO"]


def test_email_subjects_variants(settings_factory):
    s = settings_factory(company_name="SUPPLECO",
                         email_subjects="A {company_name}, B {company_name}")
    assert s.get_email_subjects() == ["A SUPPLECO", "B SUPPLECO"]


def test_ab_stats_groups_by_variant(db):
    sid = db.log_search("q")
    cid = db.add_contact(sid, "a@example.com")
    db.log_email(cid, "a@example.com", "S", delivery_status="sent", ab_variant="v1")
    db.log_email(cid, "a@example.com", "S", delivery_status="sent", ab_variant="v2")
    variants = {r["variant"]: r for r in db.get_ab_stats()}
    assert variants.get("v1", {}).get("sent") == 1
    assert variants.get("v2", {}).get("sent") == 1
    assert "open_rate" in variants["v1"]


def test_ab_variant_recorded_dry_run(settings_factory, tmp_path):
    from agent import OutreachAgent
    s = settings_factory(db_path=str(tmp_path / "ab.db"), verify_emails=False, send_window_enabled=False,
                         dry_run=True, delay_between_emails=0.0,
                         ab_testing_enabled=True,
                         email_subjects="Subject A, Subject B",
                         min_lead_score_to_email=0)
    agent = OutreachAgent(s)
    sid = agent.db.log_search("q")
    agent.db.add_contact(sid, "x@pharma.de", organization="Pharma GmbH")
    agent._running = True
    agent._process_pending_contacts(dry_run=True)

    rows = agent.db.get_all_emails(limit=5)
    assert rows and rows[0].get("ab_variant")
