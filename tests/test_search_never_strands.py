"""A search row opened as 'in_progress' must always be closed.

get_pending_searches selects only status='pending', so a row left at
'in_progress' is invisible to the loop forever - not retried, not completed,
just an amber line on the dashboard. Every one is a query the operator asked
for that will never run.

This is not theoretical: 47 rows had accumulated that way, and completing them
reproduced the mechanism live - 6 of the 47 stranded again in a single run,
each killed by a RELATIVE redirect URL ("/clev?event=StartpageResultClick&...")
returned by whichever backend ddgs routed to. That raises a bare ValueError
which none of _scrape_page's httpx handlers cover.
"""

import pytest

from modules.search import SearchModule


def _sm(db):
    return SearchModule(db, rate_limit=0.0)


def test_a_failing_scrape_does_not_abort_the_whole_query(db):
    """One unusable URL out of several must cost that URL, not the query."""
    sm = _sm(db)
    sm._ddg_search = lambda q: [
        "/clev?event=StartpageResultClick&sc=abc",  # relative - raises
        "https://good.example/contact",
    ]

    def fake_scrape(url):
        if not url.startswith("http"):
            raise ValueError(f"unknown url type: {url!r}")
        return [
            {
                "email": "info@good.example",
                "name": None,
                "role": None,
                "organization": "Good",
                "source_url": url,
                "qualification_rationale": "test",
            }
        ]

    sm._scrape_page = fake_scrape
    sid = db.log_search("q", source="test", status="in_progress")

    contacts = sm.search_and_extract("q", search_id=sid)

    assert [c["email"] for c in contacts] == ["info@good.example"], (
        "a bad URL took the whole query down with it"
    )


def test_the_row_is_closed_even_when_everything_fails(db):
    """The second-order defect, and the more important one: whatever escapes,
    the row must not be left open."""
    sm = _sm(db)

    def _boom(_query):
        raise RuntimeError("the search backend exploded")

    sm._ddg_search = _boom
    sid = db.log_search("doomed query", source="test", status="in_progress")

    with pytest.raises(RuntimeError):
        sm.search_and_extract("doomed query", search_id=sid)

    row = next(s for s in db.get_all_searches(50) if s["id"] == sid)
    assert row["status"] != "in_progress", (
        "the row was left stranded - get_pending_searches will never see it "
        "again, so this query can never run"
    )


def test_a_stranded_row_is_not_marked_completed(db):
    """It must not read as a successful, unproductive search. The learning
    loop reads results_count, so 'completed with 0 results' teaches it that a
    query which CRASHED was unproductive on its merits, biasing discovery away
    from a perfectly good term."""
    sm = _sm(db)
    sm._ddg_search = lambda q: (_ for _ in ()).throw(RuntimeError("boom"))
    sid = db.log_search("blameless query", source="test", status="in_progress")

    with pytest.raises(RuntimeError):
        sm.search_and_extract("blameless query", search_id=sid)

    row = next(s for s in db.get_all_searches(50) if s["id"] == sid)
    assert row["status"] == "no_results"


def test_the_original_error_still_propagates(db):
    """Guard: closing the row must not swallow the failure. A caller that
    cannot tell a crashed search from an empty one will keep calling it."""
    sm = _sm(db)
    sm._ddg_search = lambda q: (_ for _ in ()).throw(RuntimeError("specific cause"))
    sid = db.log_search("q", source="test", status="in_progress")

    with pytest.raises(RuntimeError, match="specific cause"):
        sm.search_and_extract("q", search_id=sid)


def test_a_successful_search_still_completes(db):
    """Guard: the wrapper must not change the happy path."""
    sm = _sm(db)
    sm._ddg_search = lambda q: ["https://ok.example/"]
    sm._scrape_page = lambda url: [
        {
            "email": "hello@ok.example",
            "name": None,
            "role": None,
            "organization": "OK",
            "source_url": url,
            "qualification_rationale": "test",
        }
    ]
    sid = db.log_search("q", source="test", status="in_progress")

    contacts = sm.search_and_extract("q", search_id=sid)

    assert len(contacts) == 1
    row = next(s for s in db.get_all_searches(50) if s["id"] == sid)
    assert row["status"] == "completed"
    assert row["results_count"] == 1
