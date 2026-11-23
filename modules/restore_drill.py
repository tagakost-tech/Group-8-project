"""The restore drill: a backup restored where it can do no harm, and proof that
the restore loses nothing it must keep (scenario 4: a restore that loses
opt-outs).

restore-check rehearses the replay and looks for anyone erased who came back.
The drill asks the other half too: is every opt-out, company block, address
block, bounce, erasure and held opt-out the live database and the journal know
of in the restored copy? A backup that misses one is fine as long as the
journal brings it back; a journal that lost one is how a restore emails
someone who objected, a blocked company or a dead address. What is lost is
counted by kind. The same goes for every email sent: one the restored copy
does not know is an email the agent would send a second time.

The backup (a fresh one by default, made by modules.backup) is copied into a
new temporary folder with the journal and the held files beside it, and
restored there exactly as replay-journal and the next start would restore it.
The live database is only read, through read-only connections, and its
journal and held files only copied. The one thing the drill writes to it is a
line in the activity log, once the folder is gone.
"""

import contextlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from modules import backup, clock, journal, optout_spool
from modules.company_block import blockable_domain
from modules.database import (
    BOUNCE_REASONS,
    ERASURE_KEY_FINGERPRINT,
    POSSIBLY_SENT_STATUSES_SQL,
    SENT_STATUSES_SQL,
    Database,
    domain_token,
    erasure_key_fingerprint,
    erasure_token,
    is_erasure_token,
    token_of,
)
from modules.private_files import make_private_dir

# How long the drill waits for another program's write lock on the live
# database before it says so and stops.
LOCK_WAIT_S = 2.0

ACTIVITY = "RESTORE_DRILL"

# The label of the check on emails sent.
SENDS = "sends"


class DatabaseLocked(Exception):
    """Another program holds the live database's write lock."""


class NoBackup(Exception):
    """There is no backup to restore, or none can be taken."""


@dataclass
class Check:
    label: str
    checked: int
    missing: int


@dataclass
class DrillReport:
    backup: Path
    how: str
    live: str
    integrity: str = "not checked"
    key: str = "not checked"
    guard: str = "not checked"
    replayed: dict = field(default_factory=dict)
    held_applied: int = 0
    checks: list[Check] = field(default_factory=list)
    in_clear: int = 0
    problems: list[str] = field(default_factory=list)
    leftover: Path | None = None
    logged: str = "not logged"

    @property
    def passed(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        """The activity log's line: counts and the backup's name, no one's
        address and no path."""
        counted = ", ".join(f"{c.checked} {c.label}" for c in self.checks)
        verdict = "PASS" if self.passed else "FAIL - " + "; ".join(self.problems)
        return (
            f"{verdict}. {self.how} {self.backup.name}; checked {counted or 'nothing'}"
        )

    def sends(self) -> str:
        """How many emails went out since the backup, and whether the restored
        copy knows every one: those the replay put back, and those it left
        unknown, which the agent would send again."""
        check = next((c for c in self.checks if c.label == SENDS), None)
        if check is None:
            return "not checked"
        since = self.replayed.get("sends", 0) + check.missing
        if check.missing:
            return (
                f"{since} send(s) since the backup, {check.missing} missing "
                "after the replay"
            )
        return f"{since} send(s) since the backup, all restored"


def _locked(error: sqlite3.Error) -> bool:
    text = str(error).lower()
    return "locked" in text or "busy" in text


def _connect(path, mode: str, timeout: float = LOCK_WAIT_S) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"{Path(path).resolve().as_uri()}?mode={mode}", uri=True, timeout=timeout
    )
    conn.isolation_level = None
    return conn


@contextlib.contextmanager
def _guard(live: Path):
    """Raise DatabaseLocked while another program holds the write lock (taken
    for an instant, nothing written); else stay open while the drill reads the
    live database. The last connection to close removes the files SQLite keeps
    beside the database (-wal, -shm), which a read-only one cannot."""
    conn = _connect(live, "rw")
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as e:
            if _locked(e):
                raise DatabaseLocked(str(e)) from None
            raise
        yield
    finally:
        conn.close()


