"""An erasure never stops half-way when a file it must change is held by another
program, and no state it leaves can hide.

On Windows a file another program holds open can be neither replaced nor
deleted: the agent or the dashboard appending an opt-out, a sync or backup tool,
an editor. An erasure rewrote the journal before it removed the person's data,
and that rewrite ends in os.replace, which nothing caught. With the journal held,
the erasure raised PermissionError after suppressing her and before removing her
data: her contact row, name and emails stayed in clear, no erasure line was
journalled, a `dsr erase` request stayed open, and the operator saw only a
traceback (MISS-02). An access answer or a held opt-out open in another program
did the same after the commit. Run again, `dsr erase` said "Already erased" and
left the files as they were, her data in clear if a request had come in under
her token.

Now the database erasure is committed first, whatever happens to the files. A
record by tokens alone is written just before that commit, so a kill at any
moment after it leaves something to report and to finish. The steps on files
follow: the journal's rewrite, tried again for a moment and reading the journal
afresh each time, the access answer and the held opt-outs. None of them stops
the erasure. The record goes once all have landed; until then doctor reports it,
the operator is told, and the next erasure, backup or replay-journal finishes
them by the tokens alone. doctor also finds a half-way erasure from any cause,
"Already erased" is said only when nothing is left, a replay while a rewrite
waits gives what it gives after it, and a backup survives a held journal.
"""

import errno
import gc
import json
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import main
from main import run_doctor_checks
from modules import backup, export, journal, optout_spool
from modules.database import Database, domain_token, erasure_token
from modules.export import DataExporter, export_folder

ANNA = "anna@brand.it"
LUCA = "luca@other.de"
MARIO = "mario@mariorossi.it"
CHECK = "Erasures finished"
ROOT = Path(__file__).resolve().parent.parent
REAL_PAUSE = journal.REWRITE_PAUSE_S
# Valid JSON nested too deep to decode: RecursionError, not ValueError.
DEEP = "[" * 5000 + "]" * 5000

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="real Windows file handles"
)


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "live" / "agent.db"))


@pytest.fixture
def db(s):
    os.makedirs(os.path.dirname(s.get_abs_db_path()), exist_ok=True)
    return Database(s.get_abs_db_path())


class _Holder:
    """Another program holding the journal open. While it does, the journal
    cannot be replaced: os.replace onto it fails, as on Windows. Appending
    still works, as beside another appender, unless appends=False (a holder
    that shares no writing, or a read-only file); reads=False refuses reading
    too (a holder that shares nothing). lets_go_after: it lets go after refusing
    that many replaces. meanwhile: what the holder does while it holds the file,
    such as appending its own line. error: what a refused replace raises."""

    def __init__(
        self,
        monkeypatch,
        db_path,
        *,
        appends=True,
        reads=True,
        lets_go_after=None,
        meanwhile=None,
        error=None,
    ):
        self.path = journal.journal_path(db_path)
        self.holding = True
        self.refused = 0
        real_replace = os.replace
        error = error or (lambda: PermissionError(13, "Access is denied"))

        def replace(src, dst, *args, **kwargs):
            refuses = lets_go_after is None or self.refused < lets_go_after
            if self.holding and refuses and Path(dst).resolve() == self.path:
                self.refused += 1
                assert self.refused < 1000, "trying without end: this would hang"
                if meanwhile:
                    meanwhile()
                raise error()
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)
        if not appends:

            def refusing_open(file, mode="r", *args, **kwargs):
                if self.holding and "a" in mode and Path(file).resolve() == self.path:
                    raise PermissionError(13, "Permission denied")
                return open(file, mode, *args, **kwargs)

            monkeypatch.setattr(journal, "open", refusing_open, raising=False)
        if not reads:
            real_read_text = Path.read_text

            def read_text(path, *args, **kwargs):
                if self.holding and path.resolve() == self.path:
                    raise PermissionError(13, "Permission denied")
                return real_read_text(path, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", read_text)
        # The tries come one after another here, with no real waiting.
        monkeypatch.setattr(journal, "REWRITE_PAUSE_S", 0, raising=False)

    def let_go(self):
        self.holding = False


class _HeldFile:
    """Another program holding a file or a folder the erasure must delete:
    the access answer, a held opt-out. reads=False: it cannot be read either."""

    def __init__(self, monkeypatch, path, *, reads=True):
        self.path = Path(path).resolve()
        self.holding = True
        real_rmtree = shutil.rmtree
        real_unlink = Path.unlink
        real_read_text = Path.read_text

        def refused(target):
            return self.holding and Path(target).resolve() == self.path

        def rmtree(target, *args, **kwargs):
            if refused(target):
                raise PermissionError(32, "The file is in use by another process")
            return real_rmtree(target, *args, **kwargs)

        def unlink(target, *args, **kwargs):
            if refused(target):
                raise PermissionError(32, "The file is in use by another process")
            return real_unlink(target, *args, **kwargs)

        monkeypatch.setattr(shutil, "rmtree", rmtree)
        monkeypatch.setattr(Path, "unlink", unlink)
        if not reads:

            def read_text(target, *args, **kwargs):
                if refused(target):
                    raise PermissionError(32, "The file is in use by another process")
                return real_read_text(target, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", read_text)

    def let_go(self):
        self.holding = False


class _RealHold:
    """A real Windows handle on a file, with the access and sharing another
    program may ask for."""

    READ, WRITE = 0x80000000, 0x40000000
    SHARE_READ, SHARE_WRITE = 1, 2

    def __init__(self, path, access, share):
        import ctypes
        from ctypes import wintypes

        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateFileW.restype = wintypes.HANDLE
        self.k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.k32.CreateFileW(
            str(path), access, share, None, 3, 0x80, None
        )
        assert self.handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()

    def close(self):
        if self.handle is not None:
            self.k32.CloseHandle(self.handle)
            self.handle = None


def _anna(db):
    """Anna, emailed, who objected: her opt-out and her company's block are
    journal lines naming her address and her domain until she is erased."""
    cid = db.add_contact(
        db.log_search("q"), ANNA, name="Anna Rossi", organization="Brand"
    )
    db.log_email(cid, ANNA, "Hello", body_preview="Hi Anna,", delivery_status="sent")
    db.record_opt_out(ANNA, "Opted out by email")
    db.block_domain("brand.it", "objection")
    return cid


def _anna_with_no_line(db):
    """Anna, emailed, who never objected: not one journal line names her."""
    cid = db.add_contact(
        db.log_search("q"), ANNA, name="Anna Rossi", organization="Brand"
    )
    db.log_email(cid, ANNA, "Hello", body_preview="Hi Anna,", delivery_status="sent")
    return cid


def _access_answer(db):
    """A full copy of her data, as `dsr export` leaves it."""
    folder = export_folder(db.db_path, ANNA)
    folder.mkdir(parents=True)
    (folder / "records.json").write_text("a full copy of her data", encoding="utf-8")
    return folder


def _erase(db, email=ANNA):
    """The erasure, and what it raised, if anything."""
    try:
        DataExporter(db).gdpr_erase(email)
    except OSError as e:  # the traceback the operator saw
        return e
    return None


def _locked(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


def _half_way(db, monkeypatch):
    """An erasure the database stopped after the suppression, and before her
    rows: suppressed by her token, everything else still in clear."""
    cid = _anna(db)
    with monkeypatch.context() as m:
        m.setattr(export, "anonymise_contact_rows", _locked)
        with pytest.raises(sqlite3.OperationalError):
            DataExporter(db).gdpr_erase(ANNA)
    assert db.is_erased(ANNA) and ANNA in journal.held_addresses(db), "setup: half-way"
    return cid


def _raw(db) -> str:
    path = journal.journal_path(db.db_path)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _kinds(db):
    return [
        (e["kind"], e.get("email") or e.get("domain") or e.get("token"))
        for e in map(json.loads, _raw(db).splitlines())
    ]


def _old_line(db, email="old@x.de"):
    """A line older than any backup: the next trim drops it."""
    with open(journal.journal_path(db.db_path), "a", encoding="utf-8") as f:
        line = {"at": "2020-01-01T00:00:00", "kind": "opt_out", "email": email}
        f.write(json.dumps(line) + "\n")


def _doctor(s):
    checks = run_doctor_checks(s, network=False)
    return next(c for c in checks if c.label == CHECK)


def _restore(db, copy=None):
    """What an operator does: put a backup, the newest, in the database's place."""
    live = db.db_path
    newest = copy or max(backup.backups_dir(live).glob("agent-*.db"))
    # Those the commands and doctor left behind too: Windows will not delete a
    # database file a connection still holds.
    db.conn.close()
    gc.collect()
    for leftover in gc.get_objects():
        if isinstance(leftover, sqlite3.Connection):
            leftover.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(live + suffix):
            os.remove(live + suffix)
    shutil.copyfile(newest, live)
    return Database(live)


def _cli(s, monkeypatch, capsys, *argv, answer="ERASE", asked=None):
    def answers(prompt=""):
        if asked is not None:
            asked.append(prompt)
        return answer

    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("builtins.input", answers)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _erasures_logged(db):
    return db.conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE action = 'GDPR_ERASURE'"
    ).fetchone()[0]


def _truncations(monkeypatch, watch):
    """Each truncation of the database's log, and what watch() saw just then."""
    seen = []
    real = Database.truncate_wal

    def truncate_wal(self):
        seen.append(watch(self))
        return real(self)

    monkeypatch.setattr(Database, "truncate_wal", truncate_wal)
    return seen


# ── The erasure itself ──


def test_an_erasure_completes_while_another_program_holds_the_journal(db, monkeypatch):
    """The verifier's reproduction (V5)."""
    cid = _anna(db)
    _Holder(monkeypatch, db.db_path)

    stopped = _erase(db)

    row = db.get_contact_by_id(cid)
    assert (row["email"], row["name"]) == (erasure_token(ANNA), None), (
        f"the erasure stopped half-way ({stopped!r}): her contact row is still held"
    )
    assert ANNA not in journal.held_addresses(db)
    assert stopped is None, "the erasure raised after removing her data"
    assert db.is_erased(ANNA)


def test_a_journal_held_for_a_moment_is_rewritten_by_trying_again(db, monkeypatch):
    _anna(db)
    holder = _Holder(monkeypatch, db.db_path, lets_go_after=2)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert holder.refused == 2, "setup: refused twice, then let go"
    assert result["journal_pending"] is False
    assert ANNA not in _raw(db) and '"brand.it"' not in _raw(db)
    assert journal.pending_rewrites(db.db_path) == []


def test_a_line_the_other_program_appends_meanwhile_is_kept(db, monkeypatch):
    """Each try reads the journal afresh: replacing it with what the first
    try read would drop the other program's line, and its opt-out with it."""
    _anna(db)
    _Holder(
        monkeypatch,
        db.db_path,
        lets_go_after=1,
        meanwhile=lambda: db.record_opt_out(LUCA, "Opted out by email"),
    )

    DataExporter(db).gdpr_erase(ANNA)

    assert ("opt_out", LUCA) in _kinds(db)
    assert ANNA not in _raw(db)


def test_a_rewrite_that_fails_leaves_no_copy_of_the_journal_behind(db, monkeypatch):
    """The copy written to replace it holds every line of the journal."""
    _anna(db)
    _Holder(monkeypatch, db.db_path)

    _erase(db)

    assert not journal.journal_path(db.db_path).with_suffix(".tmp").exists()


def test_the_tries_really_wait_and_the_wait_is_short(db, monkeypatch):
    """Long enough for an instant's handle to close, and no erasure blocked for
    hours: the real constants, the waits recorded."""
    _anna(db)
    _Holder(monkeypatch, db.db_path)
    monkeypatch.setattr(journal, "REWRITE_PAUSE_S", REAL_PAUSE)
    naps = []

    def nap(seconds):
        naps.append(seconds)
        assert len(naps) < 1000, "trying without end"

    monkeypatch.setattr(journal.time, "sleep", nap)

    DataExporter(db).gdpr_erase(ANNA)

    assert 1 <= sum(naps) <= 10, f"waited {sum(naps)} s in all"


def test_a_full_disk_does_not_stop_the_erasure(db, monkeypatch):
    """Waiting would be no use: tried once, and kept pending."""
    cid = _anna(db)
    holder = _Holder(
        monkeypatch,
        db.db_path,
        error=lambda: OSError(errno.ENOSPC, "No space left on device"),
    )

    result = DataExporter(db).gdpr_erase(ANNA)

    assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA)
    assert result["journal_pending"] is True
    assert holder.refused == 1


def test_an_undecodable_journal_does_not_stop_the_erasure(db):
    """It waits for someone to mend it: replaced, its other lines would be lost."""
    cid = _anna(db)
    path = journal.journal_path(db.db_path)
    with open(path, "ab") as f:
        f.write(b"\xff\xfe not utf-8\n")

    result = DataExporter(db).gdpr_erase(ANNA)

    assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA)
    assert result["journal_pending"] is True
    assert b"\xff\xfe not utf-8" in path.read_bytes()
    assert not path.with_suffix(".tmp").exists()


