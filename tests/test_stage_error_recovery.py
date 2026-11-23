"""Unit tests for cross-stage error recovery and edge cases (tests/test_stage_error_recovery.py).

Verifies:
- Transient vs permanent SMTP error classification.
- In-flight retry claim recovery for interrupted worker tasks (reset_stale_in_flight).
- Database busy / concurrency handling during status writes.
- Graceful recovery on missing or inaccessible register files.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from modules.database import Database
from modules.exclusion_manager import ExclusionManager
from modules.retry_queue import RetryQueue
from modules.verification import _is_transient_smtp

# ---------------------------------------------------------------------------
# 1. Transient vs permanent SMTP error classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_text,expected_transient",
    [
        # 4xx transient family
        ("451 4.3.0 Mailbox temporarily unavailable", True),
        ("421 4.7.0 Try again later, closing transmission channel", True),
        ("450 4.2.1 Mailbox busy / greylisting in effect", True),
        ("452 4.2.2 Mailbox full, try again later", True),
        # Transport / connection issues
        ("Server disconnected unexpectedly during handshake", True),
        ("Connection refused by remote host", True),
        ("Connection timed out after 30 seconds", True),
        ("Network error: unreachable host", True),
        ("Could not resolve MX records for domain", True),
        ("Unexpected error communicating with MX", True),
        # 5xx permanent family
        ("550 5.1.1 User unknown", False),
        ("554 5.7.1 Relay access denied", False),
        ("553 5.3.0 Invalid recipient address syntax", False),
        ("501 5.5.2 Syntax error in parameters", False),
        ("550 5.2.1 Mailbox disabled or non-existent", False),
        # Generic non-transient strings
        ("Recipient address rejected: unrouteable address", False),
        ("", False),
        (None, False),
    ],
)
def test_transient_vs_permanent_smtp_error_classification(
    error_text, expected_transient
):
    assert _is_transient_smtp(error_text) is expected_transient


def test_imap_dsn_soft_vs_hard_bounce_classification():
    from modules.imap_monitor import ImapMonitor

    # Create dummy monitor to test _classify_email DSN status rules
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor._FAILURE_SUBJECTS = ImapMonitor._FAILURE_SUBJECTS
    monitor._AMBIGUOUS_DSN_SUBJECTS = ImapMonitor._AMBIGUOUS_DSN_SUBJECTS

    # Mock _dsn_verdict to return transient 4.x.x status
    monitor._dsn_verdict = lambda msg: ("recipient@test.com", "failed", "4.4.7")
    res_soft = monitor._classify_email(
        "mailer-daemon@mx.test", "Delivery Failure", "body"
    )
    assert res_soft == "soft_bounce"

    # Mock _dsn_verdict to return permanent 5.x.x status
    monitor._dsn_verdict = lambda msg: ("recipient@test.com", "failed", "5.1.1")
    res_hard = monitor._classify_email(
        "mailer-daemon@mx.test", "Delivery Failure", "body"
    )
    assert res_hard == "bounce"


# ---------------------------------------------------------------------------
# 2. In-flight retry claim recovery for interrupted worker tasks
# ---------------------------------------------------------------------------


def test_reset_stale_in_flight_recovers_interrupted_tasks(db):
    rq = RetryQueue(db, max_retries=3, base_delay_minutes=5.0)

    sid = db.log_search("stale_in_flight_test")
    c1 = db.add_contact(sid, "c1@test.com")
    c2 = db.add_contact(sid, "c2@test.com")
    c3 = db.add_contact(sid, "c3@test.com")
    c4 = db.add_contact(sid, "c4@test.com")

    l1 = db.log_email(c1, "c1@test.com", "S1", delivery_status="failed")
    l2 = db.log_email(c2, "c2@test.com", "S2", delivery_status="failed")
    l3 = db.log_email(c3, "c3@test.com", "S3", delivery_status="failed")
    l4 = db.log_email(c4, "c4@test.com", "S4", delivery_status="failed")

    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_time = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with db._transaction() as conn:
        # Row 1: in_flight from 60 mins ago (stale claim)
        conn.execute(
            """INSERT INTO retry_queue
               (email_log_id, contact_id, attempt, max_attempts, next_retry_at, status, created_at)
               VALUES (?, ?, 1, 3, ?, 'in_flight', ?)""",
            (l1, c1, old_time, old_time),
        )
        # Row 2: in_flight from 5 mins ago (currently running worker)
        conn.execute(
            """INSERT INTO retry_queue
               (email_log_id, contact_id, attempt, max_attempts, next_retry_at, status, created_at)
               VALUES (?, ?, 1, 3, ?, 'in_flight', ?)""",
            (l2, c2, recent_time, recent_time),
        )
        # Row 3: pending from 60 mins ago
        conn.execute(
            """INSERT INTO retry_queue
               (email_log_id, contact_id, attempt, max_attempts, next_retry_at, status, created_at)
               VALUES (?, ?, 1, 3, ?, 'pending', ?)""",
            (l3, c3, old_time, old_time),
        )
        # Row 4: succeeded from 60 mins ago
        conn.execute(
            """INSERT INTO retry_queue
               (email_log_id, contact_id, attempt, max_attempts, next_retry_at, status, created_at)
               VALUES (?, ?, 1, 3, ?, 'succeeded', ?)""",
            (l4, c4, old_time, old_time),
        )

    # Recover rows older than 30 minutes
    recovered = rq.reset_stale_in_flight(older_than_minutes=30)
    assert recovered == 1

    with db._transaction() as conn:
        rows = {
            r["contact_id"]: r["status"]
            for r in conn.execute(
                "SELECT contact_id, status FROM retry_queue"
            ).fetchall()
        }

    # Row 1 was recovered back to pending
    assert rows[c1] == "pending"
    # Row 2 was not stale, remains in_flight
    assert rows[c2] == "in_flight"
    # Row 3 and 4 unaffected
    assert rows[c3] == "pending"
    assert rows[c4] == "succeeded"


# ---------------------------------------------------------------------------
# 3. Database busy / concurrency handling during status writes
# ---------------------------------------------------------------------------


def test_concurrent_status_writes_across_threads_without_locking(db):
    sid = db.log_search("concurrency_test")
    # Pre-seed 20 contacts
    contact_ids = [
        db.add_contact(sid, f"thread_{i}@concurrency.com", name=f"Contact {i}")
        for i in range(20)
    ]

    errors: list[Exception] = []

    def _worker(thread_idx: int, cids: list[int]):
        try:
            for cid in cids:
                # Perform realistic mixed writes
                db.update_contact_status(
                    cid, "qualified", discard_reason=f"worker_{thread_idx}"
                )
                db.log_activity(
                    "CONCURRENCY_TEST",
                    f"Thread {thread_idx} updated contact {cid}",
                    status="info",
                )
                db.log_email(
                    contact_id=cid,
                    recipient_email=f"thread_{cid}@concurrency.com",
                    subject="Test Subject",
                    delivery_status="sent",
                )
        except Exception as e:  # noqa: BLE001 - every thread error is collected and asserted empty
            errors.append(e)
        finally:
            db.close_thread_conn()

    threads = []
    # 5 worker threads operating concurrently on slices of contacts
    for t_idx in range(5):
        t = threading.Thread(target=_worker, args=(t_idx, contact_ids))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # No thread should raise sqlite3.OperationalError or any other exception
    assert len(errors) == 0

    # Verify all contacts were updated and logged
    for cid in contact_ids:
        contact = db.get_contact_by_id(cid)
        assert contact["status"] == "qualified"


# ---------------------------------------------------------------------------
# 4. Graceful recovery on missing or inaccessible register files
# ---------------------------------------------------------------------------


def test_graceful_recovery_on_missing_or_corrupt_register_files(
    tmp_path, settings_factory
):
    st = settings_factory(
        client_filter_enabled=True,
        client_list_path=str(tmp_path / "missing.xlsx"),
    )
    db = Database(str(tmp_path / "recovery.db"))

    agent = MagicMock()
    agent.db = db
    agent.settings = st
    agent.client_register = None
    agent._list_stamps = {}
    agent._list_failed = {}

    mgr = ExclusionManager(agent)
    # The stage calls its helpers through the agent; wire them to the real ones.
    agent._load_register = mgr._load_register
    agent._list_stamp = ExclusionManager._list_stamp

    # 1. Missing file does not raise, returns False
    assert mgr.load_client_register() is False
    assert agent.client_register is None

    # 2. Blank path returns False gracefully without crash
    st.client_list_path = "   "
    assert mgr.load_client_register() is False

    # 3. Corrupt file does not crash, preserves prior register and records failure stamp
    prior_register = MagicMock()
    agent.client_register = prior_register

    corrupt_file = tmp_path / "corrupt.xlsx"
    corrupt_file.write_bytes(b"\x00\x01\x02corrupted_excel_stream")
    st.client_list_path = str(corrupt_file)

    assert mgr.load_client_register() is False
    # Preserves prior register
    assert agent.client_register is prior_register
    # Failure stamp recorded so it is not re-attempted needlessly
    assert "client" in agent._list_failed
    assert agent._list_failed["client"] is not None
