"""Backups exist, and restoring one cannot bring back an objection or an erasure.

There were no backups. A backup alone would be a trap: restored, it brings
back every address erased and every objection made since it was taken.

Now (step G16):
- `main.py backup` writes a consistent copy through SQLite's backup API,
  checks it, keeps the newest 14 (L5.7) and records the run;
- every opt-out, company block, lift and erasure is appended to a journal
  beside the database - an erasure only as its token, and it rewrites the
  person's earlier lines as tokens too;
- `main.py replay-journal`, run after a restore, applies them again and
  erases every address the restored database still holds whose token the
  journal names;
- the journal is trimmed to the age of the oldest backup;
- `main.py restore-check` restores the newest backup into a scratch copy,
  replays the journal onto it and checks that nothing erased survives,
  without touching the live database.
"""

import json
import os
import shutil
import sqlite3
import time

import pytest

import main
from modules import backup, journal
from modules.database import Database, domain_token, erasure_token
from modules.export import DataExporter

ANNA = "anna.rossi@brandco.de"
LUCA = "luca@other.de"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "live" / "agent.db"))


@pytest.fixture
def db(s):
    os.makedirs(os.path.dirname(s.get_abs_db_path()), exist_ok=True)
    return Database(s.get_abs_db_path())


def _contact(db, email):
    return db.add_contact(db.log_search("q"), email, organization="Brand")


