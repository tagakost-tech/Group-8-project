"""Case-insensitive address lookups are served by an index, not a table scan.

Addresses are matched with LOWER(column) = ?, because scraped casing survives
in some rows. The only indexes were on the plain columns, which SQLite cannot
use for LOWER(column), so each of these lookups read its whole table: once per
Direct Send, per IMAP reply, per qualified contact (the consent row), and in
the bounce guard's per-bounce check against the email log - run before every
contact the campaign sends. Expression indexes on LOWER(...) serve them.

The statements are the ones the code really runs, captured with SQLite's
trace callback and then explained.
"""

import sqlite3

import pytest

from modules.database import Database


@pytest.fixture
def traced(tmp_path, monkeypatch):
    path = str(tmp_path / "a.db")
    db = Database(path)
    sid = db.log_search("q")
    cid = db.add_contact(sid, "buyer@brand.de", organization="Brand")
    db.log_email(cid, "Buyer@Brand.de", "s", delivery_status="sent")
    db.add_to_suppression("buyer@brand.de", "bounce")
    seen: list[str] = []
    real = sqlite3.connect

    def connect(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr("modules.database.sqlite3.connect", connect)
    return db, seen, path


def _plan(path, needle, seen):
    stmts = [s for s in seen if needle in s]
    assert stmts, f"no statement containing {needle!r} ran"
    conn = sqlite3.connect(path)
    try:
        return [
            row[3]
            for s in stmts
            for row in conn.execute("EXPLAIN QUERY PLAN " + s).fetchall()
        ]
    finally:
        conn.close()


def _searches(plan, index):
    """A keyed lookup on the expression, not a scan that merely walks the index."""
    return any(
        p.startswith("SEARCH") and index in p and "<expr>=?" in p for p in plan
    )


def test_a_contact_is_found_by_address_through_an_index(traced):
    db, seen, path = traced
    db.get_contact_by_email("BUYER@brand.de")
    plan = _plan(path, "FROM contacts WHERE LOWER(email)", seen)
    assert not any(p.startswith("SCAN contacts") for p in plan), plan


def test_the_consent_check_uses_an_index(traced):
    db, seen, path = traced
    db.log_consent("buyer@brand.de", "legitimate_interest", "test")
    plan = _plan(path, "FROM consent_log WHERE LOWER(email)", seen)
    assert not any(p.startswith("SCAN consent_log") for p in plan), plan


@pytest.mark.parametrize(
    "call", ["get_deliverability_metrics", "get_domain_bounce_stats"]
)
def test_the_bounce_checks_find_the_send_through_an_index(traced, call):
    db, seen, path = traced
    getattr(db, call)()
    plan = _plan(path, "LOWER(e.recipient_email) = LOWER(s.email)", seen)
    assert _searches(plan, "idx_email_log_recipient_lower"), plan


def test_the_retention_job_checks_lead_alerts_through_an_index(
    traced, settings_factory
):
    from modules.retention import apply_retention

    db, seen, path = traced
    apply_retention(db, settings_factory(db_path=path))
    plan = _plan(path, "FROM lead_alerts l WHERE LOWER(l.email)", seen)
    assert _searches(plan, "idx_lead_alerts_email_lower"), plan
