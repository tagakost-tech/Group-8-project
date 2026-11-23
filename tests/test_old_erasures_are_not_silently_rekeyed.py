"""Erasures made before ERASURE_KEY are never silently left behind.

Before the key existed, an erasure left an unkeyed token in contacts and the
REAL address on the suppression list. A database holding those, opened with
a key for the first time, would record the key's fingerprint and carry on:
the old tokens no longer match anything, so an erased person could be
collected and emailed again, and the old plain entries stay in clear. Such a
database is refused until it is migrated. (None exists today: the product
starts clean and the prototype made no erasure.)

And the backlog queries matched the suppression list against the plain
address only. A contact row left by an erasure that failed half-way stayed
in the backlog, even though its suppression entry was already the token.
"""

import sqlite3

import pytest

from modules import database
from modules.database import Database, set_erasure_key

EMAIL = "anna.rossi@brand.de"


def _made_without_a_key(path, monkeypatch, statement, params):
    with monkeypatch.context() as m:
        m.setattr(database, "_erasure_key", None)
        Database(path)
    raw = sqlite3.connect(path)
    raw.execute(statement, params)
    raw.commit()
    raw.close()


def test_an_old_erased_contact_blocks_the_first_keyed_open(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    _made_without_a_key(
        path,
        monkeypatch,
        "INSERT INTO contacts (search_id, email, status) VALUES (1, ?, 'erased')",
        ("erased_0123456789abcdef@redacted.invalid",),
    )
    with pytest.raises(RuntimeError, match="before ERASURE_KEY"):
        Database(path)


def test_an_old_plain_erasure_entry_blocks_the_first_keyed_open(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    _made_without_a_key(
        path,
        monkeypatch,
        "INSERT INTO suppression_list (email, reason) VALUES (?, 'gdpr_erasure')",
        (EMAIL,),
    )
    with pytest.raises(RuntimeError, match="before ERASURE_KEY"):
        Database(path)


def test_a_database_with_only_opt_outs_opens_normally(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    _made_without_a_key(
        path,
        monkeypatch,
        "INSERT INTO suppression_list (email, reason) VALUES (?, 'unsubscribe')",
        (EMAIL,),
    )
    Database(path)  # opt-outs are meant to stay plain


def test_keyed_erasures_do_not_trip_the_check_later(tmp_path):
    from modules.export import DataExporter

    path = str(tmp_path / "new.db")
    DataExporter(Database(path)).gdpr_erase(EMAIL)
    Database(path)  # reopening a database erased under its own key is fine


@pytest.fixture
def half_erased(tmp_path):
    """Suppressed as the token, but the contact row never got anonymised."""
    db = Database(str(tmp_path / "h.db"))
    sid = db.log_search("q", source="test")
    db.add_contact(sid, EMAIL, name="Anna")
    db.suppress_erased(EMAIL)
    return db


def test_a_half_erased_contact_is_not_in_the_backlog(half_erased):
    assert [c["email"] for c in half_erased.get_pending_contacts()] == []


def test_a_half_erased_contact_is_not_counted_as_waiting(half_erased):
    assert half_erased.count_pending_contacts() == 0


def test_the_token_function_works_inside_sql(tmp_path):
    set_erasure_key("sql-key-" + "q" * 32)
    db = Database(str(tmp_path / "s.db"))
    (value,) = db.conn.execute("SELECT erasure_token(?)", (EMAIL,)).fetchone()
    assert value == database.erasure_token(EMAIL)
