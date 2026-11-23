"""A run with no configured query searches with SUPPLECO's own words.

On 10 November 2026 SUPPLECO sent its own search keywords, in Italian and in
English. The agent did not use them: a run with a blank TARGET_QUERIES
searched a supplement vocabulary we wrote ("probiotic distributor Germany").

The list mixes two kinds of phrase. Some describe the companies SUPPLECO wants
as customers ("brand di integratori alimentari", "nutraceutical brand"); the
rest describe what SUPPLECO itself is ("produzione conto terzi", "seeking
CDMO"). A search for the second kind returns SUPPLECO's competitors, so only
the first kind is searched. The split is recorded here, literally, as the
decision.
"""

import sqlite3

from agent import OutreachAgent
from modules.client_profile import VALUE_PROPOSITION_PHRASES
from modules.query_generator import (
    BUYER_SEEDS,
    EUROPEAN_COUNTRIES,
    MODIFIERS,
    VERTICAL_SEEDS,
    QueryGenerator,
    names_a_supplier,
)
from modules.relevance import (
    COMPETITOR_KEYWORDS,
    COMPETITOR_SELF_DESCRIPTION_KEYWORDS,
    RelevanceFilter,
)

IT_BUYER = [
    "azienda nutraceutica",
    "brand di integratori alimentari",
    "proprietario di brand integratori",
    "marchio di integratori",
    "brand owner nutraceutico",
    "azienda di alimenti dietetici",
    "società di nutraceutica",
    "brand wellness",
    "azienda di prodotti per il benessere",
]
EN_BUYER = [
    "nutraceutical brand",
    "supplement brand owner",
    "dietary supplement brand",
    "wellness brand",
    "nutraceutical company",
    "food supplement brand",
    "brand owner nutraceuticals",
    "CPG nutraceutical brand",
]

# Section 1 of SUPPLECO's list, the phrases that describe a supplier.
IT_SUPPLIER = [
    "private label integratori",
    "cerco produttore integratori",
    "cerco terzista integratori",
    "cerco CDMO integratori",
    "cerco private label",
    "cerco in-licensing",
    "cerco prodotti ready to market",
    "cerco Ready o Market",
    "cerco formulazioni già sviluppate",
    "cerco produzione conto terzi",
    "cerco partner produttivo",
    "produzione conto terzi integratori alimentari",
    "contract manufacturing integratori",
    "sviluppo formulazioni integratori",
    "produzione compresse integratori",
    "produzione capsule",
    "produzione stick pack",
    "produzione bustine",
    "confezionamento integratori",
]
EN_SUPPLIER = [
    "private label supplements company",
    "private label manufacturer seeking",
    "looking for contract manufacturer",
    "seeking CDMO",
    "looking for private label manufacturer",
    "seeking third party manufacturer",
    "looking for in-licensing",
    "seeking ready to market products",
    "looking for RTM products",
    "seeking formulation development",
    "looking for manufacturing partner",
    "seeking turnkey solutions",
    "contract manufacturing dietary supplements",
    "CDMO nutraceuticals",
    "private label supplements manufacturing",
    "third party manufacturer supplements",
    "contract manufacturer food supplements",
    "white label supplements",
    "turnkey supplements manufacturing",
    "formulation development supplements",
    "tablet manufacturing supplements",
    "capsule manufacturing",
    "stick pack manufacturing",
    "sachet manufacturing",
    "packaging services supplements",
]

# Words that, inside a search, find a supplier rather than a buyer.
SUPPLIER_STEMS = [
    "manufactur",
    "produttore",
    "produzione",
    "terzist",
    "conto terzi",
    "cdmo",
    "private label",
    "white label",
    "third party",
    "third-party",
    "turnkey",
    "in-licensing",
    "ready to market",
    "rtm",
    "oem",
    "odm",
    "formulazion",
    "formulation",
    "confezionamento",
    "packaging",
    "cerco",
    "looking for",
    "seeking",
]


def _phrase_queries(gen=None):
    gen = gen or QueryGenerator()
    phrases = [*IT_BUYER, *EN_BUYER]
    return [
        q for q in gen.all_variations() if any(q.startswith(f"{p} ") for p in phrases)
    ]


