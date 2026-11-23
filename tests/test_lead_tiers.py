"""Every tier boundary must be pinned, at the exact point it turns.

The tier decides what happens to a contact: min_lead_score_to_email gates
sending, and the dashboard sorts and chips by tier, so moving a boundary
silently changes who gets emailed.

Nothing pinned A (75) or B (50). The one test that looked like it did,
test_high_value_contact_scores_high, uses a contact scoring a clamped 100
(35 role + 25 email + 25 org + 15 source), so the A threshold could be set
anywhere from 0 to 100 and it would still pass. Mutation testing found it:
moving 75 to 70 and 50 to 40 both survived the entire suite.

Boundaries are tested through _tier_label directly, on purpose. Driving them
through assess() means composing a contact that happens to score 74, which
breaks whenever any unrelated weight is retuned - the test would then fail for
a reason that has nothing to do with the boundary it is guarding.
"""

import pytest

from modules.lead_scoring import LeadScorer


@pytest.mark.parametrize(
    "score,tier",
    [
        # Each boundary, and the point one below it. A test that only checks
        # the middle of a band cannot tell 75 from 70.
        (100, "A"),
        (76, "A"),
        (75, "A"),  # the A boundary itself
        (74, "B"),  # one below - this is what moving the threshold breaks
        (51, "B"),
        (50, "B"),  # the B boundary
        (49, "C"),
        (26, "C"),
        (25, "C"),  # the C boundary
        (24, "D"),
        (1, "D"),
        (0, "D"),
    ],
)
def test_each_tier_boundary_is_exact(score, tier):
    assert LeadScorer._tier_label(score) == tier, (
        f"a score of {score} must be tier {tier} - the documented bands are "
        f"A 75+, B 50-74, C 25-49, D under 25"
    )


def test_the_bands_are_contiguous_and_ordered():
    """No score may fall into a gap, and the tiers may not go backwards.
    Catches a boundary edited to overlap or invert rather than merely move."""
    order = {"D": 0, "C": 1, "B": 2, "A": 3}
    labels = [LeadScorer._tier_label(s) for s in range(101)]

    assert set(labels) <= set(order), f"unexpected tier letter in {set(labels)}"
    ranks = [order[label] for label in labels]
    assert ranks == sorted(ranks), "tier quality decreases as the score rises"
    assert labels[0] == "D" and labels[100] == "A"


def test_a_scored_contact_lands_in_the_tier_its_score_implies():
    """The join between the two halves: whatever assess() scores, the tier it
    reports must be the one _tier_label gives for that score. Without this,
    both halves could be individually correct and wired together wrongly."""
    scorer = LeadScorer()
    contacts = [
        {
            "email": "ceo@brand.de",
            "role": "CEO",
            "organization": "Brand Supplements",
            "source_url": "https://brand.de/team",
        },
        {
            "email": "info@shop.de",
            "role": "",
            "organization": "",
            "source_url": "https://directory.example/listing",
        },
        {
            "email": "buyer@pharma.de",
            "role": "Procurement Manager",
            "organization": "Pharma Labs",
            "source_url": "https://pharma.de/about",
        },
    ]
    for contact in contacts:
        result = scorer.score_contact(contact)
        assert result["tier"] == LeadScorer._tier_label(result["score"]), (
            f"{contact['email']} scored {result['score']} but was labelled "
            f"{result['tier']}"
        )
