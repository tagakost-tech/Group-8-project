"""Regression tests for the self-learning engine.

The learner is a multi-armed bandit that was scored as a plain ratio with no
sample-size confidence and no upper bound. Each test below pins one of the
pathologies that produced, measured on the old weight function:

    1 search, 1 lucky reply   ->  8.30
    100 searches, 20 replies  ->  3.10   (a fluke outranked proof 2.7:1)
    single jackpot search     -> 30.30   (918x compounded across seed*country)
    50 contacts, all junk     ->  0.30   (3x better than finding nothing)
"""

import random

import pytest

from modules.learning import (
    MAX_WEIGHT,
    MIN_WEIGHT,
    PerformanceLearner,
)
from modules.query_generator import QueryGenerator

SEEDS = ["vitamin", "multivitamin", "food supplement", "nutraceutical"]
COUNTRIES = ["Germany", "France", "Ireland"]


class FakeDB:
    """Serves canned search outcomes, newest first (as the real query does)."""

    def __init__(self, outcomes):
        self._outcomes = outcomes

    def get_search_outcomes(self, limit=500):
        return self._outcomes[:limit]


def _outcome(query, results=0, verified=0, responded=0):
    return {
        "search_query": query,
        "results_count": results,
        "verified": verified,
        "responded": responded,
    }


def _learner(outcomes, **kw):
    return PerformanceLearner(FakeDB(outcomes), SEEDS, COUNTRIES, cache_ttl=0, **kw)


# ── 1. Evidence has to be earned ───────────────────────────────────────────


def test_a_fluke_does_not_outrank_a_proven_performer():
    """The headline defect: one search with one reply scored 8.30 while a
    hundred searches with twenty replies scored 3.10."""
    fluke = _learner(
        [_outcome("vitamin distributor Germany", results=8, verified=3, responded=1)]
    )
    proven = _learner(
        [
            _outcome("nutraceutical distributor France", results=9, verified=2)
            for _ in range(100)
        ]
        # ...of which twenty also drew a reply
        + [
            _outcome("nutraceutical distributor France", results=9, responded=1)
            for _ in range(20)
        ]
    )

    fluke_w = fluke.weights()[0]["vitamin"]
    proven_w = proven.weights()[0]["nutraceutical"]

    assert proven_w > fluke_w, (
        f"a single lucky search ({fluke_w}) still outranks sustained evidence "
        f"({proven_w})"
    )


def test_one_lucky_search_stays_near_neutral():
    """Shrinkage: a single observation should move the estimate a little, not
    launch it to 8x. Measured against the untried baseline rather than an
    absolute number, so the assertion survives recalibration of the constants."""
    lea = _learner(
        [_outcome("vitamin distributor Germany", results=8, verified=3, responded=1)]
    )
    weights = lea.weights()[0]
    lucky = weights["vitamin"]
    untried = weights["nutraceutical"]  # never searched at all

    assert lucky > untried, "a positive result should still count for something"
    assert lucky < untried * 1.5, (
        f"one lucky search moved the weight from {untried} to {lucky} - that is "
        f"not shrinkage, that is the old fluke-wins behaviour"
    )


def test_a_reply_is_worth_more_to_the_bandit_than_a_verified_address():
    """A reply is the only evidence that the TARGETING was right rather than
    merely that the scraper found a working mailbox. Two seeds can produce
    identical search volume and identical verified-contact counts while only
    one produces conversations with SUPPLECO - and REPLY_VALUE is the entire
    mechanism that tells them apart.

    Drop it and the learner optimises for finding addresses instead of finding
    customers. Discovery keeps running, the dashboard keeps showing verified
    contacts climbing, and the vocabulary drifts toward whatever scrapes
    easily. Nothing looks wrong; the agent has just stopped learning the only
    thing worth learning.
    """
    answered = _learner(
        [
            _outcome("vitamin distributor Germany", results=9, verified=2, responded=1)
            for _ in range(20)
        ]
    )
    ignored = _learner(
        [
            _outcome("nutraceutical distributor France", results=9, verified=2)
            for _ in range(20)
        ]
    )

    answered_w = answered.weights()[0]["vitamin"]
    ignored_w = ignored.weights()[0]["nutraceutical"]

    assert answered_w > ignored_w, (
        f"a seed that produced twenty replies is weighted {answered_w}, the "
        f"same seed volume with no reply at all is weighted {ignored_w} - "
        f"replies are not reaching the score"
    )
    # Not merely different: a reply is worth REPLY_VALUE verified contacts, so
    # the gap has to be substantial or the signal is lost in the shrinkage.
    assert answered_w > ignored_w * 1.3, (
        f"replies barely register: {ignored_w} -> {answered_w}"
    )


