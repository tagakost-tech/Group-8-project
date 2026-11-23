"""The warm-up figure the operator reads must be the one the sender obeys.

get_daily_allowance (what the sender checks before every send) and
get_warmup_status (what the dashboard displays) each computed
start + (day-1)*increment, capped at max_daily, from their own copy of the
formula. Two copies of one rule drift: change the ramp in one place and the
dashboard reports a number the sender is not honouring.

That failure is silent and points the wrong way. The operator sees a healthy
"10 of 10 today" while the sender issues far more, and the first real signal is
SUPPLECO's single sending domain landing on a blocklist.
"""

import pytest

from modules.database import Database
from modules.warmup import WarmupScheduler


@pytest.fixture
def warmup(tmp_path):
    return WarmupScheduler(
        db=Database(str(tmp_path / "w.db")),
        warmup_enabled=True,
        warmup_start_daily=5,
        warmup_increment=3,
        warmup_max_daily=10,
    )


def test_the_dashboard_figure_is_the_senders_figure(warmup):
    """The property that matters: one number, from one place."""
    assert warmup.get_warmup_status()["daily_allowance"] == warmup.get_daily_allowance()


def test_the_displayed_allowance_respects_the_ceiling(warmup):
    """warmup_max_daily is the operator's hard cap. On a 5/+3 ramp the raw
    formula reaches 38/day by day 12; only the cap keeps it at 10."""
    status = warmup.get_warmup_status()
    assert status["daily_allowance"] <= 10


def test_remaining_never_exceeds_what_the_sender_would_allow(warmup):
    """The dashboard's "remaining" drives the operator's expectation of how
    much will go out. It must not promise more than the sender will send."""
    warmup.record_send()
    status = warmup.get_warmup_status()
    assert status["remaining"] == warmup.get_remaining_today()


def test_they_still_agree_once_the_ramp_is_capped(warmup):
    """Both agreeing at day 1 proves little - they agree there by arithmetic
    accident. Agreement has to hold where the cap is actually doing work."""
    import sqlite3

    raw = sqlite3.connect(warmup.db.db_path)
    raw.execute(
        "INSERT INTO daily_send_counts (date, count, allowance) VALUES (?, 0, 0)",
        ("1999-01-01",),  # an old row makes today a later warm-up day
    )
    raw.commit()
    raw.close()

    status = warmup.get_warmup_status()
    assert status["daily_allowance"] == warmup.get_daily_allowance()
    assert status["daily_allowance"] <= 10
