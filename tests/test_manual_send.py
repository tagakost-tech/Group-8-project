"""Tests for the manual/direct-send feature and the human email helpers."""

from conftest import _dry_run_logged


def _agent(settings_factory, tmp_path, **overrides):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "agent.db"),
        verify_emails=False,
        dry_run=True,
        delay_between_emails=0.0,
        ai_humanize_emails=False,  # deterministic body in tests (no API)
        **overrides,
    )
    return OutreachAgent(s)


# ── Human greeting / body helpers ──────────────────────────────────────────


def test_build_greeting_uses_first_name(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path)
    assert agent._build_greeting({"name": "Maria Rossi"}) == "Hi Maria,"
    assert agent._build_greeting({"name": "gian luca bianchi"}) == "Hi Gian,"


def test_build_greeting_falls_back_when_no_name(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path)
    assert agent._build_greeting({}) == "Hi there,"
    assert agent._build_greeting({"name": "Sir/Madam"}) == "Hi there,"


def test_body_to_html_makes_paragraphs_and_escapes(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path)
    html = agent._body_to_html("First para.\n\nSecond <b>para</b>.")
    assert html.count("<p") == 2
    assert "&lt;b&gt;" in html  # HTML in the body text is escaped
    assert "<b>para</b>" not in html


def test_default_body_mentions_company(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path, company_name="SUPPLECO")
    html = agent._build_email_body({"organization": "Nordic Wellness"})
    assert "SUPPLECO" in html
    assert "Nordic Wellness" in html
    assert "<p" in html


def test_strip_greeting_and_signoff():
    from modules.ai_generator import AIGenerator

    raw = "Hi Maria,\n\nWe make supplements and could help you.\n\nBest,\nKostas"
    cleaned = AIGenerator._strip_greeting_and_signoff(raw)
    assert cleaned == "We make supplements and could help you."


# ── send_single_email ──────────────────────────────────────────────────────


def test_manual_send_dry_run_previews_a_stored_contact(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path)
    sid = agent.db.log_search("q")
    cid = agent.db.add_contact(sid, "buyer@nordicwellness.se")
    ok, msg = agent.send_single_email(
        "buyer@nordicwellness.se",
        name="Maria Rossi",
        organization="Nordic Wellness",
        dry_run=True,
    )
    assert ok, msg
    # Asserted on the email log, not the contact: a dry run must not consume
    # the contact, or it can never be emailed for real afterwards.
    assert _dry_run_logged(agent.db, cid), "no simulated send logged"
    assert agent.db.get_contact_by_id(cid)["status"] == "pending"


def test_manual_send_dry_run_of_a_new_address_stores_nothing(
    settings_factory, tmp_path
):
    """A stored preview would be 'pending' - the backlog the campaign emails.
    See tests/test_direct_send_preview_leaves_nothing_behind.py."""
    agent = _agent(settings_factory, tmp_path)
    ok, msg = agent.send_single_email("buyer@nordicwellness.se", dry_run=True)
    assert ok, msg
    assert agent.db.get_contact_by_email("buyer@nordicwellness.se") is None


def test_manual_send_rejects_invalid_email(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path)
    ok, msg = agent.send_single_email("not-an-email", dry_run=True)
    assert not ok
    assert "valid" in msg.lower()


def test_manual_send_respects_suppression(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path)
    agent.db.add_to_suppression("gone@example.de", reason="unsubscribed")
    ok, msg = agent.send_single_email("gone@example.de", dry_run=True)
    assert not ok
    assert "suppress" in msg.lower() or "unsubscrib" in msg.lower()


def test_manual_dry_run_does_not_enroll(settings_factory, tmp_path):
    agent = _agent(
        settings_factory, tmp_path, sequences_enabled=True, min_lead_score_to_email=0
    )
    sid = agent.db.log_search("q")
    agent.db.add_contact(sid, "solo@brand.fr")
    ok, _ = agent.send_single_email("solo@brand.fr", dry_run=True)
    assert ok
    # A dry-run manual send is a one-off preview: no sequence enrollment.
    rows = agent.db.conn.execute("SELECT COUNT(*) FROM sequence_enrollments").fetchone()
    assert rows[0] == 0
