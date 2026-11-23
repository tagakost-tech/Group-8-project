"""A company asking FOR a contract manufacturer is a prospect, not a rival.

"Buscamos un CDMO..." was fixed: a brand shopping for exactly what SUPPLECO
sells had been binned as a competitor. But the fix covered the one token
"cdmo". Every other competitor term - "contract manufacturer", "private
label", "Lohnhersteller", "fabricante por contrato" - still matched inside a
want-ad, so the best lead there is was discarded and logged as a routine
competitor rejection.

The rule is about voice: a term the company says about itself counts; the
same term in "we are looking for..." does not.
"""

import pytest

from modules.relevance import RelevanceFilter


@pytest.fixture
def rf():
    return RelevanceFilter(require_vertical=False)


def _reason(rf, org, rationale):
    return rf.assess(
        {
            "email": "info@example.de",
            "organization": org,
            "qualification_rationale": rationale,
            "source_url": "https://example.de/about",
        }
    )["reason"]


@pytest.mark.parametrize(
    "rationale",
    [
        "Cerchiamo un contract manufacturer per i nostri integratori.",
        "We are looking for a private label manufacturer for our vitamins.",
        "Wir suchen einen Lohnhersteller für unsere Nahrungsergänzungsmittel.",
        "Buscamos fabricante por contrato para nuestros complementos.",
        "Seeking a white label producer for our sports nutrition line.",
    ],
)
def test_a_want_ad_is_not_a_competitor(rf, rationale):
    assert "competitor" not in _reason(rf, "Brand GmbH", rationale)


@pytest.mark.parametrize(
    "org, rationale",
    [
        ("Brand GmbH", "We are a contract manufacturer of food supplements."),
        ("Lohnhersteller Süd GmbH", "Nahrungsergänzungsmittel."),
        ("Naturacare", "A European CDMO and dietary supplement manufacturer."),
        # A rival inviting brands in. "partner with" signals a want-ad only
        # when the company is asking for a partner, not offering itself.
        ("Brand GmbH", "Partner with us, a leading contract manufacturer."),
        # Qualifier in the name, manufacturing claim in the description.
        ("BioLab Private Label", "Nutraceutical manufacturer."),
    ],
)
def test_a_self_description_is_still_a_competitor(rf, org, rationale):
    assert "competitor" in _reason(rf, org, rationale)
