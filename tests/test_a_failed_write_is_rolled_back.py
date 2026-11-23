"""A write that fails half-way on the agent's own connection is rolled back.

The agent thread keeps one database connection for the retention job, the
follow-up sequences and the "replied" marks. Its transaction stays open until
someone commits. When a write failed part-way, nothing rolled it back:

- the connection went on holding the write lock, so every other connection -
  the agent's own activity log, the dashboard, an opt-out - waited five
  seconds and gave up with "database is locked", until the next commit;
- that next commit, for anything at all, saved the half that had been
  written. An erasure that stopped after the contact row was rewritten was
  saved as "erased" with the person's address still in the other tables, and
  the retention job never looked at that contact again.

Now each of those writers undoes what it did not commit before the error goes
on, the continuous loop and the dashboard's agent thread do the same for
anything else, and an erasure that was undone is found and done whole by the
next run. An opt-out or a bounce is written on its own connection and stays.
"""

import sqlite3
import threading

import pytest

import main
from agent import OutreachAgent
from modules import clock, export
from modules.database import Database, erasure_token
from modules.export import DataExporter
from modules.imap_monitor import ImapMonitor, watermark_key
from modules.retention import apply_retention
from modules.sequences import SequenceEngine

ANNA = "anna@brand.example"
BEN = "ben@other.example"
STEPS = [{"delay_days": 3, "subject": "s", "template_name": "followup_1"}]


def _write_locked(db) -> bool:
    """Whether another connection is refused the write lock right now."""
    other = sqlite3.connect(db.db_path, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        other.close()


def _left_clean(db) -> bool:
    return not db.conn.in_transaction and not _write_locked(db)


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(
        db_path=str(tmp_path / "w.db"),
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        ai_humanize_emails=False,
    )


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


def _objector(db, email):
    """Someone who opted out, with records in the tables an erasure visits
    after the contact row."""
    cid = db.add_contact(db.log_search("q"), email, name="Anna", organization="Brand")
    db.log_email(cid, email, "Hello", delivery_status="sent")
    db.log_consent(email, "legitimate_interest", "first email")
    db.record_opt_out(email, "unsubscribe link")
    return cid


def _named_in_consent_log(db, email) -> int:
    return db.conn.execute(
        "SELECT COUNT(*) FROM consent_log WHERE LOWER(email) = ?", (email,)
    ).fetchone()[0]


@pytest.fixture
def fail_erasing(monkeypatch):
    """Make the erasure of one address fail after its contact row and its
    emails are rewritten, before the rest. Returns a switch to mend it."""
    real = export._redact
    broken = {"for": None, "with": sqlite3.OperationalError("disk I/O error")}

    def redact(db, table, column, email_lower, token):
        if email_lower == broken["for"]:
            raise broken["with"]
        return real(db, table, column, email_lower, token)

    monkeypatch.setattr(export, "_redact", redact)
    return broken


# ── The retention job ────────────────────────────────────────────────


def test_a_failed_erasure_is_undone_and_done_whole_on_the_next_pass(s, fail_erasing):
    agent = OutreachAgent(s)
    db = agent.db
    cid = _objector(db, ANNA)
    fail_erasing["for"] = ANNA

    agent._maybe_apply_retention(False)

    assert _left_clean(db), "the failed erasure still holds the write lock"
    assert db.get_state("retention:last_run_date") is None, "marked done"
    # An unrelated commit on the same connection saves nothing of it.
    db.conn.commit()
    assert db.get_contact_by_id(cid)["email"] == ANNA
    assert db.get_contact_by_id(cid)["status"] == "unsubscribed"
    assert _named_in_consent_log(db, ANNA) == 1
    assert db.is_suppressed(ANNA), "the opt-out must outlive a failed erasure"

    fail_erasing["for"] = None
    agent._maybe_apply_retention(False)

    assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA)
    assert db.get_contact_by_id(cid)["status"] == "erased"
    assert _named_in_consent_log(db, ANNA) == 0
    assert db.get_state("retention:last_run_date") == clock.today_str()
    assert db.is_suppressed(ANNA)


