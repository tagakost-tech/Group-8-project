"""The "why" history shows every event in the app's local time.

Most tables store local time ("YYYY-MM-DD HH:MM:SS", Europe/Rome), but the
retry queue stores UTC with a "Z" and enrolments store ISO with an offset.
The explainer cut every stamp to its first 19 characters, so a retry queued
right after a failed send showed - and sorted - two hours before the send that
caused it (one hour in winter): the operator read a retry queued before the
failure. Zone-qualified stamps are now converted to local time first.
"""

from modules import clock
from modules.contact_explainer import explain_contact
from modules.database import Database
from modules.retry_queue import RetryQueue


def test_a_retry_shows_at_the_local_time_it_was_queued(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    cid = db.add_contact(db.log_search("q"), "buyer@brand.de")
    log_id = db.log_email(cid, "buyer@brand.de", "s", delivery_status="failed")
    RetryQueue(db).add_to_retry(log_id, cid, "Initial send failed")
    now = clock.db_now()

    timeline = explain_contact(db, cid)["timeline"]
    retry = next(e for e in timeline if e["what"].startswith("Retry queued"))
    failed = next(e for e in timeline if e["what"].startswith("Email failed"))

    assert retry["when"][:13] == now[:13], (retry, now)
    assert timeline.index(retry) <= timeline.index(failed), (
        "the retry is listed as older than the failure that queued it"
    )


def test_a_due_retry_is_shown_in_local_time(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    cid = db.add_contact(db.log_search("q"), "buyer@brand.de")
    log_id = db.log_email(cid, "buyer@brand.de", "s", delivery_status="failed")
    RetryQueue(db).add_to_retry(log_id, cid, "Initial send failed")
    facts = dict(explain_contact(db, cid)["facts"])
    assert "Z" not in facts["Retry"] and "T" not in facts["Retry"].split("due ")[1]