def test_a_holder_that_refuses_reading_too_does_not_stop_the_erasure(db, monkeypatch):
    cid = _anna(db)
    _Holder(monkeypatch, db.db_path, appends=False, reads=False)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA)
    assert result["journal_pending"] is True


def test_her_data_is_gone_before_the_journal_is_touched(db, monkeypatch):
    """Read through a second connection at the first refused replace: with the
    old order, the error merely caught, a kill here is the original half-way."""
    cid = _anna(db)
    seen = []

    def look():
        conn = sqlite3.connect(db.db_path)
        try:
            seen.append(
                conn.execute(
                    "SELECT email, name FROM contacts WHERE id = ?", (cid,)
                ).fetchone()
            )
        finally:
            conn.close()

    _Holder(monkeypatch, db.db_path, meanwhile=look)

    DataExporter(db).gdpr_erase(ANNA)

    assert seen[0] == (erasure_token(ANNA), None)


def test_every_later_step_runs_while_the_rewrite_is_pending(db, monkeypatch):
    """The erasure's steps after the journal: the access answer, the held
    opt-out, the accountability row, the log's old page images."""
    _anna(db)
    answer = _access_answer(db)
    optout_spool.spool_optout(db.db_path, ANNA, "one-click unsubscribe")
    assert optout_spool.pending_addresses(db.db_path) == {ANNA}
    _Holder(monkeypatch, db.db_path)
    log_state = _truncations(monkeypatch, _erasures_logged)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert result["journal_pending"] is True
    assert not answer.exists()
    assert optout_spool.pending_addresses(db.db_path) == set()
    assert _erasures_logged(db) == 1
    assert log_state == [1], "the log is emptied, after the accountability row"
    wal = Path(db.db_path + "-wal")
    assert not (wal.exists() and ANNA.encode() in wal.read_bytes())


def test_the_failure_is_logged_without_naming_her(db, monkeypatch, caplog):
    """The log says why, where the operator's message can only guess."""
    _anna(db)
    _Holder(monkeypatch, db.db_path)

    with caplog.at_level(logging.ERROR):
        DataExporter(db).gdpr_erase(ANNA)

    assert "Could not rewrite the journal" in caplog.text
    assert "Access is denied" in caplog.text
    assert ANNA not in caplog.text and "brand.it" not in caplog.text


# ── A refused erasure line ──


def test_a_refused_erasure_line_with_nothing_to_rewrite_is_kept_pending(
    s, db, monkeypatch
):
    """The journal refuses her erasure line, and no earlier line names her: there
    is nothing else to replace it for, and a restore would bring her back."""
    _anna_with_no_line(db)
    # Kept apart: a second backup in the same second would overwrite it.
    from_before = Path(db.db_path + ".from-before")
    shutil.copyfile(backup.make_backup(db), from_before)
    holder = _Holder(monkeypatch, db.db_path, appends=False)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert result["journal_pending"] is True, "the erasure line is lost, silently"
    assert _doctor(s).ok is False

    holder.let_go()
    backup.make_backup(db)
    assert ("erasure", erasure_token(ANNA)) in _kinds(db)
    assert journal.pending_rewrites(db.db_path) == []

    restored = _restore(db, from_before)
    journal.replay_journal(restored)
    assert restored.get_contact_by_email(ANNA) is None, "a restore brings her back"


def test_a_refused_erasure_line_is_written_when_the_holder_lets_go_during_the_tries(
    db, monkeypatch
):
    """The holder refuses the append, then lets go: nothing is pending then, so
    the line must be there."""
    _anna(db)
    backup.make_backup(db)
    _Holder(monkeypatch, db.db_path, appends=False, lets_go_after=2)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert result["journal_pending"] is False
    assert ("erasure", erasure_token(ANNA)) in _kinds(db)
    restored = _restore(db)
    journal.replay_journal(restored)
    assert restored.get_contact_by_email(ANNA) is None


def test_a_refused_erasure_line_after_her_lines_aged_out(s, db, monkeypatch, capsys):
    """The usual `dsr erase`, a fortnight after the request: the backups' trim
    has dropped her earlier lines, so the rewrite has nothing else to change."""
    _anna(db)
    rid = db.add_dsr(ANNA, "erasure", "manual")
    backup.make_backup(db)
    journal.trim(db.db_path, time.time() + 1)
    db.record_opt_out(LUCA, "Opted out by email")
    _Holder(monkeypatch, db.db_path, appends=False)

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "pending" in out.lower() and journal.pending_rewrites(db.db_path)
    restored = _restore(db)
    journal.replay_journal(restored)
    assert restored.get_contact_by_email(ANNA) is None


