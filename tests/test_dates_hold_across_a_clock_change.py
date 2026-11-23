"""Date arithmetic holds across a daylight-saving change (Europe/Rome).

Python subtracts and shifts two datetimes that share a tzinfo as wall-clock
times, which ignores a clock change between them. The wait for the send
window took opens - now, so from Saturday evening before the spring change
the agent slept 36 hours instead of 35 and woke at 09:00 on Monday, an hour
after the window opened (at 07:00 after the autumn change). And a request's
deadline was a month added in the fixed offset of the arrival time it was
given, such as the INTERNALDATE an inbox scan passes, so a request received
in winter fell due an hour late in summer time - a day late on the local
calendar when it arrived late in the evening.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from modules import clock
from modules.database import Database
from modules.imap_monitor import ImapMonitor
from tests.test_loop_orchestration import DummyAgent

ROME = ZoneInfo("Europe/Rome")


def _orchestrator(tmp_path, settings_factory):
    settings = settings_factory(
        send_window_enabled=True,
        send_window_start_hour=8,
        send_window_end_hour=19,
        send_window_days="0,1,2,3,4",
    )
    return DummyAgent(Database(str(tmp_path / "w.db")), settings).orchestrator


@pytest.mark.parametrize(
    "saturday_evening,hours",
    [
        (datetime(2027, 3, 27, 20, 0, tzinfo=ROME), 35),  # clocks go forward
        (datetime(2027, 10, 30, 20, 0, tzinfo=ROME), 37),  # clocks go back
        (datetime(2027, 1, 16, 20, 0, tzinfo=ROME), 36),  # no change: control
    ],
)
def test_the_agent_wakes_when_the_window_opens_across_a_clock_change(
    tmp_path, settings_factory, monkeypatch, saturday_evening, hours
):
    orch = _orchestrator(tmp_path, settings_factory)
    monkeypatch.setattr(clock, "now", lambda: saturday_evening)

    wait = orch.seconds_until_send_window()

    assert wait == hours * 3600
    wakes = datetime.fromtimestamp(saturday_evening.timestamp() + wait, ROME)
    assert (wakes.weekday(), wakes.hour, wakes.minute) == (0, 8, 0)


@pytest.mark.parametrize(
    "received,due",
    [
        ("2027-03-10T23:30:00+01:00", "2027-04-10T23:30:00+02:00"),
        ("2027-10-10T23:30:00+02:00", "2027-11-10T23:30:00+01:00"),
        ("2027-01-10T23:30:00+01:00", "2027-02-10T23:30:00+01:00"),  # control
    ],
)
def test_a_request_falls_due_on_the_local_calendar_across_a_clock_change(
    tmp_path, monkeypatch, received, due
):
    monkeypatch.setattr(clock, "_tz", ROME)
    monkeypatch.setattr(clock, "now", lambda: datetime(2028, 1, 1, 12, 0, tzinfo=ROME))
    db = Database(str(tmp_path / "r.db"))

    request_id = db.add_dsr("anna@brand.de", "access", "reply", received_at=received)

    row = db.get_dsr(request_id)
    assert row["due_at"] == due
    assert datetime.fromisoformat(row["received_at"]) == datetime.fromisoformat(received)


def test_inbox_receipt_ordering_holds_across_the_repeated_hour(monkeypatch):
    monkeypatch.setattr(clock, "_tz", ROME)
    window_start = datetime(2027, 10, 30, 0, 0, tzinfo=ROME)

    # Scan in the second 02:30 (01:30 UTC), 45 minutes after a 02:45 CEST
    # message (00:45 UTC): wall-clock min(02:45, 02:30) moved it 45 min ahead.
    second_hour = datetime(2027, 10, 31, 2, 30, tzinfo=ROME, fold=1)
    monkeypatch.setattr(clock, "now", lambda: second_hour)
    assert (
        ImapMonitor._received_at(
            b'1 (INTERNALDATE "31-Oct-2027 02:45:00 +0200" RFC822 {1})',
            "Sun, 31 Oct 2027 02:45:00 +0200",
            window_start,
        )
        == "2027-10-31T02:45:00+02:00"
    )
    assert (
        ImapMonitor._received_at(
            None, "Sun, 31 Oct 2027 02:45:00 +0200", window_start
        )
        == "2027-10-31T02:45:00+02:00"
    )

    # Scan in the first 02:30 (00:30 UTC) with a Date header in the second
    # hour (02:15 CET = 01:15 UTC, 45 min in the future): must clamp to scan.
    first_hour = datetime(2027, 10, 31, 2, 30, tzinfo=ROME, fold=0)
    monkeypatch.setattr(clock, "now", lambda: first_hour)
    assert (
        ImapMonitor._received_at(
            None, "Sun, 31 Oct 2027 02:15:00 +0100", window_start
        )
        == "2027-10-31T02:30:00+02:00"
    )

