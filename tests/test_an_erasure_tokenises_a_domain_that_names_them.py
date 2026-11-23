"""An erasure leaves no activity row naming a domain that names the person.

A sole trader's domain names them (mario.rossi@mariorossi.it). Erasure kept
their company block only as a keyed token and deleted the domain's send
counts, but the activity log kept the domain in clear wherever a row carried
it without the address: "Skipping mariorossi.it: 40% bounce rate" and
"Company block lifted for mariorossi.it: ...". Retention masks addresses
only, so those rows were kept for ever. A shared mail domain (gmail.com)
names no one and is left alone in other people's rows.
"""

import pathlib
import sqlite3

import pytest

from modules.company_block import block_company
from modules.database import Database, domain_token
from modules.export import DataExporter

SOLE_TRADER = "mario.rossi@mariorossi.it"
BACKOFF = "Skipping {}: 40% bounce rate (2/5 in 7d)"
LIFTED = "Company block lifted for {}: blocked by mistake"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "d.db"))


def _details(db):
    return [
        r[0]
        for r in db.conn.execute(
            "SELECT details FROM activity_log WHERE action IN "
            "('DOMAIN_BACKOFF', 'DOMAIN_UNBLOCKED') ORDER BY id"
        )
    ]


def _raw_hits(db_path, needle):
    raw = sqlite3.connect(db_path)
    hits = []
    for (table,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [c[1] for c in raw.execute(f"PRAGMA table_info({table})")]
        for row in raw.execute(f"SELECT * FROM {table}"):
            for col, val in zip(cols, row):
                if isinstance(val, str) and needle in val.lower():
                    hits.append(f"{table}.{col}")
    raw.close()
    return hits


def _files_holding(folder, needle):
    return [
        str(p.relative_to(folder))
        for p in pathlib.Path(folder).rglob("*")
        if p.is_file() and needle.encode() in p.read_bytes().lower()
    ]


def _erase_with_history(db, email, domain, *others):
    sid = db.log_search("q", source="test")
    db.add_contact(sid, email, name="Mario Rossi", organization="Studio Rossi")
    block_company(db, email, "one-click unsubscribe")
    db.lift_domain_block(domain)
    db.log_activity("DOMAIN_BACKOFF", BACKOFF.format(domain), status="warning")
    db.log_activity("DOMAIN_UNBLOCKED", LIFTED.format(domain))
    for other in others:
        db.log_activity("DOMAIN_BACKOFF", BACKOFF.format(other), status="warning")
    DataExporter(db).gdpr_erase(email)
    return _details(db)


def test_the_rows_naming_a_sole_traders_domain_keep_only_its_token(db, tmp_path):
    rows = _erase_with_history(
        db, SOLE_TRADER, "mariorossi.it", "brand.de", "gmail.com"
    )

    tok = domain_token("mariorossi.it")
    assert rows == [
        BACKOFF.format(tok),
        LIFTED.format(tok),
        BACKOFF.format("brand.de"),
        BACKOFF.format("gmail.com"),
    ]
    assert _raw_hits(db.db_path, "mariorossi") == []
    assert _files_holding(tmp_path, "mariorossi") == []


def test_a_domain_inside_a_longer_one_is_not_rewritten(db):
    rows = _erase_with_history(db, "anna@rossi.it", "rossi.it", "mariorossi.it")

    tok = domain_token("rossi.it")
    assert rows == [
        BACKOFF.format(tok),
        LIFTED.format(tok),
        BACKOFF.format("mariorossi.it"),
    ]


def test_a_shared_mail_domain_is_left_in_other_peoples_rows(db):
    sid = db.log_search("q", source="test")
    db.add_contact(sid, "mario@gmail.com", name="Mario Rossi")
    db.log_activity("DOMAIN_BACKOFF", BACKOFF.format("gmail.com"), status="warning")

    DataExporter(db).gdpr_erase("mario@gmail.com")

    assert _details(db) == [BACKOFF.format("gmail.com")]