def test_a_refused_erasure_line_beside_someone_elses(s, db, monkeypatch):
    """Luca's erasure line is in the journal already: it is not hers."""
    db.add_contact(db.log_search("q"), LUCA, name="Luca", organization="Other")
    DataExporter(db).gdpr_erase(LUCA)
    _anna_with_no_line(db)
    holder = _Holder(monkeypatch, db.db_path, appends=False)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert result["journal_pending"] is True, "her erasure line is lost, silently"
    holder.let_go()
    backup.make_backup(db)
    assert ("erasure", erasure_token(ANNA)) in _kinds(db)


# ── A record a kill cannot lose ──


def test_the_record_stands_while_the_files_are_changed_and_goes_when_they_are(
    db, monkeypatch
):
    """Not only once the tries have failed: a kill during them left nothing."""
    _anna(db)
    records = []
    _Holder(
        monkeypatch,
        db.db_path,
        lets_go_after=1,
        meanwhile=lambda: records.append(len(journal.pending_rewrites(db.db_path))),
    )

    DataExporter(db).gdpr_erase(ANNA)

    assert records == [1]
    assert journal.pending_rewrites(db.db_path) == []


def test_a_record_that_cannot_be_deleted_stops_nothing(db, monkeypatch, caplog):
    """Everything landed, and the record itself is held. The erasure does not
    stop for it: finishing it again next time changes nothing."""
    _anna(db)
    folder = journal.pending_folder(db.db_path)
    real_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.parent == folder and path.suffix == ".json":
            raise PermissionError(32, "The file is in use by another process")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", unlink)
        with caplog.at_level(logging.ERROR):
            result = DataExporter(db).gdpr_erase(ANNA)

    assert result["files_pending"] == []
    assert _erasures_logged(db) == 1
    assert ANNA not in _raw(db)
    assert "still marked pending" in caplog.text
    assert len(journal.pending_rewrites(db.db_path)) == 1

    journal.finish_pending(db.db_path)

    assert journal.pending_rewrites(db.db_path) == []


def test_a_record_another_process_finished_meanwhile_is_no_failure(
    db, monkeypatch, caplog
):
    """A backup elsewhere finished it during the tries."""
    _anna(db)
    _Holder(
        monkeypatch,
        db.db_path,
        lets_go_after=1,
        meanwhile=lambda: shutil.rmtree(journal.pending_folder(db.db_path)),
    )

    with caplog.at_level(logging.ERROR):
        result = DataExporter(db).gdpr_erase(ANNA)

    assert result["files_pending"] == []
    assert _erasures_logged(db) == 1
    assert "still marked pending" not in caplog.text


@pytest.mark.parametrize(
    "step", ["block_company", "anonymise_contact_rows", "blockable_domain"]
)
def test_an_erasure_stopped_before_its_commit_leaves_no_record(db, monkeypatch, step):
    """The database locked, at the first write or at a later one: the operator
    sees the error, and there is no finished erasure to finish."""
    _anna(db)
    monkeypatch.setattr(export, step, _locked)

    with pytest.raises(sqlite3.OperationalError):
        DataExporter(db).gdpr_erase(ANNA)

    assert journal.pending_rewrites(db.db_path) == []
    assert not journal.pending_folder(db.db_path).exists()


class _KilledRightAfterTheCommit:
    """The thread's connection, in a process that dies as its commit returns."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        self._conn.commit()
        raise KeyboardInterrupt


def test_a_kill_right_after_the_commit_leaves_a_record_the_next_backup_finishes(
    s, db, monkeypatch
):
    cid = _anna(db)
    answer = _access_answer(db)
    held = optout_spool.spool_optout(db.db_path, ANNA, "one-click unsubscribe")
    real_conn = Database.conn.fget

    with monkeypatch.context() as m:
        m.setattr(
            Database,
            "conn",
            property(lambda self: _KilledRightAfterTheCommit(real_conn(self))),
        )
        with pytest.raises(KeyboardInterrupt):
            DataExporter(db).gdpr_erase(ANNA)

    assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA), "setup: committed"
    assert len(journal.pending_rewrites(db.db_path)) == 1
    assert ANNA in _raw(db) and answer.exists() and held.exists()
    assert _doctor(s).ok is False

    backup.make_backup(db)

    assert ANNA not in _raw(db) and '"brand.it"' not in _raw(db)
    assert not answer.exists() and not held.exists()
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


def test_an_interrupt_while_waiting_for_the_journal_hides_nothing(
    s, db, monkeypatch, capsys
):
    """Ctrl+C during the tries, then `dsr erase` again with the journal free: it
    said "Already erased" and left her in the journal and the answer on disk."""
    _anna(db)
    rid = db.add_dsr(ANNA, "erasure", "manual")
    answer = _access_answer(db)
    holder = _Holder(monkeypatch, db.db_path)

    def ctrl_c(_seconds):
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(journal.time, "sleep", ctrl_c)
        with pytest.raises(KeyboardInterrupt):
            _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))
    capsys.readouterr()

    assert db.get_dsr(rid)["status"] == "open"
    assert ANNA in _raw(db) and answer.exists()
    assert len(journal.pending_rewrites(db.db_path)) == 1
    assert _doctor(s).ok is False

    holder.let_go()
    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "already erased" in out.lower()
    assert db.get_dsr(rid)["status"] == "closed"
    assert ANNA not in _raw(db) and '"brand.it"' not in _raw(db)
    assert not answer.exists()
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


_ERASE_THEN_DIE = """
import sys
from modules import journal
from modules.database import Database, set_erasure_key
from modules.export import DataExporter

db_path, key, address = sys.argv[1:4]
set_erasure_key(key)
db = Database(db_path)
held = open(journal.journal_path(db_path), "a", encoding="utf-8")
print("READY", flush=True)
DataExporter(db).gdpr_erase(address)
print("DONE", flush=True)
input()
"""


@windows_only
def test_a_real_kill_during_the_tries_leaves_a_record(s, db):
    """A second process erases, holding the journal open as an appender does,
    and is killed once its commit can be seen."""
    cid = _anna(db)
    token = erasure_token(ANNA)
    child = subprocess.Popen(
        [sys.executable, "-c", _ERASE_THEN_DIE, db.db_path, s.erasure_key, ANNA],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        deadline = time.monotonic() + 60
        while db.get_contact_by_id(cid)["email"] != token:
            assert time.monotonic() < deadline, "the erasure never committed"
            time.sleep(0.01)
    finally:
        child.kill()
        child.wait()
        child.stdin.close()
        child.stdout.close()

    assert len(journal.pending_rewrites(db.db_path)) == 1
    assert ANNA in _raw(db)
    assert _doctor(s).ok is False

    backup.make_backup(db)

    assert ANNA not in _raw(db) and '"brand.it"' not in _raw(db)
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


# ── The other files: the access answer, the held opt-outs ──


def test_a_held_access_answer_does_not_stop_the_erasure_and_is_removed_later(
    s, db, monkeypatch, capsys, caplog
):
    """It stopped the erasure after its commit, the request open, and a re-run
    said "Already erased" with the full copy of her data still on disk."""
    _anna(db)
    rid = db.add_dsr(ANNA, "erasure", "manual")
    answer = _access_answer(db)
    held = optout_spool.spool_optout(db.db_path, ANNA, "one-click unsubscribe")
    file = _HeldFile(monkeypatch, answer)
    log_state = _truncations(monkeypatch, _erasures_logged)

    with caplog.at_level(logging.ERROR):
        out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert db.get_dsr(rid)["status"] == "closed"
    assert db.get_contact_by_email(ANNA) is None
    assert _erasures_logged(db) == 1
    assert log_state[0] == 1, "the erasure truncated the log, after its row"
    # Every other step has landed: only the access answer waits.
    assert ANNA not in _raw(db) and not held.exists()
    assert answer.exists() and len(journal.pending_rewrites(db.db_path)) == 1
    assert "access answer" in out and "doctor" in out.lower() and ANNA not in out
    assert "access answer" in caplog.text and "in use" in caplog.text
    assert ANNA not in caplog.text and "brand.it" not in caplog.text
    check = _doctor(s)
    assert check.ok is False and ANNA not in check.detail

    backup.make_backup(db)
    assert answer.exists(), "still held at the backup: still pending"
    assert _doctor(s).ok is False

    file.let_go()
    backup.make_backup(db)

    assert not answer.exists()
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


@pytest.mark.parametrize("reads", [True, False], ids=["deletes", "reads too"])
def test_a_held_opt_out_does_not_stop_the_erasure_and_is_removed_later(
    s, db, monkeypatch, capsys, reads
):
    """A file that cannot be read may be hers: it is not passed over as nobody's."""
    _anna(db)
    answer = _access_answer(db)
    held = optout_spool.spool_optout(db.db_path, ANNA, "one-click unsubscribe")
    file = _HeldFile(monkeypatch, held, reads=reads)

    out = _cli(s, monkeypatch, capsys, "gdpr-erase", ANNA)

    assert f"Erased: {erasure_token(ANNA)}" in out
    assert "opt-out" in out and "doctor" in out.lower() and ANNA not in out
    assert db.get_contact_by_email(ANNA) is None
    assert _erasures_logged(db) == 1
    assert ANNA not in _raw(db) and not answer.exists()
    assert held.exists() and len(journal.pending_rewrites(db.db_path)) == 1
    assert _doctor(s).ok is False

    file.let_go()
    backup.make_backup(db)

    assert not held.exists()
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


