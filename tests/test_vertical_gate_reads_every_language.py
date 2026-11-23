"""A German prospect must not be dropped for spelling ä as ae.

The vertical gate decides whether a company is in SUPPLECO's market at all. A
miss here is invisible: the contact is discarded with "no SUPPLECO vertical
signal", which reads exactly like the filter working, and nobody notices a lead
that never appeared.

Two real gaps, both the same shape as defects already fixed elsewhere in this
file:

  German writes an umlaut three ways - the character, the character stripped by
  a bad decode, and the ae/oe/ue transliteration used in ASCII contexts by
  convention. Only the first two were listed, so "Wir stellen
  Nahrungsergaenzungsmittel her" was rejected. This compounds with any decoding
  fault upstream: a page with a misdeclared charset arrives mangled, and the
  gate then bins the company for it.

  French plurals were left broken after the identical Spanish bug was fixed.
  The plural's 's' falls between the two words, so "complement alimentaire"
  never matches "complements alimentaires" - one sibling repaired, its twin
  missed, which is this codebase's recurring shape.
"""

import pytest

from modules.relevance import RelevanceFilter


@pytest.fixture
def rf():
    return RelevanceFilter(require_vertical=True)


def _assess(rf, rationale):
    return rf.assess(
        {
            "email": "info@example.de",
            "organization": "Example",
            "qualification_rationale": rationale,
            "source_url": "https://example.de/about",
        }
    )


@pytest.mark.parametrize(
    "label,rationale",
    [
        ("German, transliterated", "Wir stellen Nahrungsergaenzungsmittel her."),
        ("German, umlaut stripped", "Wir stellen Nahrungserganzungsmittel her."),
        ("German, as written", "Wir stellen Nahrungsergänzungsmittel her."),
        ("German, vitamin prep", "Hersteller von Vitaminpraeparaten fuer Europa."),
        ("French, plural plain", "Nous produisons des complements alimentaires."),
        ("French, plural accented", "Nous produisons des compléments alimentaires."),
        ("French, singular", "Un complement alimentaire naturel."),
        ("Italian", "Produciamo integratori alimentari."),
        ("Spanish, plural", "Producimos complementos alimenticios."),
        ("Spanish, singular", "Un complemento alimenticio natural."),
        ("English", "We produce dietary supplements."),
    ],
)
def test_the_market_is_recognised_in_the_language_it_writes_in(rf, label, rationale):
    result = _assess(rf, rationale)
    assert result["relevant"] is True, (
        f"{label}: a real supplement company was dropped - {result['reason']!r}"
    )


@pytest.mark.parametrize(
    "rationale",
    [
        "We manufacture industrial bearings for machinery.",
        "Wir bauen Werkzeugmaschinen fuer die Industrie.",
        "Nous fabriquons des pieces automobiles.",
        "Produciamo mobili per ufficio.",
    ],
)
def test_an_off_target_company_is_still_rejected(rf, rationale):
    """Control. Broadening the list must not turn the gate into a pass-through
    - over-blocking is invisible, but so is under-blocking, and the second
    wastes sends on companies that will never buy."""
    assert _assess(rf, rationale)["relevant"] is False