def _retention_failures(db):
    return [r for r in db.get_recent_errors() if r["action"] == "RETENTION_FAILED"]


def test_a_retention_run_that_keeps_failing_is_shown_among_the_errors(s, fail_erasing):
    """The undone erasure is tried again on every loop. A job that never
    finishes showed only in the log file; the dashboard said nothing."""
    agent = OutreachAgent(s)
    db = agent.db
    _objector(db, ANNA)
    fail_erasing["for"] = ANNA

    agent._maybe_apply_retention(False)
    agent._maybe_apply_retention(False)

    shown = _retention_failures(db)
    assert len(shown) == 1, "an error once a day, not none and not one per loop"
    assert "disk I/O error" in shown[0]["details"]
    assert ANNA not in shown[0]["details"]

    fail_erasing["for"] = None
    agent._maybe_apply_retention(False)
    assert db.get_state("retention:last_run_date") == clock.today_str()
    assert len(_retention_failures(db)) == 1

    # A run that fails after one that worked is a new failure, said again.
    db.set_state("retention:last_run_date", None)
    _objector(db, BEN)
    fail_erasing["for"] = BEN
    agent._maybe_apply_retention(False)
    assert len(_retention_failures(db)) == 2


def test_a_retention_failure_that_cannot_be_recorded_does_not_stop_the_loop(
    s, fail_erasing, monkeypatch, caplog
):
    agent = OutreachAgent(s)
    _objector(agent.db, ANNA)
    fail_erasing["for"] = ANNA
    write = Database.log_activity

    def locked(self, event, *args, **kwargs):
        if event == "RETENTION_FAILED":
            raise sqlite3.OperationalError("database is locked")
        return write(self, event, *args, **kwargs)

    monkeypatch.setattr(Database, "log_activity", locked)

    agent._maybe_apply_retention(False)

    assert "Could not record RETENTION_FAILED" in caplog.text
    monkeypatch.setattr(Database, "log_activity", write)
    agent._maybe_apply_retention(False)
    assert len(_retention_failures(agent.db)) == 1, "not said once it could be"


def test_the_addresses_erased_before_the_failure_stay_erased(s, db, fail_erasing):
    first = _objector(db, ANNA)
    second = _objector(db, BEN)
    fail_erasing["for"] = BEN

    with pytest.raises(sqlite3.OperationalError):
        apply_retention(db, s)

    assert _left_clean(db)
    assert db.get_contact_by_id(first)["status"] == "erased"
    assert db.get_contact_by_id(second)["email"] == BEN
    assert _named_in_consent_log(db, BEN) == 1


def test_an_error_that_is_not_the_databases_is_undone_too(s, db, fail_erasing):
    cid = _objector(db, ANNA)
    fail_erasing["for"] = ANNA
    fail_erasing["with"] = RuntimeError("a bug in the erasure")

    with pytest.raises(RuntimeError):
        apply_retention(db, s)

    assert _left_clean(db)
    db.conn.commit()
    assert db.get_contact_by_id(cid)["email"] == ANNA


def test_a_ctrl_c_in_the_middle_of_an_erasure_is_undone_too(s, db, fail_erasing):
    cid = _objector(db, ANNA)
    fail_erasing["for"] = ANNA
    fail_erasing["with"] = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        apply_retention(db, s)

    assert _left_clean(db)
    db.log_activity("AGENT_STOPPED", "Stopped by user (KeyboardInterrupt)")
    assert db.get_contact_by_id(cid)["email"] == ANNA


@pytest.mark.parametrize("stopped_by", [RuntimeError("a bug"), KeyboardInterrupt()])
def test_an_undo_that_fails_does_not_take_the_place_of_the_error(
    s, db, fail_erasing, monkeypatch, caplog, stopped_by
):
    """The caller acts on what stopped the write. With the undo's own error
    in its place, a bug read as a busy database to be tried again quietly,
    and a Ctrl+C as an error the loop carries on from."""
    _objector(db, ANNA)
    fail_erasing["for"] = ANNA
    fail_erasing["with"] = stopped_by

    def broken(self):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Database, "rollback_thread_conn", broken)

    with pytest.raises(type(stopped_by)):
        apply_retention(db, s)

    assert "Could not undo the failed write" in caplog.text


