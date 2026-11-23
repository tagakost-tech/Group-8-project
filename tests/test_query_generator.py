"""Tests for the free (no-API) heuristic query generator."""
from modules.query_generator import EUROPEAN_COUNTRIES, QueryGenerator


def test_generates_european_queries():
    batch = QueryGenerator().next_batch(count=5)
    assert len(batch) == 5
    assert all(any(c in q for c in EUROPEAN_COUNTRIES) for q in batch)


def test_excludes_used_queries():
    qg = QueryGenerator(seeds=["supplement"], modifiers=["manufacturer"],
                        countries=["Germany", "France"])
    variations = qg.all_variations()
    assert len(variations) == 2
    batch = qg.next_batch(count=5, exclude={variations[0]})
    assert variations[0] not in batch
    assert variations[1] in batch


def test_count_respected():
    assert len(QueryGenerator().next_batch(count=3)) == 3


def test_falls_back_when_all_excluded():
    qg = QueryGenerator(seeds=["supplement"], modifiers=["manufacturer"],
                        countries=["Germany"])
    only = qg.all_variations()
    # Everything excluded -> still returns something (re-search is allowed).
    assert qg.next_batch(count=5, exclude=set(only)) == only
