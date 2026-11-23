"""Every column of the schema says what an erasure does with it.

Erasure grew table by table, and each survivor was found by a review: the
organisation name, the daily send counts, the request register's outcome.
Nothing made the next column added to the schema answer the question.

Now PERSONAL_COLUMNS (modules/compliance.py, step G18) classifies every
column - the address replaced by its token, cleared, redacted, marked, the
row deleted, kept only beside the token, or not about a person - and this
test fails on a column the registry does not know, or one it names that no
longer exists.
"""

import pytest

from modules.compliance import ERASURE_TREATMENTS, PERSONAL_COLUMNS
from modules.database import Database


@pytest.fixture
def schema(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    tables = [
        t
        for (t,) in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        t: {row[1] for row in db.conn.execute(f"PRAGMA table_info({t})")}
        for t in tables
    }


def test_every_column_is_classified(schema):
    missing = sorted(
        f"{t}.{c}"
        for t, columns in schema.items()
        for c in columns
        if c not in PERSONAL_COLUMNS.get(t, {})
    )
    assert missing == [], "classify these in PERSONAL_COLUMNS"


def test_the_registry_names_no_column_that_is_gone(schema):
    stale = sorted(
        f"{t}.{c}"
        for t, columns in PERSONAL_COLUMNS.items()
        for c in columns
        if c not in schema.get(t, set())
    )
    assert stale == []


def test_every_treatment_is_one_erasure_knows():
    used = {v for columns in PERSONAL_COLUMNS.values() for v in columns.values()}
    assert used <= set(ERASURE_TREATMENTS)


def test_the_address_columns_are_tokenised_or_deleted():
    """Where an address is stored as such, erasure must never just keep it."""
    for table, column in (
        ("contacts", "email"),
        ("email_log", "recipient_email"),
        ("suppression_list", "email"),
        ("dsr_requests", "email"),
        ("lead_alerts", "email"),
        ("consent_log", "email"),
        ("suppressed_domains", "domain"),
    ):
        assert PERSONAL_COLUMNS[table][column] in ("token", "deleted"), (table, column)
