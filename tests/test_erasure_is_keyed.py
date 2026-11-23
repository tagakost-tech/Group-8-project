"""An erased address leaves only a keyed token, and never its plain form.

Erasure replaced the address with an UNKEYED SHA-256 prefix, which anyone
holding the database can match by hashing guessed addresses (name@domain is
easy to guess) - pseudonymisation in name only. And it wrote the REAL address
to the suppression list, on purpose, so every "erased" person stayed on file
in clear. Both are now a token made with ERASURE_KEY: with the key it can
only confirm whether a given address was erased; without it, nothing.

A changed key would silently stop matching everyone erased under the old one,
so their addresses could be collected and emailed again. The database
remembers a fingerprint of its key and refuses to open with another.
"""

import hashlib
import sqlite3

import pytest

from agent import OutreachAgent
from modules import database
from modules.database import Database, erasure_token, set_erasure_key
from modules.export import DataExporter

EMAIL = "anna.rossi@brand.de"


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


def test_the_token_depends_on_the_key():
    set_erasure_key("key-one-" + "x" * 32)
    one = erasure_token(EMAIL)
    set_erasure_key("key-two-" + "x" * 32)
    two = erasure_token(EMAIL)
    unkeyed = hashlib.sha256(EMAIL.encode()).hexdigest()[:16]
    assert one != two
    assert unkeyed not in one and unkeyed not in two
    assert one.startswith("erased_") and one.endswith("@redacted.invalid")


def test_no_token_without_a_key(monkeypatch):
    monkeypatch.setattr(database, "_erasure_key", None)
    with pytest.raises(RuntimeError):
        erasure_token(EMAIL)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_is_refused(blank):
    with pytest.raises(ValueError):
        set_erasure_key(blank)


@pytest.fixture
def erased(tmp_path):
    db = Database(str(tmp_path / "k.db"))
    sid = db.log_search("q", source="test")
    db.add_contact(sid, EMAIL, name="Anna Rossi", organization="Brand GmbH")
    db.record_opt_out(EMAIL, "replied: remove me")
    DataExporter(db).gdpr_erase(EMAIL)
    return db


def test_the_plain_address_is_nowhere_after_erasure(erased):
    assert _raw_hits(erased.db_path, EMAIL) == []
    reasons = erased.conn.execute(
        "SELECT email, reason FROM suppression_list"
    ).fetchall()
    assert [tuple(r) for r in reasons] == [(erasure_token(EMAIL), "gdpr_erasure")]


def test_an_erased_address_stays_suppressed_and_cannot_be_collected(erased):
    assert erased.is_suppressed(EMAIL.upper())
    assert erased.get_suppression_reason(EMAIL) == "gdpr_erasure"
    sid = erased.log_search("q2", source="test")
    assert erased.add_contact(sid, EMAIL) is None


def test_an_old_unsubscribe_link_does_not_bring_the_address_back(erased):
    erased.record_opt_out(EMAIL, "Opted out via the unsubscribe link")
    assert _raw_hits(erased.db_path, EMAIL) == []


def test_the_database_refuses_a_different_key(tmp_path):
    path = str(tmp_path / "f.db")
    set_erasure_key("first-key-" + "y" * 32)
    Database(path)
    Database(path)  # same key: fine
    set_erasure_key("other-key-" + "y" * 32)
    with pytest.raises(RuntimeError, match="ERASURE_KEY"):
        Database(path)


def test_the_fingerprint_does_not_reveal_the_key(tmp_path):
    key = "secret-key-" + "z" * 32
    set_erasure_key(key)
    db = Database(str(tmp_path / "p.db"))
    stored = db.get_state(database.ERASURE_KEY_FINGERPRINT)
    assert stored and key not in stored


def test_the_agent_will_not_start_without_a_key(settings_factory, tmp_path):
    with pytest.raises(ValueError, match="ERASURE_KEY"):
        OutreachAgent(settings_factory(db_path=str(tmp_path / "a.db"), ERASURE_KEY=""))


def test_doctor_fails_a_blank_key(settings_factory):
    import main

    s = settings_factory(ERASURE_KEY="")
    check = {c.label: c for c in main.run_doctor_checks(s, network=False)}[
        "Erasure key"
    ]
    assert check.ok is False and check.blocking


def test_someone_erased_before_being_collected_cannot_be_collected(tmp_path):
    """Refused by the suppression token alone: there is no contact row whose
    token could also refuse them."""
    # A shared mail domain, so no company block refuses them either.
    shared = "anna.rossi@gmail.com"
    db = Database(str(tmp_path / "n.db"))
    DataExporter(db).gdpr_erase(shared)
    sid = db.log_search("q", source="test")
    assert db.add_contact(sid, shared) is None


def test_the_dashboard_will_not_start_without_a_key(settings_factory, tmp_path):
    import main

    with pytest.raises(ValueError, match="ERASURE_KEY"):
        main.create_dashboard_app(
            settings_factory(db_path=str(tmp_path / "d.db"), ERASURE_KEY="")
        )


@pytest.mark.parametrize("command", [["status"], ["gdpr-erase", EMAIL]])
def test_the_commands_refuse_a_blank_key(settings_factory, monkeypatch, capsys, command):
    import main

    monkeypatch.setattr(main, "load_settings", lambda: settings_factory(ERASURE_KEY=""))
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *command])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1
    assert "ERASURE_KEY" in capsys.readouterr().out