def test_one_held_opt_out_does_not_keep_her_others(db, monkeypatch):
    _anna(db)
    a = optout_spool.spool_optout(db.db_path, ANNA, "one-click unsubscribe")
    b = optout_spool.spool_optout(db.db_path, ANNA, "reply")
    assert a != b, "setup: two files"
    first = next(iter(optout_spool.spool_dir(db.db_path).glob("*.json")))
    other = b if first == a else a
    _HeldFile(monkeypatch, first)

    DataExporter(db).gdpr_erase(ANNA)

    assert first.exists()
    assert not other.exists(), "one held file kept her other opt-out on disk"


def test_a_held_journal_does_not_stop_a_backup_finishing_the_other_files(
    db, monkeypatch
):
    """The access answer let go, the journal held at the backup: the answer is
    removed all the same."""
    _anna(db)
    answer = _access_answer(db)
    file = _HeldFile(monkeypatch, answer)
    DataExporter(db).gdpr_erase(ANNA)
    assert answer.exists() and journal.pending_rewrites(db.db_path), "setup"
    file.let_go()
    _old_line(db)
    _Holder(monkeypatch, db.db_path)

    backup.make_backup(db)

    assert not answer.exists(), "the held journal kept her access answer on disk"
    assert journal.pending_rewrites(db.db_path) == []


def test_held_opt_outs_are_matched_by_the_token_alone(db):
    """After the commit, and in a record, the address is held nowhere to match
    by. A file being replayed is left alone: its replay finds the erasure."""
    hers = optout_spool.spool_optout(db.db_path, "Anna@Brand.it ", "one-click")
    claimed = optout_spool.spool_optout(db.db_path, ANNA, "one-click")
    claimed = claimed.rename(claimed.with_suffix(".claimed"))
    his = optout_spool.spool_optout(db.db_path, LUCA, "one-click")
    no_address = optout_spool.spool_optout(db.db_path, None, "one-click", token="abc")
    folder = optout_spool.spool_dir(db.db_path)
    odd = [folder / "list.json", folder / "number.json", folder / "undecodable.json"]
    odd[0].write_text("[5]", encoding="utf-8")
    odd[1].write_text('{"email": 5}', encoding="utf-8")
    odd[2].write_bytes(b"\xff\xfe")

    removed = optout_spool.discard_held(db.db_path, erasure_token(ANNA))

    assert removed == 1
    assert not hers.exists()
    assert claimed.exists() and his.exists() and no_address.exists()
    assert all(path.exists() for path in odd)


def test_a_held_opt_out_claimed_meanwhile_is_nothing_left(db, monkeypatch):
    """Another process's replay took it between the listing and the read: that
    is no file held open, and nothing to keep a record for."""
    held = optout_spool.spool_optout(db.db_path, ANNA, "one-click unsubscribe")
    real_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == held:
            path.rename(path.with_suffix(".claimed"))
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    assert optout_spool.discard_held(db.db_path, erasure_token(ANNA)) == 0


def test_a_record_cannot_steer_what_is_deleted(db):
    """A record's token names a folder to delete: one that is not a token is
    no record at all, wherever it points."""
    other = export.exports_dir(db.db_path) / "other"
    other.mkdir(parents=True)
    folder = journal.pending_folder(db.db_path)
    folder.mkdir(parents=True)
    (folder / "steer.json").write_text(
        json.dumps({"token": "erased_x/../other@redacted.invalid"}), encoding="utf-8"
    )

    journal.finish_pending(db.db_path)

    assert other.exists()
    assert (folder / "steer.json").exists()


def test_a_failure_during_a_replay_is_logged_and_reported_with_no_record(
    db, monkeypatch, caplog
):
    """Journalling is off in a replay. What it could not delete is said, and
    leaves no record: the replay's own erasures are not journalled either."""
    cid = _anna(db)
    answer = _access_answer(db)
    _HeldFile(monkeypatch, answer)
    db.journaling = False

    with caplog.at_level(logging.ERROR):
        result = DataExporter(db).gdpr_erase(ANNA)

    assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA)
    assert _erasures_logged(db) == 1
    assert result.get("files_pending") == ["the access answer"]
    assert result.get("pending_recorded") is False
    assert result["journal_pending"] is False
    assert not journal.pending_folder(db.db_path).exists()
    assert "access answer" in caplog.text and ANNA not in caplog.text


def test_a_replay_writes_no_record_when_all_goes_well(db):
    _anna(db)
    before = _raw(db)
    db.journaling = False

    result = DataExporter(db).gdpr_erase(ANNA)

    assert result.get("files_pending") == []
    assert not journal.pending_folder(db.db_path).exists()
    assert _raw(db) == before, "a replay does not rewrite the journal it replays"


# ── Kept pending, reported, finished ──


def test_a_rewrite_that_cannot_be_done_now_is_kept_pending_by_tokens_only(
    s, db, monkeypatch
):
    _anna(db)
    _Holder(monkeypatch, db.db_path)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert result["journal_pending"] is True
    # Appending is not refused beside another handle: the erasure is in.
    assert ("erasure", erasure_token(ANNA)) in _kinds(db)
    files = list(journal.pending_folder(db.db_path).iterdir())
    assert files, "the rewrite is recorded as pending"
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert ANNA not in text and "brand.it" not in text
    check = _doctor(s)
    assert (
        check.ok is False and ANNA not in check.detail and "brand" not in check.detail
    )
    assert check.blocking is False, "doctor warns: the erasure is done, sending is safe"


def test_the_next_backup_finishes_the_rewrite_once_the_journal_is_free(
    s, db, monkeypatch
):
    _anna(db)
    holder = _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    backup.make_backup(db)
    assert _doctor(s).ok is False, "still held at the backup: still pending"

    holder.let_go()
    backup.make_backup(db)

    raw = _raw(db)
    assert ANNA not in raw and '"brand.it"' not in raw
    assert erasure_token(ANNA) in raw and domain_token("brand.it") in raw
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


def test_the_next_erasure_finishes_an_earlier_one_left_pending(db, monkeypatch):
    _anna(db)
    holder = _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    holder.let_go()
    db.add_contact(db.log_search("q"), LUCA, organization="Other")
    db.record_opt_out(LUCA, "Opted out by email")

    DataExporter(db).gdpr_erase(LUCA)

    raw = _raw(db)
    assert ANNA not in raw and LUCA not in raw and '"brand.it"' not in raw
    assert journal.pending_rewrites(db.db_path) == []


def test_one_backup_finishes_every_pending_erasure(db, monkeypatch):
    _anna(db)
    db.add_contact(db.log_search("q"), LUCA, organization="Other")
    db.record_opt_out(LUCA, "Opted out by email")
    holder = _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    DataExporter(db).gdpr_erase(LUCA)
    assert len(journal.pending_rewrites(db.db_path)) == 2
    holder.let_go()

    backup.make_backup(db)

    assert journal.pending_rewrites(db.db_path) == []
    assert ANNA not in _raw(db) and LUCA not in _raw(db)


def test_a_journal_refused_once_is_not_waited_for_again(db, monkeypatch):
    """The wait is once, not once per record; and what does not need the journal
    is still done for all of them."""
    _anna(db)
    db.add_contact(db.log_search("q"), LUCA, organization="Other")
    db.record_opt_out(LUCA, "Opted out by email")
    _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    DataExporter(db).gdpr_erase(LUCA)
    assert len(journal.pending_rewrites(db.db_path)) == 2
    answers = _access_answer(db), export_folder(db.db_path, LUCA)
    answers[1].mkdir(parents=True)
    naps = []
    monkeypatch.setattr(journal.time, "sleep", naps.append)

    journal.finish_pending(db.db_path)

    assert len(naps) == journal.REWRITE_ATTEMPTS - 1
    assert not answers[0].exists() and not answers[1].exists()
    assert len(journal.pending_rewrites(db.db_path)) == 2


def test_an_erasure_line_the_journal_refused_is_written_once_it_is_free(
    db, monkeypatch
):
    _anna(db)
    holder = _Holder(monkeypatch, db.db_path, appends=False)
    DataExporter(db).gdpr_erase(ANNA)
    assert ("erasure", erasure_token(ANNA)) not in _kinds(db), "setup: refused"

    holder.let_go()
    backup.make_backup(db)

    assert ("erasure", erasure_token(ANNA)) in _kinds(db)
    assert ANNA not in _raw(db)


