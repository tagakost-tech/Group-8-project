"""
Performance learning loop for the B2B Outreach Agent.

Turns historical search/email outcomes into a bias for future discovery:
verticals and countries that produced verified contacts (and replies) get
up-weighted; those that repeatedly came back empty get down-weighted and
eventually skipped.

Deterministic and free (no LLM). The QueryGenerator consumes the numeric
weights; the Strategist consumes the human-readable summary.

This is a multi-armed bandit, and it used to be scored as a plain ratio -
(verified + 5*responded) / searches - with no notion of how much evidence sat
behind each number and no upper bound. Three consequences, all measured on the
real weight function before this was rewritten:

  * ONE search with one lucky reply scored 8.30, while a hundred searches with
    twenty replies scored 3.10. A fluke outranked a proven performer 2.7:1.
  * A single jackpot search reached 30.30. Because the QueryGenerator
    multiplies the seed weight by the country weight, that pair became 918x
    more likely to be picked than an untried one - not a bias but a lock-in,
    after which the agent stopped exploring at all.
  * Untried arms sat at exactly 1.0, so once anything won they were never
    sampled again and could never prove themselves.

The fixes below are the standard answers: shrink small samples toward a prior
so evidence has to be earned, clamp the output so compounding cannot explode,
and give under-sampled arms an optimism bonus so exploration continues.
"""

import logging
import math
import random
import re
import sqlite3
import time

logger = logging.getLogger(__name__)

# A person's judgement of a find. An approval says the search found the right
# kind of company, worth more than a find that merely verified; a rejection
# says it found the wrong kind, and takes back about as much as a verified
# find gives. The observed value never goes below zero.
APPROVAL_VALUE = 2.0
REJECTION_PENALTY = 1.0

# A reply is worth far more than a raw find - it is the only signal that the
# targeting was actually right rather than merely productive.
REPLY_VALUE = 5.0

# Shrinkage. An arm's observed value is blended with PRIOR_VALUE as though we
# had already watched it for PRIOR_STRENGTH searches, so a single lucky result
# moves the estimate a little rather than a lot. Higher = more sceptical.
PRIOR_STRENGTH = 10.0
PRIOR_VALUE = 0.5  # ~ the value per search of an unremarkable arm

# The value-to-weight mapping is deliberately sub-linear. Linear mapping
# saturated the clamp almost immediately - a decent arm and an outstanding
# one both pinned at MAX_WEIGHT, so the CLAMP was doing the discriminating
# and the score was not. A square root keeps the whole useful range spread
# out and makes the weight insensitive to the absolute scale of `value`,
# which varies with whether verification is even switched on.
WEIGHT_EXPONENT = 0.5

# Balanced stance: the best arm may be 4x a neutral one and the worst 0.25x.
# Because seed and country weights MULTIPLY, this bounds a pair at 16x rather
# than the 918x the unclamped version reached.
MIN_WEIGHT = 0.25
MAX_WEIGHT = 4.0

# Optimism under uncertainty: decays as evidence accumulates, so a never-tried
# arm is slightly preferred over a mediocre well-tested one and the map keeps
# getting explored.
EXPLORE_BONUS = 0.6

# Outcomes are read newest-first. Each step back multiplies by this, so a
# result roughly RECENCY_HALF_LIFE searches ago counts half as much as today's.
# Markets move; a vertical that worked last winter is not evidence about now.
RECENCY_HALF_LIFE = 120.0

# A term proven empty is skipped - but released with this probability each
# cycle, so the skip cannot become a permanent ratchet. Without it a term that
# was dead for three searches would never be searched again, which means it can
# never be observed recovering.
REPROBE_PROBABILITY = 0.1