def test_verified_contacts_still_count_on_their_own():
    """The control. Valuing replies must not mean ignoring everything else -
    a seed that produces verified addresses and has simply not been replied to
    yet must still outrank one that produces nothing at all, or discovery would
    abandon every new vertical before it had a chance to convert."""
    productive = _learner(
        [
            _outcome("vitamin distributor Germany", results=9, verified=3)
            for _ in range(20)
        ]
    )
    barren = _learner(
        [_outcome("nutraceutical distributor France", results=9) for _ in range(20)]
    )

    assert productive.weights()[0]["vitamin"] > barren.weights()[0]["nutraceutical"]


# ── 2. Compounding must not explode ────────────────────────────────────────


def test_weights_are_clamped():
    """seed and country weights MULTIPLY in the generator, so an unbounded
    weight of 30 became a 918x bias for one pair and exploration stopped."""
    jackpot = _learner(
        [
            _outcome(
                "vitamin distributor Germany", results=40, verified=10, responded=4
            )
            for _ in range(50)
        ]
    )
    seed_w, country_w = jackpot.weights()

    assert seed_w["vitamin"] <= MAX_WEIGHT
    assert country_w["Germany"] <= MAX_WEIGHT
    compounded = seed_w["vitamin"] * country_w["Germany"]
    assert compounded <= MAX_WEIGHT**2, f"pair bias {compounded:.0f}x"
    assert compounded < 20, (
        f"a single winning pair is {compounded:.0f}x a neutral one - that is a "
        f"lock-in, not a preference"
    )


def test_a_dead_arm_keeps_a_floor():
    """At weight 0 an arm can never be sampled again, so it can never be
    observed recovering."""
    dead = _learner([_outcome("nutraceutical distributor France") for _ in range(40)])
    assert dead.weights()[0]["nutraceutical"] >= MIN_WEIGHT


# ── 3. Substring double-counting ───────────────────────────────────────────


def test_a_seed_is_not_credited_for_a_longer_seeds_searches():
    """`if name in query` credited every "multivitamin" search to "vitamin"
    too, folding one arm's evidence into another's."""
    lea = _learner(
        [
            _outcome(
                "multivitamin distributor Germany",
                results=20,
                verified=10,
                responded=3,
            )
            for _ in range(20)
        ]
    )
    snap = {r["name"]: r for r in lea.snapshot()["top_verticals"]}

    assert "multivitamin" in snap, "the seed that was actually searched is missing"
    assert "vitamin" not in snap, (
        "'vitamin' was credited with 'multivitamin' searches - one arm's "
        "evidence is contaminating another's"
    )


def test_a_multi_word_seed_still_matches():
    """Guard: word-boundary matching must not stop a legitimate phrase hit."""
    lea = _learner(
        [_outcome("food supplement distributor Germany", results=5, verified=2)]
    )
    names = [r["name"] for r in lea.snapshot()["top_verticals"]]
    assert "food supplement" in names


# ── 4. Junk is not better than nothing ─────────────────────────────────────


def test_finding_only_junk_beats_nothing_by_at_most_a_hair():
    """Fifty contacts that never verify and never reply cost scraping and
    verification budget to produce nothing. The old formula scored that 3x
    better than a search that found no rows at all."""
    junk = _learner(
        [_outcome("vitamin distributor Germany", results=50) for _ in range(10)]
    )
    nothing = _learner(
        [_outcome("vitamin distributor Germany", results=0) for _ in range(10)]
    )

    assert junk.weights()[0]["vitamin"] == pytest.approx(
        nothing.weights()[0]["vitamin"], abs=0.01
    ), "raw result count is being rewarded even when nothing came of it"


