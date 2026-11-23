"""The dashboard's "Recent Errors" panel keeps an error after a busy hour.

/api/errors read the newest 200 activity rows of every kind and kept the
errors among them. A campaign writes several rows per contact, so one error -
a paused blocklist guard, an inbox that could not be read - left the panel
within the hour, and the panel then said "No recent errors". A broken
component looked like a quiet one.

Now the database is asked for the error rows themselves: status 'error',
newest first, from the last 14 days, up to the limit asked for.
"""

from datetime import timedelta

import pytest

import main
from modules import clock
from modules.database import Database


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "e.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


def _panel(s, query=""):
    return main.create_dashboard_app(s).test_client().get(f"/api/errors{query}")


def _aged(db, action, days):
    """An error row written that many days ago."""
    db.log_activity(action, "an old failure", status="error")
    stamp = (clock.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    db.conn.execute(
        "UPDATE activity_log SET timestamp = ? WHERE action = ?", (stamp, action)
    )
    db.conn.commit()


def test_one_error_is_still_shown_after_300_ordinary_rows(s, db):
    db.log_activity("IMAP_ERROR", "Inbox check failed for me@corp.it", status="error")
    for i in range(300):
        db.log_activity("EMAIL_SENT", f"contact {i}", status="success")

    rows = _panel(s).get_json()

    assert [r["action"] for r in rows] == ["IMAP_ERROR"]
    assert rows[0]["details"] == "Inbox check failed for me@corp.it"
    assert rows[0]["status"] == "error"


def test_only_errors_are_shown_and_the_newest_comes_first(s, db):
    _aged(db, "OLDER_ERROR", 2)
    db.log_activity("A_WARNING", "not an error", status="warning")
    db.log_activity("NEWER_ERROR", "the latest failure", status="error")
    db.log_activity("AN_INFO", "not an error", status="info")

    assert [r["action"] for r in _panel(s).get_json()] == ["NEWER_ERROR", "OLDER_ERROR"]


def test_two_errors_in_the_same_second_keep_the_order_they_were_written(s, db):
    for n in range(3):
        db.log_activity(f"ERROR_{n}", "same second", status="error")
    db.conn.execute("UPDATE activity_log SET timestamp = '2026-06-15 12:00:00'")
    db.conn.commit()

    rows = db.get_recent_errors(days=10_000)

    assert [r["action"] for r in rows] == ["ERROR_2", "ERROR_1", "ERROR_0"]


def test_an_error_older_than_14_days_is_no_longer_recent(s, db):
    _aged(db, "LAST_MONTH", 15)
    _aged(db, "LAST_WEEK", 13)

    assert [r["action"] for r in _panel(s).get_json()] == ["LAST_WEEK"]


def test_the_limit_asked_for_is_kept_and_bounded(s, db):
    for n in range(25):
        db.log_activity(f"ERROR_{n:02d}", "one of many", status="error")

    assert len(_panel(s).get_json()) == 20
    assert [r["action"] for r in _panel(s, "?limit=2").get_json()] == [
        "ERROR_24",
        "ERROR_23",
    ]
    assert len(_panel(s, "?limit=-1").get_json()) == 20
