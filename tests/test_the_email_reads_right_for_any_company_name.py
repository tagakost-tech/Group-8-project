"""The first email reads right whatever the company is called, and says what
SUPPLECO makes.

SUPPLECO's first email ends a sentence with the recipient's organisation: "...
collaborazione con {{organization}}." and "...partnership with
{{organization}}.". Most Italian companies are called "... S.p.A." or
"... S.r.l.", so the email read "Rossi Integratori S.p.A..". The renderer now
writes one full stop where a value ends with one and the template's next
character is one. Nothing else changes: the template's words, a name in
mid-sentence, a value merged as markup, and escaping.

Follow-up 1 said SUPPLECO brings "food supplements, cosmetics, and medical
devices" to market. SUPPLECO does not make cosmetics; its own email names food
supplements, medical devices and foods for special medical purposes.
"""

import html
import os
import re
from pathlib import Path

import pytest
from test_first_email_language import TO, _agent, _first_email

from modules.email_sender import EmailSender

ROOT = Path(__file__).resolve().parent.parent
# The fixed client's email texts (conftest.py), not the live client's.
TEMPLATES = Path(os.environ["CLIENT_TEMPLATES_DIR"])

SENTENCE = {
    "it": "per esplorare una possibile collaborazione con {org}",
    "en": "and explore a potential partnership with {org}",
}


def _plain(email):
    return " ".join(email["plain_body"].split())


@pytest.mark.parametrize("language", ["it", "en"])
@pytest.mark.parametrize("organization", ["Rossi Integratori S.p.A.", "Acme Inc."])
def test_a_name_ending_in_a_full_stop_is_followed_by_one(
    settings_factory, tmp_path, language, organization
):
    a, sent = _agent(settings_factory, tmp_path)
    email = _first_email(a, sent, TO[language], organization=organization)
    sentence = SENTENCE[language].format(org=organization)
    assert sentence in _plain(email)
    assert sentence + "." not in _plain(email)
    assert html.escape(sentence) + "</p>" in email["html_body"]
    assert organization + "." not in email["html_body"]


@pytest.mark.parametrize("language", ["it", "en"])
def test_a_name_without_a_final_full_stop_keeps_the_templates_one(
    settings_factory, tmp_path, language
):
    a, sent = _agent(settings_factory, tmp_path)
    email = _first_email(a, sent, TO[language], organization="Rossi Integratori SpA")
    sentence = SENTENCE[language].format(org="Rossi Integratori SpA")
    assert sentence + "." in _plain(email)
    assert html.escape(sentence) + ".</p>" in email["html_body"]


@pytest.mark.parametrize("language", ["it", "en"])
def test_the_name_is_still_escaped_once(settings_factory, tmp_path, language):
    a, sent = _agent(settings_factory, tmp_path)
    email = _first_email(a, sent, TO[language], organization="A & B S.r.l.")
    sentence = SENTENCE[language].format(org="A & B S.r.l.")
    assert sentence + "." not in _plain(email)
    assert sentence in _plain(email)
    assert (
        SENTENCE[language].format(org="A &amp; B S.r.l.") + "</p>"
        in (email["html_body"])
    )
    assert "&amp;amp;" not in email["html_body"]


def test_a_name_ending_in_a_full_stop_in_mid_sentence_is_unchanged():
    template = (
        "<p>At {{organization}}, today.</p><p>{{organization}} is busy.</p>"
        "<p>{{name}}!</p><p>{{name}},</p>"
    )
    html_body, _plain_body = EmailSender.render_template(
        template, {"organization": "Rossi S.p.A.", "name": "Maria..."}, "Suppleco", "G"
    )
    assert html_body == (
        "<p>At Rossi S.p.A., today.</p><p>Rossi S.p.A. is busy.</p>"
        "<p>Maria...!</p><p>Maria...,</p>"
    )


def test_every_text_placeholder_drops_the_second_full_stop_and_markup_does_not():
    template = (
        "<p>{{sender_title}}.</p><p>{{name}}.</p><p>{{company_name}}.</p>"
        "<p>{{sender_signature}}.</p>"
    )
    html_body, plain = EmailSender.render_template(
        template,
        {"name": "Dr. Rossi Jr."},
        "Suppleco S.r.l.",
        "G",
        values={"sender_title": "R&D Dept."},
        markup={"sender_signature": "Suppleco S.r.l."},
    )
    assert html_body == (
        "<p>R&amp;D Dept.</p><p>Dr. Rossi Jr.</p><p>Suppleco S.r.l.</p>"
        "<p>Suppleco S.r.l..</p>"
    )
    assert plain.splitlines()[:3] == ["R&D Dept.", "Dr. Rossi Jr.", "Suppleco S.r.l."]


def test_markup_that_replaces_a_text_value_keeps_the_templates_full_stop():
    """Markup given under a text placeholder's name is what goes in, and it
    is inserted as it is, the template's full stop after it too."""
    html_body, _plain_body = EmailSender.render_template(
        "<p>{{organization}}.</p>",
        {"organization": "Rossi S.p.A."},
        "Suppleco",
        "G",
        markup={"organization": "<b>Rossi S.p.A.</b>"},
    )
    assert html_body == "<p><b>Rossi S.p.A.</b>.</p>"


@pytest.mark.parametrize("language", ["it", "en"])
def test_a_subject_ending_with_the_name_gets_one_full_stop(
    settings_factory, tmp_path, language
):
    a, sent = _agent(
        settings_factory,
        tmp_path,
        email_subject="Company profile for {org}.",
        email_subject_it="Profilo aziendale per {org}.",
    )
    email = _first_email(a, sent, TO[language], organization="Acme Inc.")
    expected = {"it": "Profilo aziendale per", "en": "Company profile for"}[language]
    assert email["subject"] == f"{expected} Acme Inc."
    other = {"it": "acquisti@acme.it", "en": "einkauf@acme.de"}[language]
    plain = _first_email(a, sent, other, organization="Acme")
    assert plain["subject"] == f"{expected} Acme."


# ── What SUPPLECO makes ──────────────────────────────────────────────────────


def _recipient_templates():
    return sorted(
        p for p in TEMPLATES.glob("*.html") if p.name != "dashboard.html"
    )


def test_every_template_a_recipient_can_get_is_checked():
    names = {p.name for p in _recipient_templates()}
    assert {
        "email_template.html",
        "email_template_it.html",
        "followup_1.html",
        "followup_1_it.html",
        "followup_2.html",
        "followup_2_it.html",
    } <= names


@pytest.mark.parametrize("path", _recipient_templates(), ids=lambda p: p.name)
def test_no_email_says_suppleco_makes_cosmetics(path):
    text = path.read_text(encoding="utf-8").lower()
    assert "cosmetic" not in text and "cosmetici" not in text


@pytest.mark.parametrize(
    "name,families",
    [
        (
            "followup_1.html",
            "dietary supplements, medical devices and foods for special medical purposes",
        ),
        (
            "followup_1_it.html",
            "integratori, dispositivi medici e alimenti a fini medici speciali",
        ),
    ],
)
def test_follow_up_1_names_the_three_product_families(name, families):
    template = (TEMPLATES / name).read_text(encoding="utf-8")
    _html, plain = EmailSender.render_template(
        template, {"organization": "Rossi S.p.A."}, "Suppleco", "Giulia"
    )
    assert families in re.sub(r"\s+", " ", plain)
