"""The spam linter must not cry wolf on the product's own vocabulary.

It matched with `w in text_lower` — raw substring — which is the defect family
this codebase has now shipped six times ("news" in "newsletter", "coo" in
"coordinator", "vitamin" in "multivitamin", "lab" in "Labels", "cash" in
"cashew"). Here it flagged "prized" as "prize" and "cashew" as "cash".

Worse for this business specifically: bare "free" matched "gluten-free",
"sugar-free" and "lactose-free" — the label claims SUPPLECO's own customers put
on their products — and it fired on the shipped followup_1.html, whose opt-out
line reads "please feel free to". A linter that flags the polite sentence in
every template teaches the operator to ignore it, and then it is worth less
than nothing: it is a check that looks like it is working.
"""

import pathlib

import pytest

from modules.deliverability import DeliverabilityLinter


def _spam(text: str) -> list:
    issues = DeliverabilityLinter.lint(f"<p>{text}</p>")
    return [i for i in issues if i.startswith("Spam-trigger")]


# ── Legitimate copy must not be flagged ────────────────────────────────────


@pytest.mark.parametrize(
    "copy",
    [
        "Our gluten-free and sugar-free range is certified.",
        "A lactose-free formulation from an allergen-free facility.",
        "Cashew protein isolate, cold-pressed.",
        "Our prized formulation won an industry award.",
        "We can offer cheaper unit costs at volume.",
        "If this is not relevant, please feel free to unsubscribe.",
        "We can send a free sample of the base formulation.",
    ],
)
def test_ordinary_supplement_copy_is_not_spam(copy):
    """Every one of these was flagged before the matcher used whole terms.
    'free sample' matters especially: sending samples is literally what a
    contract manufacturer offers a prospect."""
    assert _spam(copy) == [], f"legitimate copy flagged as spam: {copy!r}"


def test_the_shipped_templates_are_clean():
    """followup_1.html was flagged for the word 'free' in its opt-out line."""
    root = pathlib.Path(__file__).resolve().parent.parent / "templates"
    for name in ("email_template.html", "followup_1.html", "followup_2.html"):
        html = (root / name).read_text(encoding="utf-8")
        assert _spam(html) == [], f"{name} trips the spam linter"


# ── Real spam must still be caught ─────────────────────────────────────────


@pytest.mark.parametrize(
    "copy,expected",
    [
        ("Act now! This limited time offer expires soon.", "act now"),
        ("Click here to buy now and earn money.", "click here"),
        ("Congratulations, you are a winner of our lottery prize!", "lottery"),
        ("Risk-free investment with no obligation.", "risk-free"),
        ("Start your free trial today.", "free trial"),
        ("Get it for free, at no cost.", "for free"),
        ("100% guaranteed returns, double your money.", "100%"),
    ],
)
def test_real_spam_is_still_flagged(copy, expected):
    """Control: the fix must not work by never matching anything."""
    found = _spam(copy)
    assert found, f"real spam went unflagged: {copy!r}"
    assert expected in found[0]


@pytest.mark.parametrize(
    "copy",
    [
        "Our cash-flow projections for the quarter.",
        "A prize-winning formulation.",
        "Investment-grade packaging materials.",
        "We offer a discount-rate for volume orders.",
    ],
)
def test_a_hyphen_does_not_open_a_word_boundary(copy):
    """Ordinary business compounds, all flagged if the boundary treats "-" as
    a word break: cash-flow as "cash", prize-winning as "prize",
    investment-grade as "investment", discount-rate as "discount"."""
    assert _spam(copy) == [], f"a hyphenated compound was flagged: {copy!r}"


def test_a_hyphenated_trigger_still_matches_itself():
    """The boundary must not become so strict that "risk-free" stops matching.
    And it must match ONCE - with a plain word boundary, "Risk-free trial"
    also matched "free trial" through the hyphen, double-counting one phrase."""
    found = _spam("Risk-free trial of the base formulation.")
    assert found and "risk-free" in found[0]
    assert "free trial" not in found[0]


def test_a_trigger_ending_in_punctuation_still_matches():
    r"""\b would assert the wrong thing after '%' or '$', so the matcher uses
    lookarounds instead."""
    assert _spam("100% risk free returns")
    assert _spam("Send $$$ today")


# ── The rest of the linter still works ─────────────────────────────────────


def test_the_other_checks_are_unaffected():
    """Guard: changing the spam matcher must not disturb the sibling rules."""
    html = "<p>" + ("<a href='#'>x</a>" * 6) + "Hello!!!! WORDS SHOUTING HERE NOW</p>"
    issues = DeliverabilityLinter.lint(html)
    assert any("Many links" in i for i in issues)
    assert any("exclamation" in i for i in issues)
    assert any("unsubscribe" in i for i in issues)