def test_an_erasure_on_request_that_fails_is_undone_and_stays_suppressed(
    db, fail_erasing
):
    cid = db.add_contact(db.log_search("q"), ANNA, name="Anna", organization="Brand")
    db.log_email(cid, ANNA, "Hello", delivery_status="sent")
    fail_erasing["for"] = ANNA

    with pytest.raises(sqlite3.OperationalError):
        DataExporter(db).gdpr_erase(ANNA)

    assert _left_clean(db)
    db.conn.commit()
    assert db.get_contact_by_id(cid)["email"] == ANNA, "saved half-erased"
    # Suppressed first, in its own transaction: that part stands.
    assert db.is_suppressed(ANNA)

    fail_erasing["for"] = None
    DataExporter(db).gdpr_erase(ANNA)
    assert db.get_contact_by_id(cid)["status"] == "erased"


# ── Follow-up sequences ──────────────────────────────────────────────


def test_a_failed_enrolment_leaves_the_database_free(db):
    seq = SequenceEngine(db)
    sequence_id = seq.create_sequence("default", STEPS)

    with pytest.raises(sqlite3.IntegrityError):
        seq.enroll_contact(999_999, sequence_id)  # no such contact

    assert _left_clean(db), "the failed enrolment still holds the write lock"
    db.log_activity("STILL_WRITABLE", "from another connection")


def test_a_sequence_that_fails_half_built_is_not_saved_by_the_next_commit(db):
    seq = SequenceEngine(db)

    with pytest.raises(KeyError):
        seq.create_sequence("half", [STEPS[0], {"subject": "no delay or template"}])

    assert _left_clean(db)
    cid = db.add_contact(db.log_search("q"), ANNA)
    seq.enroll_contact(cid, seq.create_sequence("whole", STEPS))  # commits
    names = [r[0] for r in db.conn.execute("SELECT name FROM sequences")]
    assert names == ["whole"]


@pytest.mark.parametrize("write", ["advance_enrollment", "cancel_enrollment"])
def test_a_failed_step_or_cancellation_leaves_the_database_free(db, write):
    seq = SequenceEngine(db)
    cid = db.add_contact(db.log_search("q"), ANNA)
    enrollment = seq.enroll_contact(cid, seq.create_sequence("default", STEPS))
    db.conn.execute(
        "CREATE TRIGGER broken BEFORE UPDATE ON sequence_enrollments "
        "BEGIN SELECT RAISE(ABORT, 'write failed'); END"
    )
    db.conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        if write == "advance_enrollment":
            seq.advance_enrollment(enrollment, 0)
        else:
            seq.cancel_enrollment(enrollment, "replied")

    assert _left_clean(db)


# ── The inbox monitor ────────────────────────────────────────────────


def _monitor(db):
    return ImapMonitor(db, "imap.test", 993, "me@corp.example", "pw")


def _break_the_reply_mark_of(db, email):
    db.conn.execute(
        "CREATE TRIGGER broken BEFORE UPDATE OF last_reply_at ON contacts "
        f"WHEN OLD.email = '{email}' "
        "BEGIN SELECT RAISE(ABORT, 'write failed'); END"
    )
    db.conn.commit()


def test_a_failed_reply_mark_undoes_the_marks_before_it(db):
    sid = db.log_search("q")
    first = db.add_contact(sid, ANNA)
    db.add_contact(sid, BEN)
    _break_the_reply_mark_of(db, BEN)

    with pytest.raises(sqlite3.IntegrityError):
        _monitor(db).process_replies([{"email": ANNA}, {"email": BEN}])

    assert _left_clean(db), "the failed marks still hold the write lock"
    db.conn.commit()
    assert db.get_contact_by_id(first)["status"] == "pending"