# What the decision means: Italian phrases in Italy, English ones everywhere.
SUPPLECO_QUERIES = {
    *(f"{p} Italy" for p in IT_BUYER),
    *(f"{p} {c}" for p in EN_BUYER for c in EUROPEAN_COUNTRIES),
}


# ── The decision ─────────────────────────────────────────────────────────────


def test_supplecos_buyer_words_are_the_built_in_seeds_in_its_own_order():
    assert BUYER_SEEDS == {"it": IT_BUYER, "en": EN_BUYER}
    assert VERTICAL_SEEDS[:17] == [*IT_BUYER, *EN_BUYER]
    assert VERTICAL_SEEDS[17:] == [
        "food supplement",
        "dietary supplement",
        "nutraceutical",
        "vitamin",
        "multivitamin",
        "mineral supplement",
        "probiotic",
        "omega 3 supplement",
        "collagen supplement",
        "protein supplement",
        "sports nutrition",
        "pre-workout supplement",
        "herbal supplement",
        "botanical supplement",
        "wellness supplement",
        "beauty supplement",
        "nutricosmetic",
    ]


def test_no_built_in_search_contains_a_supplier_word():
    """A search for what SUPPLECO is finds SUPPLECO's competitors."""
    forbidden = {
        *(t.lower() for t in IT_SUPPLIER + EN_SUPPLIER),
        *(t.lower() for ts in VALUE_PROPOSITION_PHRASES.values() for t in ts),
        *COMPETITOR_KEYWORDS,
        *COMPETITOR_SELF_DESCRIPTION_KEYWORDS,
        *SUPPLIER_STEMS,
    }
    searched = [*VERTICAL_SEEDS, *MODIFIERS, *QueryGenerator().all_variations()]
    hits = sorted(
        {(s, term) for s in searched for term in forbidden if term in s.lower()}
    )
    assert not hits, f"a built-in search carries a supplier word: {hits[:10]}"


def test_the_supplier_screen_knows_every_supplier_phrase_and_no_built_in_search():
    """The screen the AI strategist's queries pass through reads SUPPLECO's
    supplier side, and none of the searches the agent builds itself."""
    missed = [t for t in IT_SUPPLIER + EN_SUPPLIER if not names_a_supplier(t)]
    assert not missed, f"the screen lets these supplier searches through: {missed}"
    flagged = [q for q in QueryGenerator().all_variations() if names_a_supplier(q)]
    assert not flagged, f"the screen drops these buyer searches: {flagged[:5]}"


def test_no_built_in_search_reads_as_a_competitor():
    gate = RelevanceFilter()
    rivals = [
        q
        for q in QueryGenerator().all_variations()
        if "competitor"
        in gate.assess({"email": "info@acme.it", "organization": q})["reason"]
    ]
    assert not rivals, f"these searches describe a contract manufacturer: {rivals}"


# ── How a phrase is searched ─────────────────────────────────────────────────


def test_an_suppleco_phrase_is_searched_as_written_with_only_a_country():
    """SUPPLECO's phrases already name the company type: a modifier would make
    "nutraceutical brand brands Germany"."""
    for q in _phrase_queries():
        phrase = next(p for p in [*IT_BUYER, *EN_BUYER] if q.startswith(f"{p} "))
        country = q[len(phrase) + 1 :]
        assert country in EUROPEAN_COUNTRIES, f"{q!r} has more than a country"


def test_italian_phrases_are_searched_in_italy_english_ones_everywhere():
    queries = set(_phrase_queries())
    assert {f"{p} Italy" for p in IT_BUYER} == {
        q for q in queries if any(q.startswith(f"{p} ") for p in IT_BUYER)
    }
    for p in EN_BUYER:
        assert {f"{p} {c}" for c in EUROPEAN_COUNTRIES} == {
            q for q in queries if q.startswith(f"{p} ")
        }


def test_the_category_searches_are_unchanged():
    """The categories still make seed x modifier x country. Some spell one of
    SUPPLECO's phrases ("nutraceutical" + "company"); each query is listed once."""
    variations = QueryGenerator().all_variations()
    assert len(variations) == len(set(variations)), "a query is listed twice"
    categories = {
        f"{s} {m} {c}"
        for s in VERTICAL_SEEDS[17:]
        for m in MODIFIERS
        for c in EUROPEAN_COUNTRIES
    }
    assert "food supplement brand Germany" in categories
    assert set(variations) == categories | SUPPLECO_QUERIES


