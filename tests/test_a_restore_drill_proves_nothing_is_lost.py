"""A restore drill proves a backup restores without losing an opt-out.

restore-check rehearsed the replay and looked only for someone erased who came
back. Nothing checked the other way: that every opt-out, company block,
erasure and held opt-out the live database and the journal know of is in the
restored copy. A journal that missed a line (a full disk, a write refused
while another program held the file) made a restore email someone who had
objected, and no drill would have said so (scenario 4).

Now `main.py restore-drill`:
- takes a fresh backup with modules.backup (or drills the newest, or a named
  file), copies it into a new temporary folder with the journal and the held
  files, and restores it there as replay-journal and the next start would;
- passes only if the restored copy holds every suppressed address and token,
  every company block, every erasure and every held opt-out the live database,
  the journal and the held files know of, holds no one erased in clear, and
  was made under the same ERASURE_KEY;
- prints counts, never an address; exits 1 on FAIL;
- writes nothing live but one line in the activity log, deletes the folder,
  and stops, saying so, while another program holds the database's lock.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

import main
from modules import backup, journal, optout_spool, restore_drill
from modules.database import Database
from modules.export import DataExporter

ANNA = "anna.rossi@brandco.de"
LUCA = "luca.verdi@other.de"
COLLEAGUE = "marco.bianchi@brandco.de"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "live" / "agent.db"))


@pytest.fixture
def db(s):
    os.makedirs(os.path.dirname(s.get_abs_db_path()), exist_ok=True)
    return Database(s.get_abs_db_path())


@pytest.fixture
def scratch_folders(monkeypatch):
    """Every temporary folder the drill makes."""
    made = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        made.append(Path(real(*args, **kwargs)))
        return str(made[-1])

    monkeypatch.setattr(restore_drill.tempfile, "mkdtemp", recording)
    return made


def _contact(db, email):
    return db.add_contact(db.log_search("q"), email, organization="Brand")


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _failing(s, monkeypatch, capsys, *argv):
    with pytest.raises(SystemExit) as stopped:
        _cli(s, monkeypatch, capsys, *argv)
    assert stopped.value.code == 1
    return capsys.readouterr().out


def _drill_lines(db):
    raw = sqlite3.connect(db.db_path)
    try:
        return raw.execute(
            "SELECT details, status FROM activity_log WHERE action = 'RESTORE_DRILL'"
        ).fetchall()
    finally:
        raw.close()


def _row(out, label):
    """The checked and missing counts the report prints for one label."""
    line = next(line for line in out.splitlines() if line.strip().startswith(label))
    checked, missing = line.split()[-2:]
    return int(checked), int(missing)


def _files(folder: Path) -> dict:
    """Every file under folder, by its path, with a hash of its bytes."""
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def _tables(path) -> dict:
    """Every row of every table, as a logical copy of the database."""
    raw = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        names = [
            n
            for (n,) in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {n: raw.execute(f"SELECT * FROM {n}").fetchall() for n in names}
    finally:
        raw.close()


# ── It passes ──


def test_a_fresh_backup_with_everything_kept_passes(
    s, db, monkeypatch, capsys, scratch_folders
):
    _contact(db, ANNA)
    _contact(db, LUCA)
    db.record_opt_out(ANNA, "Opted out by email")
    db.block_domain("third.de", "objection")
    DataExporter(db).gdpr_erase(LUCA)
    before = set(backup.backups_dir(db.db_path).glob("agent-*.db"))

    out = _cli(s, monkeypatch, capsys, "restore-drill")

    assert "Restore drill PASSED" in out
    assert "fresh backup" in out
    assert len(set(backup.backups_dir(db.db_path).glob("agent-*.db")) - before) == 1
    assert _row(out, "opt-outs") == (1, 0)
    assert _row(out, "erasures") == (1, 0)
    assert _row(out, "company blocks")[1] == 0
    assert ANNA not in out and LUCA not in out
    ((details, status),) = _drill_lines(db)
    assert details.startswith("PASS") and status == "info"
    assert ANNA not in details and LUCA not in details
    assert scratch_folders and not any(p.exists() for p in scratch_folders)


def test_an_opt_out_the_backup_missed_is_brought_back_by_the_journal(
    s, db, monkeypatch, capsys
):
    _contact(db, ANNA)
    backup.make_backup(db)
    db.record_opt_out(ANNA, "Opted out by email")

    out = _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill PASSED" in out
    assert "1 opt-out(s)" in out, "the journal put it back"
    assert _row(out, "opt-outs") == (1, 0)


def test_an_erased_person_in_the_backup_is_erased_again(s, db, monkeypatch, capsys):
    _contact(db, LUCA)
    backup.make_backup(db)
    DataExporter(db).gdpr_erase(LUCA)

    out = _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill PASSED" in out
    assert "1 erasure(s)" in out
    assert "Erased and back in clear: 0" in out
    assert LUCA not in out


def test_a_held_opt_out_is_applied_to_the_restored_copy(s, db, monkeypatch, capsys):
    held = optout_spool.spool_optout(db.db_path, ANNA, "one-click", company=True)
    kept = held.read_bytes()

    out = _cli(s, monkeypatch, capsys, "restore-drill")

    assert "Restore drill PASSED" in out
    assert _row(out, "held opt-outs") == (1, 0)
    assert _row(out, "company blocks") == (1, 0)
    assert held.read_bytes() == kept, "the live held file is left for the agent"


# ── It fails ──


@pytest.mark.parametrize(
    "lost, label",
    [
        (
            lambda db: db.record_opt_out(ANNA, "Opted out by email"),
            "opt-outs",
        ),
        (lambda db: db.block_domain("third.de", "objection"), "company blocks"),
    ],
    ids=["opt-out", "company-block"],
)
def test_what_the_journal_lost_since_the_backup_fails_the_drill(
    s, db, monkeypatch, capsys, lost, label
):
    _contact(db, ANNA)
    backup.make_backup(db)
    db.journaling = False
    lost(db)

    out = _failing(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill FAILED" in out
    assert _row(out, label) == (1, 1)
    assert "missing from the restored copy" in out
    ((details, status),) = _drill_lines(db)
    assert details.startswith("FAIL") and status == "error"


def test_a_held_opt_out_the_restore_did_not_apply_fails(s, db, monkeypatch, capsys):
    optout_spool.spool_optout(db.db_path, ANNA, "one-click")
    monkeypatch.setattr(
        restore_drill.optout_spool, "replay_optout_spool", lambda db, resolve: 0
    )

    out = _failing(s, monkeypatch, capsys, "restore-drill")

    assert _row(out, "held opt-outs") == (1, 1)


def test_an_erased_person_who_comes_back_fails(s, db, monkeypatch, capsys):
    _contact(db, LUCA)
    backup.make_backup(db)
    DataExporter(db).gdpr_erase(LUCA)
    monkeypatch.setattr(
        restore_drill.journal,
        "replay_journal",
        lambda db, path=None: {"opt_outs": 0, "blocks": 0, "lifts": 0, "erased": 0},
    )

    out = _failing(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "back in clear" in out
    assert LUCA not in out


def test_a_backup_made_under_another_key_fails(s, db, monkeypatch, capsys):
    path = backup.make_backup(db)
    raw = sqlite3.connect(path)
    raw.execute(
        "UPDATE agent_state SET value = 'another-key-0000' "
        "WHERE key = 'erasure_key_fingerprint'"
    )
    raw.commit()
    raw.close()

    out = _failing(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "another ERASURE_KEY" in out
    assert "Erasure key:   differs" in out


def test_a_damaged_backup_fails(s, db, monkeypatch, capsys, tmp_path):
    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(b"not a database at all" * 100)

    out = _failing(s, monkeypatch, capsys, "restore-drill", "--backup", str(damaged))

    assert "integrity check" in out


def test_a_backup_file_that_is_not_there_stops_it(s, db, monkeypatch, capsys, tmp_path):
    out = _failing(
        s, monkeypatch, capsys, "restore-drill", "--backup", str(tmp_path / "x.db")
    )
    assert "No backup file" in out and "Nothing was changed" in out


def test_newest_with_no_backup_stops_it(s, db, monkeypatch, capsys):
    out = _failing(s, monkeypatch, capsys, "restore-drill", "--newest")
    assert "No backup" in out


# ── It changes nothing live ──


def test_the_live_files_are_byte_identical_apart_from_the_log_line(
    s, db, monkeypatch, capsys, scratch_folders
):
    """--newest takes no backup, so the drill's one write is its activity line,
    caught here: then every file in the live data folder, the database, its
    journal, a pending erasure's record and a held opt-out, is byte for byte
    what it was."""
    _contact(db, ANNA)
    _contact(db, LUCA)
    backup.make_backup(db)
    db.record_opt_out(ANNA, "Opted out by email")
    DataExporter(db).gdpr_erase(LUCA)
    optout_spool.spool_optout(db.db_path, "held@brandco.de", "one-click")
    db.truncate_wal()
    db.close_thread_conn()
    data = Path(db.db_path).parent
    before = _files(data)
    assert {"agent.db", "journal.jsonl"} <= set(before), "setup"
    logged = []
    monkeypatch.setattr(
        restore_drill, "log_result", lambda live, report: logged.append(report)
    )

    out = _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill PASSED" in out
    assert _files(data) == before
    assert len(logged) == 1 and logged[0].passed
    assert all(data not in p.parents for p in scratch_folders)


def test_the_activity_line_is_the_only_change_to_the_live_database(
    s, db, monkeypatch, capsys
):
    _contact(db, ANNA)
    backup.make_backup(db)
    db.record_opt_out(ANNA, "Opted out by email")
    db.close_thread_conn()
    before = _tables(db.db_path)

    _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    after = _tables(db.db_path)
    added = after.pop("activity_log")[len(before.pop("activity_log")) :]
    numbered = after.pop("sqlite_sequence")
    assert [n for n in numbered if n[0] != "activity_log"] == before.pop(
        "sqlite_sequence"
    )
    assert after == before
    assert [row[2] for row in added] == ["RESTORE_DRILL"]


def test_a_locked_database_stops_the_drill_and_says_so(
    s, db, monkeypatch, capsys, scratch_folders
):
    monkeypatch.setattr(restore_drill, "LOCK_WAIT_S", 0.1)
    holder = sqlite3.connect(db.db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        out = _failing(s, monkeypatch, capsys, "restore-drill")
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert "locked" in out and "Nothing was changed" in out
    assert not list(backup.backups_dir(db.db_path).glob("agent-*.db"))
    assert scratch_folders == [], "it stopped before restoring anything"
    assert _drill_lines(db) == []


def test_with_the_live_database_gone_it_checks_against_the_journal(
    s, db, monkeypatch, capsys, tmp_path
):
    """After a disk loss the backup can still be drilled before it is put back:
    the journal and the held files say what it must hold."""
    _contact(db, ANNA)
    path = backup.make_backup(db)
    off_machine = tmp_path / "copy.db"
    off_machine.write_bytes(path.read_bytes())
    db.record_opt_out(ANNA, "Opted out by email")
    db.close_thread_conn()
    for suffix in ("", "-wal", "-shm"):
        Path(db.db_path + suffix).unlink(missing_ok=True)

    out = _cli(s, monkeypatch, capsys, "restore-drill", "--backup", str(off_machine))

    assert "Restore drill PASSED" in out
    assert "missing - compared with the journal" in out
    assert _row(out, "opt-outs") == (1, 0)
    assert "there is no live database" in out
    assert not Path(db.db_path).exists(), "it creates no live database"


def test_a_damaged_live_database_is_reported_and_the_backup_still_drilled(
    s, db, monkeypatch, capsys
):
    _contact(db, ANNA)
    backup.make_backup(db)
    db.record_opt_out(ANNA, "Opted out by email")
    db.close_thread_conn()
    for suffix in ("-wal", "-shm"):
        Path(db.db_path + suffix).unlink(missing_ok=True)
    Path(db.db_path).write_bytes(b"ransomware was here" * 200)

    out = _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill PASSED" in out
    assert "unreadable" in out and "compared with the journal" in out
    assert _row(out, "opt-outs") == (1, 0)
    assert "not logged" in out


def test_a_fresh_backup_of_a_damaged_database_is_refused(s, db, monkeypatch, capsys):
    db.close_thread_conn()
    Path(db.db_path).write_bytes(b"ransomware was here" * 200)

    out = _failing(s, monkeypatch, capsys, "restore-drill")

    assert "no fresh backup can be taken" in out


def test_with_the_live_database_gone_an_erased_person_back_fails(
    s, db, monkeypatch, capsys, tmp_path
):
    _contact(db, LUCA)
    off_machine = tmp_path / "copy.db"
    off_machine.write_bytes(backup.make_backup(db).read_bytes())
    DataExporter(db).gdpr_erase(LUCA)
    db.close_thread_conn()
    for suffix in ("", "-wal", "-shm"):
        Path(db.db_path + suffix).unlink(missing_ok=True)
    monkeypatch.setattr(
        restore_drill.journal,
        "replay_journal",
        lambda db, path=None: {"opt_outs": 0, "blocks": 0, "lifts": 0, "erased": 0},
    )

    out = _failing(
        s, monkeypatch, capsys, "restore-drill", "--backup", str(off_machine)
    )

    assert "1 erased address(es) or company domain(s) back in clear" in out


def _company_blocked_then_a_colleague_erased(db, tmp_path, *, lifted=False):
    """A backup that blocks brandco.de in clear; then someone at brandco.de it
    never held is added and erased, which keeps the company only as its token
    (and, lifted, the block is then lifted). Returns an off-machine copy of
    the backup."""
    _contact(db, ANNA)
    db.block_domain("brandco.de", "objection")
    off_machine = tmp_path / "copy.db"
    off_machine.write_bytes(backup.make_backup(db).read_bytes())
    _contact(db, COLLEAGUE)
    DataExporter(db).gdpr_erase(COLLEAGUE)
    if lifted:
        assert db.lift_domain_block("brandco.de") == "keyed", "setup"
    return off_machine


def _plain_blocks(path):
    raw = sqlite3.connect(path)
    try:
        return [
            d
            for (d,) in raw.execute("SELECT domain FROM suppressed_domains")
            if not d.startswith("domain_")
        ]
    finally:
        raw.close()


@pytest.mark.parametrize(
    "lifted, block_line_lost",
    [(False, False), (True, False), (True, True)],
    ids=["blocked", "lifted", "lifted-block-line-lost"],
)
def test_the_replay_keeps_an_erased_colleagues_company_only_as_its_token(
    s, db, monkeypatch, capsys, tmp_path, lifted, block_line_lost
):
    """The replay erases again only the people the backup holds. A colleague
    added and erased since is not one of them, so the backup's plain block of
    their company stayed in clear, and after a lift stayed blocked too, also
    when the journal refused the erasure's block line and kept only the lift."""
    off_machine = _company_blocked_then_a_colleague_erased(db, tmp_path, lifted=lifted)
    if block_line_lost:
        path = journal.journal_path(db.db_path)
        kept = [e for e in journal.read(path) if e.get("kind") != "block"]
        path.write_text("".join(json.dumps(e) + "\n" for e in kept), encoding="utf-8")
    restored = tmp_path / "restored" / "agent.db"
    restored.parent.mkdir()
    restored.write_bytes(off_machine.read_bytes())
    copy = Database(str(restored))
    assert _plain_blocks(restored) == ["brandco.de"], "setup"

    journal.replay_journal(copy, journal.journal_path(db.db_path))
    copy.close_thread_conn()

    assert _plain_blocks(restored) == []
    assert copy.company_blocked(ANNA) is not lifted
    out = _cli(s, monkeypatch, capsys, "restore-drill", "--backup", str(off_machine))
    assert "Restore drill PASSED" in out
    assert "Erased and back in clear: 0" in out


@pytest.mark.parametrize("lifted", [False, True], ids=["blocked", "lifted"])
def test_an_erased_company_back_in_clear_fails_with_the_live_database_gone(
    s, db, monkeypatch, capsys, tmp_path, lifted
):
    """Only the journal's token says the company was erased once the live
    database is gone, or once its block was lifted."""
    off_machine = _company_blocked_then_a_colleague_erased(db, tmp_path, lifted=lifted)
    db.close_thread_conn()
    for suffix in ("", "-wal", "-shm"):
        Path(db.db_path + suffix).unlink(missing_ok=True)
    monkeypatch.setattr(
        restore_drill.journal, "_drop_plain_block", lambda db, token: None
    )

    out = _failing(
        s, monkeypatch, capsys, "restore-drill", "--backup", str(off_machine)
    )

    assert "1 erased address(es) or company domain(s) back in clear" in out
    assert "brandco" not in out


def test_its_summary_line_names_no_one(s, db):
    _contact(db, ANNA)
    db.record_opt_out(ANNA, "Opted out by email")
    report = restore_drill.run_drill(s)
    assert report.passed
    assert ANNA not in report.summary() and "brandco" not in report.summary()
