"""A send that fails while the database is struggling does not end the batch.

After a failed first send, process_contact queues a retry linked to the
failed send's email_log row. When reading that row's id failed (a locked
database), the code carried on with id 0 - its comment said the retry "must
still be queued" - but retry_queue.email_log_id is NOT NULL and references
email_log, so the insert raised IntegrityError. Nothing caught it: the
exception left process_contact and took the rest of the campaign batch with
it. The retry write is now contained like every other post-send write; the
lost retry is logged as an error.
"""

import sqlite3

from agent import OutreachAgent


def test_a_retry_that_cannot_be_linked_does_not_end_the_batch(
    settings_factory, tmp_path, monkeypatch
):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    a._running = True
    a._interruptible_sleep = lambda *_a, **_k: None
    attempts = []

    def failing_dispatch(**k):
        attempts.append(k["contact"]["email"])
        return 0

    def locked(_contact_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(a.email_sender, "dispatch_to_contact", failing_dispatch)
    monkeypatch.setattr(a.db, "get_last_email_log_id", locked)
    sid = a.db.log_search("q")
    batch = [
        a.db.get_contact_by_id(a.db.add_contact(sid, f"p{i}@brand.de"))
        for i in range(2)
    ]

    a._email_contacts(batch)

    assert attempts == ["p0@brand.de", "p1@brand.de"], "the batch stopped early"