def _as_block(domain: str) -> str:
    """A company block as its token, the one form both kinds of row share."""
    return domain if domain.startswith("domain_") else domain_token(domain)


def _stored_fingerprint(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM agent_state WHERE key = ?", (ERASURE_KEY_FINGERPRINT,)
    ).fetchone()
    return row[0] if row else None


@dataclass
class _Expected:
    """What a restore must keep, as the live database and its files say."""

    opt_outs: set = field(default_factory=set)
    address_blocks: set = field(default_factory=set)
    bounces: set = field(default_factory=set)
    erased: set = field(default_factory=set)
    blocks: set = field(default_factory=set)
    erased_companies: set = field(default_factory=set)
    # Companies (by token) the journal names in clear after an erasure:
    # blocked or lifted again since, which writes the domain in clear live too.
    named_again: set = field(default_factory=set)
    held: set = field(default_factory=set)
    held_tokens: set = field(default_factory=set)
    # Each email sent, by its Message-ID, or by whose it was and when for one
    # that has none (_send_key).
    sends: set = field(default_factory=set)
    fingerprint: str | None = None

    def suppressed_as(self, reason: str) -> set:
        """Where an address suppressed for this reason is counted."""
        if reason == "gdpr_erasure":
            return self.erased
        if reason == "blocked_via_dashboard":
            return self.address_blocks
        if reason in BOUNCE_REASONS:
            return self.bounces
        return self.opt_outs


def _send_key(event: dict) -> tuple | None:
    """What one send line is known by, or None for a line the replay skips."""
    if journal.send_line(event, set()) is None:
        return None
    if event.get("id"):
        return ("id", event["id"])
    return ("at", token_of(event.get("email") or event["token"]), event["sent"])