def test_verified_contacts_do_raise_the_weight():
    """Control: the value signal must still respond to real outcomes."""
    barren = _learner(
        [_outcome("vitamin distributor Germany", results=50) for _ in range(10)]
    )
    productive = _learner(
        [
            _outcome("vitamin distributor Germany", results=50, verified=8)
            for _ in range(10)
        ]
    )
    assert productive.weights()[0]["vitamin"] > barren.weights()[0]["vitamin"]


# ── 5. Recency ─────────────────────────────────────────────────────────────


def test_recent_evidence_outweighs_stale_evidence():
    """Outcomes arrive newest-first. A vertical that worked last winter is not
    evidence about this week."""
    # Newest 5 searches are barren; 200 older ones were productive.
    good = _outcome("vitamin distributor Germany", results=6, verified=1)
    barren = _outcome("vitamin distributor Germany")

    # Productive history, barren lately.
    faded = _learner([barren] * 60 + [good] * 200)
    # Identical totals, but the productive searches are the recent ones.
    fresh = _learner([good] * 200 + [barren] * 60)

    assert fresh.weights()[0]["vitamin"] > faded.weights()[0]["vitamin"]


# ── 6. The avoid list has to actually do something ─────────────────────────


def test_a_proven_empty_term_is_excluded_not_merely_reported():
    """avoid_terms() was read only by the dashboard and the strategist summary.
    Nothing stopped the generator searching those terms, while the UI said the
    agent had backed off."""
    lea = _learner(
        [_outcome("nutraceutical distributor France") for _ in range(5)],
        min_searches_to_avoid=3,
    )
    assert "nutraceutical" in lea.avoid_terms()

    never_reprobe = random.Random(0)
    never_reprobe.random = lambda: 1.0  # always above the re-probe threshold
    excluded = lea.excluded_terms(rng=never_reprobe)
    assert "nutraceutical" in excluded

    gen = QueryGenerator(seeds=SEEDS, countries=COUNTRIES)
    queries = gen.next_batch(count=40, avoid_terms=excluded)

    assert queries, "the generator produced nothing at all"
    assert not any("nutraceutical" in q for q in queries), (
        "an excluded term was still searched"
    )
    assert any("vitamin" in q for q in queries), "healthy terms were dropped too"


def test_the_exclusion_is_released_sometimes():
    """Without a re-probe the skip is a ratchet: a term that is never searched
    can never be observed recovering."""
    lea = _learner(
        [_outcome("nutraceutical distributor France") for _ in range(5)],
        min_searches_to_avoid=3,
    )
    always_reprobe = random.Random(0)
    always_reprobe.random = lambda: 0.0  # always below the threshold

    assert lea.excluded_terms(rng=always_reprobe) == set()


def test_a_term_below_the_threshold_is_not_avoided():
    """Guard: two empty searches is not yet proof."""
    lea = _learner(
        [_outcome("nutraceutical distributor France") for _ in range(2)],
        min_searches_to_avoid=3,
    )
    assert lea.avoid_terms() == []


# ── 7. Shape the rest of the system depends on ─────────────────────────────


def test_snapshot_keeps_the_shape_the_dashboard_renders():
    lea = _learner(
        [_outcome("vitamin distributor Germany", results=9, verified=4, responded=1)]
    )
    snap = lea.snapshot()

    assert set(snap) == {"top_verticals", "top_countries", "avoid"}
    row = snap["top_verticals"][0]
    for field in ("name", "weight", "searches", "verified", "responded"):
        assert field in row, f"the dashboard reads {field!r}"
    assert isinstance(row["searches"], int), "counts are rendered, not decayed floats"


def test_a_database_failure_leaves_discovery_unbiased_not_broken():
    class Broken:
        def get_search_outcomes(self, limit=500):
            import sqlite3

            raise sqlite3.OperationalError("database is locked")

    lea = PerformanceLearner(Broken(), SEEDS, COUNTRIES, cache_ttl=0)
    seed_w, country_w = lea.weights()

    assert set(seed_w) == set(SEEDS)
    assert set(country_w) == set(COUNTRIES)
    assert lea.avoid_terms() == []
