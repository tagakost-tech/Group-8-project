"""A subject-access export returns everything held about the person.

GDPR Art. 15 gives a person a copy of all their personal data. gdpr_export
returned contacts, email_log, suppression_list, sequence_enrollments and
open_tracking - and left out four tables that hold them:

  consent_log     the legal basis we recorded for contacting them
  lead_alerts     their own reply, up to 500 characters of it
  retry_queue     failed-send records about them
  activity_log    every line the agent wrote mentioning their address

An export that says "this is everything" while omitting their own words is
not an answer to the request.
"""

from modules.database import Database
from modules.export import DataExporter
from modules.retry_queue import RetryQueue

EMAIL = "prospect@brand.de"


def test_every_table_that_holds_the_address_is_exported(tmp_path):
    db = Database(str(tmp_path / "x.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, EMAIL, organization="Brand GmbH")
    log_id = db.log_email(cid, EMAIL, "Hello", delivery_status="failed")
    db.log_consent(EMAIL, "legitimate_interest", "Publicly listed on brand.de")
    db.record_lead_alert(EMAIL, notified_to="ops@suppleco.it", reply_excerpt="Yes please")
    RetryQueue(db).add_to_retry(log_id, cid, "Initial send failed")
    db.log_activity("EMAIL_PREPARING", f"Preparing email to {EMAIL.upper()}")
    db.log_activity("OTHER", "Preparing email to someone@else.de")

    data = DataExporter(db).gdpr_export(EMAIL)

    for table in ("consent_log", "lead_alerts", "retry_queue", "activity_log"):
        assert data.get(table), f"{table} was left out of the export"
    assert data["lead_alerts"][0]["reply_excerpt"] == "Yes please"
    assert len(data["activity_log"]) == 1, "someone else's activity was exported"
