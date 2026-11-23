"""Where a run gets its queries from.

There were three interfaces for one concept and they disagreed:

    dashboard "Add Query"  -> queued the search in the DB          (worked)
    CLI `add-query`        -> printed advice to hand-edit .env      (did nothing)
    `run` / `run --dry-run`-> hard-failed on an empty TARGET_QUERIES
    `run --continuous`     -> queued searches, then the generator   (worked)

So the same installation ran fine continuously and refused to run once, which
reads as the agent being broken.
"""

import main
from modules.database import Database


def _agent(tmp_path, settings_factory, **kw):
    from agent import OutreachAgent

    return OutreachAgent(settings_factory(db_path=str(tmp_path / "q.db"), **kw))


def test_a_queued_search_is_picked_up(tmp_path, settings_factory):
    a = _agent(tmp_path, settings_factory, target_queries="")
    sid = a.db.log_search(
        "probiotic distributor Belgium", source="cli", status="pending"
    )

    queries, search_ids = a._queries_for_a_standalone_run()

    assert queries == ["probiotic distributor Belgium"]
    # The queued row must be COMPLETED, not duplicated - without the id mapping
    # each run mints a second row and leaves the first at 'in_progress' forever.
    assert search_ids == {"probiotic distributor Belgium": sid}


def test_a_picked_up_search_is_marked_in_progress(tmp_path, settings_factory):
    a = _agent(tmp_path, settings_factory, target_queries="")
    sid = a.db.log_search("vitamin wholesaler Italy", source="cli", status="pending")

    a._queries_for_a_standalone_run()

    row = next(s for s in a.db.get_all_searches(50) if s["id"] == sid)
    assert row["status"] == "in_progress", (
        "a second concurrent run would pick the same query up again"
    )


def test_it_falls_back_to_heuristic_generation(tmp_path, settings_factory):
    """Nothing configured and nothing queued is not an error - continuous mode
    has always generated its own queries in exactly this situation."""
    a = _agent(tmp_path, settings_factory, target_queries="")
    assert not a.db.get_pending_searches(10)

    queries, search_ids = a._queries_for_a_standalone_run()

    assert queries, "a standalone run found nothing to do and would have refused"
    assert search_ids == {}, "generated queries have no queued row to complete"


def test_queued_searches_win_over_generation(tmp_path, settings_factory):
    """What the operator explicitly asked for comes first."""
    a = _agent(tmp_path, settings_factory, target_queries="")
    a.db.log_search("explicit ask", source="dashboard", status="pending")

    queries, _ = a._queries_for_a_standalone_run()

    assert queries == ["explicit ask"]


def test_configured_queries_still_win(tmp_path, settings_factory):
    """Guard: the fallback must not override TARGET_QUERIES."""
    a = _agent(tmp_path, settings_factory, target_queries="collagen supplier Austria")
    a.db.log_search("something queued", source="dashboard", status="pending")

    assert a.settings.get_target_queries() == ["collagen supplier Austria"]


def test_the_continuous_loop_still_manages_its_own_queries(tmp_path, settings_factory):
    """Guard: run_single called FROM the loop must not start second-guessing
    it - the loop already selected, queued and marked its own searches."""
    a = _agent(tmp_path, settings_factory, target_queries="")
    a.query_generator = None  # so a fallback would produce nothing anyway
    called = []
    a._queries_for_a_standalone_run = lambda: called.append(1) or ([], {})

    a.run_single(queries=None, dry_run=True, _from_continuous=True)

    assert not called, "the standalone fallback fired inside the continuous loop"


def test_nothing_anywhere_is_reported_not_crashed(tmp_path, settings_factory):
    a = _agent(tmp_path, settings_factory, target_queries="")
    a.query_generator = None

    queries, search_ids = a._queries_for_a_standalone_run()

    assert queries == []
    assert search_ids == {}


def test_a_locked_database_does_not_break_query_selection(tmp_path, settings_factory):
    import sqlite3

    a = _agent(tmp_path, settings_factory, target_queries="")

    def _boom(*args, **kw):
        raise sqlite3.OperationalError("database is locked")

    a.db.get_pending_searches = _boom
    queries, _ = a._queries_for_a_standalone_run()

    assert queries, "a DB hiccup should degrade to generation, not to nothing"


# ── The CLI ────────────────────────────────────────────────────────────────


def test_the_cli_actually_queues_the_query(tmp_path, settings_factory):
    """It used to print instructions and exit having changed nothing."""
    s = settings_factory(db_path=str(tmp_path / "cli.db"))

    class Args:
        query_text = "botanical extract distributor Portugal"

    main.cmd_add_query(Args(), s)

    pending = Database(str(tmp_path / "cli.db")).get_pending_searches(10)
    assert [p["search_query"] for p in pending] == [
        "botanical extract distributor Portugal"
    ]
