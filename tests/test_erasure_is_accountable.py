"""An erasure leaves a record that it happened, and nothing that names the person.

gdpr-erase printed and logged the very address it had just erased, kept the
person's company name and their company's daily send counts, left their
name and company in the activity log, left a pending retry scheduled, and
left any held opt-out file holding the address. Nothing recorded on the
dashboard that an erasure had been done, and nothing told the operator
which copies the code cannot reach (the mailboxes, the log files).
"""

import logging
import sqlite3

import pytest

import main
from modules import optout_spool
from modules.database import Database, erasure_token
from modules.export import DataExporter
from modules.retry_queue import RetryQueue

EMAIL = "anna.rossi@brandco.de"
NAME = "Anna Rossi"
ORG = "Brandco Nutrition GmbH"


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "e.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, EMAIL, name=NAME, organization=ORG)
    log_id = db.log_email(cid, EMAIL, "Hello", delivery_status="failed")
    RetryQueue(db).add_to_retry(log_id, cid, "421 try later")
    with db._transaction() as conn:
        conn.execute(
            "INSERT INTO domain_send_counts (date, domain, count) VALUES ('2026-10-27', 'brandco.de', 2)"
        )
    db.log_activity("EMAIL_SENT", f"Sent to {NAME} <{EMAIL}> at {ORG}")
    # Another person's row that happens to share the first name.
    db.log_activity("EMAIL_SENT", "Sent to Anna Bianchi <a.bianchi@other.it>")
    return db


def _raw_hits(db, needle):
    raw = sqlite3.connect(db.db_path)
    hits = []
    for (table,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [c[1] for c in raw.execute(f"PRAGMA table_info({table})")]
        for row in raw.execute(f"SELECT * FROM {table}"):
            for col, val in zip(cols, row):
                if isinstance(val, str) and needle.lower() in val.lower():
                    hits.append(f"{table}.{col}")
    raw.close()
    return hits


def test_the_result_and_the_log_name_only_the_token(db, caplog):
    with caplog.at_level(logging.INFO):
        result = DataExporter(db).gdpr_erase(EMAIL)
    assert result["email"] == erasure_token(EMAIL)
    assert EMAIL not in str(result)
    assert EMAIL not in caplog.text


def test_the_dashboard_records_that_an_erasure_was_done(db):
    DataExporter(db).gdpr_erase(EMAIL)
    rows = db.conn.execute(
        "SELECT details FROM activity_log WHERE action = 'GDPR_ERASURE'"
    ).fetchall()
    assert len(rows) == 1
    assert erasure_token(EMAIL) in rows[0][0] and EMAIL not in rows[0][0]


def test_nothing_names_the_person_or_their_company_afterwards(db):
    DataExporter(db).gdpr_erase(EMAIL)
    for needle in (EMAIL, NAME, ORG, "brandco.de"):
        assert _raw_hits(db, needle) == [], needle


def test_another_persons_record_is_left_alone(db):
    """Including a colleague's row naming the same company: the company name
    is redacted only where it sits beside the erased person."""
    colleague = "Sent to Marco Neri <marco.neri@brandco.de> at Brandco Nutrition GmbH"
    db.log_activity("EMAIL_SENT", colleague)

    DataExporter(db).gdpr_erase(EMAIL)

    assert _raw_hits(db, "Anna Bianchi") == ["activity_log.details"]
    details = [r[0] for r in db.conn.execute("SELECT details FROM activity_log")]
    assert colleague in details


def test_a_pending_retry_is_cancelled(db):
    DataExporter(db).gdpr_erase(EMAIL)
    status, why = db.conn.execute(
        "SELECT status, last_error FROM retry_queue"
    ).fetchone()
    assert status == "cancelled"
    assert "erased" in why and EMAIL not in why


def test_a_held_opt_out_file_for_the_person_is_removed(db):
    optout_spool.spool_optout(db.db_path, EMAIL, "one-click unsubscribe")
    optout_spool.spool_optout(db.db_path, "someone@else.de", "one-click unsubscribe")

    DataExporter(db).gdpr_erase(EMAIL)

    assert optout_spool.pending_addresses(db.db_path) == {"someone@else.de"}


def test_the_command_prints_the_token_and_what_it_could_not_reach(
    settings_factory, tmp_path, monkeypatch, capsys
):
    s = settings_factory(
        db_path=str(tmp_path / "c.db"), lead_alert_email="sales@suppleco.example"
    )
    Database(s.get_abs_db_path())
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", "gdpr-erase", EMAIL])

    main.main()

    out = capsys.readouterr().out
    assert EMAIL not in out
    assert erasure_token(EMAIL) in out
    for place in ("Sent Items", "sales@suppleco.example", "log files"):
        assert place in out, place