def _lines(db):
    path = journal.journal_path(db.db_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


# ── Backups ──


def test_a_backup_is_a_checked_copy_and_the_run_is_recorded(db):
    _contact(db, ANNA)
    path = backup.make_backup(db)
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT email FROM contacts").fetchall() == [(ANNA,)]
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()
    assert db.get_state("backup:last_run")
    assert not list(path.parent.glob("*.partial"))


def test_only_the_newest_fourteen_are_kept(db, monkeypatch):
    stamps = iter(range(100))
    monkeypatch.setattr(backup, "_stamp", lambda: f"20261101-0000{next(stamps):02d}")
    for _ in range(16):
        backup.make_backup(db)
    kept = sorted(p.name for p in backup.backups_dir(db.db_path).glob("agent-*.db"))
    assert len(kept) == 14
    assert kept[0].endswith("0002.db"), "the two oldest are the ones removed"


def test_a_copy_that_fails_its_check_is_not_kept(db, monkeypatch):
    monkeypatch.setattr(backup, "_integrity", lambda conn: "row 3 missing")
    with pytest.raises(RuntimeError):
        backup.make_backup(db)
    assert not list(backup.backups_dir(db.db_path).iterdir())
    assert db.get_state("backup:last_run") is None


def test_the_command_writes_a_backup(s, db, monkeypatch, capsys):
    out = _cli(s, monkeypatch, capsys, "backup")
    assert "agent-" in out and list(backup.backups_dir(db.db_path).glob("agent-*.db"))


# ── The journal ──


def test_opt_outs_blocks_lifts_and_erasures_are_journalled(db):
    db.record_opt_out(ANNA, "Opted out by email")
    db.block_domain("third.de", "objection")
    db.lift_domain_block("third.de")
    _contact(db, LUCA)
    DataExporter(db).gdpr_erase(LUCA)

    kinds = [
        (e["kind"], e.get("email") or e.get("domain") or e.get("token"))
        for e in _lines(db)
    ]
    assert ("opt_out", ANNA) in kinds
    assert ("lift", "third.de") in kinds
    assert ("erasure", erasure_token(LUCA)) in kinds
    raw = journal.journal_path(db.db_path).read_text(encoding="utf-8")
    assert LUCA not in raw


def test_an_erasure_rewrites_the_persons_earlier_lines(db):
    db.record_opt_out(ANNA, "Opted out by email")
    db.block_domain("brandco.de", "objection")
    _contact(db, ANNA)

    DataExporter(db).gdpr_erase(ANNA)

    raw = journal.journal_path(db.db_path).read_text(encoding="utf-8")
    assert ANNA not in raw and '"brandco.de"' not in raw
    assert erasure_token(ANNA) in raw and domain_token("brandco.de") in raw


# ── Restoring ──


def _restore(db, live_path):
    """What an operator does: put the newest backup in the database's place."""
    newest = max(backup.backups_dir(live_path).glob("agent-*.db"))
    db.conn.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(live_path + suffix):
            os.remove(live_path + suffix)
    shutil.copyfile(newest, live_path)
    return Database(live_path)


def test_replay_after_a_restore_brings_back_nothing_erased(db):
    _contact(db, ANNA)
    _contact(db, LUCA)
    backup.make_backup(db)
    db.record_opt_out(ANNA, "Opted out by email")
    db.block_domain("third.de", "objection")
    DataExporter(db).gdpr_erase(LUCA)
    lines_before = len(_lines(db))

    restored = _restore(db, db.db_path)
    assert restored.get_contact_by_email(LUCA), "setup: the backup predates the erasure"
    counts = journal.replay_journal(restored)

    assert restored.is_suppressed(ANNA)
    assert restored.company_blocked("x@third.de")
    assert restored.get_contact_by_email(LUCA) is None
    assert restored.is_suppressed(LUCA)
    assert counts["erased"] == 1
    assert len(_lines(restored)) == lines_before, "replay journals nothing again"


def test_a_plain_line_for_someone_erased_later_is_not_applied(db):
    path = journal.journal_path(db.db_path)
    path.write_text(
        json.dumps(
            {"at": "2026-10-01T10:00:00+02:00", "kind": "opt_out", "email": ANNA}
        )
        + "\n"
        + json.dumps(
            {
                "at": "2026-10-02T10:00:00+02:00",
                "kind": "erasure",
                "token": erasure_token(ANNA),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    counts = journal.replay_journal(db)
    held = [r[0] for r in db.conn.execute("SELECT email FROM suppression_list")]
    assert held == [erasure_token(ANNA)]
    # Never written back at all, not even until the final sweep erases it
    # again: a replay that stopped half-way would leave it in clear.
    assert counts["erased"] == 0


def test_the_journal_is_trimmed_to_the_oldest_backup(db, monkeypatch):
    path = journal.journal_path(db.db_path)
    path.write_text(
        json.dumps(
            {"at": "2020-01-01T10:00:00+01:00", "kind": "opt_out", "email": ANNA}
        )
        + "\n",
        encoding="utf-8",
    )
    db.record_opt_out(LUCA, "Opted out by email")
    backup.make_backup(db)
    emails = [e.get("email") for e in _lines(db)]
    assert ANNA not in emails, "a line older than every backup is no longer needed"
    assert LUCA in emails


def test_restore_check_rehearses_a_restore_without_touching_the_live_database(
    s, db, monkeypatch, capsys
):
    _contact(db, LUCA)
    backup.make_backup(db)
    DataExporter(db).gdpr_erase(LUCA)
    db.conn.close()
    live = s.get_abs_db_path()
    before = (os.path.getsize(live), os.path.getmtime(live))
    time.sleep(0.01)

    out = _cli(s, monkeypatch, capsys, "restore-check")

    assert "passed" in out.lower()
    assert LUCA not in out
    assert (os.path.getsize(live), os.path.getmtime(live)) == before
    leftovers = [
        p for p in backup.backups_dir(live).iterdir() if not p.name.startswith("agent-")
    ]
    assert leftovers == [], "the scratch copy is removed"


def test_restore_check_fails_when_an_erased_address_survives(
    s, db, monkeypatch, capsys
):
    _contact(db, LUCA)
    backup.make_backup(db)
    DataExporter(db).gdpr_erase(LUCA)
    monkeypatch.setattr(
        "modules.journal.replay_journal",
        lambda db, path=None: {"opt_outs": 0, "blocks": 0, "lifts": 0, "erased": 0},
    )
    with pytest.raises(SystemExit) as stopped:
        _cli(s, monkeypatch, capsys, "restore-check")
    assert stopped.value.code == 1
    assert "FAILED" in capsys.readouterr().out


def test_replay_erases_again_what_retention_erased_since_the_backup(
    s, db, monkeypatch, capsys
):
    """Retention erasures are not journalled: a contact retention erased after
    the backup came back in clear, name included, until the next retention run.
    Found by the review of the counsel sheet. replay-journal now runs it."""
    from modules.retention import apply_retention

    cid = db.add_contact(db.log_search("q"), LUCA, name="Luca Verdi")
    db.conn.execute(
        "UPDATE contacts SET timestamp = datetime('now','localtime','-31 days') "
        "WHERE id = ?",
        (cid,),
    )
    db.conn.commit()
    backup.make_backup(db)
    apply_retention(db, s)
    assert db.get_contact_by_email(LUCA) is None

    restored = _restore(db, db.db_path)
    assert restored.get_contact_by_email(LUCA), "setup: the backup holds him"
    restored.close_thread_conn()
    _cli(s, monkeypatch, capsys, "replay-journal")

    after = Database(s.get_abs_db_path())
    assert after.get_contact_by_email(LUCA) is None


def test_restore_check_rehearses_retention_too(s, db, monkeypatch, capsys):
    from modules.retention import apply_retention

    cid = db.add_contact(db.log_search("q"), LUCA, name="Luca Verdi")
    db.conn.execute(
        "UPDATE contacts SET timestamp = datetime('now','localtime','-31 days') "
        "WHERE id = ?",
        (cid,),
    )
    db.conn.commit()
    backup.make_backup(db)
    apply_retention(db, s)

    out = _cli(s, monkeypatch, capsys, "restore-check")
    assert "passed" in out.lower()


def test_restore_check_fails_when_a_retention_erasure_survives(
    s, db, monkeypatch, capsys
):
    """What the live database no longer holds must not survive the replay,
    whether it was erased on request or by retention."""
    from modules.retention import apply_retention

    cid = db.add_contact(db.log_search("q"), LUCA, name="Luca Verdi")
    db.conn.execute(
        "UPDATE contacts SET timestamp = datetime('now','localtime','-31 days') "
        "WHERE id = ?",
        (cid,),
    )
    db.conn.commit()
    backup.make_backup(db)
    apply_retention(db, s)
    monkeypatch.setattr("modules.retention.apply_retention", lambda db, s: {})

    with pytest.raises(SystemExit) as stopped:
        _cli(s, monkeypatch, capsys, "restore-check")
    assert stopped.value.code == 1