class PerformanceLearner:
    """Learns which verticals/countries actually produce results."""

    def __init__(
        self,
        db,
        seeds,
        countries,
        min_searches_to_avoid: int = 3,
        cache_ttl: float = 30.0,
    ):
        self.db = db
        self.seeds = [s for s in seeds if s]
        self.countries = [c for c in countries if c]
        self.min_searches_to_avoid = min_searches_to_avoid
        # Pre-compile a word-boundary pattern per term, once. A bare substring
        # test credited every "multivitamin" search to "vitamin" as well,
        # double-counting one seed's outcomes into another's evidence - the
        # same defect that made the relevance filter match "news" inside
        # "newsletter".
        self._seed_res = [(s, self._pattern(s)) for s in self.seeds]
        self._country_res = [(c, self._pattern(c)) for c in self.countries]
        # Short cache so weights()/focus_summary()/snapshot() called within one
        # discovery cycle share a single DB scan instead of recomputing 3x.
        self._cache_ttl = cache_ttl
        self._cache = None
        self._cache_at = 0.0

    @staticmethod
    def _pattern(term: str) -> re.Pattern:
        r"""Whole-word matcher for a term or multi-word phrase.

        \b on both ends, with internal whitespace allowed to be any run of
        space, so "food supplement" still matches "food  supplement".
        """
        return re.compile(
            r"\b" + r"\s+".join(re.escape(w) for w in term.split()) + r"\b",
            re.IGNORECASE,
        )

    def _aggregate(self) -> dict:
        """Aggregate the per-search outcome signal per vertical seed and country
        (cached briefly so repeated calls in one cycle reuse a single DB scan).

        Every counter is recency-weighted, so `searches` is an effective sample
        size rather than a raw count and old evidence fades instead of
        outvoting new evidence forever.
        """
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_at) < self._cache_ttl:
            return self._cache

        def blank():
            return {
                "searches": 0.0,
                "results": 0.0,
                "verified": 0.0,
                "responded": 0.0,
                "approved": 0.0,
                "rejected": 0.0,
                "raw_searches": 0,
                "raw_results": 0,
            }

        stats = {
            "seeds": {s: blank() for s in self.seeds},
            "countries": {c: blank() for c in self.countries},
        }
        try:
            outcomes = self.db.get_search_outcomes(500)
        except sqlite3.Error as e:
            # Zeroed stats mean "no history", which reads as a neutral weight -
            # so a read failure quietly disables the learning loop. Keep it
            # visible rather than letting discovery silently go unbiased.
            logger.warning(f"Learning: could not read outcomes: {e}")
            return stats

        decay_per_step = 0.5 ** (1.0 / max(1.0, RECENCY_HALF_LIFE))
        for i, o in enumerate(outcomes):  # index 0 is the most recent search
            recency = decay_per_step**i
            q = o.get("search_query") or ""
            results = o.get("results_count") or 0
            verified = o.get("verified") or 0
            responded = o.get("responded") or 0
            approved = o.get("approved") or 0
            rejected = o.get("rejected") or 0
            for group, pairs in (
                ("seeds", self._seed_res),
                ("countries", self._country_res),
            ):
                for name, pattern in pairs:
                    if pattern.search(q):
                        d = stats[group][name]
                        d["searches"] += recency
                        d["results"] += results * recency
                        d["verified"] += verified * recency
                        d["responded"] += responded * recency
                        d["approved"] += approved * recency
                        d["rejected"] += rejected * recency
                        # Undecayed counts, for the avoid rule and the
                        # dashboard - "searched 3 times, found nothing" is a
                        # statement about history, not about recent history.
                        d["raw_searches"] += 1
                        d["raw_results"] += results

        self._cache = stats
        self._cache_at = now
        return stats

    @staticmethod
    def _weight(d: dict) -> float:
        """Map a component's aggregate outcome to a bounded selection weight.

        Shrunk toward PRIOR_VALUE by PRIOR_STRENGTH pseudo-searches, so an arm
        has to earn its confidence: one search with one reply now lands near
        the prior instead of at 8.3, and a hundred consistent searches move the
        estimate much further than a single lucky one.

        Note the value counts VERIFIED contacts and REPLIES only. Raw
        results_count is deliberately not rewarded: a search returning fifty
        contacts that never verify and never answer has cost scraping and
        verification budget to produce nothing, and the old formula scored that
        three times better than finding nothing at all.
        """
        searches = d["searches"]
        # A rejection may take the value below the prior: a search whose
        # finds people keep turning down is worse than one never tried.
        # Floored after the prior, never before it - floored before, a
        # rejection counted for nothing wherever nothing had verified yet.
        observed = (
            d["verified"]
            + REPLY_VALUE * d["responded"]
            + APPROVAL_VALUE * d.get("approved", 0.0)
            - REJECTION_PENALTY * d.get("rejected", 0.0)
        )

        # Posterior mean under a simple conjugate-style prior.
        value = max(
            0.0,
            (observed + PRIOR_STRENGTH * PRIOR_VALUE) / (searches + PRIOR_STRENGTH),
        )

        # Relative to the prior, compressed. An arm with no evidence lands
        # at 1.0 here and is then lifted by the exploration bonus below, so
        # untried arms sit slightly ABOVE mediocre well-tested ones - which
        # is the whole mechanism that keeps the vocabulary being explored.
        weight = (value / PRIOR_VALUE) ** WEIGHT_EXPONENT

        # Optimism under uncertainty - large when evidence is thin, negligible
        # once an arm is well sampled.
        weight += EXPLORE_BONUS / math.sqrt(1.0 + searches)

        return round(min(MAX_WEIGHT, max(MIN_WEIGHT, weight)), 3)

    def weights(self):
        """Return (seed_weights, country_weights) for the QueryGenerator."""
        stats = self._aggregate()
        return (
            {s: self._weight(d) for s, d in stats["seeds"].items()},
            {c: self._weight(d) for c, d in stats["countries"].items()},
        )

    def avoid_terms(self) -> list:
        """Components searched repeatedly that produced literally nothing.

        Judged on the UNDECAYED counts, and on results_count rather than
        verified/responded: "we ran this three times and got zero rows back" is
        unambiguous and does not depend on whether email verification happens
        to be switched on.
        """
        stats = self._aggregate()
        avoid = []
        for group in ("seeds", "countries"):
            for name, d in stats[group].items():
                if (
                    d["raw_searches"] >= self.min_searches_to_avoid
                    and d["raw_results"] == 0
                ):
                    avoid.append(name)
        return avoid

    def excluded_terms(self, rng=None) -> set:
        """Terms to actually skip this cycle.

        avoid_terms() used to be reported and nothing more - the dashboard said
        the agent had backed off while the generator went on searching them.
        This is the list the generator honours.

        Each avoided term is released with REPROBE_PROBABILITY so the skip
        cannot ratchet shut: a term that is never searched can never be
        observed recovering, and a market that comes back would stay invisible
        forever.
        """
        rng = rng or random
        keep_out = set()
        for term in self.avoid_terms():
            if rng.random() >= REPROBE_PROBABILITY:
                keep_out.add(term)
            else:
                logger.info("Learning: re-probing previously empty term %r", term)
        return keep_out

    def focus_summary(self, top_n: int = 4) -> str:
        """Short human-readable hint for the Strategist."""
        stats = self._aggregate()

        def good(group):
            scored = [
                (n, self._weight(d))
                for n, d in stats[group].items()
                if d["searches"] > 0 and self._weight(d) > 1.0
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            return [n for n, _ in scored[:top_n]]

        parts = []
        winners = good("seeds") + good("countries")
        if winners:
            parts.append("working well: " + ", ".join(winners))
        avoid = self.avoid_terms()
        if avoid:
            parts.append("avoid (no results): " + ", ".join(avoid[:top_n]))
        return " | ".join(parts)

    def snapshot(self, top_n: int = 8) -> dict:
        """Structured view for the dashboard /api/learning endpoint."""
        stats = self._aggregate()

        def top(group):
            rows = []
            for n, d in stats[group].items():
                if d["raw_searches"] <= 0:
                    continue
                rows.append(
                    {
                        "name": n,
                        "weight": self._weight(d),
                        # Whole numbers for display; the decayed floats are an
                        # implementation detail nobody wants to read.
                        "searches": d["raw_searches"],
                        "results": d["raw_results"],
                        "verified": round(d["verified"]),
                        "responded": round(d["responded"]),
                        "approved": round(d["approved"]),
                        "rejected": round(d["rejected"]),
                    }
                )
            rows.sort(key=lambda r: r["weight"], reverse=True)
            return rows[:top_n]

        return {
            "top_verticals": top("seeds"),
            "top_countries": top("countries"),
            "avoid": self.avoid_terms(),
        }