# ── "Already erased" hides nothing ──


def test_a_request_under_her_token_finishes_a_half_way_erasure(
    s, db, monkeypatch, capsys
):
    """The database stopped an erasure after the suppression. A request then
    comes in, registered under her token, and `dsr erase` said "Already erased"
    with her rows in clear. Now it asks as usual, and erases her."""
    cid = _half_way(db, monkeypatch)
    rid = db.add_dsr(ANNA, "erasure", "manual")
    assert db.get_dsr(rid)["email"] == erasure_token(ANNA), "setup: under her token"
    asked = []

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid), asked=asked)

    assert "already erased" not in out.lower()
    assert len(asked) == 1 and "ERASE" in asked[0] and ANNA not in asked[0]
    row = db.get_contact_by_id(cid)
    assert (row["email"], row["name"]) == (erasure_token(ANNA), None)
    assert ANNA not in journal.held_addresses(db)
    assert (db.get_dsr(rid)["status"], db.get_dsr(rid)["outcome"]) == (
        "closed",
        "erased",
    )
    assert ANNA not in out
    assert ANNA not in _raw(db)


def test_refusing_to_confirm_a_half_way_erasure_erases_nothing(
    s, db, monkeypatch, capsys
):
    cid = _half_way(db, monkeypatch)
    rid = db.add_dsr(ANNA, "erasure", "manual")

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid), answer="no")

    assert "nothing erased" in out.lower()
    assert db.get_contact_by_id(cid)["email"] == ANNA
    assert db.get_dsr(rid)["status"] == "open"


def test_already_erased_is_not_said_while_a_file_is_still_held(
    s, db, monkeypatch, capsys
):
    """Finished by token, closed, the log emptied, and the truth told."""
    _anna(db)
    holder = _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    rid = db.add_dsr(ANNA, "erasure", "manual")
    assert db.get_dsr(rid)["email"] == erasure_token(ANNA), "setup: under her token"
    log_state = _truncations(monkeypatch, lambda d: d.get_dsr(rid)["status"])
    asked = []

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid), asked=asked)

    assert "already erased" not in out.lower()
    assert "pending" in out.lower() and "doctor" in out.lower() and ANNA not in out
    assert asked == [], "there is nothing to erase, so nothing to confirm"
    row = db.get_dsr(rid)
    assert (row["status"], row["outcome"]) == ("closed", "erased")
    assert log_state[-1:] == ["closed"], "the log is emptied after the request closes"
    assert _doctor(s).ok is False
    assert journal.pending_rewrites(db.db_path)

    holder.let_go()
    backup.make_backup(db)
    assert ANNA not in _raw(db) and '"brand.it"' not in _raw(db)
    assert _doctor(s).ok is True


def test_already_erased_finishes_a_held_access_answer_once_it_is_free(
    s, db, monkeypatch, capsys
):
    _anna(db)
    answer = _access_answer(db)
    file = _HeldFile(monkeypatch, answer)
    DataExporter(db).gdpr_erase(ANNA)
    assert answer.exists() and journal.pending_rewrites(db.db_path), "setup: waiting"
    file.let_go()
    rid = db.add_dsr(ANNA, "erasure", "manual")

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "already erased" in out.lower()
    assert not answer.exists()
    assert journal.pending_rewrites(db.db_path) == []
    row = db.get_dsr(rid)
    assert (row["status"], row["outcome"]) == ("closed", "already erased")


def test_already_erased_finishes_by_the_token_with_no_record_to_go_by(
    s, db, monkeypatch, capsys
):
    """What an interrupted erasure left before records were written before the
    commit: only the token to go by, which is enough for all but the company."""
    _anna(db)
    answer = _access_answer(db)
    held = optout_spool.spool_optout(db.db_path, ANNA, "one-click unsubscribe")
    holder = _Holder(monkeypatch, db.db_path)
    file = _HeldFile(monkeypatch, answer)
    held_file = _HeldFile(monkeypatch, held)
    DataExporter(db).gdpr_erase(ANNA)
    shutil.rmtree(journal.pending_folder(db.db_path))
    holder.let_go()
    file.let_go()
    held_file.let_go()
    rid = db.add_dsr(ANNA, "erasure", "manual")

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "already erased" in out.lower()
    assert ANNA not in _raw(db)
    assert not answer.exists() and not held.exists()
    assert journal.pending_rewrites(db.db_path) == []


def test_already_erased_keeps_a_record_of_what_it_cannot_finish_by_the_token(
    s, db, monkeypatch, capsys
):
    """No record to go by, and the journal still held: one is made, or nothing
    would report it and nothing would finish it."""
    _anna(db)
    holder = _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    shutil.rmtree(journal.pending_folder(db.db_path))
    rid = db.add_dsr(ANNA, "erasure", "manual")

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "already erased" not in out.lower()
    assert "pending" in out.lower() and "doctor" in out.lower()
    pending = journal.pending_rewrites(db.db_path)
    assert [p["token"] for p in pending] == [erasure_token(ANNA)]
    assert _doctor(s).ok is False

    holder.let_go()
    backup.make_backup(db)
    assert ANNA not in _raw(db)
    assert journal.pending_rewrites(db.db_path) == []


def test_with_no_record_already_erased_is_not_said_either(s, db, monkeypatch, capsys):
    _anna(db)
    _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    shutil.rmtree(journal.pending_folder(db.db_path).parent)
    _no_room_for_a_record(db)
    rid = db.add_dsr(ANNA, "erasure", "manual")

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "already erased" not in out.lower()
    assert "could not be recorded" in out and "doctor" not in out.lower()
    assert (db.get_dsr(rid)["status"], db.get_dsr(rid)["outcome"]) == (
        "closed",
        "erased",
    )


def test_an_already_erased_request_with_nothing_left_writes_nothing(
    s, db, monkeypatch, capsys
):
    """The usual second request: quiet, closed, and the journal not replaced."""
    _anna(db)
    DataExporter(db).gdpr_erase(ANNA)
    rid = db.add_dsr(ANNA, "erasure", "manual")
    inode = os.stat(journal.journal_path(db.db_path)).st_ino
    _Holder(monkeypatch, db.db_path)
    seen = _truncations(monkeypatch, lambda d: d.get_dsr(rid)["status"])

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "already erased" in out.lower() and "pending" not in out.lower()
    assert os.stat(journal.journal_path(db.db_path)).st_ino == inode
    assert not list(journal.pending_folder(db.db_path).glob("*"))
    assert seen == ["closed"], "the log was not emptied after closing the request"


def _his_erasure_unrecorded(s, db, monkeypatch, capsys):
    """Mario, whose company is named after him, erased while the journal was
    held and the disk had no room for a record: nothing will finish it. Another
    company's block, from no erasure and named first, stays as it is."""
    db.block_domain("other.de", "objection")
    db.add_contact(db.log_search("q"), MARIO, name="Mario Rossi", organization="MR")
    db.record_opt_out(MARIO, "Opted out by email")
    db.block_domain("mariorossi.it", "objection")
    holder = _Holder(monkeypatch, db.db_path)
    _no_room_for_a_record(db)
    out = _cli(s, monkeypatch, capsys, "gdpr-erase", MARIO)
    assert "could not be recorded" in out and '"mariorossi.it"' in _raw(db), "setup"
    journal.pending_folder(db.db_path).parent.unlink()
    return holder, db.add_dsr(MARIO, "erasure", "email")


def test_already_erased_is_not_said_while_the_journal_still_names_his_company(
    s, db, monkeypatch, capsys
):
    """A later request from him comes in under his token, with no record to name
    his company by. The database keeps it only as a token: the journal must too."""
    holder, rid = _his_erasure_unrecorded(s, db, monkeypatch, capsys)
    holder.let_go()

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "already erased" in out.lower()
    assert "mariorossi.it" not in _raw(db) and MARIO not in _raw(db)
    assert ("block", domain_token("mariorossi.it")) in _kinds(db)
    assert ("block", "other.de") in _kinds(db), "a block from no erasure is kept"
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


def test_a_record_made_by_the_token_keeps_his_company(s, db, monkeypatch, capsys):
    """The journal still held at that request: the record it makes names his
    company's token, so the backup that finishes it leaves the domain in no line."""
    holder, rid = _his_erasure_unrecorded(s, db, monkeypatch, capsys)

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "already erased" not in out.lower() and "pending" in out.lower()
    [record] = journal.pending_rewrites(db.db_path)
    assert record["domain_token"] == domain_token("mariorossi.it")
    holder.let_go()
    backup.make_backup(db)
    assert "mariorossi.it" not in _raw(db) and MARIO not in _raw(db)
    assert ("block", "other.de") in _kinds(db)


PIA = "pia@piaverdi.it"


