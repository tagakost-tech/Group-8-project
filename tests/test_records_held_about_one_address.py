"""What the agent holds about one address: the GDPR access export, and the
same collection reused by the dashboard's "why" view.

The collection lived inside gdpr_export. It is now records_held_about(db,
email), which gdpr_export returns unchanged. The first test characterises the
export as it was before the move - table order, which rows, and that another
person's longer address never leaks in - so the move can be proven to change
nothing.
"""

import pytest

from modules.database import Database
from modules.export import DataExporter, records_held_about


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "e.db"))
    sid = db.log_search("q")
    cid = db.add_contact(sid, "Anna@Brand.de", organization="Brand")
    other = db.add_contact(sid, "eu-anna@brand.de", organization="Brand")
    log1 = db.log_email(cid, "Anna@Brand.de", "s1", delivery_status="sent")
    db.log_email(cid, "anna@brand.de", "s2", delivery_status="failed")
    db.log_email(other, "eu-anna@brand.de", "s3", delivery_status="sent")
    db.conn.execute(
        "INSERT INTO open_tracking (email_log_id, tracking_id, opened_at) "
        "VALUES (?, 't1', '2026-01-01 10:00:00')",
        (log1,),
    )
    db.conn.execute("INSERT INTO sequences (id, name) VALUES (1, 's')")
    db.conn.execute(
        "INSERT INTO sequence_enrollments (contact_id, sequence_id) VALUES (?, 1)",
        (cid,),
    )
    db.conn.execute(
        "INSERT INTO lead_alerts (email, notified_to) VALUES ('anna@brand.de', 'x')"
    )
    db.conn.commit()
    db.add_to_suppression("anna@brand.de", "unsubscribe")
    db.log_consent("anna@brand.de", "legitimate_interest", "found on brand.de")
    db.conn.execute(
        "INSERT INTO retry_queue (email_log_id, contact_id, next_retry_at) "
        "VALUES (?, ?, '2026-01-01')",
        (log1, cid),
    )
    db.conn.commit()
    db.log_activity("EMAIL_SENT", "Successfully sent to Anna@Brand.de")
    db.log_activity("EMAIL_SENT", "Successfully sent to eu-anna@brand.de")
    db.cid = cid
    return db


def test_the_export_holds_every_record_and_nobody_elses(db):
    result = DataExporter(db).gdpr_export("anna@brand.de")
    assert list(result) == [
        "contacts",
        "email_log",
        "suppression_list",
        "consent_log",
        "lead_alerts",
        "sequence_enrollments",
        "open_tracking",
        "retry_queue",
        "activity_log",
    ]
    assert [r["id"] for r in result["contacts"]] == [db.cid]
    assert sorted(r["subject"] for r in result["email_log"]) == ["s1", "s2"]
    assert len(result["open_tracking"]) == 1
    assert len(result["retry_queue"]) == 1
    assert [r["details"] for r in result["activity_log"]] == [
        "Successfully sent to Anna@Brand.de"
    ]


def test_the_collection_is_what_the_export_returns(db):
    assert records_held_about(db, "anna@brand.de") == DataExporter(db).gdpr_export(
        "anna@brand.de"
    )
