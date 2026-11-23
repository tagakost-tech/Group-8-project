"""An opt-out ends every send still queued for that person.

record_opt_out suppressed the address and ended the follow-up sequence, but
a retry of a failed first email stayed 'pending'. The send check refused it
when its turn came, so no email went out - but the queue went on showing a
send scheduled to someone who had objected, the retry panel counted it as
work to do, and nothing about it said why (GDPR Art. 21(3)).
"""

from modules.database import Database
from modules.retry_queue import RetryQueue

EMAIL = "objector@brand.de"


def _queued_retry(db, email):
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, email, name="Anna")
    log_id = db.log_email(cid, email, "Hello", delivery_status="failed")
    retry_id = RetryQueue(db).add_to_retry(log_id, cid, "421 try later")
    return cid, retry_id


def _retry(db, retry_id):
    return db.conn.execute(
        "SELECT status, last_error FROM retry_queue WHERE id = ?", (retry_id,)
    ).fetchone()


def test_a_pending_retry_is_cancelled_with_the_reason(tmp_path):
    db = Database(str(tmp_path / "o.db"))
    _, retry_id = _queued_retry(db, EMAIL)

    result = db.record_opt_out(EMAIL.upper(), "replied: please remove me")

    status, why = _retry(db, retry_id)
    assert status == "cancelled"
    assert "please remove me" in why
    assert result["retries_cancelled"] == 1


def test_a_retry_already_being_sent_is_left_to_the_send_check(tmp_path):
    """An in-flight row belongs to the sender, which checks suppression just
    before it sends; overwriting it here would race that final update."""
    db = Database(str(tmp_path / "o.db"))
    _, retry_id = _queued_retry(db, EMAIL)
    with db._transaction() as conn:
        conn.execute(
            "UPDATE retry_queue SET status = 'in_flight' WHERE id = ?", (retry_id,)
        )

    result = db.record_opt_out(EMAIL, "one-click unsubscribe")

    assert _retry(db, retry_id)[0] == "in_flight"
    assert result["retries_cancelled"] == 0


def test_another_persons_retry_is_untouched(tmp_path):
    db = Database(str(tmp_path / "o.db"))
    _, other_retry = _queued_retry(db, "colleague@brand.de")

    db.record_opt_out(EMAIL, "one-click unsubscribe")

    assert _retry(db, other_retry)[0] == "pending"
