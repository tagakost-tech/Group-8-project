"""A request's one-month deadline runs from when it arrived (Art. 12(3)).

The register dated every request by when the agent got to it: the scan that
read it, the replay of a copy held while the database was busy, or the day
an operator typed it in. After an inbox outage or a weekend stop, a request
was due up to 14 days after its legal deadline, and doctor's overdue check,
which reads due_at, called it on time. Now the inbox dates a request by the
server's receipt (INTERNALDATE), else by its Date header kept within the
scan's window; a held copy keeps that time; and `dsr add --received` takes
the day a request arrived by hand, refusing a future one.
"""

from datetime import datetime, timedelta

import pytest

import main
from modules import clock, optout_spool
from modules.database import Database
from modules.imap_monitor import ImapMonitor
from tests.test_imap_inbox_scan import FakeIMAP, _raw

ANNA = "anna@brand.it"
REQUEST = "Vi chiedo di cancellare tutti i miei dati personali."


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "r.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


class _ServerIMAP(FakeIMAP):
    """A fake server that reports each message's INTERNALDATE, as a real one
    does when asked for it."""

    def __init__(self, messages, received):
        super().__init__(messages)
        self.received = received

    def fetch(self, msg_id, parts):
        if "INTERNALDATE" not in parts:
            return super().fetch(msg_id, parts)
        idx = int(msg_id) - 1
        stamp = imaplib_date(self.received[idx]).encode()
        return "OK", [(b"1 (INTERNALDATE " + stamp + b" RFC822 {1})", self.messages[idx])]


def imaplib_date(when):
    import imaplib

    return imaplib.Time2Internaldate(when.timestamp())


def _request(date_header="Mon, 1 Jun 2026 10:00:00 +0200"):
    raw = _raw(ANNA, "Re: hello", REQUEST)
    return raw.replace(b"Date: Mon, 1 Jun 2026 10:00:00 +0200", f"Date: {date_header}".encode())


def _only_request(db):
    (row,) = db.list_dsrs()
    return row


def _scan(db, fake):
    monitor = ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw")
    monitor._connect = lambda: fake
    monitor.run_check()


def test_an_emailed_request_is_dated_by_the_servers_receipt(db):
    arrived = (clock.now() - timedelta(hours=20)).replace(microsecond=0)
    _scan(db, _ServerIMAP([_request()], [arrived]))

    row = _only_request(db)
    assert row["received_at"] == arrived.isoformat()
    assert row["due_at"] == clock.add_months(arrived, 1).isoformat()


def test_without_a_server_date_the_date_header_counts(db):
    arrived = (clock.now() - timedelta(hours=5)).replace(microsecond=0)
    header = arrived.strftime("%a, %d %b %Y %H:%M:%S %z")
    _scan(db, FakeIMAP([_request(header)]))

    assert _only_request(db)["received_at"] == arrived.isoformat()


def test_a_date_header_with_an_accented_timezone_comment_is_read(db):
    arrived = (clock.now() - timedelta(hours=5)).replace(microsecond=0)
    header = arrived.strftime("%a, %d %b %Y %H:%M:%S %z") + " (Mitteleuropäische Zeit)"
    _scan(db, FakeIMAP([_request(header)]))

    assert _only_request(db)["received_at"] == arrived.isoformat()


def test_a_date_header_older_than_the_scan_window_is_brought_into_it(db):
    _scan(db, FakeIMAP([_request("Mon, 1 Jun 2020 10:00:00 +0200")]))

    received = datetime.fromisoformat(_only_request(db)["received_at"])
    assert clock.now() - received < timedelta(days=2)


def test_a_date_header_in_the_future_is_brought_back_to_the_scan(db):
    future = clock.now() + timedelta(days=30)
    _scan(db, FakeIMAP([_request(future.strftime("%a, %d %b %Y %H:%M:%S %z"))]))

    received = datetime.fromisoformat(_only_request(db)["received_at"])
    assert received <= clock.now()


def test_the_inbox_dates_no_message_after_its_scan():
    future = clock.now() + timedelta(days=30)
    window_start = clock.now() - timedelta(days=1)

    received = ImapMonitor._received_at(
        None, future.strftime("%a, %d %b %Y %H:%M:%S %z"), window_start
    )

    assert datetime.fromisoformat(received) <= clock.now()


def test_the_register_dates_no_request_in_the_future(db):
    future = clock.now() + timedelta(days=30)

    db.add_dsr(ANNA, "access", "manual", received_at=future.isoformat())

    assert datetime.fromisoformat(_only_request(db)["received_at"]) <= clock.now()


def test_a_request_held_while_the_database_was_busy_keeps_the_time_it_arrived(db):
    arrived = (clock.now() - timedelta(days=3)).replace(microsecond=0)
    optout_spool.spool_optout(
        db.db_path,
        ANNA,
        "Data request (erasure) received by email",
        company="if_emailed",
        request="erasure",
        received_at=arrived.isoformat(),
    )

    optout_spool.replay_optout_spool(db)

    row = _only_request(db)
    assert datetime.fromisoformat(row["received_at"]) == arrived
    assert row["due_at"] == clock.add_months(arrived, 1).isoformat()


def test_a_held_request_without_its_arrival_keeps_the_time_it_was_held(db):
    optout_spool.spool_optout(
        db.db_path, ANNA, "Data request (access) received by email", request="access"
    )
    held = clock.now()

    optout_spool.replay_optout_spool(db)

    received = datetime.fromisoformat(_only_request(db)["received_at"])
    assert abs(received - held) < timedelta(seconds=5)


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def test_dsr_add_takes_the_day_a_request_arrived(s, db, monkeypatch, capsys):
    day = (clock.now() - timedelta(days=40)).date()

    _cli(s, monkeypatch, capsys, "dsr", "add", ANNA, "--kind", "access", "--received", day.isoformat())

    row = _only_request(db)
    received = datetime.fromisoformat(row["received_at"]).astimezone(
        clock.get_timezone()
    )
    assert received.date() == day
    assert row["due_at"] == clock.add_months(received, 1).isoformat()


def test_dsr_add_refuses_a_day_still_to_come(s, db, monkeypatch, capsys):
    tomorrow = (clock.now() + timedelta(days=1)).date().isoformat()

    with pytest.raises(SystemExit):
        _cli(s, monkeypatch, capsys, "dsr", "add", ANNA, "--kind", "access", "--received", tomorrow)

    assert db.list_dsrs() == []
