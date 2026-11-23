"""Every data-subject request is registered, with its one-month deadline.

A request was a log line at best: nothing listed the open ones, nothing said
when each was due (Art. 12(3): without undue delay, at the latest within one
month of receipt), and nothing recorded the answer. Now each request found
in the inbox or entered by hand gets a register row with a due date one
calendar month after receipt, and `main.py dsr` lists, shows, closes and
erases by request id. The list masks addresses; `dsr show` prints the full
address to the operator only, so they can act on it. An erasure needs a
typed confirmation, and afterwards the register keeps only the token.
"""

import sqlite3
from datetime import datetime

import pytest

import main
from modules import clock
from modules.database import Database, erasure_token
from modules.imap_monitor import ImapMonitor

EMAIL = "anna.rossi@brandco.de"


@pytest.mark.parametrize(
    "start,expected",
    [
        ("2026-10-27", "2026-11-27"),
        ("2026-01-31", "2026-02-28"),
        ("2028-01-31", "2028-02-29"),
        ("2026-12-15", "2027-01-15"),
    ],
)
def test_a_calendar_month_later(start, expected):
    day = datetime.fromisoformat(start)
    assert clock.add_months(day, 1).date().isoformat() == expected


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "r.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def test_a_request_found_in_the_inbox_is_registered_with_its_deadline(db):
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    monitor.process_requests([{"email": EMAIL, "request_kind": "access"}])

    (row,) = db.list_dsrs()
    assert row["email"] == EMAIL and row["kind"] == "access"
    assert row["channel"] == "reply" and row["status"] == "open"
    # Compared as instants, the month added in the app's zone: re-adding it in
    # the fixed offset parsed back failed whenever the month crossed a clock
    # change (1 Mar 2027, 5 Oct 2027).
    received = datetime.fromisoformat(row["received_at"]).astimezone(
        clock.get_timezone()
    )
    assert row["due_at"] == clock.add_months(received, 1).isoformat()


def test_the_list_masks_addresses_and_flags_late_requests(s, db, monkeypatch, capsys):
    late = db.add_dsr(EMAIL, "erasure", "manual")
    db.conn.execute(
        "UPDATE dsr_requests SET due_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (late,),
    )
    db.conn.commit()

    out = _cli(s, monkeypatch, capsys, "dsr", "list")

    assert EMAIL not in out
    assert "a***@brandco.de" in out
    assert "OVERDUE" in out


def test_show_prints_the_full_address_to_the_operator(s, db, monkeypatch, capsys):
    rid = db.add_dsr(EMAIL, "access", "manual")
    assert EMAIL in _cli(s, monkeypatch, capsys, "dsr", "show", str(rid))


def test_a_request_can_be_added_by_hand_and_closed_with_its_outcome(
    s, db, monkeypatch, capsys
):
    _cli(s, monkeypatch, capsys, "dsr", "add", EMAIL, "--kind", "access")
    (row,) = db.list_dsrs()
    _cli(
        s,
        monkeypatch,
        capsys,
        "dsr",
        "close",
        str(row["id"]),
        "--outcome",
        "answered by letter",
    )

    closed = db.get_dsr(row["id"])
    assert closed["status"] == "closed" and closed["outcome"] == "answered by letter"
    assert closed["channel"] == "manual"


def test_erase_needs_a_typed_confirmation(s, db, monkeypatch, capsys):
    rid = db.add_dsr(EMAIL, "erasure", "reply")
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")

    _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert db.get_dsr(rid)["status"] == "open"
    assert db.get_dsr(rid)["email"] == EMAIL


def test_erase_by_id_erases_closes_and_keeps_only_the_token(s, db, monkeypatch, capsys):
    sid = db.log_search("q", source="test")
    db.add_contact(sid, EMAIL, name="Anna Rossi")
    rid = db.add_dsr(EMAIL, "erasure", "reply")
    monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    row = db.get_dsr(rid)
    assert row["status"] == "closed" and row["outcome"] == "erased"
    assert row["email"] == erasure_token(EMAIL)
    assert EMAIL not in out


def test_any_erasure_tokenises_the_register(db):
    from modules.export import DataExporter

    db.add_dsr(EMAIL, "access", "reply")
    DataExporter(db).gdpr_erase(EMAIL)

    raw = sqlite3.connect(db.db_path)
    assert raw.execute("SELECT email FROM dsr_requests").fetchall() == [
        (erasure_token(EMAIL),)
    ]
    raw.close()


def test_retention_keeps_requests_only_as_the_token_and_cleans_the_outcome(db):
    """Retention anonymises through the routine erasure uses, so the register
    cannot keep an address the contact table no longer holds. The outcome is
    the operator's free text, and may quote the address or name the person."""
    from modules.export import anonymise_contact_rows

    sid = db.log_search("q", source="test")
    db.add_contact(sid, EMAIL, name="Anna Rossi", organization="Brandco")
    rid = db.add_dsr(EMAIL, "access", "reply")
    db.close_dsr(rid, f"Answered Anna Rossi at {EMAIL}")

    anonymise_contact_rows(db, EMAIL, discard_reason="retention")
    db.conn.commit()

    row = db.get_dsr(rid)
    assert row["email"] == erasure_token(EMAIL)
    assert EMAIL not in row["outcome"]
    assert erasure_token(EMAIL) in row["outcome"]
    assert "Anna Rossi" not in row["outcome"]
    assert row["outcome"].startswith("Answered")


def test_the_access_answer_includes_their_requests(db):
    from modules.export import records_held_about

    rid = db.add_dsr(EMAIL, "access", "reply")
    held = records_held_about(db, EMAIL)
    assert [r["id"] for r in held["dsr_requests"]] == [rid]


def test_a_request_that_meets_a_locked_database_is_registered_later(db, monkeypatch):
    """It was held as an opt-out only, and its register entry - the
    one-month deadline - was lost. Found by the review of the counsel sheet."""
    from modules import optout_spool

    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    request = {"email": EMAIL, "subject": "Re", "body": "x", "request_kind": "erasure"}
    with monkeypatch.context() as m:

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        m.setattr(db, "record_opt_out", locked)
        monitor.process_requests([request])
    assert db.list_dsrs() == []
    assert optout_spool.pending_addresses(db.db_path) == {EMAIL}

    optout_spool.replay_optout_spool(db)

    [row] = db.list_dsrs()
    assert (row["email"], row["kind"], row["channel"]) == (EMAIL, "erasure", "reply")
    assert db.is_suppressed(EMAIL)
