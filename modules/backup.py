"""Backups of the database (step G16, decision L5.7).

SQLite's online backup API copies a consistent snapshot while the agent
keeps writing. The copy is checked with PRAGMA integrity_check before it
counts, written under a temporary name and renamed into place, so a crash
mid-backup never leaves a half copy that looks whole. The newest 14 are
kept; the journal is trimmed to the oldest of them.

A backup holds what was erased after it was taken: replay-journal, run
after a restore, erases it again. The copies are not encrypted here - keep
them on an encrypted disk, and any off-machine copy too (IT, decision L5.7).
"""

import logging
import os
import sqlite3
from pathlib import Path

from modules import clock, journal
from modules.private_files import make_private_dir

logger = logging.getLogger(__name__)

BACKUP_COPIES = 14


def backups_dir(db_path) -> Path:
    return Path(db_path).resolve().parent / "backups"


def _stamp() -> str:
    return clock.now().strftime("%Y%m%d-%H%M%S")


def _integrity(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def _behind_its_journal(db_path) -> str | None:
    """Why the journal must not be trimmed now, or None. A database restored
    and not yet replayed lacks the lines since its backup; a trim to a newer
    copy would drop them, opt-outs and sends with them, and the restore guard
    (journal.unreplayed) would no longer see the gap."""
    try:
        behind = journal.unreplayed(db_path)
    except (sqlite3.Error, OSError, ValueError) as e:
        return f"may be behind it: that could not be checked ({e})"
    return f"is behind it by {behind} line(s)" if behind else None


def make_backup(db) -> Path:
    """Write a checked backup, prune to BACKUP_COPIES, trim the journal unless
    the database is behind it (_behind_its_journal)."""
    folder = make_private_dir(backups_dir(db.db_path))
    target = folder / f"agent-{_stamp()}.db"
    partial = target.with_suffix(".partial")
    source = sqlite3.connect(db.db_path)
    copy = sqlite3.connect(partial)
    try:
        source.backup(copy)
        verdict = _integrity(copy)
    finally:
        copy.close()
        source.close()
    if verdict != "ok":
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"the backup failed its integrity check: {verdict}")
    os.replace(partial, target)

    copies = sorted(folder.glob("agent-*.db"))
    for old in copies[:-BACKUP_COPIES]:
        old.unlink()
    oldest = min((p.stat().st_mtime for p in folder.glob("agent-*.db")), default=None)
    # What an erasure left pending on files another program held open:
    # finished now, if they are free (this never raises).
    journal.finish_pending(db.db_path)
    behind = _behind_its_journal(db.db_path)
    if behind:
        logger.error(
            "Not trimming the journal: the database %s. Run "
            "`py -3.15 main.py replay-journal`; a later backup trims it.",
            behind,
        )
    elif oldest is not None:
        try:
            # An hour's margin for a line written while the oldest copy was made.
            journal.trim(db.db_path, oldest - 3600)
        except (OSError, ValueError) as e:
            # Held open, or not decodable. The backup is good, and doctor's and
            # the operator's advice while a file is held is to run this.
            logger.error(
                "Could not trim the journal; its old lines wait for the next "
                "backup: %s",
                e,
            )
    db.set_state("backup:last_run", clock.now_iso())
    return target