def _from_live(live: Path, expected: _Expected) -> None:
    """The live database's suppressions, blocks, sends and key, in one read."""
    conn = _connect(live, "ro")
    try:
        conn.execute("BEGIN")
        for email, reason in conn.execute(
            "SELECT LOWER(email), reason FROM suppression_list"
        ):
            expected.suppressed_as(reason).add(email)
        for (domain,) in conn.execute("SELECT LOWER(domain) FROM suppressed_domains"):
            expected.blocks.add(_as_block(domain))
            if domain.startswith("domain_"):
                expected.erased_companies.add(domain)
        # Each send by its Message-ID. A row with none is named by the
        # journal's own line, by whose it was and when, or is older than the
        # column, as a database not opened since then is: every backup holds
        # those.
        if any(
            c[1] == "message_id" for c in conn.execute("PRAGMA table_info(email_log)")
        ):
            for (message_id,) in conn.execute(
                "SELECT message_id FROM email_log "
                f"WHERE delivery_status IN {SENT_STATUSES_SQL} "
                "AND message_id IS NOT NULL AND TRIM(message_id) != ''"
            ):
                expected.sends.add(("id", message_id))
        expected.fingerprint = _stored_fingerprint(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()


def _from_journal(live: Path, expected: _Expected) -> None:
    """What the journal holds, in order: a lift undoes the block before it,
    and an unblock the address block before it. Added to what the live
    database holds, never taken from it: a block made again after a lift,
    whose line the journal lost, stays expected. A company named only by its
    token was erased, whether it is blocked now or not, and must not be back
    in clear even when the live database is gone."""
    path = journal.journal_path(live)
    seen = _Expected()
    for event in journal.read(path):
        kind = event.get("kind")
        who = (event.get("email") or event.get("token") or "").strip().lower()
        if kind == "opt_out" and who:
            seen.opt_outs.add(who)
        elif kind == "address_block" and who:
            seen.address_blocks.add(who)
        elif kind == "address_unblock" and who:
            seen.address_blocks.discard(who)
        elif kind == "suppress" and who:
            seen.suppressed_as(event.get("reason") or "").add(who)
        elif kind == "erasure" and event.get("token"):
            seen.erased.add(event["token"])
        elif kind == "send":
            key = _send_key(event)
            if key:
                seen.sends.add(key)
        elif kind in ("block", "lift"):
            domain = (event.get("domain") or event.get("token") or "").strip().lower()
            if not domain:
                continue
            if domain.startswith("domain_"):
                # A company line kept as its token: an erasure's.
                seen.erased_companies.add(domain)
                seen.named_again.discard(domain)
            else:
                # An erasure rewrites the lines before it as tokens, so a
                # line in clear after the last token one is a later block or
                # lift, which names the company in clear again.
                seen.named_again.add(domain_token(domain))
            if kind == "block":
                seen.blocks.add(_as_block(domain))
            else:
                seen.blocks.discard(_as_block(domain))
    for pending in journal.pending_rewrites(path):
        if pending.get("token"):
            seen.erased.add(pending["token"])
        if pending.get("domain_token"):
            # Its rewrite has not run: the lines in clear may be older.
            seen.erased_companies.add(pending["domain_token"])
            seen.named_again.discard(pending["domain_token"])
    for kind in (
        "opt_outs",
        "address_blocks",
        "bounces",
        "erased",
        "blocks",
        "erased_companies",
        "named_again",
        "sends",
    ):
        getattr(expected, kind).update(getattr(seen, kind))


def _from_held(live: Path, expected: _Expected) -> None:
    """The opt-outs held on disk while the database was busy."""
    folder = optout_spool.spool_dir(str(live))
    if not folder.is_dir():
        return
    for path in folder.iterdir():
        if path.suffix not in (".json", ".claimed"):
            continue
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(entry, dict):
            continue
        email = entry.get("email")
        if isinstance(email, str) and email.strip():
            expected.held.add(email.strip().lower())
            # "if_emailed" is decided at replay, by the restored database.
            domain = blockable_domain(email) if entry.get("company") is True else None
            if domain:
                expected.blocks.add(domain_token(domain))
        elif isinstance(entry.get("token"), str):
            token = entry["token"]
            # Anything else is no link of ours, and the replay drops it.
            if token.isascii() and token.isalnum():
                expected.held_tokens.add(token)


def _copy_beside(live: Path, restored: Path) -> None:
    """The journal, the erasures pending on files and the held opt-outs, copied
    beside the restored copy, where replay and start-up look for them. A held
    file claimed by a running process counts as unclaimed, as it will after a
    stop."""
    source = journal.journal_path(live)
    if source.exists():
        shutil.copyfile(source, journal.journal_path(restored))
    pending = journal.pending_folder(live)
    if pending.is_dir():
        target = make_private_dir(journal.pending_folder(restored))
        for path in pending.glob("*.json"):
            shutil.copyfile(path, target / path.name)
    held = optout_spool.spool_dir(str(live))
    if held.is_dir():
        target = make_private_dir(optout_spool.spool_dir(str(restored)))
        for path in held.iterdir():
            if path.suffix in (".json", ".claimed"):
                shutil.copyfile(path, target / f"{path.stem}.json")
            elif path.suffix == ".unmatched":
                # Its age decides when retention lets it go.
                shutil.copy2(path, target / path.name)


def _restore(copy: Database, settings) -> tuple[dict, int]:
    """What a real restore does once the backup is in place: replay-journal,
    then the held opt-outs the next start applies."""
    from modules.unsubscribe import UnsubscribeHandler

    replayed = journal.replay_journal(copy)
    if settings.retention_enabled:
        from modules.retention import apply_retention

        apply_retention(copy, settings)
    handler = UnsubscribeHandler(copy, settings.unsubscribe_secret)
    replayed["links"] = handler.record_links_again()
    applied = optout_spool.replay_optout_spool(copy, resolve=handler.verify_token)
    return replayed, applied


def _compare(
    copy: Database, expected: _Expected, live_held: set | None, report: DrillReport
) -> None:
    conn = copy.conn
    suppressed_in_copy = {
        e for (e,) in conn.execute("SELECT LOWER(email) FROM suppression_list")
    }
    domains_in_copy = {
        d for (d,) in conn.execute("SELECT LOWER(domain) FROM suppressed_domains")
    }
    blocks_in_copy = {_as_block(d) for d in domains_in_copy}
    set_aside = optout_spool.unmatched_tokens(copy.db_path)

    def suppressed(who: str) -> bool:
        return who in suppressed_in_copy or (
            not is_erasure_token(who) and erasure_token(who) in suppressed_in_copy
        )

    def held_click_kept(token: str) -> bool:
        address = copy.address_for_unsubscribe_token(token)
        return suppressed(address) if address else token in set_aside

    sent_in_copy = {
        m
        for (m,) in conn.execute(
            "SELECT message_id FROM email_log "
            f"WHERE delivery_status IN {POSSIBLY_SENT_STATUSES_SQL} "
            "UNION SELECT message_id FROM prior_sends"
        )
    }

    def send_known(key: tuple) -> bool:
        """Whether the copy knows this email went out: in email_log, or kept
        by the token for an address the backup does not hold."""
        if key[0] == "id":
            return key[1] in sent_in_copy
        _, token, sent = key
        if conn.execute(
            "SELECT 1 FROM prior_sends WHERE token = ? AND sent_at = ?", (token, sent)
        ).fetchone():
            return True
        return any(
            token_of(recipient) == token
            for (recipient,) in conn.execute(
                "SELECT recipient_email FROM email_log WHERE timestamp = ? "
                f"AND delivery_status IN {POSSIBLY_SENT_STATUSES_SQL}",
                (sent,),
            )
        )

    for label, wanted, kept, what in (
        ("opt-outs", expected.opt_outs, suppressed, "opt-out(s)"),
        (
            "company blocks",
            expected.blocks,
            blocks_in_copy.__contains__,
            "company block(s)",
        ),
        ("address blocks", expected.address_blocks, suppressed, "address block(s)"),
        ("bounces", expected.bounces, suppressed, "bounce(s)"),
        ("erasures", expected.erased, suppressed_in_copy.__contains__, "erasure(s)"),
        ("held opt-outs", expected.held, suppressed, "held opt-out(s)"),
        ("held clicks", expected.held_tokens, held_click_kept, "held click(s)"),
        (SENDS, expected.sends, send_known, "send(s)"),
    ):
        missing = sum(1 for w in wanted if not kept(w))
        report.checks.append(Check(label, len(wanted), missing))
        if missing:
            report.problems.append(f"{missing} {what} missing from the restored copy")

    # What the live data hold in clear: the database, and the held opt-outs
    # on disk that the restore applied.
    still_held = None if live_held is None else live_held | expected.held
    people = [
        address
        for address in journal.held_addresses(copy)
        if erasure_token(address) in expected.erased
        # Erased by retention, which is not journalled.
        or (still_held is not None and address not in still_held)
    ]
    erased_only = expected.erased_companies - expected.named_again
    companies = [
        d
        for d in domains_in_copy
        if not d.startswith("domain_") and domain_token(d) in erased_only
    ]
    report.in_clear = len(people) + len(companies)
    if report.in_clear:
        report.problems.append(
            f"{report.in_clear} erased address(es) or company domain(s) back in clear"
        )


def _unreplayed(restored: Path, report: DrillReport) -> int | None:
    """The restore guard on the restored copy (journal.unreplayed): what keeps
    the agent from starting before the replay, and lets it after."""
    try:
        return journal.unreplayed(restored)
    except (sqlite3.Error, OSError) as e:
        report.guard = f"could not be checked ({e})"
        report.problems.append("the start guard could not be checked on the copy")
        return None


def _choose_backup(live: Path, readable: bool, newest: bool, backup_file):
    if backup_file:
        source = Path(backup_file)
        if not source.is_file():
            raise NoBackup(f"No backup file at {source}.")
        return source, "named backup"
    if newest:
        copies = sorted(backup.backups_dir(live).glob("agent-*.db"))
        if not copies:
            raise NoBackup(
                f"No backup in {backup.backups_dir(live)} - run `main.py backup`, "
                "or leave out --newest to take one now."
            )
        return copies[-1], "newest backup"
    if not readable:
        raise NoBackup(
            "The live database cannot be read, so no fresh backup can be taken. "
            "Drill an existing one: --newest, or --backup <file>."
        )
    db = Database(str(live))
    try:
        return backup.make_backup(db), "fresh backup"
    finally:
        db.close_thread_conn()


def run_drill(settings, *, newest: bool = False, backup_file=None) -> DrillReport:
    """Restore a backup into a new temporary folder and check it against the
    live database, the journal and the held opt-outs. Raises DatabaseLocked or
    NoBackup when it cannot run; every other outcome is in the report."""
    live = Path(settings.get_abs_db_path())
    expected = _Expected()
    ours = erasure_key_fingerprint()
    with contextlib.ExitStack() as reading:
        readable, state = live.exists(), "read"
        if not readable:
            state = "missing - compared with the journal and held files only"
        else:
            try:
                reading.enter_context(_guard(live))
                _from_live(live, expected)
            except sqlite3.DatabaseError as e:
                if _locked(e):
                    raise DatabaseLocked(str(e)) from None
                readable = False
                state = (
                    f"unreadable ({e}) - compared with the journal and held files only"
                )
        # Read before a fresh backup is taken: anything recorded meanwhile is
        # in the backup, never only in what it is checked against.
        _from_journal(live, expected)
        _from_held(live, expected)
        if expected.fingerprint not in (None, ours) and not (newest or backup_file):
            raise NoBackup(
                "ERASURE_KEY is not the key the live database was erased with, "
                "so it cannot be opened to take a backup. Restore the original "
                "key first."
            )
        try:
            source, how = _choose_backup(live, readable, newest, backup_file)
            live_held = journal.held_addresses_at(live) if readable else None
        except sqlite3.OperationalError as e:
            if _locked(e):
                raise DatabaseLocked(str(e)) from None
            raise

    report = DrillReport(source, how, state)
    if expected.fingerprint not in (None, ours):
        report.problems.append(
            "the live database was erased under another ERASURE_KEY than the one set"
        )
    scratch = Path(tempfile.mkdtemp(prefix="restore-drill-"))
    restored = scratch / "agent.db"
    copy = None
    try:
        shutil.copyfile(source, restored)
        _copy_beside(live, restored)
        raw = sqlite3.connect(restored)
        try:
            report.integrity = raw.execute("PRAGMA integrity_check").fetchone()[0]
            stored = _stored_fingerprint(raw)
        except sqlite3.DatabaseError as e:
            report.integrity, stored = f"unreadable ({e})", None
        finally:
            raw.close()
        if report.integrity != "ok":
            report.problems.append(
                f"the backup failed its integrity check: {report.integrity}"
            )
        elif stored not in (None, ours):
            report.key = "differs"
            report.problems.append(
                "the backup was made under another ERASURE_KEY: no one it holds "
                "as erased would be recognised"
            )
        else:
            report.key = "matches" if stored else "not recorded yet (this key will be)"
            before = _unreplayed(restored, report)
            copy = Database(str(restored))
            report.replayed, report.held_applied = _restore(copy, settings)
            after = _unreplayed(restored, report)
            if before is not None and after is not None:
                report.guard = (
                    f"{before} journal line(s) newer than the backup before the "
                    f"replay, {after} after it"
                )
                if after:
                    report.problems.append(
                        f"the restored copy would still refuse to start: {after} "
                        "journal line(s) newer than it after the replay"
                    )
            _compare(copy, expected, live_held, report)
    finally:
        if copy is not None:
            copy.close_thread_conn()
        shutil.rmtree(scratch, ignore_errors=True)
    if scratch.exists():
        report.leftover = scratch
        report.problems.append(
            "the temporary folder, which holds a copy of the database, could not "
            "be deleted"
        )
    return report


def log_result(live: Path, report: DrillReport) -> None:
    """The drill's one write to the live database: a line in the activity log.
    Written directly, not through Database, whose opening may migrate the
    schema."""
    if not live.exists():
        report.logged = "not logged: there is no live database"
        return
    try:
        conn = _connect(live, "rw", timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO activity_log (action, details, status, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (
                    ACTIVITY,
                    report.summary(),
                    "info" if report.passed else "error",
                    clock.db_now(),
                ),
            )
        finally:
            conn.close()
    except sqlite3.Error as e:
        report.logged = f"not logged: {e}"
        return
    report.logged = "logged"
