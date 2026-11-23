"""A retry that is already being sent still counts as queued.

add_to_retry refuses to queue a second row while one is open for the same
contact. "Open" has to mean pending OR in_flight, because in_flight is exactly
the window in which the duplicate gets created: get_due_retries claims the row
and hands it to the sender, the send fails, and the agent's failure branch
calls add_to_retry again - with the original row sitting in_flight.

If in_flight does not count, that call inserts a SECOND row at attempt=0 while
the first is still live. Both rows are real SMTP sends to the same prospect,
and because each child restarts the attempt counter, max_attempts never
terminates the family. The prospect receives the same message over and over
from SUPPLECO, and the queue looks busy rather than broken.

Nothing raises when this happens, so the controls below matter as much as the
assertions: the deduplication must not turn into "never queue anything", or a
transient SMTP failure would silently cost a real lead their follow-up.
"""

from modules.retry_queue import RetryQueue

LONG_PAST = "2020-01-01T00:00:00Z"


def _failed_send(db, email):
    """A contact whose first send failed. Returns (contact_id, email_log_id)."""
    sid = db.log_search("integratori alimentari", source="test")
    cid = db.add_contact(sid, email)
    eid = db.log_email(cid, email, "Following up", delivery_status="failed")
    return cid, eid


def _retry_rows(db, contact_id):
    with db._transaction() as conn:
        rows = conn.execute(
            "SELECT id, attempt, status FROM retry_queue "
            "WHERE contact_id = ? ORDER BY id",
            (contact_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _make_due(db, retry_id):
    """Bring a row's scheduled time forward instead of waiting five minutes."""
    with db._transaction() as conn:
        conn.execute(
            "UPDATE retry_queue SET next_retry_at = ? WHERE id = ?",
            (LONG_PAST, retry_id),
        )


def test_a_claimed_retry_blocks_a_second_one_for_the_same_contact(db):
    """The duplicate-send window: the row is claimed, the send fails, and the
    failure branch tries to queue the same contact again."""
    rq = RetryQueue(db)
    cid, eid = _failed_send(db, "info@brand.de")

    first = rq.add_to_retry(eid, cid, "smtp timeout")
    _make_due(db, first)

    claimed = rq.get_due_retries()
    assert [r["id"] for r in claimed] == [first]
    assert _retry_rows(db, cid)[0]["status"] == "in_flight"

    # The claimed send fails; _process_contact's failure branch re-queues.
    again = rq.add_to_retry(eid, cid, "smtp timeout")

    assert again == first, (
        "queued a new retry while one was already in flight - this contact is "
        "now scheduled to be emailed twice"
    )
    rows = _retry_rows(db, cid)
    assert len(rows) == 1, (
        f"one open retry per contact, found {len(rows)}: {rows} - each extra "
        f"row is another SMTP send to the same person, and each restarts the "
        f"attempt counter so max_attempts never gives up"
    )


def test_an_in_flight_retry_is_reported_as_outstanding_work(db):
    """The same 'open means pending or in_flight' rule the stats rely on. A
    dashboard that drops claimed rows reports an empty queue mid-batch."""
    rq = RetryQueue(db)
    cid, eid = _failed_send(db, "sales@nutrabrand.it")
    first = rq.add_to_retry(eid, cid, "smtp timeout")
    _make_due(db, first)
    rq.get_due_retries()

    assert rq.get_retry_stats()["pending"] == 1


def test_a_resolved_retry_does_not_block_the_next_genuine_failure(db):
    """Control. Once the retry is finished the contact is no longer 'open', so
    a later failure must queue normally - otherwise this guard would quietly
    strand every contact that ever needed one retry."""
    rq = RetryQueue(db)
    cid, eid = _failed_send(db, "info@brand.de")

    first = rq.add_to_retry(eid, cid, "smtp timeout")
    rq.mark_retry_success(first)

    later = rq.add_to_retry(eid, cid, "mailbox full, weeks later")
    assert later != first
    assert len(_retry_rows(db, cid)) == 2


def test_one_contacts_in_flight_retry_does_not_block_another_contact(db):
    """Control. The guard is per contact, not a global lock - a batch where one
    address is mid-retry must still queue retries for everyone else."""
    rq = RetryQueue(db)
    cid_a, eid_a = _failed_send(db, "a@brand.de")
    cid_b, eid_b = _failed_send(db, "b@brand.de")

    first = rq.add_to_retry(eid_a, cid_a, "smtp timeout")
    _make_due(db, first)
    rq.get_due_retries()

    other = rq.add_to_retry(eid_b, cid_b, "smtp timeout")
    assert other is not None and other != first
    assert len(_retry_rows(db, cid_b)) == 1