def _erased_unrecorded(s, db, monkeypatch, capsys, who, company):
    """Erased while the journal was held and the disk had no room for a record."""
    db.add_contact(db.log_search("q"), who, name="X Y", organization="O")
    db.record_opt_out(who, "Opted out by email")
    db.block_domain(company, "objection")
    holder = _Holder(monkeypatch, db.db_path)
    _no_room_for_a_record(db)
    out = _cli(s, monkeypatch, capsys, "gdpr-erase", who)
    assert "could not be recorded" in out and f'"{company}"' in _raw(db), "setup"
    journal.pending_folder(db.db_path).parent.unlink()
    holder.let_go()


def test_two_erasures_with_no_record_each_then_a_request_from_each(
    s, db, monkeypatch, capsys
):
    """Pia, then Mario, each erased with nothing to finish it. His request comes
    first: it must not take her company for his and leave his in clear."""
    _erased_unrecorded(s, db, monkeypatch, capsys, PIA, "piaverdi.it")
    _erased_unrecorded(s, db, monkeypatch, capsys, MARIO, "mariorossi.it")

    his, hers = (
        db.add_dsr(MARIO, "erasure", "email"),
        db.add_dsr(PIA, "erasure", "email"),
    )

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(his))

    assert "already erased" in out.lower()
    assert "mariorossi.it" not in _raw(db) and MARIO not in _raw(db)
    # Her address waits for her own request; her company is a leftover too.
    assert '"piaverdi.it"' not in _raw(db)

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(hers))

    assert "already erased" in out.lower()
    assert "piaverdi.it" not in _raw(db) and "mariorossi.it" not in _raw(db)
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


def test_doctor_reports_an_erased_company_still_named_and_a_backup_ends_it(
    s, db, monkeypatch, capsys
):
    _erased_unrecorded(s, db, monkeypatch, capsys, PIA, "piaverdi.it")
    _erased_unrecorded(s, db, monkeypatch, capsys, MARIO, "mariorossi.it")

    check = _doctor(s)

    assert check.ok is False and "2 erased company domain(s)" in check.detail
    assert "piaverdi.it" not in check.detail and "mariorossi.it" not in check.detail
    backup.make_backup(db)
    assert '"piaverdi.it"' not in _raw(db) and '"mariorossi.it"' not in _raw(db)
    assert _doctor(s).ok is True


def test_a_token_no_erasure_of_ours_made_is_closed_and_nothing_else(
    s, db, monkeypatch, capsys
):
    """A request can arrive from any address that looks like a token. It
    chooses no folder and earns no line in the journal."""
    rid = db.add_dsr("nobody@redacted.invalid", "erasure", "manual")

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "already erased" in out.lower()
    assert db.get_dsr(rid)["status"] == "closed"
    assert "erasure" not in [kind for kind, _ in _kinds(db)]
    assert journal.pending_rewrites(db.db_path) == []


# ── doctor ──


def test_doctor_counts_what_is_pending_and_names_unreadable_records_apart(
    s, db, monkeypatch
):
    _anna(db)
    db.add_contact(db.log_search("q"), LUCA, organization="Other")
    db.record_opt_out(LUCA, "Opted out by email")
    _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    DataExporter(db).gdpr_erase(LUCA)
    folder = journal.pending_folder(db.db_path)
    (folder / "left-by-hand.json").write_text("{trunc", encoding="utf-8")

    check = _doctor(s)

    assert check.ok is False and check.blocking is False
    assert "2 erasure(s)" in check.detail
    assert "left-by-hand.json" in check.detail
    # Unreadable: named by file, kept apart, and never counted with the others.
    assert "3 erasure(s)" not in check.detail
    assert ANNA not in check.detail and LUCA not in check.detail
    # A full disk or a read-only file gets here as well.
    assert "another program held" not in check.detail
    assert "may" in check.detail


def test_doctor_counts_a_half_way_erasure_from_any_cause(s, db, monkeypatch):
    """Her token on the suppression list, her address still in clear: no record
    says so, as the erasure never reached its commit."""
    _half_way(db, monkeypatch)
    assert journal.pending_rewrites(db.db_path) == [], "setup: nothing recorded"

    check = _doctor(s)

    assert check.ok is False and check.blocking is False
    assert "1 address(es)" in check.detail
    assert "dsr erase" in check.detail and "gdpr-erase" in check.detail
    assert ANNA not in check.detail and "brand" not in check.detail

    DataExporter(db).gdpr_erase(ANNA)
    assert _doctor(s).ok is True


def test_doctor_counts_only_a_token_suppressed_as_an_erasure(s, db):
    """The reason says it: this token is here for another, so no erasure of
    hers began."""
    db.add_contact(db.log_search("q"), ANNA, organization="Brand")
    with db._transaction() as conn:
        conn.execute(
            "INSERT INTO suppression_list (email, reason) VALUES (?, 'bounce')",
            (erasure_token(ANNA),),
        )

    assert _doctor(s).ok is True


def test_doctor_says_it_could_not_check_without_a_key(
    s, db, settings_factory, tmp_path, monkeypatch
):
    """doctor runs without a key, to report it: it cannot match a token then,
    and "yes" would claim a check it did not make. Records need no key."""
    keyless = settings_factory(db_path=s.get_abs_db_path(), ERASURE_KEY="")

    check = _doctor(keyless)
    assert check.ok is None and check.blocking is False
    assert "could not check" in check.detail

    _anna(db)
    _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    check = _doctor(keyless)
    assert check.ok is False
    assert "1 erasure(s)" in check.detail and "could not check" in check.detail


def test_doctor_says_it_could_not_check_a_database_it_cannot_read(s, db, monkeypatch):
    monkeypatch.setattr(journal, "half_way_erasures", _locked)

    check = _doctor(s)

    assert check.ok is None and check.blocking is False
    assert "could not check" in check.detail and "database is locked" in check.detail


def test_doctor_is_content_with_nothing_to_report(s, db):
    check = _doctor(s)
    assert (check.ok, check.detail) == (True, "yes")


# ── The operator is told ──


def test_dsr_erase_closes_the_request_and_says_the_rewrite_is_pending(
    s, db, monkeypatch, capsys
):
    """Every `dsr erase` has a line to rewrite: registering the request
    journalled her address."""
    rid = db.add_dsr(ANNA, "erasure", "manual")
    _Holder(monkeypatch, db.db_path)

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert db.get_dsr(rid)["status"] == "closed"
    assert "pending" in out.lower() and "doctor" in out.lower()
    assert ANNA not in out


def test_gdpr_erase_says_the_rewrite_is_pending(s, db, monkeypatch, capsys):
    _anna(db)
    _Holder(monkeypatch, db.db_path)

    out = _cli(s, monkeypatch, capsys, "gdpr-erase", ANNA)

    assert f"Erased: {erasure_token(ANNA)}" in out
    assert "pending" in out.lower() and "doctor" in out.lower()
    assert ANNA not in out


def test_a_completed_rewrite_says_nothing_of_the_kind(s, db, monkeypatch, capsys):
    _anna(db)

    out = _cli(s, monkeypatch, capsys, "gdpr-erase", ANNA)

    assert "pending" not in out.lower()
    assert _doctor(s).ok is True


def _no_room_for_a_record(db):
    # "spool" is a file, so the folder for the record cannot be made
    journal.pending_folder(db.db_path).parent.write_text("", encoding="utf-8")


def test_a_record_that_cannot_be_written_does_not_stop_the_erasure(db, monkeypatch):
    """It is written just before the commit: raising there, it would stop the
    erasure with her rows in clear."""
    cid = _anna(db)
    _Holder(monkeypatch, db.db_path)
    _no_room_for_a_record(db)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA)
    assert _erasures_logged(db) == 1
    assert result["journal_pending"] is True
    assert result.get("pending_recorded") is False


@pytest.mark.parametrize("command", ["gdpr-erase", "dsr erase"])
def test_with_no_record_the_operator_is_told_the_truth(
    s, db, monkeypatch, capsys, caplog, command
):
    """Nothing will report it and nothing will finish it, so neither is promised:
    what is left is the operator's to run again."""
    _anna(db)
    rid = db.add_dsr(ANNA, "erasure", "manual")
    _Holder(monkeypatch, db.db_path)
    _no_room_for_a_record(db)
    argv = (
        ["gdpr-erase", ANNA] if command == "gdpr-erase" else ["dsr", "erase", str(rid)]
    )

    with caplog.at_level(logging.ERROR):
        out = _cli(s, monkeypatch, capsys, *argv)

    assert "could not be recorded" in out
    assert "gdpr-erase <address>" in out
    assert "doctor" not in out.lower() and "backup" not in out.lower()
    assert ANNA not in out
    assert "Could not keep a record" in caplog.text and ANNA not in caplog.text


