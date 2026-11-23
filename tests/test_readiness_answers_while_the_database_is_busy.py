"""/api/readiness answers promptly while another process holds the database.

The dashboard polls it. Its Data-subject requests row opened a Database,
whose start-up writes the schema, so with a second connection holding a write
lock the request waited out the 5 s busy timeout before it answered. The row
only reads: it reads without writing, and says so when even a read must wait.
"""

import sqlite3
import time

import pytest

import main
from modules.database import Database


@pytest.fixture
def app(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "busy.db"))
    client = main.create_dashboard_app(s).test_client()
    Database(s.get_abs_db_path()).add_dsr("late@brand.de", "access", "manual")
    return client, s.get_abs_db_path()


def _timed_requests_row(client):
    start = time.monotonic()
    resp = client.get("/api/readiness")
    took = time.monotonic() - start
    assert resp.status_code == 200
    rows = {c["label"]: c for c in resp.get_json()["privacy"]}
    return took, rows["Data-subject requests"]


def test_a_held_write_lock_does_not_hold_up_the_answer(app):
    client, path = app
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE dsr_requests SET channel = 'reply'")
        took, row = _timed_requests_row(client)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert took < 2, f"readiness took {took:.1f}s behind a write lock"
    assert row["ok"] is True and row["detail"] == "1 open, none overdue", row


def _requests_check(settings, for_page):
    start = time.monotonic()
    (check,) = main.run_doctor_checks(
        settings, network=False, only=["Data-subject requests"], for_page=for_page
    )
    return time.monotonic() - start, check


def test_a_database_no_one_else_may_read_is_said_to_be_busy(app, settings_factory):
    """With the database held exclusively not even a read can go; the page's
    row says so at once, and the terminal's doctor still waits its 5 s."""
    _client, path = app
    settings = settings_factory(db_path=path)
    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE dsr_requests SET channel = 'reply'")
        holder.execute("COMMIT")
        took, check = _requests_check(settings, for_page=True)
        waited, terminal = _requests_check(settings, for_page=False)
    finally:
        holder.close()
    assert took < 2, f"the row took {took:.1f}s behind an exclusive lock"
    assert check.ok is None
    assert check.detail == "could not be checked - the database is busy"
    assert waited >= 4, f"the terminal's doctor gave up after {waited:.1f}s"
    assert terminal.detail == "could not be checked - the database is busy"


def _held_exclusively(path):
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA locking_mode=EXCLUSIVE")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE dsr_requests SET channel = 'reply'")
    holder.execute("COMMIT")
    return holder


def test_the_page_answers_promptly_under_an_exclusive_lock(app):
    """The Erasures finished row waited out its own reads, about 14 s in all,
    behind an exclusive lock; the page's row now says it could not look."""
    client, path = app
    holder = _held_exclusively(path)
    try:
        start = time.monotonic()
        resp = client.get("/api/readiness")
        took = time.monotonic() - start
    finally:
        holder.close()
    assert took < 3, f"readiness took {took:.1f}s behind an exclusive lock"
    rows = {c["label"]: c for c in resp.get_json()["privacy"]}
    row = rows["Erasures finished"]
    assert row["ok"] is None
    assert row["detail"] == "could not be checked - the database is busy", row


def test_the_terminal_doctor_still_waits_for_the_erasure_check(app, settings_factory):
    _client, path = app
    settings = settings_factory(db_path=path)
    holder = _held_exclusively(path)
    try:
        start = time.monotonic()
        (check,) = main.run_doctor_checks(
            settings, network=False, only=["Erasures finished"], for_page=False
        )
        waited = time.monotonic() - start
    finally:
        holder.close()
    assert waited >= 4, f"the terminal's doctor gave up after {waited:.1f}s"
    assert check.ok is None
    assert "could not check for half-way erasures" in check.detail
