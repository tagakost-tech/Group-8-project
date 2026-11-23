"""The follow-ups describe SUPPLECO in SUPPLECO's own words.

Follow-up 1 said "we help brands bring food supplements ... to market -
handling R&D, formulation, full-scale production, and regulatory compliance
under one roof", and follow-up 2 offered to "support your product development
and production". Both were our words. SUPPLECO sent its own value proposition on
10 November 2026 (modules/suppleco_profile.py), and the sentences that say what
SUPPLECO does now use its phrases, verbatim, in the template's language, with
only the connecting words a sentence needs.

Nothing else in a follow-up changes: its greeting, its other sentences, its
placeholders, its sign-off, its footer and its unsubscribe line. The first
email is SUPPLECO's own text and is not touched.
"""

import os
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from modules.client_profile import VALUE_PROPOSITION_PHRASES
from modules.email_sender import EmailSender

ROOT = Path(__file__).resolve().parents[1]
# The fixed client's email texts (conftest.py), not the live client's.
TEMPLATES = Path(os.environ["CLIENT_TEMPLATES_DIR"])

# template: (language, SUPPLECO's phrases it now uses, in order)
SUPPLECO_WORDS = {
    "followup_1.html": (
        "en",
        [
            "specialized CDMO for food supplements",
            "manufacturing and packaging of dietary supplements",
            "medical devices and foods for special medical purposes",
            "support from formulation to commercialization",
        ],
    ),
    "followup_1_it.html": (
        "it",
        [
            "CDMO specializzata in integratori alimentari",
            "produzione e confezionamento di integratori",
            "dispositivi medici e alimenti a fini medici speciali",
            "supporto dalla formulazione alla commercializzazione",
        ],
    ),
    "followup_2.html": ("en", ["support from formulation to commercialization"]),
    "followup_2_it.html": (
        "it",
        ["supporto dalla formulazione alla commercializzazione"],
    ),
}

OUR_OLD_WORDS = [
    "we help brands bring",
    "under one roof",
    "regulatory compliance",
    "product development and production",
    "aiutiamo i marchi",
    "sotto un unico tetto",
    "conformità normativa",
    "lo sviluppo e la produzione dei Suoi prodotti",
]

# What each follow-up was before its SUPPLECO sentence changed: its
# placeholders, its paragraphs, and the words a recipient reads before the
# footer.
SHAPE = {
    "followup_1.html": (8, 71),
    "followup_1_it.html": (8, 75),
    "followup_2.html": (7, 70),
    "followup_2_it.html": (7, 72),
}
PLACEHOLDERS = {
    "{{greeting}}",
    "{{organization}}",
    "{{company_name}}",
    "{{sender_name}}",
    "{{unsubscribe_url}}",
    "{{legal_footer}}",
    "{{tracking_pixel}}",
}


def _template(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _plain(name):
    _html, plain = EmailSender.render_template(
        _template(name), {"organization": "Rossi S.p.A."}, "Suppleco", "Giulia"
    )
    return re.sub(r"\s+", " ", plain)


@pytest.mark.parametrize("name", sorted(SUPPLECO_WORDS))
def test_the_follow_up_says_what_suppleco_does_in_supplecos_words(name):
    language, phrases = SUPPLECO_WORDS[name]
    plain = _plain(name)
    for phrase in phrases:
        assert phrase in VALUE_PROPOSITION_PHRASES[language], (
            f"{phrase!r} is not SUPPLECO's own {language} wording"
        )
        assert phrase in plain, f"{name} does not say {phrase!r}"
    at = [plain.index(p) for p in phrases]
    assert at == sorted(at), f"{name} reorders SUPPLECO's phrases"


@pytest.mark.parametrize("name", sorted(SUPPLECO_WORDS))
def test_our_own_description_of_suppleco_is_gone(name):
    plain = _plain(name)
    left = [w for w in OUR_OLD_WORDS if w in plain]
    assert not left, f"{name} still says {left}"


@pytest.mark.parametrize("name", sorted(SHAPE))
def test_the_follow_up_keeps_its_shape(name):
    template = _template(name)
    paragraphs, words = SHAPE[name]
    assert set(re.findall(r"\{\{\w+\}\}", template)) == PLACEHOLDERS
    assert template.count("<p ") == paragraphs
    before_footer = template.split("<hr", 1)[0]
    read = BeautifulSoup(before_footer, "html.parser").get_text(" ").split()
    assert abs(len(read) - words) <= words * 0.1, (
        f"{name} reads {len(read)} words, it read {words}"
    )
