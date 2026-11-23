"""An erased address leaves no bytes behind in the database files.

Erasure rewrote every row - but SQLite does not wipe what it frees. The old
values stayed in the database file's free space, and in the write-ahead log
(WAL) beside it until those pages happened to be reused: anyone reading the
files themselves (a copy, a backup, a stolen laptop) could find the address.
Every connection now runs with secure_delete, which zeroes freed content,
and erasure and retention end with a TRUNCATE checkpoint, which copies the
log into the database and empties it. If another connection holds the log
open, that is recorded, and the next checkpoint finishes the job.
"""

import pathlib
import sqlite3

import pytest

from modules.database import Database
from modules.export import DataExporter
from modules.retention import apply_retention

EMAIL = "anna.rossi@brandco.de"


def _file_bytes(db):
    data = b""
    for suffix in ("", "-wal"):
        path = pathlib.Path(db.db_path + suffix)
        if path.exists():
            data += path.read_bytes()
    return data


def _with_history(db):
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, EMAIL, name="Anna Rossi", organization="Brandco")
    db.log_email(
        cid, EMAIL, "Hello", body_preview="Hi Anna, ...", delivery_status="sent"
    )
    db.log_activity("EMAIL_SENT", f"Sent to <{EMAIL}>")
    return cid


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "b.db"))


def test_every_connection_zeroes_what_it_frees(db):
    assert db.conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    with db._transaction() as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_an_erasure_leaves_no_trace_in_the_files(db):
    _with_history(db)
    assert EMAIL.encode() in _file_bytes(db)  # the test can see it before

    DataExporter(db).gdpr_erase(EMAIL)

    assert EMAIL.encode() not in _file_bytes(db)
    assert db.get_state("wal_truncate_pending") == "0"


def test_retention_leaves_no_trace_in_the_files(db, settings_factory):
    cid = _with_history(db)
    db.update_contact_status(cid, "emailed")
    db.conn.execute(
        "UPDATE email_log SET timestamp = datetime('now','localtime','-400 days')"
    )
    db.conn.execute(
        "UPDATE contacts SET timestamp = datetime('now','localtime','-500 days')"
    )
    db.conn.commit()

    apply_retention(db, settings_factory(db_path=db.db_path))

    assert EMAIL.encode() not in _file_bytes(db)


def test_a_log_held_open_is_recorded_and_finished_next_time(db):
    _with_history(db)
    reader = sqlite3.connect(db.db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM contacts").fetchone()

    DataExporter(db).gdpr_erase(EMAIL)

    assert db.get_state("wal_truncate_pending") == "1"
    reader.rollback()
    reader.close()

    assert db.truncate_wal() is True
    assert db.get_state("wal_truncate_pending") == "0"
    assert EMAIL.encode() not in _file_bytes(db)
