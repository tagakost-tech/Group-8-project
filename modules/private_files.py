"""Folders and files that hold personal data, readable by their owner only.

The database's folder (data/ by default), its backups, access answers and
held opt-outs, and the journal hold addresses and full copies of the
database. On POSIX (the Docker image) they are created 0700 and the journal
0600, so another account on the host cannot read them, whatever its umask.
On Windows a new folder takes the permissions of the folder it is made in:
IT sets those on the install folder (docs/DEPLOY.md).
"""

import contextlib
import os
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def owner_only() -> bool:
    """True where this module sets the permissions itself."""
    return os.name == "posix"


def make_private_dir(path) -> Path:
    """Create path and any missing parent, each owner-only on POSIX. A folder
    that already exists is left as it is."""
    path = Path(path)
    if not owner_only():
        path.mkdir(parents=True, exist_ok=True)
        return path
    missing = [folder for folder in (path, *path.parents) if not folder.exists()]
    for folder in reversed(missing):
        # Another process may make it between the check and here.
        with contextlib.suppress(FileExistsError):
            folder.mkdir(mode=PRIVATE_DIR_MODE)
    return path


def make_private_file(path) -> Path:
    """Create an empty file owner-only on POSIX, unless it already exists.

    For a file another library creates, such as SQLite's database: it keeps
    the mode of a file that is there, and SQLite gives its -wal and -shm
    files the database's mode. A missing folder is left for that library to
    report."""
    path = Path(path)
    if owner_only() and path.parent.is_dir():
        with contextlib.suppress(FileExistsError):
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, PRIVATE_FILE_MODE))
    return path


def private_opener(path, flags):
    """An opener for open(): a file it creates is owner-only on POSIX."""
    if owner_only():
        return os.open(path, flags, PRIVATE_FILE_MODE)
    return os.open(path, flags)
