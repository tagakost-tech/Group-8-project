"""The chart's days must be the same days the rows were stamped in.

get_chart_data built its day boundaries from datetime.now(timezone.utc) while
every row it buckets is written by clock.db_now() in the app timezone. Rome is
one or two hours ahead of UTC, so anything logged just after local midnight had
a local date that was not yet in the window - and silently vanished from the
chart until UTC caught up.

This is pinned behaviourally, not by grepping the source. The existing guard
test (test_no_module_reaches_for_the_system_clock_directly) searches modules/
for `datetime.now(`, which pins the SPELLING: the identical defect written as
clock.now().astimezone(timezone.utc) would sail past it. A rule about how code
is written is not a test of what it does.

The offset is exaggerated to +14 so the two calendars disagree reliably rather
than only in a one-hour window around local midnight.
"""

import pytest

from modules import clock
from modules.analytics import Analytics
from modules.database import Database


@pytest.fixture
def far_east_clock():
    """Move the app clock as far ahead of UTC as real timezones go."""
    original = clock.get_timezone()
    try:
        clock.set_timezone("Pacific/Kiritimati")  # UTC+14, no DST
        yield
    finally:
        clock.set_timezone(str(original))


def _log_one_email(db):
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "buyer@brand.de")
    db.log_email(cid, "buyer@brand.de", "subject", delivery_status="sent")


def test_an_email_logged_today_appears_on_the_chart(tmp_path, far_east_clock):
    """The whole point. The row is stamped in app-local time; the chart's last
    day must be that same local day, or the row falls outside the window."""
    db = Database(str(tmp_path / "c.db"))
    _log_one_email(db)

    chart = Analytics(db).get_chart_data(days=30)
    per_day = chart["emails_per_day"]

    assert sum(per_day["data"]) == 1, (
        "an email logged today is missing from the chart - the day boundaries "
        "were built in a different timezone from the timestamps they bucket"
    )


def test_the_chart_window_ends_on_the_app_clocks_today(tmp_path, far_east_clock):
    """Stated directly, so a future refactor cannot satisfy the test above by
    widening the window rather than fixing the clock."""
    db = Database(str(tmp_path / "d.db"))
    chart = Analytics(db).get_chart_data(days=7)

    assert chart["emails_per_day"]["labels"][-1] == clock.today_str()


def test_it_still_works_when_the_two_clocks_agree(tmp_path):
    """Control, on the default timezone: the fix must not depend on the offset
    being large, and must not break where UTC and local happen to match."""
    db = Database(str(tmp_path / "e.db"))
    _log_one_email(db)

    chart = Analytics(db).get_chart_data(days=30)

    assert sum(chart["emails_per_day"]["data"]) == 1
    assert chart["emails_per_day"]["labels"][-1] == clock.today_str()
