"""The unsubscribe secret and the erasure key are long enough not to be guessed.

Any value but the shipped placeholder was accepted, a few letters included.
An unsubscribe token is HMAC(UNSUBSCRIBE_SECRET, address), and every
recipient knows their own address and link: from one email they could test
guessed secrets offline, then forge an opt-out for anyone. An erasure token
is HMAC(ERASURE_KEY, address): whoever holds a copy of the database could
guess a short key, then test guessed addresses against every erasure.

Now (2026-11 security hardening, stage 3):
- start-up refuses an UNSUBSCRIBE_SECRET shorter than 32 characters;
- a new database (no key fingerprint recorded yet) refuses an ERASURE_KEY
  shorter than 32 characters;
- a database that already keeps a shorter key still opens, because the key
  can never change, and doctor fails on it, saying to start again with a new
  database and a long key before go-live.
"""

import pytest

import main
from config import Settings, load_settings
from modules import database
from modules.database import Database, erasure_key_fingerprint, set_erasure_key

SHORT = "k" * 31
LONG = "k" * 32


def test_a_short_unsubscribe_secret_refuses_to_start(monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", SHORT)
    with pytest.raises(ValueError, match="shorter than 32") as refused:
        load_settings()
    assert SHORT not in str(refused.value)


def test_a_32_character_unsubscribe_secret_starts(monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", LONG)
    assert load_settings().unsubscribe_secret == LONG


def test_padding_does_not_count_towards_the_length(monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", f"  {SHORT}  ")
    with pytest.raises(ValueError, match="UNSUBSCRIBE_SECRET"):
        load_settings()


def test_the_minimum_is_32_characters():
    assert Settings.SECRET_MIN_LENGTH == 32
    assert database.ERASURE_KEY_MIN_LENGTH == 32


def test_a_new_database_refuses_a_short_erasure_key(tmp_path):
    set_erasure_key(SHORT)
    with pytest.raises(RuntimeError, match="shorter than 32"):
        Database(str(tmp_path / "new.db"))


def test_a_new_database_takes_a_32_character_key(tmp_path):
    set_erasure_key(LONG)
    db = Database(str(tmp_path / "new.db"))
    assert db.get_state(database.ERASURE_KEY_FINGERPRINT) == erasure_key_fingerprint(
        LONG
    )


def _database_keeping(path, key):
    """A database made before the minimum, which recorded a short key."""
    set_erasure_key("a-long-key-to-create-it-" + "x" * 32)
    db = Database(str(path))
    db.set_state(database.ERASURE_KEY_FINGERPRINT, erasure_key_fingerprint(key))


def test_a_database_that_keeps_a_short_key_still_opens(tmp_path):
    path = tmp_path / "old.db"
    _database_keeping(path, SHORT)
    set_erasure_key(SHORT)
    db = Database(str(path))
    assert db.get_state(database.ERASURE_KEY_FINGERPRINT) == erasure_key_fingerprint(
        SHORT
    )


def _erasure_key_row(settings_factory, path, key):
    s = settings_factory(db_path=str(path), ERASURE_KEY=key)
    (row,) = main.run_doctor_checks(s, network=False, only={"Erasure key"})
    return row


def test_doctor_fails_a_short_key_a_database_already_keeps(settings_factory, tmp_path):
    path = tmp_path / "old.db"
    _database_keeping(path, SHORT)
    row = _erasure_key_row(settings_factory, path, SHORT)
    assert row.ok is False and row.blocking
    assert "cannot be changed" in row.detail and "new database" in row.detail
    assert SHORT not in row.detail


def test_doctor_fails_a_short_key_before_the_first_start(settings_factory, tmp_path):
    row = _erasure_key_row(settings_factory, tmp_path / "none.db", SHORT)
    assert row.ok is False and row.blocking
    assert "new database refuses it" in row.detail


def test_doctor_passes_a_32_character_key(settings_factory, tmp_path):
    row = _erasure_key_row(settings_factory, tmp_path / "none.db", LONG)
    assert row.ok is True
