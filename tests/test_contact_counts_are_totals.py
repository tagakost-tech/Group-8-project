"""The Contacts chips must count the TABLE, not the page.

/api/contacts returns one page - the top 200 by lead score. The status chips
were built by tallying the rendered rows, so they were counts of that page
wearing the clothes of a total: the chip read "pending 41" while the Overview
card read "pending 437", for the same quantity, on the same screen.

Worse than the wrong number: clicking the chip filtered client-side, so it
could only ever offer those 41. The other 396 pending contacts were
unreachable from the UI, with nothing on screen suggesting they existed.
"""

import main


def _seed(db, n_pending, n_emailed):
    """Contacts whose page-order deliberately disagrees with the whole table.

    Pending contacts get a LOW lead score, emailed ones high - so the first
    page (ordered by score) is dominated by 'emailed' and any tally of the
    rendered rows badly under-counts 'pending'. That is the real-world shape:
    the 437 pending were mostly newly discovered and unscored.
    """
    sid = db.log_search("q", source="test")
    for i in range(n_emailed):
        cid = db.add_contact(sid, f"emailed{i}@brand.de", lead_score=90)
        db.update_contact_status(cid, "emailed")
    for i in range(n_pending):
        db.add_contact(sid, f"pending{i}@brand.de", lead_score=1)


def test_status_counts_cover_the_whole_table_not_a_page(db):
    _seed(db, n_pending=300, n_emailed=250)

    counts = db.get_stats()["contact_status_counts"]

    assert counts["pending"] == 300
    assert counts["emailed"] == 250


def test_the_first_page_alone_would_have_lied(db):
    """Proves the fixture actually reproduces the bug: tallying one page gives
    a materially different answer, so a test that passes here is testing
    something real."""
    _seed(db, n_pending=300, n_emailed=250)

    page = db.get_all_contacts(limit=200)
    page_pending = sum(1 for c in page if c["status"] == "pending")
    true_pending = db.get_stats()["contact_status_counts"]["pending"]

    assert page_pending < true_pending, (
        "the fixture does not reproduce the page/total divergence"
    )
    assert page_pending == 0  # the whole first page is high-scoring 'emailed'


def test_filtering_by_status_reaches_past_the_first_page(db):
    """Client-side filtering could only sieve rows already fetched. Server-side
    it must reach contacts the page never contained."""
    _seed(db, n_pending=300, n_emailed=250)

    rows = db.get_all_contacts(limit=200, status="pending")

    assert len(rows) == 200, "the filter did not reach past the first page"
    assert all(r["status"] == "pending" for r in rows)


def test_no_filter_still_returns_the_ranked_page(db):
    """Guard: the default behaviour must not change."""
    _seed(db, n_pending=300, n_emailed=250)

    rows = db.get_all_contacts(limit=200)

    assert len(rows) == 200
    assert all(r["status"] == "emailed" for r in rows), "page is no longer score-ranked"


def test_the_api_exposes_the_counts(tmp_path, settings_factory):
    from modules.database import Database

    s = settings_factory(db_path=str(tmp_path / "c.db"))
    app = main.create_dashboard_app(s)
    _seed(Database(str(tmp_path / "c.db")), n_pending=300, n_emailed=250)

    stats = app.test_client().get("/api/stats").get_json()

    assert stats["contact_status_counts"]["pending"] == 300


def test_the_api_filters_by_status(tmp_path, settings_factory):
    from modules.database import Database

    s = settings_factory(db_path=str(tmp_path / "d.db"))
    app = main.create_dashboard_app(s)
    _seed(Database(str(tmp_path / "d.db")), n_pending=300, n_emailed=250)
    client = app.test_client()

    rows = client.get("/api/contacts?limit=50&status=pending").get_json()

    assert len(rows) == 50
    assert all(r["status"] == "pending" for r in rows)


def test_an_unknown_status_returns_nothing_rather_than_everything(
    tmp_path, settings_factory
):
    """A filter that silently falls back to "all" is worse than an empty
    result: it looks like the filter worked."""
    from modules.database import Database

    s = settings_factory(db_path=str(tmp_path / "e.db"))
    app = main.create_dashboard_app(s)
    _seed(Database(str(tmp_path / "e.db")), n_pending=10, n_emailed=10)

    rows = app.test_client().get("/api/contacts?status=nonsense").get_json()

    assert rows == []


def test_a_blank_status_means_no_filter(tmp_path, settings_factory):
    """Guard: ?status= (empty) must not be read as "match the empty string"."""
    from modules.database import Database

    s = settings_factory(db_path=str(tmp_path / "f.db"))
    app = main.create_dashboard_app(s)
    _seed(Database(str(tmp_path / "f.db")), n_pending=10, n_emailed=10)

    rows = app.test_client().get("/api/contacts?status=").get_json()

    assert len(rows) == 20


# ── The counter and the backlog must agree about who is pending ────────────
#
# get_pending_contacts (what the sender drains) and count_pending_contacts
# (what the dashboard displays) carry the same suppression clause, written out
# twice. Only the first had a mutation pinning it, so the counter's copy could
# have been broken with the whole suite still green - this codebase's signature
# defect, applied to the coverage rather than to the code.


def test_the_pending_count_excludes_suppressed_contacts(db):
    """Unsubscribe ten people and the dashboard must stop counting them as
    work waiting to be done. Otherwise the operator sees a backlog that will
    never drain, and the suppression looks broken while it is working."""
    sid = db.log_search("q", source="test")
    for i in range(10):
        db.add_contact(sid, f"stays{i}@brand.de")
    for i in range(10):
        db.add_contact(sid, f"optout{i}@brand.de")
        db.add_to_suppression(f"optout{i}@brand.de", "unsubscribe")

    assert db.count_pending_contacts() == 10


def test_the_counter_agrees_with_the_backlog_it_describes(db):
    """The property that matters: the number on screen is the number of
    contacts the sender would actually work through. Two copies of one clause
    drift, and the direction of that drift is a dashboard describing a queue
    that does not exist."""
    sid = db.log_search("q", source="test")
    for i in range(8):
        db.add_contact(sid, f"live{i}@brand.de")
    for i in range(5):
        db.add_contact(sid, f"gone{i}@brand.de")
        db.add_to_suppression(f"gone{i}@brand.de", "bounce")

    assert db.count_pending_contacts() == len(db.get_pending_contacts())


def test_the_count_still_counts_when_nobody_is_suppressed(db):
    """Control: the fix must not work by returning zero."""
    sid = db.log_search("q", source="test")
    for i in range(6):
        db.add_contact(sid, f"p{i}@brand.de")

    assert db.count_pending_contacts() == 6