def test_gdpr_erase_again_finishes_what_could_not_be_recorded(
    s, db, monkeypatch, capsys
):
    """As the operator is told to, once the disk has room."""
    _anna(db)
    holder = _Holder(monkeypatch, db.db_path)
    _no_room_for_a_record(db)
    _cli(s, monkeypatch, capsys, "gdpr-erase", ANNA)
    assert ANNA in _raw(db), "setup: not finished"
    holder.let_go()
    journal.pending_folder(db.db_path).parent.unlink()

    out = _cli(s, monkeypatch, capsys, "gdpr-erase", ANNA)

    assert "could not be recorded" not in out and "pending" not in out.lower()
    assert ANNA not in _raw(db) and '"brand.it"' not in _raw(db)
    assert journal.pending_rewrites(db.db_path) == []


def test_an_erasure_line_appended_beside_a_holder_is_all_a_restore_may_have(
    db, monkeypatch
):
    """Neither the rewrite nor a record could be written. The line appended when
    she was suppressed, which works beside another handle, still erases her."""
    _anna(db)
    backup.make_backup(db)
    _Holder(monkeypatch, db.db_path)
    _no_room_for_a_record(db)
    DataExporter(db).gdpr_erase(ANNA)

    restored = _restore(db)
    assert restored.get_contact_by_email(ANNA), "setup: the backup holds her"
    journal.replay_journal(restored)

    assert restored.get_contact_by_email(ANNA) is None


# ── Restoring while a rewrite is pending ──


def test_a_restore_while_the_rewrite_is_pending_erases_her_again(db, monkeypatch):
    _anna(db)
    backup.make_backup(db)
    _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)

    restored = _restore(db)
    assert restored.get_contact_by_email(ANNA), "setup: the backup holds her"
    journal.replay_journal(restored)

    assert restored.get_contact_by_email(ANNA) is None
    assert restored.is_erased(ANNA)
    assert ANNA not in journal.held_addresses(restored)


def test_a_restore_erases_her_again_when_the_journal_refused_her_erasure_line(
    s, db, monkeypatch, capsys
):
    """Only the pending record names her erasure, by its token."""
    _anna(db)
    backup.make_backup(db)
    _Holder(monkeypatch, db.db_path, appends=False)
    DataExporter(db).gdpr_erase(ANNA)
    assert ("erasure", erasure_token(ANNA)) not in _kinds(db), "setup: refused"

    assert "passed" in _cli(s, monkeypatch, capsys, "restore-check").lower()

    restored = _restore(db)
    journal.replay_journal(restored)
    assert restored.get_contact_by_email(ANNA) is None
    assert restored.is_erased(ANNA)


def test_replay_journal_finishes_the_rewrite_once_the_journal_is_free(db, monkeypatch):
    _anna(db)
    backup.make_backup(db)
    holder = _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    holder.let_go()

    restored = _restore(db)
    journal.replay_journal(restored)

    assert ANNA not in _raw(restored) and '"brand.it"' not in _raw(restored)
    assert journal.pending_rewrites(restored.db_path) == []
    assert restored.get_contact_by_email(ANNA) is None


def _what_a_replay_left(db):
    with db._transaction() as conn:
        return {
            "domains": sorted(
                row[0] for row in conn.execute("SELECT domain FROM suppressed_domains")
            ),
            "suppressed": sorted(
                tuple(row)
                for row in conn.execute("SELECT email, reason FROM suppression_list")
            ),
            "requests": sorted(
                tuple(row)
                for row in conn.execute(
                    "SELECT email, kind, status, outcome FROM dsr_requests"
                )
            ),
            "held": sorted(journal._held(conn)),
        }


@pytest.mark.parametrize("asked", [False, True], ids=["his company", "his request"])
def test_a_replay_while_pending_equals_a_replay_after_the_rewrite(
    db, monkeypatch, asked
):
    """His plain lines were replayed as they stood: his company's domain, which
    can name him, and his name in how a request ended went into the restored
    database in clear, and stayed there once the journal was rewritten."""
    backup.make_backup(db)  # before anything about him
    db.add_contact(
        db.log_search("q"), MARIO, name="Mario Rossi", organization="Mario Rossi"
    )
    db.record_opt_out(MARIO, "Opted out by email")
    db.block_domain("mariorossi.it", "objection")
    if asked:
        # A request held in clear gets him erased again, and his block keyed.
        rid = db.add_dsr(MARIO, "access", "manual")
        db.close_dsr(rid, "Access answer emailed to Mario Rossi")
    holder = _Holder(monkeypatch, db.db_path)
    assert DataExporter(db).gdpr_erase(MARIO)["journal_pending"] is True

    restored = _restore(db)
    journal.replay_journal(restored)
    while_pending = _what_a_replay_left(restored)

    assert "mariorossi.it" not in while_pending["domains"]
    assert "Mario Rossi" not in str(while_pending["requests"])
    assert journal.pending_rewrites(restored.db_path), "setup: still pending"
    assert "mariorossi.it" in _raw(restored), "setup: the journal not rewritten"

    holder.let_go()
    restored = _restore(restored)
    journal.replay_journal(restored)

    assert journal.pending_rewrites(restored.db_path) == [], "setup: rewritten now"
    assert _what_a_replay_left(restored) == while_pending


def test_a_pending_token_reaches_the_suppression_list(db, monkeypatch):
    """The backup is older than her contact row, so a replay has no row to erase
    again: only the pending record can put her token back on the list."""
    backup.make_backup(db)
    _anna(db)
    _Holder(monkeypatch, db.db_path, appends=False)
    DataExporter(db).gdpr_erase(ANNA)

    restored = _restore(db)
    journal.replay_journal(restored)

    assert restored.is_erased(ANNA)


def test_restore_check_changes_nothing_live(s, db, monkeypatch, capsys):
    """It replays the live journal onto a scratch copy. The journal is free and
    a rewrite is pending: all it needs to touch something live."""
    _anna(db)
    backup.make_backup(db)
    holder = _Holder(monkeypatch, db.db_path)
    DataExporter(db).gdpr_erase(ANNA)
    holder.let_go()
    before = journal.journal_path(db.db_path).read_bytes()
    pending = journal.pending_rewrites(db.db_path)
    assert pending, "setup: pending"

    out = _cli(s, monkeypatch, capsys, "restore-check")

    assert "passed" in out.lower()
    assert journal.journal_path(db.db_path).read_bytes() == before
    assert journal.pending_rewrites(db.db_path) == pending


# ── Bad data ──


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{trunc",
        "{}",
        '{"token": null}',
        '{"token": 5}',
        '{"token": "not a token"}',
        "null",
        "[]",
        '"x"',
        "5",
        pytest.param(DEEP, id="nested too deep"),
    ],
)
def test_an_unreadable_record_stops_nothing_and_doctor_names_it(s, db, content):
    """No file written here looks like this. Backup, replay and the next erasure
    all stopped on one, and the backup doctor advised could never clear it."""
    folder = journal.pending_folder(db.db_path)
    folder.mkdir(parents=True)
    (folder / "left-by-hand.json").write_text(content, encoding="utf-8")

    backup.make_backup(db)
    journal.replay_journal(db)
    DataExporter(db).gdpr_erase(LUCA)

    check = _doctor(s)
    assert check.ok is False and "left-by-hand.json" in check.detail
    assert "0 erasure(s)" not in check.detail
    assert (folder / "left-by-hand.json").exists()


@pytest.mark.parametrize(
    "line",
    [
        "5",
        "[]",
        "null",
        '"x"',
        '{"kind": "opt_out", "email": 5}',
        '{"domain": []}',
        pytest.param(DEEP, id="nested too deep"),
    ],
)
def test_a_journal_line_that_is_no_event_is_skipped_and_logged(db, caplog, line):
    """As an undecodable line is: a rewrite or a replay would stop on it, after
    the erasure's commit."""
    _anna(db)
    with open(journal.journal_path(db.db_path), "a", encoding="utf-8") as f:
        f.write(line + "\n")

    with caplog.at_level(logging.ERROR):
        events = journal.read(journal.journal_path(db.db_path))

    assert all(isinstance(e, dict) for e in events)
    assert len(events) == 2, "her opt-out and her company's block"
    assert "journal line" in caplog.text

    result = DataExporter(db).gdpr_erase(ANNA)

    assert result["journal_pending"] is False
    assert ANNA not in _raw(db)
    journal.replay_journal(db)


# ── A line appended while the journal is being replaced ──


def _appends_after_the_first_read(db, monkeypatch):
    """Another process appending between the read and the replace."""
    real_read = journal.read
    done = []

    def read(path):
        events = real_read(path)
        if not done:
            done.append(True)
            db.record_opt_out(LUCA, "Opted out by email")
        return events

    monkeypatch.setattr(journal, "read", read)


def test_a_line_appended_between_the_read_and_the_replace_is_kept(db, monkeypatch):
    _anna(db)
    _appends_after_the_first_read(db, monkeypatch)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert result["journal_pending"] is False
    assert ("opt_out", LUCA) in _kinds(db), "his opt-out went with the old file"
    assert ANNA not in _raw(db)


