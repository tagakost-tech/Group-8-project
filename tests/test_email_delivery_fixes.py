"""Tests for the deliverability fixes: working (mailto) unsubscribe, no
localhost tracking pixel, personalized subject, and List-Unsubscribe header."""
from modules.database import Database
from modules.email_sender import EmailSender


def _agent(settings_factory, tmp_path, **overrides):
    from agent import OutreachAgent
    s = settings_factory(
        db_path=str(tmp_path / "a.db"), verify_emails=False, dry_run=True,
        delay_between_emails=0.0, ai_humanize_emails=False, **overrides)
    return OutreachAgent(s)


def _capture_dispatch(agent, monkeypatch):
    captured = {}
    def cap(**kw):
        captured.update(kw)
        return True
    monkeypatch.setattr(agent.email_sender, "dispatch_to_contact", cap)
    return captured


# ── BASE_URL gating (bug #1 / #2) ──────────────────────────────────────────

def test_localhost_base_url_is_not_public(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path, base_url="http://127.0.0.1:5000")
    assert a._is_public_base_url() is False
    assert a._unsubscribe_target("x@brand.de").startswith("mailto:")


def test_public_https_base_url_uses_web_unsubscribe(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path, base_url="https://reach.example.com")
    assert a._is_public_base_url() is True
    assert a._unsubscribe_target("x@brand.de").startswith("https://reach.example.com")


def test_local_send_has_working_unsub_and_no_pixel(settings_factory, tmp_path, monkeypatch):
    # Tracking ON: it is off by default now, and with it off this test passed
    # without ever reaching the BASE_URL check it exists to cover.
    a = _agent(settings_factory, tmp_path, base_url="http://127.0.0.1:5000",
               OPEN_TRACKING_ENABLED=True)
    cap = _capture_dispatch(a, monkeypatch)
    a._process_contact({"id": 1, "email": "lena@vitalbrand.de",
                        "organization": "VitalBrand"}, dry_run=True)
    assert "127.0.0.1" not in cap["html_body"]      # no unreachable localhost links
    assert "mailto:" in cap["html_body"]            # a working unsubscribe exists
    assert cap["tracking_id"] is None               # tracking pixel skipped locally


# ── Subject personalization (bug #3) ───────────────────────────────────────

def test_subject_uses_recipient_company(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, email_subject="Quick question about {org}")
    cap = _capture_dispatch(a, monkeypatch)
    a._process_contact({"id": 1, "email": "x@vitalbrand.de",
                        "organization": "VitalBrand"}, dry_run=True)
    assert cap["subject"] == "Quick question about VitalBrand"


def test_subject_fallback_when_no_org(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, email_subject="Quick question about {org}")
    cap = _capture_dispatch(a, monkeypatch)
    a._process_contact({"id": 1, "email": "x@brand.de"}, dry_run=True)
    assert "{org}" not in cap["subject"]
    assert "your brand" in cap["subject"]


# ── List-Unsubscribe header correctness (bug #1) ───────────────────────────

def _sender(tmp_path):
    return EmailSender(Database(str(tmp_path / "s.db")), "smtp.test", 465,
                       "u@test.com", "pw")


def test_list_unsub_mailto_has_no_false_oneclick(tmp_path):
    msg = _sender(tmp_path).create_email(
        "to@x.de", "Hi", "<p>h</p>", "h",
        unsubscribe_url="mailto:u@test.com?subject=Unsubscribe")
    assert "mailto:u@test.com" in msg["List-Unsubscribe"]
    # One-click must NOT be claimed for a mailto target.
    assert msg["List-Unsubscribe-Post"] is None


def test_list_unsub_oneclick_only_for_https(tmp_path):
    msg = _sender(tmp_path).create_email(
        "to@x.de", "Hi", "<p>h</p>", "h",
        unsubscribe_url="https://reach.example.com/u/abc")
    assert "https://reach.example.com/u/abc" in msg["List-Unsubscribe"]
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
