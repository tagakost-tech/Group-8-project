"""Every commercial email says who sent it, that it is commercial, and why.

The footer carried a first name, a trading name and an unsubscribe link. For
a cold B2B email from an Italian company that leaves out what the law asks
for at first contact:

- D.Lgs. 70/2003 Art. 8: an unsolicited commercial email must be identifiable
  as such, and must say the recipient can object.
- Cod. Civ. Art. 2250: registered office and Registro Imprese details in all
  company correspondence, email included.
- GDPR Art. 14: controller identity, legal basis, where the address came
  from, and a route to the full notice - at the latest with the first email.

The legal details are SUPPLECO's to supply, so they are settings. A blank one is
rendered as a visible "[set ...]" marker, never guessed.
"""

import pytest

from agent import OutreachAgent
from modules.compliance import render_legal_footer

LEGAL = {
    "legal_entity_name": "Test Manufacturing S.r.l.",
    "legal_registered_office": "Via Roma 1, 00100 Roma RM, Italy",
    "legal_company_registration": "Reg. Imprese di Roma n. 00000000000 - P.IVA IT00000000000",
    "privacy_notice_url": "https://example.test/privacy",
}

TEMPLATES = [
    "templates/email_template.html",
    "templates/followup_1.html",
    "templates/followup_2.html",
]


@pytest.mark.parametrize("path", TEMPLATES)
def test_every_template_carries_the_legal_footer(path):
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert "{{legal_footer}}" in (root / path).read_text(encoding="utf-8")


def test_the_fallback_template_carries_it_too():
    """Used whenever the HTML file is missing - the likeliest moment for a
    footer to go unnoticed."""
    assert "{{legal_footer}}" in OutreachAgent._default_template()


def _send_and_capture(settings_factory, tmp_path, monkeypatch, contact, **kw):
    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        ai_humanize_emails=False,
        **{**LEGAL, **kw},
    )
    a = OutreachAgent(s)
    captured = []
    monkeypatch.setattr(
        a.email_sender,
        "dispatch_to_contact",
        lambda **k: captured.append(k) or 1,
    )
    sid = a.db.log_search("q")
    cid = a.db.add_contact(
        sid,
        email=contact["email"],
        organization=contact.get("organization"),
        source_url=contact.get("source_url"),
    )
    a._process_contact({**contact, "id": cid}, dry_run=True)
    assert captured, "nothing was composed"
    return captured[0]["html_body"]


def test_the_rendered_email_identifies_the_sender(
    settings_factory, tmp_path, monkeypatch
):
    """Drives the real compose path, not the helper: a footer the helper can
    render but _process_contact never inserts protects nobody."""
    html = _send_and_capture(
        settings_factory,
        tmp_path,
        monkeypatch,
        {
            "email": "info@brand.de",
            "organization": "Brand GmbH",
            "source_url": "https://www.brand.de/kontakt",
        },
    )
    for must in (
        "Test Manufacturing S.r.l.",
        "Via Roma 1, 00100 Roma RM, Italy",
        "P.IVA IT00000000000",
        "commercial communication",
        "6(1)(f)",
        "object",
        "https://example.test/privacy",
        "brand.de",
    ):
        assert must in html, f"footer is missing {must!r}"
    assert "{{legal_footer}}" not in html


def test_a_manual_contact_names_public_sources(settings_factory, tmp_path):
    """Direct Send contacts have no source URL. The footer must still say
    something true rather than print an empty "from ."."""
    s = settings_factory(**LEGAL)
    html = render_legal_footer(s, {"email": "x@y.de"}, "mailto:a@b.it")
    assert "publicly available business sources" in html


def test_a_blank_field_is_marked_never_guessed(settings_factory):
    s = settings_factory(**{**LEGAL, "legal_entity_name": ""})
    html = render_legal_footer(s, {}, "mailto:a@b.it")
    assert "[set LEGAL_ENTITY_NAME]" in html


def test_values_are_escaped(settings_factory):
    """Operator-typed text lands inside HTML. "A & B <Srl>" must not break
    the footer or inject markup."""
    s = settings_factory(**{**LEGAL, "legal_entity_name": "A & B <Srl>"})
    html = render_legal_footer(s, {}, "mailto:a@b.it")
    assert "A &amp; B &lt;Srl&gt;" in html
    assert "<Srl>" not in html


def test_the_share_capital_appears_when_set(settings_factory):
    s = settings_factory(**{**LEGAL, "legal_share_capital": "EUR 10.000 i.v."})
    assert "EUR 10.000 i.v." in render_legal_footer(s, {}, "mailto:a@b.it")


def test_missing_legal_identity_names_the_env_keys(settings_factory):
    s = settings_factory(**{**LEGAL, "privacy_notice_url": "", "legal_entity_name": ""})
    assert set(s.missing_legal_identity()) == {
        "LEGAL_ENTITY_NAME",
        "PRIVACY_NOTICE_URL",
    }


def test_a_relative_privacy_url_is_rejected(settings_factory):
    """ "www.x.it" becomes a relative link inside an email - it opens nothing."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings_factory(privacy_notice_url="www.example.it/privacy")
