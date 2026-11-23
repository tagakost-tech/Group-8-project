"""The retry panel's numbers add up: cancelled retries have their own count.

get_retry_stats counted pending, succeeded and exhausted rows, and a total of
every row. A cancelled retry - its contact replied, opted out, was screened or
was already emailed - landed only in the total, so the dashboard showed
"4 pending, 9 succeeded, 2 exhausted, 18 total" with three rows unaccounted
for. The shared send check cancels a retry wherever it would otherwise spend
an attempt on a closed contact, so the gap grew with it.
"""

from modules.database import Database
from modules.retry_queue import RetryQueue


def test_cancelled_retries_are_counted_and_the_parts_add_up(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    rq = RetryQueue(db, max_retries=1)
    sid = db.log_search("q")
    ids = []
    for i in range(4):
        cid = db.add_contact(sid, f"buyer{i}@brand.de", organization="Brand")
        log_id = db.log_email(cid, f"buyer{i}@brand.de", "s", delivery_status="failed")
        ids.append(rq.add_to_retry(log_id, cid, "Initial send failed"))
    rq.cancel_retry(ids[0], "contact replied")
    rq.mark_retry_success(ids[1])
    rq.mark_retry_failed(ids[2], "Retry send failed")

    stats = rq.get_retry_stats()

    assert stats["cancelled"] == 1
    assert (stats["pending"], stats["succeeded"], stats["exhausted"]) == (1, 1, 1)
    parts = ("pending", "succeeded", "exhausted", "cancelled")
    assert sum(stats[k] for k in parts) == stats["total"]
