"""A failed retry attempt with attempts left goes back to the queue.

get_due_retries() claims each row as 'in_flight' before returning it, so a
crash cannot send it twice. Success, cancellation, exhaustion and release all
move the row out of that state again; a failed attempt with retries left did
not. The row stayed claimed, get_due_retries() never offered it again, and it
was freed only by the crash-recovery reset half an hour after its backoff had
already elapsed - which also logged a "stranded by an earlier crash" warning
for every ordinary failure, and showed the retry as in progress meanwhile.
"""

from modules.retry_queue import RetryQueue


def _claimed_retry(db):
    rq = RetryQueue(db, max_retries=3, base_delay_minutes=5.0)
    sid = db.log_search("q")
    cid = db.add_contact(sid, "buyer@brand.de")
    log_id = db.log_email(cid, "buyer@brand.de", "s", delivery_status="failed")
    rq.add_to_retry(log_id, cid, "Initial send failed")
    db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    db.conn.commit()
    (row,) = rq.get_due_retries()
    return rq, row["id"]


def _status(db, retry_id):
    return db.conn.execute(
        "SELECT status FROM retry_queue WHERE id = ?", (retry_id,)
    ).fetchone()[0]


def test_a_failed_attempt_is_pending_again(db):
    rq, rid = _claimed_retry(db)
    rq.mark_retry_failed(rid, "Retry send failed")
    assert _status(db, rid) == "pending"


def test_it_comes_back_when_its_backoff_elapses(db):
    rq, rid = _claimed_retry(db)
    rq.mark_retry_failed(rid, "Retry send failed")
    db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    db.conn.commit()
    assert [r["id"] for r in rq.get_due_retries()] == [rid]


def test_the_last_attempt_is_still_exhausted(db):
    """The control: running out of attempts must not put the row back."""
    rq, rid = _claimed_retry(db)
    for _ in range(3):
        rq.mark_retry_failed(rid, "Retry send failed")
    assert _status(db, rid) == "exhausted"