def test_opt_outs_and_bounces_stand_when_the_reply_marks_fail(db):
    """They are written on their own connections before the replies are
    marked: the rollback must not reach them, and the failed check leaves the
    watermark, so the replies are read again."""
    sid = db.log_search("q")
    replied = db.add_contact(sid, ANNA)
    db.add_contact(sid, BEN)
    for email in ("gone@dead.example", "stop@brand.example"):
        cid = db.add_contact(sid, email)
        db.log_email(cid, email, "Hello", delivery_status="sent")
    _break_the_reply_mark_of(db, BEN)
    monitor = _monitor(db)
    scan = {
        "ok": True,
        "folder": "INBOX",
        "scan_started": clock.now_iso(),
        "window_start": clock.now(),
        "junk_folder": None,
        "bounces": [{"email": "gone@dead.example", "subject": "Undeliverable"}],
        "unsubscribes": [{"email": "stop@brand.example", "subject": "unsubscribe"}],
        "requests": [],
        "replies": [{"email": ANNA}, {"email": BEN}],
    }
    monitor.check_inbox = lambda *a, **k: dict(scan)

    with pytest.raises(sqlite3.IntegrityError):
        monitor.run_check()

    assert _left_clean(db)
    assert db.is_suppressed("gone@dead.example"), "the bounce was lost"
    assert db.is_suppressed("stop@brand.example"), "the opt-out was lost"
    assert db.get_contact_by_email("stop@brand.example")["status"] == "unsubscribed"
    assert db.get_contact_by_id(replied)["status"] == "pending"
    assert db.get_state(watermark_key("me@corp.example")) is None

    db.conn.execute("DROP TRIGGER broken")
    db.conn.commit()
    monitor.run_check()

    assert db.get_contact_by_id(replied)["status"] == "responded"
    assert db.get_state(watermark_key("me@corp.example")) == scan["scan_started"]
    assert db.is_suppressed("gone@dead.example")
    assert db.is_suppressed("stop@brand.example")


# ── Anything else: the loop and the dashboard's agent thread ─────────


def _half_write(db, cid):
    db.conn.execute("UPDATE contacts SET status = 'responded' WHERE id = ?", (cid,))


def test_the_loop_undoes_a_failed_pass_before_it_records_the_error(s):
    agent = OutreachAgent(s)
    db = agent.db
    cid = db.add_contact(db.log_search("q"), ANNA)
    agent.search.search_and_extract = lambda *_a, **_k: []
    agent._process_sequences = lambda *_a, **_k: None
    agent._maybe_check_imap = lambda *_a, **_k: None
    agent._process_retries = lambda: None
    agent._maybe_apply_retention = lambda *_a, **_k: None
    passes = []

    def backlog(*_a, **_k):
        passes.append(1)
        if len(passes) == 1:
            _half_write(db, cid)
            raise RuntimeError("the pass broke after a write")
        # A later pass commits on the same connection, for something else.
        db.conn.commit()

    agent._process_pending_contacts = backlog
    sleeps = []

    def sleep(*_a, **_k):
        sleeps.append(1)
        if len(sleeps) >= 2:
            agent.stop()

    agent._interruptible_sleep = sleep

    agent.run_continuous(dry_run=False, loop_delay=60)

    assert len(passes) == 2
    errors = [r for r in db.get_recent_errors() if r["action"] == "LOOP_ERROR"]
    assert len(errors) == 1, "the error could not be recorded: the lock was held"
    assert db.get_contact_by_id(cid)["status"] == "pending", "saved half-done"


def test_the_dashboard_agent_undoes_a_failed_run_before_it_records_the_error(
    s, monkeypatch
):
    uncaught = []
    monkeypatch.setattr(
        threading, "excepthook", lambda args: uncaught.append(args.exc_value)
    )
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), ANNA)

    def fail(self, *_a, **_k):
        _half_write(self.db, cid)
        raise RuntimeError("the run broke after a write")

    monkeypatch.setattr(OutreachAgent, "run_single", fail)
    client = main.create_dashboard_app(s).test_client()

    resp = client.post("/api/agent/start", json={"mode": "single"})
    for t in threading.enumerate():
        if "run_agent" in t.name:
            t.join(20)

    assert resp.status_code == 200 and uncaught == []
    errors = [r for r in db.get_recent_errors() if r["action"] == "AGENT_ERROR"]
    assert len(errors) == 1, "the error could not be recorded: the lock was held"
    assert db.get_contact_by_id(cid)["status"] == "pending"