def test_a_phrase_the_learner_dropped_is_not_searched():
    batch = QueryGenerator().next_batch(count=50, avoid_terms={"wellness brand"})
    assert len(batch) == 50
    assert not [q for q in batch if q.startswith("wellness brand ")]


# ── SUPPLECO's words come first ────────────────────────────────────────────────


def test_supplecos_words_are_searched_before_the_categories():
    for _ in range(20):
        batch = QueryGenerator().next_batch(count=5)
        assert len(batch) == 5 and set(batch) <= SUPPLECO_QUERIES, batch


def test_the_categories_follow_once_supplecos_words_are_searched():
    gen = QueryGenerator()
    phrases = SUPPLECO_QUERIES
    almost = set(sorted(phrases)[:-2])
    batch = gen.next_batch(count=6, exclude=almost)
    assert len(batch) == 6
    assert set(batch[:2]) == phrases - almost, "the two left are searched first"
    assert not set(batch[2:]) & phrases

    batch = gen.next_batch(count=7, exclude=phrases)
    assert len(batch) == 7 and not set(batch) & phrases


def test_only_the_phrases_never_searched_go_first():
    almost = set(sorted(SUPPLECO_QUERIES)[:-2])
    batch = QueryGenerator().next_batch(count=2, searched_before=almost)
    assert set(batch) == SUPPLECO_QUERIES - almost


def test_a_phrase_searched_long_ago_does_not_jump_the_categories():
    """The loop excludes only its last 500 searches. A phrase that left that
    window went back to the front, ahead of categories never searched once:
    over 3,000 searches, 845 were repeats of SUPPLECO's phrases."""
    gen = QueryGenerator(
        seeds=["nutraceutical brand", "probiotic"],
        modifiers=["brand"],
        countries=["Germany"],
    )
    picked = {
        gen.next_batch(count=1, searched_before={"nutraceutical brand Germany"})[0]
        for _ in range(30)
    }
    assert "probiotic brand Germany" in picked


def test_phrase_queries_are_the_ones_built_from_supplecos_words():
    assert set(QueryGenerator().phrase_queries()) == SUPPLECO_QUERIES


# ── What a run searches ──────────────────────────────────────────────────────


def _agent(settings_factory, tmp_path, **kw):
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "t.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            **kw,
        )
    )


def _searched_by_a_run(agent):
    searched = []
    agent.search.search_and_extract = lambda query, **_k: searched.append(query) or []
    agent._process_pending_contacts = lambda *_a, **_k: None
    agent._process_sequences = lambda *_a, **_k: None
    agent._maybe_apply_retention = lambda *_a, **_k: None
    agent.run_single(dry_run=True)
    return searched


def test_a_run_with_no_query_searches_supplecos_own_words(settings_factory, tmp_path):
    searched = _searched_by_a_run(_agent(settings_factory, tmp_path, target_queries=""))
    assert searched, "a run with no query searched nothing"
    assert set(searched) <= SUPPLECO_QUERIES, f"a run with no query searched {searched}"


def test_a_run_reads_the_whole_history_not_just_the_last_500(
    settings_factory, tmp_path
):
    """Every phrase searched once, then 500 other searches: the phrases have
    left the exclusion window, and must not all come back first."""
    agent = _agent(settings_factory, tmp_path, target_queries="")
    for q in sorted(SUPPLECO_QUERIES):
        agent.db.log_search(q, status="completed")
    with sqlite3.connect(tmp_path / "t.db") as conn:
        conn.execute("UPDATE search_log SET timestamp = '2000-01-01 00:00:00'")
    for i in range(500):
        agent.db.log_search(f"filler {i}", status="completed")
    searched = _searched_by_a_run(agent)
    assert len(searched) == 5
    assert not set(searched) <= SUPPLECO_QUERIES, searched


def test_target_queries_still_win(settings_factory, tmp_path):
    searched = _searched_by_a_run(
        _agent(settings_factory, tmp_path, target_queries="collagen supplier Austria")
    )
    assert searched == ["collagen supplier Austria"]


def test_the_learner_tracks_supplecos_words(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path, learning_enabled=True)
    assert agent.learner.seeds[:17] == [*IT_BUYER, *EN_BUYER]
