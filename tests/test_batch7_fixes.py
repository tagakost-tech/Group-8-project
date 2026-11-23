"""Regression tests for the Batch 7 time and configuration defects.

The clock has the widest blast radius in the codebase: a wrong timezone shifts
the send window, the daily cap rollover and every follow-up simultaneously, and
it did so with no log line at all.
"""

import pathlib
import sqlite3
from datetime import timezone

import pytest

from modules import clock
from modules.analytics import Analytics

# ── 1. The timezone fallback was silent ──────────────────────────────────────
# Windows ships no IANA database, so zoneinfo needs the `tzdata` package - which
# was missing from requirements.txt. On a clean Windows install
# ZoneInfo("Europe/Rome") raised, the exception was swallowed, and the whole app
# ran on UTC. In summer that sends mail at 21:00 Rome (the window is 8-19),
# rolls the daily cap at 02:00 instead of midnight, and fires every follow-up
# two hours late.


def test_an_unresolvable_timezone_is_reported(caplog):
    with caplog.at_level("ERROR"):
        tz = clock._resolve("Not/AZone")
    assert tz is timezone.utc, "must still fall back rather than crash"
    assert caplog.records, "the fallback was silent - no operator would ever know"
    assert "UTC" in caplog.text


def test_the_fallback_is_queryable(caplog):
    """A startup check needs to be able to ask, not just read the log."""
    with caplog.at_level("ERROR"):
        clock.set_timezone("Not/AZone")
    assert clock.timezone_is_fallback() is True

    clock.set_timezone("Europe/Rome")
    assert clock.timezone_is_fallback() is False


def test_a_valid_timezone_resolves_without_complaint(caplog):
    """Guard: the happy path must stay quiet."""
    with caplog.at_level("ERROR"):
        tz = clock._resolve("Europe/Rome")
    assert tz is not timezone.utc
    assert not caplog.records


def test_tzdata_is_declared_as_a_dependency():
    """The actual root cause. Without it a clean Windows install runs on UTC."""
    req = (
        pathlib.Path(__file__).resolve().parent.parent / "requirements.txt"
    ).read_text(encoding="utf-8")
    assert "tzdata" in req, (
        "zoneinfo has no timezone database on Windows without tzdata, so the "
        "app silently falls back to UTC"
    )


def test_rome_is_actually_resolvable_here():
    """If this fails, tzdata is genuinely missing from the environment."""
    assert clock._resolve("Europe/Rome") is not timezone.utc


# ── 2. opened_at was written in three incompatible formats ───────────────────
# database.mark_email_opened used SQLite datetime('now') (UTC) while every other
# timestamp used clock.db_now() (app-local), and analytics wrote an ISO string
# with a +00:00 offset into the same column. An email could appear OPENED TWO
# HOURS BEFORE IT WAS SENT, and the offset form breaks SQLite's datetime().


def test_open_timestamps_match_the_send_timestamp_format(db):
    sid = db.log_search("q", source="t")
    cid = db.add_contact(sid, "fmt@brand.de")
    log_id = db.log_email(cid, "fmt@brand.de", "s")

    analytics = Analytics(db)
    tracking_id = analytics.generate_tracking_id(log_id)
    db.update_email_tracking(log_id, tracking_id)
    analytics.record_open(tracking_id)
    db.mark_email_opened(tracking_id)

    raw = sqlite3.connect(db.db_path)
    raw.row_factory = sqlite3.Row
    row = raw.execute(
        "SELECT timestamp, opened_at FROM email_log WHERE id = ?", (log_id,)
    ).fetchone()
    tracked = raw.execute(
        "SELECT opened_at FROM open_tracking WHERE tracking_id = ?", (tracking_id,)
    ).fetchone()
    raw.close()

    values = [row["timestamp"], row["opened_at"], tracked["opened_at"]]
    assert all(v for v in values), f"a timestamp was not written: {values}"
    assert len({len(str(v)) for v in values}) == 1, (
        f"three different timestamp shapes in one system: {values}"
    )
    assert "+00:00" not in str(tracked["opened_at"]), (
        "an offset-bearing string breaks SQLite's datetime() on this column"
    )


def test_an_email_is_never_opened_before_it_was_sent(db):
    sid = db.log_search("q", source="t")
    cid = db.add_contact(sid, "order@brand.de")
    log_id = db.log_email(cid, "order@brand.de", "s")

    analytics = Analytics(db)
    tracking_id = analytics.generate_tracking_id(log_id)
    db.update_email_tracking(log_id, tracking_id)  # links it to email_log
    db.mark_email_opened(tracking_id)

    raw = sqlite3.connect(db.db_path)
    row = raw.execute(
        "SELECT timestamp, opened_at FROM email_log WHERE id = ?", (log_id,)
    ).fetchone()
    raw.close()
    assert row[1] >= row[0], (
        f"opened_at {row[1]} precedes timestamp {row[0]} - mixing UTC and "
        f"app-local in one column"
    )


def test_sqlite_can_parse_the_open_timestamp(db):
    """DATE(opened_at) grouping is what the charts rely on."""
    sid = db.log_search("q", source="t")
    cid = db.add_contact(sid, "parse@brand.de")
    log_id = db.log_email(cid, "parse@brand.de", "s")
    tracking_id = Analytics(db).generate_tracking_id(log_id)
    db.update_email_tracking(log_id, tracking_id)
    db.mark_email_opened(tracking_id)

    raw = sqlite3.connect(db.db_path)
    parsed = raw.execute(
        "SELECT DATE(opened_at) FROM email_log WHERE id = ?", (log_id,)
    ).fetchone()[0]
    raw.close()
    assert parsed is not None, "SQLite could not parse opened_at"


# ── 3. Config accepted settings that silently stop the agent sending ─────────
# send_window_end_after_start was the only field with this guard; these are the
# rest of the same failure shape - accepted happily, then the agent runs forever
# and sends nothing.


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"warmup_max_daily": 0}, "allowance 0 - never sends"),
        ({"warmup_max_daily": -5}, "negative allowance"),
        ({"warmup_increment": -3}, "ramp decreases to zero"),
        ({"max_bounce_rate": 0.0}, "pauses permanently on the first bounce"),
        ({"max_bounce_rate": 99.0}, "breaker can never trip"),
        ({"max_bounce_rate": 1.0}, "an operator's 1%: trips only on more bounces"),
        ({"send_window_days": "7"}, "weekday() is 0-6, so 7 matches nothing"),
        ({"send_window_days": "99"}, "out of range"),
        ({"send_window_days": "Mon,Tue"}, "no digits - silently fell back"),
        ({"send_window_days": ""}, "no days at all"),
    ],
)
def test_dangerous_settings_are_rejected(settings_factory, overrides, reason):
    with pytest.raises(Exception, match=r"(?s).*"):
        settings_factory(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"send_window_days": "0,1,2"},
        {"send_window_days": "6"},
        {"max_bounce_rate": 0.05},
        {"warmup_max_daily": 1},
        {"warmup_increment": 1},
        # 0 pins the allowance at warmup_start_daily: a constant rate with no
        # ramp. Existing tests rely on it to hold the cap flat, and it is a
        # legitimate operator choice - only a negative value is broken.
        {"warmup_increment": 0},
    ],
)
def test_valid_settings_are_still_accepted(settings_factory, overrides):
    """Guard: the new validators must not reject legitimate configuration."""
    assert settings_factory(**overrides) is not None
