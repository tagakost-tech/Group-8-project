"""
Heuristic search-query generator (zero API cost).

Autonomous discovery previously depended on an AI model to invent company
names, and when the model was off or not answering the continuous loop found
nothing new to do.

This generator produces SUPPLECO-relevant discovery queries deterministically by
combining target-vertical seeds x role modifiers x European countries. The
existing (free) DuckDuckGo search then turns those queries into real leads. No
external API, no cost.

The first seeds are SUPPLECO's own words for its buyers, from the keyword list it
sent on 10 November 2026. The other half of that list describes what SUPPLECO
itself is (contract manufacturing, CDMO, conto terzi...), and a search for those
words finds SUPPLECO's competitors, so it is never searched.
"""

import random

from modules.client_profile import PROFILE

# The client's own words for the companies it wants as customers, per language
# and in the order the client wrote them (client/<client>.json). Each already
# names a company type, so it is searched as written plus a country, never with
# a modifier.
BUYER_SEEDS = PROFILE.buyer_seeds

# A phrase in one language finds pages in that language: searching an Italian
# phrase with "Germany" spends a query on results it cannot have. A language
# not listed is searched in every country.
PHRASE_COUNTRIES = PROFILE.phrase_countries

# The client's buyer phrases first, then the market categories it sells into.
VERTICAL_SEEDS = [*PROFILE.buyer_phrases, *PROFILE.vertical_seeds]

_PHRASE_LANGUAGE = {
    phrase: lang for lang, phrases in BUYER_SEEDS.items() for phrase in phrases
}

# What kind of company we want them to be: potential CLIENTS, never words that
# describe the client itself, which would surface its competitors instead.
MODIFIERS = PROFILE.modifiers

# Words that, inside a search, find a supplier like the client rather than a
# buyer. None is a built-in seed; a query suggested by the AI strategist, which
# is shown the client's value proposition, is dropped if it carries one.
SUPPLIER_WORDS_RE = PROFILE.supplier_words_re


def names_a_supplier(query: str) -> bool:
    """True if a search query would find the client's competitors, not its buyers."""
    return SUPPLIER_WORDS_RE.search(query or "") is not None


# European countries to localise the search.
EUROPEAN_COUNTRIES = [
    "Germany",
    "France",
    "Italy",
    "Spain",
    "United Kingdom",
    "Netherlands",
    "Poland",
    "Belgium",
    "Sweden",
    "Switzerland",
    "Austria",
    "Denmark",
    "Portugal",
    "Ireland",
    "Czech Republic",
    "Greece",
    "Finland",
    "Norway",
    "Romania",
    "Hungary",
]


class QueryGenerator:
    """Builds European, SUPPLECO-vertical search queries without any API."""

    def __init__(self, seeds=None, modifiers=None, countries=None):
        self.seeds = [s for s in (seeds or VERTICAL_SEEDS) if s]
        self.modifiers = [m for m in (modifiers or MODIFIERS) if m]
        self.countries = [c for c in (countries or EUROPEAN_COUNTRIES) if c]

    def _queries(self):
        """Every (query, seed, country) this generator can search.

        One of SUPPLECO's own phrases is searched as written plus a country,
        because it already says what kind of company it is: a modifier would
        make "nutraceutical brand brands Germany". Every other seed is a
        category, which the modifier turns into a kind of company.

        A query is listed once. "nutraceutical" + "company" spells SUPPLECO's
        own "nutraceutical company", and a query listed twice could be picked
        twice for one batch, once as SUPPLECO's word and once as a category.
        """
        seen = set()
        for seed in self.seeds:
            lang = _PHRASE_LANGUAGE.get(seed)
            if lang is not None:
                where = PHRASE_COUNTRIES.get(lang, self.countries)
                built = [(f"{seed} {c}", c) for c in self.countries if c in where]
            else:
                built = [
                    (f"{seed} {mod} {c}", c)
                    for mod in self.modifiers
                    for c in self.countries
                ]
            for q, country in built:
                if q not in seen:
                    seen.add(q)
                    yield q, seed, country

    def all_variations(self) -> list:
        """Every query: SUPPLECO's phrases x country, categories x modifier x country."""
        return [q for q, _seed, _country in self._queries()]

    def phrase_queries(self) -> list:
        """The queries built from SUPPLECO's own phrases, which go first while
        they have never been searched."""
        return [q for q, seed, _country in self._queries() if seed in _PHRASE_LANGUAGE]

    def next_batch(
        self,
        count: int = 5,
        exclude=None,
        seed_weights=None,
        country_weights=None,
        avoid_terms=None,
        searched_before=None,
    ) -> list:
        """Return up to ``count`` fresh queries.

        ``exclude`` skips already-searched queries so the loop keeps exploring.
        When ``seed_weights`` / ``country_weights`` are supplied (by the learning
        loop), selection is biased toward higher-weighted verticals and
        countries. When everything is excluded it falls back to re-searching.

        ``avoid_terms`` drops any query built from a seed or country the
        learner has proven empty. Down-weighting alone was not enough: a term
        at the minimum weight is still sampled regularly, and the dashboard was
        already telling the operator those terms had been dropped.

        SUPPLECO's own words for its buyers are searched before the categories.
        Putting them first in the seed list would not do it: the sample is
        weighted, and they make up about 8% of the queries, so about 8% of
        searches would have used them.

        They go first only until searched: ``searched_before`` holds the
        queries searched at any time, which ``exclude`` (the recent searches)
        cannot tell apart from new ones. Judged by ``exclude`` alone, a phrase
        returned to the front as soon as it left the window, ahead of
        categories never searched once. Once searched, a phrase is sampled by
        weight with the categories.
        """
        n = max(0, count)
        if n == 0:
            return []
        exclude = set(exclude or [])
        before = set(searched_before or [])
        avoid = set(avoid_terms or [])
        candidates = []
        for q, seed, country in self._queries():
            if seed in avoid or country in avoid or q in exclude:
                continue
            sw = (seed_weights or {}).get(seed, 1.0)
            cw = (country_weights or {}).get(country, 1.0)
            candidates.append((q, seed, max(0.01, sw * cw)))

        if not candidates:  # everything excluded -> allow re-search
            return self._pick([(q, "", 1.0) for q in self.all_variations()], n)

        first = [
            c for c in candidates if c[1] in _PHRASE_LANGUAGE and c[0] not in before
        ]
        firsts = {c[0] for c in first}
        rest = [c for c in candidates if c[0] not in firsts]
        chosen = self._pick(first, n)
        chosen += self._pick(rest, n - len(chosen))
        return chosen

    @staticmethod
    def _pick(candidates, n: int) -> list:
        """Up to ``n`` distinct queries from (query, seed, weight) candidates,
        sampled by weight."""
        if n <= 0 or not candidates:
            return []
        queries = [c[0] for c in candidates]
        # Weighted sample without replacement (over-sample, then dedupe).
        chosen, seen = [], set()
        for q in random.choices(
            queries,
            weights=[c[2] for c in candidates],
            k=min(len(queries), max(n * 4, n)),
        ):
            if q not in seen:
                seen.add(q)
                chosen.append(q)
                if len(chosen) >= n:
                    break
        if len(chosen) < n:  # top up if the sample didn't yield enough uniques
            for q in queries:
                if q not in seen:
                    seen.add(q)
                    chosen.append(q)
                    if len(chosen) >= n:
                        break
        return chosen[:n]
