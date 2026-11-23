"""Erasure reaches every table that holds the address, not only the obvious ones.

gdpr_erase covered contacts, email_log's address and content columns,
consent_log and lead_alerts. The address - and the reader's device data -
still sat in:

  email_log.smtp_response / error_message   servers echo the recipient back
  retry_queue.last_error                    "send to <address> failed"
  activity_log.details                      "Preparing email to <address>"
  open_tracking.ip_address / user_agent     who opened, from where

activity_log is the audit trail and is kept on purpose (accountability,
Art. 5(2)). Keeping the ROW does not require keeping the IDENTIFIER: the
address is replaced by the same erasure token contacts get, so the record of
what the agent did survives and the person does not.
"""

import sqlite3

from modules.analytics import Analytics
from modules.database import Database
from modules.export import DataExporter
from modules.retry_queue import RetryQueue

EMAIL = "prospect@brand.de"
MIXED = "Prospect@Brand.de"
NAME = "Anna Rossi"
IP = "203.0.113.5"
UA = "TestAgent/1.0"


def _surviving(db, needles):
    raw = sqlite3.connect(db.db_path)
    hits = []
    tables = [
        r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    for t in tables:
        cols = [c[1] for c in raw.execute(f"PRAGMA table_info({t})").fetchall()]
        for row in raw.execute(f"SELECT * FROM {t}").fetchall():
            for col, val in zip(cols, row):
                if isinstance(val, str) and any(n in val for n in needles):
                    hits.append(f"{t}.{col}")
    raw.close()
    return hits


def _erase(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, EMAIL, name=NAME, organization="Brand GmbH")
    log_id = db.log_email(
        cid,
        EMAIL,
        "Hello",
        delivery_status="failed",
        smtp_response=f"550 5.1.1 <{EMAIL}>: Recipient address rejected",
        error_message=f"Recipient refused: {{'{MIXED}': (550, b'unknown')}}",
    )
    RetryQueue(db).add_to_retry(log_id, cid, f"send to {EMAIL} failed")
    db.log_activity("EMAIL_PREPARING", f"Preparing email to {MIXED}")
    analytics = Analytics(db)
    analytics.generate_tracking_id(log_id, "tid-1")
    analytics.record_open("tid-1", ip_address=IP, user_agent=UA)
    activity_rows = db.conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]

    DataExporter(db).gdpr_erase(EMAIL)
    return db, activity_rows


def test_nothing_identifying_survives(tmp_path):
    """Not even the suppression entry: it holds the keyed token now."""
    db, _ = _erase(tmp_path)
    survivors = _surviving(db, [EMAIL, MIXED, NAME, IP, UA])
    assert survivors == [], (
        f"personal data survived the erasure in: {survivors}"
    )


def test_the_audit_trail_keeps_its_rows(tmp_path):
    """Redacted, not deleted: what the agent did stays on record."""
    db, before = _erase(tmp_path)
    after = db.conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
    assert after >= before
    details = [
        r[0] for r in db.conn.execute("SELECT details FROM activity_log").fetchall()
    ]
    assert any("erased_" in (d or "") for d in details)
