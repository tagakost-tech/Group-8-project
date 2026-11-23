"""The warm-up ramp climbs forever; warmup_max_daily is what stops it.

get_daily_allowance is start + (day-1)*increment, which is unbounded - on a
5/+3 ramp it reaches 38/day by day 12 and 155/day by day 51. warmup_max_daily
is the operator's statement of how much this mailbox may ever send in a day,
and min() is the only thing enforcing it.

Drop that min() and nothing raises, nothing logs, and the dashboard's
get_warmup_status still shows the capped number because it computes its own
min() separately - so the UI reports 10/day while the sender is issuing 38.
SUPPLECO has one sending domain. Quietly sending several times the configured
volume is how it gets onto a blocklist, and after that nothing SUPPLECO sends
reaches anyone, cold or otherwise.
"""

from modules.warmup import WarmupScheduler

RAMP = {"warmup_start_daily": 5, "warmup_increment": 3, "warmup_max_daily": 10}


def _with_sending_history(db, days: int) -> WarmupScheduler:
    """A scheduler for a mailbox that has already been sending for `days` days.

    The warm-up day number is the count of distinct dates in daily_send_counts,
    so past days are seeded directly - there is no other way to age a mailbox
    without waiting for the calendar.
    """
    scheduler = WarmupScheduler(db, warmup_enabled=True, **RAMP)
    with db._transaction() as conn:
        for day in range(1, days + 1):
            conn.execute(
                "INSERT OR IGNORE INTO daily_send_counts (date, count, allowance) "
                "VALUES (?, 0, 0)",
                (f"2020-01-{day:02d}",),
            )
    return scheduler


def test_a_long_running_mailbox_is_still_held_to_its_configured_ceiling(db):
    """Day 12 of a 5/+3 ramp wants 38 sends. The operator said 10."""
    scheduler = _with_sending_history(db, days=11)

    allowance = scheduler.get_daily_allowance("")
    assert allowance == RAMP["warmup_max_daily"], (
        f"the ramp escaped its ceiling: allowed {allowance}/day against a "
        f"configured maximum of {RAMP['warmup_max_daily']}"
    )
    assert scheduler.get_remaining_today("") == RAMP["warmup_max_daily"]


def test_the_ceiling_actually_stops_the_sender(db):
    """The allowance only matters through can_send, which is what the send
    loop asks before every message."""
    scheduler = _with_sending_history(db, days=11)

    for _ in range(RAMP["warmup_max_daily"]):
        assert scheduler.can_send("") is True
        scheduler.record_send("")

    assert scheduler.can_send("") is False, (
        f"the mailbox sent its full {RAMP['warmup_max_daily']}/day ceiling and "
        f"is still willing to send more"
    )
    assert scheduler.get_remaining_today("") == 0


def test_the_ramp_is_not_simply_pinned_to_the_ceiling(db):
    """The control. A ceiling that also flattens the early ramp would pass the
    tests above while destroying the warm-up itself - a brand-new mailbox
    firing 10 messages on day one is the reputation damage warm-up exists to
    avoid. Below the ceiling the ramp must still be the ramp."""
    day_one = WarmupScheduler(db, warmup_enabled=True, **RAMP)
    assert day_one.get_daily_allowance("") == RAMP["warmup_start_daily"] == 5

    day_two = _with_sending_history(db, days=1)
    assert day_two.get_daily_allowance("") == 8  # 5 + 1*3, under the ceiling of 10
    assert day_two.can_send("") is True