def test_the_trim_keeps_a_line_appended_between_its_read_and_its_replace(
    db, monkeypatch
):
    _old_line(db)
    _appends_after_the_first_read(db, monkeypatch)

    assert journal.trim(db.db_path, time.time() - 3600) == 1

    assert ("opt_out", LUCA) in _kinds(db), "his opt-out went with the old file"
    assert "old@x.de" not in _raw(db)


def test_a_journal_that_keeps_growing_is_left_pending_not_chased_for_ever(
    db, monkeypatch
):
    _anna(db)
    path = journal.journal_path(db.db_path)
    real_read = journal.read
    reads = []

    def read(target):
        events = real_read(target)
        reads.append(1)
        assert len(reads) < 1000, "reading without end"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "opt_out", "email": LUCA}) + "\n")
        return events

    monkeypatch.setattr(journal, "read", read)

    result = DataExporter(db).gdpr_erase(ANNA)

    assert result["journal_pending"] is True
    assert not path.with_suffix(".tmp").exists()


# ── A backup and a held journal ──


@pytest.mark.parametrize("reads", [True, False], ids=["replaces", "reads too"])
def test_a_backup_survives_a_held_journal_and_its_old_lines_wait(
    s, db, monkeypatch, capsys, caplog, reads
):
    """The operator is told to run a backup while it may well still be held, and
    it raised, with the run unrecorded, doctor warning of a missing backup too."""
    _anna(db)
    _old_line(db)
    holder = _Holder(monkeypatch, db.db_path, reads=reads)
    DataExporter(db).gdpr_erase(ANNA)

    with caplog.at_level(logging.ERROR):
        out = _cli(s, monkeypatch, capsys, "backup")

    assert "Backup written" in out
    assert list(backup.backups_dir(db.db_path).glob("agent-*.db"))
    assert db.get_state("backup:last_run")
    assert "trim" in caplog.text and ANNA not in caplog.text
    holder.let_go()
    assert "old@x.de" in _raw(db), "its lines wait for the next backup"

    backup.make_backup(db)

    assert "old@x.de" not in _raw(db)
    assert journal.pending_rewrites(db.db_path) == []


def test_a_backup_survives_an_undecodable_journal(db):
    _old_line(db)
    with open(journal.journal_path(db.db_path), "ab") as f:
        f.write(b"\xff\xfe not utf-8\n")

    assert backup.make_backup(db).exists()

    assert db.get_state("backup:last_run")
    assert b"old@x.de" in journal.journal_path(db.db_path).read_bytes()


def test_a_backup_survives_a_journal_line_nested_too_deep(db):
    """Skipped as unreadable, so the trim goes on without it."""
    _old_line(db)
    with open(journal.journal_path(db.db_path), "a", encoding="utf-8") as f:
        f.write(DEEP + "\n")

    assert backup.make_backup(db).exists()

    assert db.get_state("backup:last_run")
    assert "old@x.de" not in _raw(db)


def test_dsr_erase_survives_a_journal_line_nested_too_deep(s, db, monkeypatch, capsys):
    _anna(db)
    rid = db.add_dsr(ANNA, "erasure", "manual")
    with open(journal.journal_path(db.db_path), "a", encoding="utf-8") as f:
        f.write(DEEP + "\n")

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert "pending" not in out.lower()
    assert db.get_dsr(rid)["status"] == "closed"
    assert _erasures_logged(db) == 1
    assert ANNA not in _raw(db)


def test_a_backup_survives_any_os_error_from_the_trim(db, monkeypatch):
    """A full disk, not only a file held open."""
    _anna(db)
    _old_line(db)
    _Holder(
        monkeypatch,
        db.db_path,
        error=lambda: OSError(errno.ENOSPC, "No space left on device"),
    )

    backup.make_backup(db)

    assert "old@x.de" in _raw(db), "setup: the trim was refused"


def test_git_ignores_the_journals_temporary_copy():
    """It holds every line of the journal, should a crash leave it behind."""
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/journal.tmp" in ignored


# ── Real handles, no simulation ──


@windows_only
@pytest.mark.parametrize(
    "access, share",
    [
        (_RealHold.READ, _RealHold.SHARE_READ | _RealHold.SHARE_WRITE),
        (_RealHold.READ, _RealHold.SHARE_READ),
        (_RealHold.READ | _RealHold.WRITE, 0),
    ],
    ids=["a reader sharing all", "a reader denying writers", "sharing nothing"],
)
def test_a_real_handle_on_the_journal(s, db, monkeypatch, access, share):
    cid = _anna(db)
    path = journal.journal_path(db.db_path)
    monkeypatch.setattr(journal, "REWRITE_PAUSE_S", 0)
    hold = _RealHold(path, access, share)
    try:
        result = DataExporter(db).gdpr_erase(ANNA)
        assert result["journal_pending"] is True
        assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA)
        assert not path.with_suffix(".tmp").exists()
        assert len(journal.pending_rewrites(db.db_path)) == 1
        assert _doctor(s).ok is False
        assert backup.make_backup(db).exists(), "the advice holds while it is held"
        assert db.get_state("backup:last_run")
        assert len(journal.pending_rewrites(db.db_path)) == 1
    finally:
        hold.close()

    backup.make_backup(db)

    assert ANNA not in _raw(db) and ("erasure", erasure_token(ANNA)) in _kinds(db)
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


@windows_only
def test_a_real_read_only_journal(s, db, monkeypatch):
    cid = _anna(db)
    path = journal.journal_path(db.db_path)
    monkeypatch.setattr(journal, "REWRITE_PAUSE_S", 0)
    os.chmod(path, stat.S_IREAD)
    try:
        result = DataExporter(db).gdpr_erase(ANNA)
        assert result["journal_pending"] is True
        assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA)
    finally:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)

    backup.make_backup(db)

    assert ANNA not in _raw(db)
    assert journal.pending_rewrites(db.db_path) == []


@windows_only
def test_a_real_handle_let_go_during_the_tries(db, monkeypatch):
    _anna(db)
    hold = _RealHold(
        journal.journal_path(db.db_path),
        _RealHold.READ,
        _RealHold.SHARE_READ | _RealHold.SHARE_WRITE,
    )
    monkeypatch.setattr(journal.time, "sleep", lambda _seconds: hold.close())
    try:
        result = DataExporter(db).gdpr_erase(ANNA)
    finally:
        hold.close()

    assert result["journal_pending"] is False
    assert ANNA not in _raw(db)
    assert journal.pending_rewrites(db.db_path) == []


@windows_only
def test_a_real_reader_denying_writers_refuses_the_line_of_someone_with_no_other(
    s, db, monkeypatch, capsys
):
    """The journal names only Luca; Excel or a sync tool holds it. Her erasure
    line was lost, doctor said all was well, and a restore brought her back."""
    monkeypatch.setattr(journal, "REWRITE_PAUSE_S", 0)
    db.record_opt_out(LUCA, "Opted out by email")
    _anna_with_no_line(db)
    backup.make_backup(db)
    hold = _RealHold(
        journal.journal_path(db.db_path), _RealHold.READ, _RealHold.SHARE_READ
    )
    try:
        result = DataExporter(db).gdpr_erase(ANNA)
    finally:
        hold.close()

    assert result["journal_pending"] is True
    assert _doctor(s).ok is False
    assert "passed" in _cli(s, monkeypatch, capsys, "restore-check").lower()
    restored = _restore(db)
    journal.replay_journal(restored)
    assert restored.get_contact_by_email(ANNA) is None
    assert ("erasure", erasure_token(ANNA)) in _kinds(restored)
    assert _doctor(s).ok is True


@windows_only
def test_a_real_handle_on_the_access_answer(s, db, monkeypatch, capsys):
    _anna(db)
    rid = db.add_dsr(ANNA, "erasure", "manual")
    answer = _access_answer(db)
    with open(answer / "records.json", encoding="utf-8"):
        out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))
        assert db.get_dsr(rid)["status"] == "closed"
        assert "access answer" in out and ANNA not in out
        assert (answer / "records.json").exists()
        assert len(journal.pending_rewrites(db.db_path)) == 1

    backup.make_backup(db)

    assert not answer.exists()
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True


@windows_only
def test_a_real_handle_on_a_held_opt_out(s, db, monkeypatch, capsys):
    _anna(db)
    path = optout_spool.spool_optout(db.db_path, ANNA, "one-click unsubscribe")
    with open(path, encoding="utf-8"):
        out = _cli(s, monkeypatch, capsys, "gdpr-erase", ANNA)
        assert "opt-out" in out and ANNA not in out
        assert path.exists() and len(journal.pending_rewrites(db.db_path)) == 1

    backup.make_backup(db)

    assert not path.exists()
    assert journal.pending_rewrites(db.db_path) == []
    assert _doctor(s).ok is True
