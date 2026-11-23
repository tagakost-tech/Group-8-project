"""Every email's footer is the first layer of the Art. 14 notice.

The footer gave the identity, where the address came from, the basis, one
retention period and two links. It named no privacy contact, no data, no
recipients and no rights but objection, which shared a line with the privacy
link (Art. 21(4) wants it presented separately). It told everyone they were
emailed on legitimate interest, consent or not, said nothing when an AI saw
their data, and was grey on white at about 2.5:1 (decision L3.2).

Now, per L3.2's proposal (pending counsel):
- one versioned text per lawful basis; a change to the words without a new
  version fails a test, and each email records the version it carried;
- the controller and a privacy contact, the data and where it was found,
  the purpose, the basis and the interest pursued, the recipients, the
  retention, the rights including a complaint, and the full notice;
- the objection in a paragraph of its own;
- an AI line whenever an AI sees a recipient's data;
- text at a contrast of at least 4.5:1;
- and the legal identity completes Cod. Civ. 2250 (L6): a privacy contact,
  an https notice address, and for an S.r.l. or S.p.A. the share capital and
  single-member status, all required before a live send.
"""

import re

import pytest
from pydantic import ValidationError

from agent import OutreachAgent
from modules.compliance import (
    NOTICE_VERSIONS,
    notice_fingerprint,
    notice_version,
    render_legal_footer,
)

UNSUB = "https://outreach.example.test/unsubscribe/t"
CONTACT = {"email": "anna@brand.de", "source_url": "https://www.brand.de/kontakt"}


def _footer(settings_factory, basis="legitimate_interest", contact=CONTACT, **kw):
    return render_legal_footer(settings_factory(**kw), contact, UNSUB, basis=basis)


@pytest.mark.parametrize("basis", sorted(NOTICE_VERSIONS))
def test_each_basis_gets_every_first_layer_item(settings_factory, basis):
    html = _footer(settings_factory, basis)
    for must in (
        "Test Manufacturing S.r.l.",  # the controller
        "privacy@example.test",  # the privacy contact
        "name, job title and business email address",  # the data
        "brand.de",  # where it was found
        "contract manufacturing",  # the purpose
        "email provider",  # the recipients
        "365 days",  # the retention
        "access",
        "erasure",
        "complain",
        "Garante",  # the rights, with a complaint
        "https://example.test/privacy",  # the full notice
        "commercial communication",
    ):
        assert must in html, f"{basis}: footer is missing {must!r}"


def test_each_basis_states_its_own_basis(settings_factory):
    li = _footer(settings_factory, "legitimate_interest")
    consent = _footer(settings_factory, "consent")
    customer = _footer(settings_factory, "existing_customer")
    assert "legitimate interest" in li and "6(1)(f)" in li
    assert "consent" in consent and "withdraw" in consent
    assert "legitimate interest" not in consent, "a consenting contact is misinformed"
    assert "customer" in customer and "130" in customer


def test_the_objection_has_a_paragraph_of_its_own(settings_factory):
    html = _footer(settings_factory)
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.DOTALL)
    [objection] = [p for p in paragraphs if UNSUB in p]
    assert "object" in objection
    assert "privacy" not in objection.lower(), "the objection shares its paragraph"
    assert len(paragraphs) >= 3


def test_an_ai_line_appears_only_when_an_ai_sees_their_data(settings_factory):
    assert "AI" not in _footer(settings_factory)
    assert "AI" in _footer(settings_factory, ai_humanize_emails=True)
    assert "AI" in _footer(settings_factory, ai_reply_classification_enabled=True)
    # A configured AI alone sends nobody's data (step G10).
    assert "AI" not in _footer(settings_factory, llm_provider="openai_compatible", LLM_BASE_URL="http://127.0.0.1:11434/v1", LLM_MODEL="test-model")
    # Choosing search words sees no recipient's data.
    assert "AI" not in _footer(settings_factory, ai_strategist_enabled=True)


def _luminance(hex_colour):
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def test_the_text_is_readable(settings_factory):
    colours = set(re.findall(r"color:\s*#([0-9a-fA-F]{6})", _footer(settings_factory)))
    assert colours
    for colour in colours:
        ratio = 1.05 / (_luminance(colour) + 0.05)  # against white
        assert ratio >= 4.5, f"#{colour} on white is {ratio:.2f}:1"


# The words of each version, fingerprinted. Changing the words without a new
# version fails here: give the text a new version in NOTICE_VERSIONS, then
# record its fingerprint below. Version 1 (716ac1c4944af489, f60f296696e34002,
# c1b84368eaa9186f) said "an AI service"; version 2 says the AI is a model on
# SUPPLECO's own server and no outside AI company receives the data.
RECORDED = {
    "notice-li-2": "82885037798afd47",
    "notice-consent-2": "5e8fa669a050ce4b",
    "notice-customer-2": "5fff002f61a9e78e",
}


def test_changed_words_need_a_new_version():
    assert set(NOTICE_VERSIONS.values()) == set(RECORDED)
    for basis, version in NOTICE_VERSIONS.items():
        assert notice_fingerprint(basis) == RECORDED[version], basis


def test_each_email_records_the_version_of_its_basis(
    settings_factory, tmp_path, monkeypatch
):
    agent = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            ai_humanize_emails=False,
        )
    )
    sent = []
    monkeypatch.setattr(
        agent.email_sender, "dispatch_to_contact", lambda **k: sent.append(k) or 1
    )
    agent.send_single_email("anna@brand.de", channel="inbound_request")
    assert sent[0]["notice_version"] == notice_version("consent")
    assert "withdraw" in sent[0]["html_body"]
    assert "legitimate interest" not in sent[0]["html_body"]
    # Not "publicly available business sources": the operator said how.
    assert "your company&#x27;s request to us" in sent[0]["html_body"]


# ── The legal identity completes Cod. Civ. 2250 (L6) ──


def test_the_notice_address_must_be_https(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(privacy_notice_url="http://example.test/privacy")


@pytest.mark.parametrize(
    "field,value",
    [
        ("privacy_contact_email", "not-an-address"),
        ("legal_single_member", "maybe"),
    ],
)
def test_malformed_identity_values_are_refused(settings_factory, field, value):
    with pytest.raises(ValidationError):
        settings_factory(**{field: value})


def test_a_privacy_contact_is_required(settings_factory):
    assert (
        "PRIVACY_CONTACT_EMAIL"
        in settings_factory(privacy_contact_email="").missing_legal_identity()
    )


def test_an_srl_must_state_its_capital_and_single_member_status(settings_factory):
    srl = settings_factory(legal_share_capital="", legal_single_member="")
    assert {"LEGAL_SHARE_CAPITAL", "LEGAL_SINGLE_MEMBER"} <= set(
        srl.missing_legal_identity()
    )
    gmbh = settings_factory(
        legal_entity_name="Brand GmbH", legal_share_capital="", legal_single_member=""
    )
    assert gmbh.missing_legal_identity() == []


def test_a_single_member_company_says_so(settings_factory):
    assert "Socio unico" in _footer(settings_factory, legal_single_member="yes")
    assert "Socio unico" not in _footer(settings_factory, legal_single_member="no")
