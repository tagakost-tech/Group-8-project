"""The retention job lets other writers in between erasures.

apply_retention erased every expired address inside one transaction and
committed once at the end. Each erasure scans several tables, so a first run
over a large backlog held the database's write lock for minutes: a dashboard
action in the meantime waited out its five-second busy timeout and failed
with "database is locked" (the benchmark at 100x today's data saw four such
failures in one run). Each address is now committed on its own - still
atomic per address - and the job steps aside every HOLD_LOCK_S, because a
commit alone let the job take the lock back before a waiting writer woke.
"""

import sqlite3
import threading
import time

from modules import export, retention
from modules.database import Database


def test_a_dashboard_write_gets_in_between_two_erasures(
    tmp_path, settings_factory, monkeypatch
):
    path = str(tmp_path / "a.db")
    db = Database(path)
    sid = db.log_search("q")
    for i in range(3):
        db.add_contact(sid, f"old{i}@brand.de", organization="Brand")
    db.conn.execute("UPDATE contacts SET timestamp = '2000-01-01 00:00:00'")
    db.conn.commit()

    outcomes = []
    real = export.anonymise_contact_rows

    def erase_then_let_someone_write(db_, email, **kw):
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute(
                "INSERT INTO activity_log (action, details) VALUES ('X', 'dashboard')"
            )
            other.commit()
            outcomes.append("written")
        except sqlite3.OperationalError as e:
            outcomes.append(str(e))
        finally:
            other.close()
        return real(db_, email, **kw)

    monkeypatch.setattr(
        retention, "anonymise_contact_rows", erase_then_let_someone_write
    )
    counts = retention.apply_retention(db, settings_factory(db_path=path))

    assert counts["erased_never_contacted"] == 3
    assert outcomes == ["written"] * 3, outcomes


def test_the_no_reply_erasures_let_writers_in_too(
    tmp_path, settings_factory, monkeypatch
):
    path = str(tmp_path / "a.db")
    db = Database(path)
    sid = db.log_search("q")
    for i in range(2):
        cid = db.add_contact(sid, f"quiet{i}@brand.de", organization="Brand")
        db.update_contact_status(cid, "emailed")
        db.log_email(cid, f"quiet{i}@brand.de", "s", delivery_status="sent")
    db.conn.execute("UPDATE email_log SET timestamp = '2000-01-01 00:00:00'")
    db.conn.commit()

    outcomes = []
    real = export.anonymise_contact_rows

    def erase_then_let_someone_write(db_, email, **kw):
        other = sqlite3.connect(path, timeout=0)
        try:
            other.execute("INSERT INTO activity_log (action) VALUES ('X')")
            other.commit()
            outcomes.append("written")
        except sqlite3.OperationalError as e:
            outcomes.append(str(e))
        finally:
            other.close()
        return real(db_, email, **kw)

    monkeypatch.setattr(
        retention, "anonymise_contact_rows", erase_then_let_someone_write
    )
    counts = retention.apply_retention(db, settings_factory(db_path=path))

    assert counts["erased_no_reply"] == 2
    assert outcomes == ["written"] * 2, outcomes


def test_a_writer_thread_is_never_starved(tmp_path, settings_factory, monkeypatch):
    """The shape of the real problem: a dashboard thread writing while the job
    runs. Committing each address alone still starved it - the job took the
    lock back before the writer's busy handler retried."""
    path = str(tmp_path / "a.db")
    db = Database(path)
    sid = db.log_search("q")
    for i in range(30):
        db.add_contact(sid, f"old{i}@brand.de", organization="Brand")
    db.conn.execute("UPDATE contacts SET timestamp = '2000-01-01 00:00:00'")
    db.conn.commit()

    real = export.anonymise_contact_rows

    def slow_erasure(db_, email, **kw):
        result = real(db_, email, **kw)
        time.sleep(0.05)  # a large table's scans, with the lock held
        return result

    monkeypatch.setattr(retention, "anonymise_contact_rows", slow_erasure)
    monkeypatch.setattr(retention, "HOLD_LOCK_S", 0.2)

    failures, attempts, stop = [], [], threading.Event()

    def dashboard():
        while not stop.is_set():
            conn = sqlite3.connect(path, timeout=1.0)
            try:
                conn.execute("INSERT INTO activity_log (action) VALUES ('X')")
                conn.commit()
            except sqlite3.OperationalError as e:
                failures.append(str(e))
            finally:
                conn.close()
            attempts.append(1)
            time.sleep(0.02)

    thread = threading.Thread(target=dashboard)
    thread.start()
    try:
        retention.apply_retention(db, settings_factory(db_path=path))
    finally:
        stop.set()
        thread.join()

    assert attempts, "the writer never ran"
    assert failures == [], failures
