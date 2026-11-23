"""In the container, the data folders and the journal are their owner's alone.

data/ (or wherever DB_PATH points), its backups, access answers and held
opt-outs, and the journal were created with the process's default
permissions: under the usual umask, readable by every account on the host.
They hold the database, full copies of it, and the addresses of everyone who
objected.

Now (2026-11 security hardening, stage 3), on POSIX (the Docker image) every
such folder is created 0700 and the journal, rewritten or appended to,
0600. On Windows the permissions are left to IT (docs/DEPLOY.md): a folder
inherits those of the folder it is made in. The POSIX branch is tested here
on any system, by the mode each folder and file is created with.
"""

import os
import stat
from pathlib import Path

import pytest

from modules import backup, journal, private_files
from modules.database import Database, erasure_token
from modules.export import write_access_answer
from modules.optout_spool import spool_optout

ANNA = "anna.rossi@brand.de"


class Recorder:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.dirs, self.files = {}, {}

    def _ours(self, path):
        path = Path(path).resolve()
        return path if path.is_relative_to(self.root) else None

    def made_dirs(self):
        return {path.relative_to(self.root).as_posix(): m for path, m in self.dirs.items()}


@pytest.fixture
def created(monkeypatch, tmp_path):
    record = Recorder(tmp_path)
    real_mkdir, real_open = os.mkdir, os.open

    def mkdir(path, mode=0o777, *args, **kwargs):
        if record._ours(path):
            record.dirs[record._ours(path)] = mode
        return real_mkdir(path, mode, *args, **kwargs)

    def open_(path, flags, mode=0o777, *args, **kwargs):
        if record._ours(path) and flags & os.O_CREAT:
            record.files[record._ours(path)] = mode
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "open", open_)
    return record


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(private_files, "owner_only", lambda: True)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "agent.db"))


def _all_private(record, *expected):
    made = record.made_dirs()
    for folder in expected:
        assert folder in made, (folder, made)
    assert all(mode == 0o700 for mode in made.values()), made


def test_the_database_folder_is_made_owner_only(posix, created, settings_factory, tmp_path):
    s = settings_factory(db_path=str(tmp_path / "new" / "data" / "agent.db"))
    s.get_abs_db_path()
    _all_private(created, "new", "new/data")


def test_held_opt_outs_are_kept_in_owner_only_folders(posix, created, db):
    spool_optout(db.db_path, ANNA, "link")
    _all_private(created, "spool", "spool/optouts")


def test_backups_are_kept_in_an_owner_only_folder(posix, created, db):
    backup.make_backup(db)
    _all_private(created, "backups")


def test_access_answers_are_written_to_owner_only_folders(posix, created, db):
    folder = write_access_answer(
        db, ANNA, controller="Test S.r.l.", notice_url="https://example.test/privacy"
    )
    _all_private(created, "exports", f"exports/{folder.name}")


def test_unfinished_erasure_records_are_kept_owner_only(posix, created, db):
    journal.keep_pending(db.db_path, erasure_token(ANNA), None)
    _all_private(created, "spool", "spool/journal")


def test_the_journal_is_created_owner_only(posix, created, db):
    journal.append(db.db_path, "unsubscribe", email=ANNA)
    assert created.files[journal.journal_path(db.db_path)] == 0o600


def test_a_rewritten_journal_stays_owner_only(posix, created, db):
    journal.append(db.db_path, "unsubscribe", email=ANNA)
    created.files.clear()
    assert journal.trim(db.db_path, before=float("inf")) == 1
    tmp = journal.journal_path(db.db_path).with_suffix(".tmp")
    assert created.files == {tmp: 0o600}


def test_the_database_file_is_created_owner_only(posix, created, tmp_path):
    # data/ may already exist with a looser mode (a Docker bind mount is
    # made 0755), so the folder alone does not keep agent.db private.
    path = tmp_path / "agent.db"
    Database(str(path))
    assert created.files[path.resolve()] == 0o600


def test_an_existing_database_is_opened_as_it_is(posix, tmp_path):
    path = tmp_path / "agent.db"
    Database(str(path)).set_state("probe", "kept")
    assert Database(str(path)).get_state("probe") == "kept"


def test_on_windows_the_permissions_are_left_to_it(monkeypatch, created, db):
    monkeypatch.setattr(private_files, "owner_only", lambda: False)
    spool_optout(db.db_path, ANNA, "link")
    journal.append(db.db_path, "unsubscribe", email=ANNA)
    assert set(created.made_dirs().values()) == {0o777}
    assert created.files[journal.journal_path(db.db_path)] == 0o777


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX only")
def test_the_modes_on_disk(db):
    old = os.umask(0o022)
    try:
        spool_optout(db.db_path, ANNA, "link")
        journal.append(db.db_path, "unsubscribe", email=ANNA)
    finally:
        os.umask(old)
    root = Path(db.db_path).parent
    for folder in (root / "spool", root / "spool" / "optouts"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder
    assert stat.S_IMODE(journal.journal_path(db.db_path).stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX only")
def test_the_database_files_on_disk(tmp_path):
    tmp_path.chmod(0o755)
    old = os.umask(0o022)
    try:
        db = Database(str(tmp_path / "agent.db"))
        db.get_stats()
    finally:
        os.umask(old)
    for name in ("agent.db", "agent.db-wal"):
        path = tmp_path / name
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
