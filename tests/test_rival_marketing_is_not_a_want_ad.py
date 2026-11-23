"""A rival's marketing hook is not a want-ad.

Review finding, a regression from extending want-ad suppression to every
competitor term. Contract manufacturers routinely open their own copy with a
question aimed at buyers - "Looking for a contract manufacturer? NutraPro
produces for third-party brands." - and the seeking phrase in front of the
keyword made that read as a brand asking for one. Same with "Partner with
NutraPro, a private label manufacturer": "partner with" was a seeking phrase
whatever followed it.

A want-ad states a need ("We are looking for a manufacturer."). A hook asks
the reader a question. And "partner with" only asks when it is followed by
"a"/"an" - "partner with NutraPro" or "partner with brands" is a company
describing itself.
"""

import pytest

from modules.relevance import RelevanceFilter


def _reason(org, rationale):
    return RelevanceFilter(require_vertical=False).assess(
        {
            "email": "info@nutrapro.it",
            "organization": org,
            "qualification_rationale": rationale,
            "source_url": "https://nutrapro.it/",
        }
    )["reason"]


@pytest.mark.parametrize(
    "rationale",
    [
        (
            "Looking for a contract manufacturer of food supplements? NutraPro "
            "offers capsule and tablet production for third-party brands."
        ),
        "Searching for a private label manufacturer? We make it simple.",
        "Partner with NutraPro, a private label manufacturer.",
        "We partner with brands as a private label manufacturer.",
    ],
)
def test_a_rivals_hook_is_still_a_competitor(rationale):
    assert "competitor" in _reason("NutraPro", rationale)


@pytest.mark.parametrize(
    "rationale",
    [
        "We are looking for a contract manufacturer for our vitamins.",
        "We want to partner with a private label manufacturer.",
    ],
)
def test_a_real_want_ad_is_still_a_prospect(rationale):
    assert "competitor" not in _reason("Brand GmbH", rationale)
