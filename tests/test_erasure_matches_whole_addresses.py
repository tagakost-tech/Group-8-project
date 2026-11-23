"""Erasure and export touch the requested address, not addresses containing it.

Review finding. Redaction and the activity-log export matched the address as
a bare substring, so erasing export@brand.de also rewrote the records of
eu-export@brand.de, and a subject-access export for export@brand.de handed
over lines about eu-export@brand.de - another person's address. Same for
a@b.com inside a@b.com.au.
"""

from modules.database import Database
from modules.export import DataExporter


def _db(tmp_path):
    db = Database(str(tmp_path / "w.db"))
    sid = db.log_search("q", source="t")
    db.add_contact(sid, "export@brand.de")
    db.add_contact(sid, "eu-export@brand.de")
    db.log_activity("SENT", "Successfully sent to eu-export@brand.de")
    db.log_activity("SENT", "Successfully sent to export@brand.de.")
    db.log_activity("SENT", "Successfully sent to export@brand.de.au")
    return db


def _details(db):
    return [
        r[0] for r in db.conn.execute("SELECT details FROM activity_log").fetchall()
    ]


def test_erasure_leaves_a_longer_address_alone(tmp_path):
    db = _db(tmp_path)
    DataExporter(db).gdpr_erase("export@brand.de")
    details = _details(db)
    assert "Successfully sent to eu-export@brand.de" in details
    assert "Successfully sent to export@brand.de.au" in details
    # ...while the address itself, even ending a sentence, is redacted.
    assert not any(d.endswith("to export@brand.de.") for d in details)


def test_export_does_not_disclose_another_persons_lines(tmp_path):
    db = _db(tmp_path)
    rows = DataExporter(db).gdpr_export("export@brand.de").get("activity_log", [])
    assert [r["details"] for r in rows] == ["Successfully sent to export@brand.de."]
