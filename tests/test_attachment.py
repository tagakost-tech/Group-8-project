"""Tests for the company-profile attachment (first email only)."""
from modules.database import Database
from modules.email_sender import EmailSender


def _sender(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    return EmailSender(db, "smtp.test", 465, "u@test.com", "pw",
                       sender_name="Kostas", company_name="the developer")


def _attachments(msg):
    return [a for a in msg.iter_attachments()]


def test_pdf_attached_when_path_given(tmp_path):
    pdf = tmp_path / "SUPPLECO Company Profile 2026.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf bytes")
    msg = _sender(tmp_path).create_email(
        "buyer@brand.de", "Hi", "<p>hi</p>", "hi", pdf_path=str(pdf))
    atts = _attachments(msg)
    assert len(atts) == 1
    assert atts[0].get_filename() == "SUPPLECO Company Profile 2026.pdf"
    assert atts[0].get_content_type() == "application/pdf"


def test_no_attachment_when_path_none(tmp_path):
    msg = _sender(tmp_path).create_email(
        "buyer@brand.de", "Hi", "<p>hi</p>", "hi", pdf_path=None)
    assert _attachments(msg) == []


def test_process_contact_passes_pdf_on_first_email(settings_factory, tmp_path, monkeypatch):
    # First email (default attach_pdf=True) must pass the pdf path through;
    # a follow-up (attach_pdf=False) must pass None.
    from agent import OutreachAgent
    pdf = tmp_path / "profile.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    s = settings_factory(db_path=str(tmp_path / "a.db"), verify_emails=False,
                         dry_run=True, delay_between_emails=0.0,
                         pdf_path=str(pdf))
    agent = OutreachAgent(s)
    assert agent._pdf_path  # resolved

    seen = {}
    def fake_dispatch(**kwargs):
        seen["pdf_path"] = kwargs.get("pdf_path")
        return True
    monkeypatch.setattr(agent.email_sender, "dispatch_to_contact", fake_dispatch)

    contact = {"id": 1, "email": "x@brand.de", "organization": "Brand"}
    agent._process_contact(dict(contact), dry_run=True, attach_pdf=True)
    assert seen["pdf_path"] == agent._pdf_path

    agent._process_contact(dict(contact), dry_run=True, attach_pdf=False)
    assert seen["pdf_path"] is None
