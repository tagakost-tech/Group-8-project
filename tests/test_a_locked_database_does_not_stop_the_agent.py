"""A database locked for longer than the busy timeout must not stop the agent.

Another writer holding the lock past both 5-second busy timeouts made the
iteration fail and then made the handler's own LOOP_ERROR write fail too:
that second error left run_continuous, the loop stopped, and opt-outs,
bounces and hot-lead alerts went unprocessed until someone restarted it.
The dashboard's agent thread wrote AGENT_ERROR the same way. The settings
and list refresh at the top of each pass ran outside the guard altogether.
"""

import sqlite3
import threading

import main
from agent import OutreachAgent
from modules.database import Database


def _stubbed_agent(settings_factory, tmp_path):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    a.search.search_and_extract = lambda *_a, **_k: []
    a._process_sequences = lambda *_a, **_k: None
    a._maybe_check_imap = lambda *_a, **_k: None
    a._process_retries = lambda: None
    a._maybe_apply_retention = lambda *_a, **_k: None
    passes = []
    a._process_pending_contacts = lambda *_a, **_k: passes.append(1)
    sleeps = []

    def sleep(*_a, **_k):
        sleeps.append(1)
        if len(sleeps) >= 2:
            a.stop()

    a._interruptible_sleep = sleep
    return a, passes


def test_a_lock_that_outlasts_the_error_write_does_not_stop_the_loop(
    settings_factory, tmp_path
):
    a, passes = _stubbed_agent(settings_factory, tmp_path)

    def backlog(*_a, **_k):
        passes.append(1)
        if len(passes) == 1:
            raise sqlite3.OperationalError("database is locked")

    a._process_pending_contacts = backlog
    write = a.db.log_activity

    def locked(event, *args, **kwargs):
        if event == "LOOP_ERROR":
            raise sqlite3.OperationalError("database is locked")
        return write(event, *args, **kwargs)

    a.db.log_activity = locked

    a.run_continuous(dry_run=False, loop_delay=60)

    assert len(passes) == 2


def test_a_failed_refresh_at_the_top_of_a_pass_does_not_stop_the_loop(
    settings_factory, tmp_path
):
    a, passes = _stubbed_agent(settings_factory, tmp_path)
    refreshes = []

    def refresh():
        refreshes.append(1)
        if len(refreshes) == 1:
            raise OSError("the client workbook is open in Excel")

    a.refresh_lists_if_changed = refresh

    a.run_continuous(dry_run=False, loop_delay=60)

    assert len(refreshes) == 2
    assert len(passes) == 1


def test_the_dashboard_agent_logs_when_it_cannot_record_its_error(
    settings_factory, tmp_path, monkeypatch, caplog
):
    uncaught = []
    monkeypatch.setattr(threading, "excepthook", lambda args: uncaught.append(args.exc_value))

    def fail(self, *_a, **_k):
        raise RuntimeError("search failed")

    monkeypatch.setattr(OutreachAgent, "run_single", fail)
    write = Database.log_activity

    def locked(self, event, *args, **kwargs):
        if event == "AGENT_ERROR":
            raise sqlite3.OperationalError("database is locked")
        return write(self, event, *args, **kwargs)

    monkeypatch.setattr(Database, "log_activity", locked)
    client = main.create_dashboard_app(
        settings_factory(db_path=str(tmp_path / "d.db"))
    ).test_client()

    resp = client.post("/api/agent/start", json={"mode": "single"})
    for t in threading.enumerate():
        if "run_agent" in t.name:
            t.join(10)

    assert resp.status_code == 200
    assert uncaught == []
    assert "search failed" in caplog.text
    assert "database is locked" in caplog.text
