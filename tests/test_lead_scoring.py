"""Tests for the LeadScorer scoring dimensions and tiers."""
from modules.lead_scoring import LeadScorer


def test_high_value_contact_scores_high():
    r = LeadScorer().score_contact({
        "email": "jane.doe@vitalis.com",
        "role": "CEO",
        "organization": "Vitalis Supplements Ltd",
        "source_url": "https://x.com/contact",
    })
    assert r["score"] >= 75
    assert r["tier"] == "A"


def test_empty_contact_scores_low():
    r = LeadScorer().score_contact({"email": ""})
    assert r["tier"] == "D"


def test_email_quality_personal_pattern():
    r = LeadScorer().score_contact({"email": "john.smith@company.com"})
    assert r["breakdown"]["email_quality"] == 25


def test_score_is_monotonic():
    scorer = LeadScorer()
    high = scorer.score_contact({
        "email": "jane.doe@pharmaceutical.com", "role": "CEO",
        "organization": "Pharma Ltd", "source_url": "https://x.com/contact"})
    low = scorer.score_contact({"email": "random@x.com"})
    assert high["score"] > low["score"]
