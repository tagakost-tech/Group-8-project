"""The journal: what a restore from a backup must not undo (step G16).

A backup is the database as it was when it was taken; the journal is what
happened since. Every opt-out, company block and lift, address block and
unblock, bounce and other suppression, erasure, and data request registered
or closed is appended here, one JSON line each, beside the database: all
that stops or limits sending. So is every email that went out, or may have:
who it went to, which message, which follow-up step, when and from which
mailbox, never its subject or text. After a restore, replay_journal applies
them again, in order, erases every address the restored database still holds
whose erasure the journal records, and puts back every send the backup does
not know, so no one is sent the same email twice.

An erasure is journalled only as its token, and rewrites the person's earlier
lines - their opt-out, their company's block - as tokens too, so the journal
never keeps an erased address in clear. Lines older than the oldest backup
are trimmed: every backup a restore could use already holds them. Every line
carries a number the database records first, so a database restored from an
older backup, or one that lost writes, is told by the lines it does not know
(unreplayed): the agent does not start until the replay.

That rewrite replaces the file, and an erasure also deletes the person's access
answer and held opt-outs. Windows refuses all of these while another program
holds the file open. So they come after the database erasure is committed, the
rewrite tried again for a moment, and none of them can stop the erasure. A
record by the tokens alone, written just before that commit, stays until every
one has landed: doctor reports it, and the next erasure, backup or replay
finishes them, whatever stopped them, a kill included.
"""

import contextlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

from modules import clock
from modules.private_files import make_private_dir, private_opener

logger = logging.getLogger(__name__)

JOURNAL_NAME = "journal.jsonl"

# How long an erasure's rewrite of the journal keeps trying before it is left
# pending. The agent's and the dashboard's appends hold the file for an
# instant; a sync or backup tool, or an editor, for longer.
REWRITE_ATTEMPTS = 10
REWRITE_PAUSE_S = 0.2

# How many times a rewrite or a trim starts again because a line was appended
# while it worked, before it gives up for now.
REPLACE_READS = 5

# What an erasure's steps on files can leave behind, as the operator is told it:
# no word of it names anyone.
THE_JOURNAL = "the journal"
THE_ACCESS_ANSWER = "the access answer"
A_HELD_OPT_OUT = "a held opt-out"


def journal_path(db_path) -> Path:
    return Path(db_path).resolve().parent / JOURNAL_NAME


def pending_folder(db_path) -> Path:
    """Where the records of erasures with steps on files to finish wait: beside
    the journal of the database at db_path, or beside that journal itself."""
    return Path(db_path).resolve().parent / "spool" / "journal"


# The restore guard. Every line is numbered, and the number is recorded in the
# database (agent_state) before the line is written. A database that does not
# know a number the journal holds was restored from an older backup, or lost
# writes: the emails sent and the opt-outs made since are only in the journal
# until replay_journal applies them, so nothing may start before it
# (unreplayed).
LAST_LINE = "journal:last_line"
# The number the database had reached when a line was numbered on from a
# journal already ahead of it. Kept until replay_journal: without it, enough
# new lines would bring the database's number past the gap and hide it.
BEHIND_SINCE = "journal:behind_since"
# How much of the journal's end is read for its newest number.
TAIL_BYTES = 64 * 1024

_SET_STATE = (
    "INSERT INTO agent_state (key, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)


def _number(event: dict) -> int:
    """A line's number, or 0 for one written without (before the numbers, or
    when the database could not record one)."""
    n = event.get("seq")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


def _as_number(value) -> int:
    return int(value) if isinstance(value, str) and value.isdigit() else 0


def _line_number(raw: bytes) -> int:
    try:
        event = json.loads(raw)
    except (ValueError, RecursionError):
        return 0
    return _number(event) if isinstance(event, dict) else 0


def _newest_number(path: Path) -> int:
    """The highest number in the journal at path, read from its end, where it
    is: numbers are reserved in order and the lines written a moment later, so
    two writers at once can only swap neighbours. The whole file is read only
    when its end holds no numbered line (a journal from before the numbers)."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().split(b"\n")
    except FileNotFoundError:
        return 0
    if size > TAIL_BYTES:
        lines = lines[1:]  # cut part-way
    newest = max((_line_number(raw) for raw in lines), default=0)
    if newest or size <= TAIL_BYTES:
        return newest
    return max((_number(e) for e in read(path)), default=0)


def _reserve(db_path) -> int | None:
    """The next line's number, recorded in the database at db_path before the
    line is written: so the database knows every number in its journal, unless
    it was restored or lost writes. A journal already ahead marks the database
    behind (BEHIND_SINCE). None, logged, when the database cannot record it:
    the line goes unnumbered, and the guard does not count it."""
    try:
        conn = sqlite3.connect(
            f"{Path(db_path).resolve().as_uri()}?mode=rw",
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = dict(
                    conn.execute(
                        "SELECT key, value FROM agent_state WHERE key IN (?, ?)",
                        (LAST_LINE, BEHIND_SINCE),
                    ).fetchall()
                )
                last = _as_number(state.get(LAST_LINE))
                newest = _newest_number(journal_path(db_path))
                if newest > last and BEHIND_SINCE not in state:
                    conn.execute(_SET_STATE, (BEHIND_SINCE, str(last), clock.db_now()))
                number = max(last, newest) + 1
                conn.execute(_SET_STATE, (LAST_LINE, str(number), clock.db_now()))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as e:
        logger.error(
            "Could not number a journal line - the restore guard cannot see it: %s",
            e,
        )
        return None
    return number


def _numbered(db_path) -> dict:
    number = _reserve(db_path)
    return {"seq": number} if number else {}


def unreplayed(db_path, timeout: float = 5.0) -> int:
    """How many journal lines are newer than the database at db_path knows:
    lines it never saw, because it was restored from an older backup or lost
    writes. While any is, nothing may be started or sent (main.py): the emails
    sent and the opt-outs made since are only in the journal. replay_journal
    brings this back to 0.

    0 for a fresh install, an empty journal, a journal from before the numbers
    and one the backups trimmed. Read-only. Raises sqlite3.Error, OSError or
    ValueError (a journal that is not UTF-8) when it cannot be told. The
    dashboard's poll passes timeout 0, so a busy
    database answers at once."""
    path = Path(db_path)
    state = {}
    if path.exists():
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout
        )
        try:
            state = dict(
                conn.execute(
                    "SELECT key, value FROM agent_state WHERE key IN (?, ?)",
                    (LAST_LINE, BEHIND_SINCE),
                ).fetchall()
            )
        finally:
            conn.close()
    since = state.get(BEHIND_SINCE)
    known = _as_number(state.get(LAST_LINE) if since is None else since)
    newer = sum(1 for e in read(journal_path(path)) if _number(e) > known)
    return newer if since is None else max(newer, 1)


def _mark_replayed(db, events: list[dict]) -> None:
    """The database knows every line it was given: unreplayed lets it start."""
    newest = max((_number(e) for e in events), default=0)
    with db._transaction() as conn:
        conn.execute(
            "INSERT INTO agent_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = "
            "MAX(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER)), "
            "updated_at = excluded.updated_at",
            (LAST_LINE, str(newest), clock.db_now()),
        )
        conn.execute("DELETE FROM agent_state WHERE key = ?", (BEHIND_SINCE,))


def append(db_path, kind: str, **fields) -> None:
    """Add one event, numbered (_reserve). Never raises: the event itself is
    already committed."""
    line = json.dumps(
        {"at": clock.now_iso(), "kind": kind, **_numbered(db_path), **fields}
    )
    try:
        with open(
            journal_path(db_path), "a", encoding="utf-8", opener=private_opener
        ) as f:
            f.write(line + "\n")
    except OSError as e:
        logger.error("Could not write the journal - a restore would miss this: %s", e)


def _is_event(event) -> bool:
    """An object whose address, domain, token and reason, those it has, are
    text: a rewrite or a replay would stop on anything else, after an
    erasure's commit."""
    return isinstance(event, dict) and all(
        isinstance(event.get(field, ""), str)
        for field in ("email", "domain", "token", "reason")
    )


def read(path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            # RecursionError: valid JSON nested too deep to decode.
            logger.error("Skipping an unreadable journal line")
            continue
        if _is_event(event):
            events.append(event)
        else:
            logger.error("Skipping a journal line that is not an event")
    return events


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _replace(path: Path, change) -> None:
    """Replace the journal with its lines as change(lines) leaves them, unless
    it returns False: nothing to write then.

    Another process appends with no lock, and a line it adds after the read
    would go with the old file. So the size is checked just before the replace,
    and if it differs from what was read, the journal is read again. That
    narrows the race, and only a lock every append also took would close it.
    """
    tmp = path.with_suffix(".tmp")
    try:
        for _ in range(REPLACE_READS):
            seen = _size(path)
            events = read(path)
            if not change(events):
                return
            with open(tmp, "w", encoding="utf-8", opener=private_opener) as f:
                f.write("".join(json.dumps(e) + "\n" for e in events))
            if _size(path) == seen:
                os.replace(tmp, path)
                return
        raise OSError("the journal kept growing while it was being rewritten")
    finally:
        # A full copy of the journal: never left beside it.
        tmp.unlink(missing_ok=True)


def _tokenise(events: list[dict], token: str, domain_tok: str | None) -> bool:
    """The erased person's lines, and their company's, as the tokens. Matched
    by token, as replay matches them: once the database is erased, and in a
    pending rewrite, the address is held nowhere to match by."""
    from modules.database import domain_token, erasure_token

    changed = False
    for e in events:
        if "email" in e and erasure_token(e["email"]) == token:
            del e["email"]
            e["token"] = token
            if "outcome" in e:
                # How a request ended is the operator's free text, which can
                # name the person. The database keeps it redacted.
                e["outcome"] = "[erased]"
            changed = True
        if domain_tok and "domain" in e and domain_token(e["domain"]) == domain_tok:
            del e["domain"]
            e["token"] = domain_tok
            changed = True
    return changed


def _tokenise_companies(events: list[dict], companies) -> bool:
    """Lines naming any of these companies (by token) as the token instead."""
    from modules.database import domain_token

    changed = False
    for e in events:
        if isinstance(e.get("domain"), str) and domain_token(e["domain"]) in companies:
            e["token"] = domain_token(e.pop("domain"))
            changed = True
    return changed


def rewrite_as_tokens(
    db_path, token: str, domain_tok: str | None = None, companies=()
) -> bool:
    """After an erasure: the person, and their company's domain, only as tokens
    in every earlier line. True once it is done; never raises.

    The erasure's own line is added if it is missing: the journal refused that
    append too, and a restore would bring the person back. So a missing line
    forces a write of its own, even with nothing else to rewrite. Read afresh on
    every try: replacing the journal with what an earlier try read would drop a
    line another program appended since. companies: the tokens of other
    companies an erasure left in clear (_company_left_in_clear), done alike.
    """
    path = journal_path(db_path)

    def as_tokens(events: list[dict]) -> bool:
        changed = _tokenise(events, token, domain_tok)
        changed = _tokenise_companies(events, companies) or changed
        if not any(
            e.get("kind") == "erasure" and e.get("token") == token for e in events
        ):
            events.append(
                {
                    "at": clock.now_iso(),
                    "kind": "erasure",
                    **_numbered(db_path),
                    "token": token,
                }
            )
            changed = True
        return changed

    for attempt in range(REWRITE_ATTEMPTS):
        try:
            _replace(path, as_tokens)
            return True
        except PermissionError as e:
            # Held open by another program, which may let go in a moment.
            error = e
            if attempt + 1 < REWRITE_ATTEMPTS:
                time.sleep(REWRITE_PAUSE_S)
        except (OSError, ValueError) as e:
            # A full disk, a journal that cannot be decoded: waiting is no use.
            error = e
            break
    logger.error("Could not rewrite the journal after an erasure: %s", error)
    return False


def keep_pending(db_path, token: str, domain_tok: str | None) -> Path | None:
    """One small file per erasure with steps on files to finish, as held
    opt-outs are kept (modules.optout_spool): written under a temporary name,
    then renamed. Returns it, or None if it could not be written. Never raises:
    the erasure goes on without it, and says so."""
    folder = pending_folder(db_path)
    name = uuid.uuid4().hex
    tmp, record = folder / f"{name}.tmp", folder / f"{name}.json"
    entry = {"at": clock.now_iso(), "token": token, "domain_token": domain_tok}
    try:
        make_private_dir(folder)
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, record)
    except OSError as e:
        logger.error("Could not keep a record of an erasure's steps on files: %s", e)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return None
    return record


def _pending(db_path) -> list[tuple[Path, dict]]:
    from modules.database import is_wellformed_token

    folder = pending_folder(db_path)
    found = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            entry = None
        # Nothing written here looks otherwise, and a record's token chooses a
        # folder to delete: whatever else is here is unreadable, {} to the rest.
        readable = isinstance(entry, dict) and is_wellformed_token(entry.get("token"))
        found.append((path, entry if readable else {}))
    return found


def pending_rewrites(db_path) -> list[dict]:
    """The erasures with steps on files left to finish, each by its tokens."""
    return [entry for _, entry in _pending(db_path)]


def unreadable_pending(db_path) -> list[str]:
    """The file names of the records nothing can finish, as nothing can read
    them. A name is all doctor can say of one, and all it may."""
    return [path.name for path, entry in _pending(db_path) if not entry]


def _remove_files(db_path, token: str) -> tuple[list[str], list[str]]:
    """Delete what else of an erased person's is on disk: the access answer
    prepared for them, a full copy of their data, and held opt-outs, which hold
    the address. Each is tried whatever became of the other, by the token alone.
    Returns what was done and what is left, in words that name no one."""
    from modules import optout_spool
    from modules.export import remove_access_answer

    done, left = [], []

    def attempt(what: str, step):
        try:
            return step()
        except OSError as e:
            # Held open by another program, as a rule. The path in this names
            # no one: both are named by a token or at random.
            logger.error("Could not remove %s after an erasure: %s", what, e)
            left.append(what)
            return None

    if attempt(THE_ACCESS_ANSWER, lambda: remove_access_answer(db_path, token)):
        done.append("Removed the access answer prepared for them")
    held = attempt(A_HELD_OPT_OUT, lambda: optout_spool.discard_held(db_path, token))
    if held:
        done.append(f"Removed {held} held opt-out file(s)")
    return done, left


def _settle(record: Path | None, left: list[str]) -> None:
    """A record goes once, and only once, every step it stands for has landed."""
    if record and not left:
        try:
            record.unlink()
        except FileNotFoundError:
            pass  # another process finished it meanwhile
        except OSError as e:
            logger.error("A finished erasure is still marked pending: %s", e)


def finish_erasure(
    db_path,
    token: str,
    domain_tok: str | None,
    record: Path | None,
    *,
    journalled=True,
    companies=(),
) -> tuple[list[str], list[str]]:
    """An erasure's steps on files, once its commit is made: the journal's
    rewrite, then what _remove_files deletes. Never raises, and each is tried
    whatever became of the others. Returns what was done and what is left; what
    is left waits on the record. journalled is False in a replay, which rewrites
    nothing and keeps no record: what it could not do is only said. companies
    None: whether the journal still names an erased company could not be told,
    so the journal counts as left.
    """
    left = []
    if journalled:
        rewritten = rewrite_as_tokens(db_path, token, domain_tok, companies or ())
        if not rewritten or companies is None:
            left.append(THE_JOURNAL)
    done, others = _remove_files(db_path, token)
    left += others
    _settle(record, left)
    if journalled and not left:
        # All of it could be done: finish what an earlier erasure left.
        finish_pending(db_path)
    return done, left


def finish_pending(db_path) -> None:
    """Complete the steps on files that erasures left pending, each by its
    tokens. Never raises: what cannot be finished stays pending, for doctor."""
    try:
        pending = _pending(db_path)
    except OSError as e:
        logger.error("Could not read the pending erasures: %s", e)
        return
    journal_free = True
    for record, entry in pending:
        token = entry.get("token")
        if not token:
            logger.error("Unreadable pending erasure %s left", record.name)
            continue
        # Refused once, the journal would only make the rest wait for it again.
        journal_free = journal_free and rewrite_as_tokens(
            db_path, token, entry.get("domain_token")
        )
        _, left = _remove_files(db_path, token)
        _settle(record, left if journal_free else [THE_JOURNAL])
    if journal_free:
        _finish_companies(db_path)


def _finish_companies(db_path) -> None:
    """The companies erasures whose records were lost left named in clear, as
    their tokens. Never raises: what is left, doctor reports."""
    companies = _company_left_in_clear(db_path)
    if not companies:
        return
    try:
        _replace(
            journal_path(db_path),
            lambda events: _tokenise_companies(events, set(companies)),
        )
    except (OSError, ValueError) as e:
        logger.error("Could not rewrite an erased company's domain as its token: %s", e)


def _company_left_in_clear(db_path) -> list[str] | None:
    """The tokens of every company the database keeps only as a token, as an
    erasure leaves it, though a journal line still names its domain: what
    erasures whose records were lost left of their companies. A keyed block is
    never written beside a plain one, so each such line is such a leftover. None
    if it cannot be told. Never raises."""
    from modules.database import domain_token

    try:
        conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
        try:
            keyed = {
                d
                for (d,) in conn.execute("SELECT domain FROM suppressed_domains")
                if d.startswith("domain_")
            }
        finally:
            conn.close()
        if not keyed:
            return []
        named = [e["domain"] for e in read(journal_path(db_path)) if "domain" in e]
        tokens = [domain_token(d) for d in named]
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as e:
        # RuntimeError: no erasure key in this process to make the tokens with.
        logger.error("Could not look for a company an erasure left in clear: %s", e)
        return None
    return list(dict.fromkeys(t for t in tokens if t in keyed))


def finish_by_token(db_path, token: str) -> tuple[list[str], bool]:
    """Whatever an erasure left on files, for someone the database holds only as
    this token: a later request of theirs, or the same one run again after a
    kill. Returns what is still left, and whether a record of it stands. Never
    raises."""
    from modules.database import is_wellformed_token

    if not is_wellformed_token(token):
        # Looks like a token and no erasure made it: an address the request
        # came from. Nothing of theirs is on file, nor does it choose a folder.
        return [], False
    try:
        own = [m for m in _pending(db_path) if m[1].get("token") == token]
    except OSError as e:
        logger.error("Could not read the pending erasures: %s", e)
        own = []
    # The token alone cannot match the company's lines. The record knows it;
    # with none, what the database keeps of it as a token tells it. Which of
    # those is his cannot be told, and each is an erasure's leftover: all go.
    companies = _company_left_in_clear(db_path)
    domain_tok = next(
        (e["domain_token"] for _, e in own if e.get("domain_token")), None
    ) or next(iter(companies or []), None)
    record = own[0][0] if own else keep_pending(db_path, token, domain_tok)
    _, left = finish_erasure(db_path, token, domain_tok, record, companies=companies)
    return left, record is not None


def _when(event: dict) -> float:
    try:
        return datetime.fromisoformat(event["at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return float("inf")  # kept: what cannot be dated cannot be trimmed


def trim(db_path, before: float) -> int:
    """Drop lines older than `before` (a timestamp). Returns how many."""
    dropped = 0

    def without_the_old(events: list[dict]) -> bool:
        nonlocal dropped
        kept = [e for e in events if _when(e) >= before]
        dropped = len(events) - len(kept)
        events[:] = kept
        return dropped > 0

    _replace(journal_path(db_path), without_the_old)
    return dropped


# Where an address can be held after a restore; the columns erasure clears.
_ADDRESS_COLUMNS = (
    ("contacts", "email"),
    ("email_log", "recipient_email"),
    ("suppression_list", "email"),
    ("lead_alerts", "email"),
    ("consent_log", "email"),
    ("dsr_requests", "email"),
    ("unsubscribe_tokens", "email"),
)


def _held(conn) -> set[str]:
    from modules.database import is_held_address

    tables = {
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    found = set()
    for table, column in _ADDRESS_COLUMNS:
        if table not in tables:
            # A database made before the table existed: a restored backup.
            continue
        for (value,) in conn.execute(f"SELECT DISTINCT LOWER({column}) FROM {table}"):
            if (
                value
                and is_held_address(value)
                and not value.endswith("@redacted.invalid")
            ):
                found.add(value)
    return found


def held_addresses(db) -> set[str]:
    """Every address the database holds in clear."""
    with db._transaction() as conn:
        return _held(conn)


def held_addresses_at(path) -> set[str]:
    """The same, read-only from a database file nothing else may change."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return _held(conn)
    finally:
        conn.close()


def half_way_erasures(path, timeout: float = 5.0) -> int:
    """How many addresses the database at path still holds in clear though
    their token is on the suppression list as erased: an erasure stopped after
    its suppression, whatever stopped it. Read-only, as doctor reads it. The
    dashboard's poll passes timeout 0, so a busy database answers at once."""
    from modules.database import erasure_token

    conn = sqlite3.connect(
        f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=timeout
    )
    try:
        erased = {
            token
            for (token,) in conn.execute(
                "SELECT email FROM suppression_list WHERE reason = 'gdpr_erasure'"
            )
        }
        return sum(1 for address in _held(conn) if erasure_token(address) in erased)
    finally:
        conn.close()


def _apply(db, sql: str, params: tuple) -> None:
    with db._transaction() as conn:
        conn.execute(sql, params)


def _drop_plain_block(db, token: str) -> None:
    """The plain block a backup made before an erasure keeps of the erased
    person's company, which the erasure replaced with this token. The replay
    erases only the people the backup holds, so a colleague added and erased
    since would leave the company in clear."""
    from modules.database import domain_token

    with db._transaction() as conn:
        for (domain,) in conn.execute(
            "SELECT domain FROM suppressed_domains"
        ).fetchall():
            if not domain.startswith("domain_") and domain_token(domain) == token:
                conn.execute(
                    "DELETE FROM suppressed_domains WHERE domain = ?", (domain,)
                )


# The lines that stop or free one address. One for someone erased later is
# never applied: written back in clear, it would undo the erasure.
_SUPPRESSIONS = ("opt_out", "address_block", "address_unblock", "suppress")

# What a send line holds beside when and what kind it is, and what an erasure
# does with each (the words of modules.compliance.ERASURE_TREATMENTS).
# Database.journal_send is the one writer, and a test fails on a line of its
# with a field not named here: nothing personal goes in unclassified.
SEND_LINE_FIELDS = {
    "email": "token",
    "token": "pseudonymous",
    "id": "pseudonymous",
    "sent": "pseudonymous",
    "step": "none",
    "mailbox": "none",
    "notice": "none",
    "unconfirmed": "none",
    "seq": "none",
}

# The statuses a bounce turns into 'bounced' (modules/imap_monitor.py): a
# reply or a client is the more important fact.
_BOUNCEABLE = ("emailed", "pending", "failed", "blocked")


def _db_time(event: dict) -> str:
    """When a line was written, as the database writes a timestamp."""
    try:
        at = datetime.fromisoformat(event["at"])
    except (KeyError, TypeError, ValueError):
        return clock.db_now()
    return at.astimezone(clock.get_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def send_line(event: dict, erased: set) -> dict | None:
    """A send line as the replay applies it, or None for one it cannot use:
    it names no one or no message, or holds something a send line never
    does. Someone erased since is named by the token alone, as the erasure's
    rewrite leaves the line: nothing is then counted by their domain."""
    from modules.database import erasure_token

    step = event.get("step")
    texts = ("id", "sent", "mailbox", "notice")
    if (
        not (event.get("email") or event.get("token"))
        or not (event.get("id") or event.get("sent"))
        or any(not isinstance(event.get(field, ""), str) for field in texts)
        or isinstance(step, bool)
        or not (step is None or isinstance(step, int))
    ):
        return None
    if event.get("sent") or not event.get("id"):
        try:
            datetime.fromisoformat(event["sent"])
        except ValueError:
            return None
    token = erasure_token(event["email"]) if event.get("email") else None
    if token in erased:
        event = {k: v for k, v in event.items() if k != "email"} | {"token": token}
    return event


def _mark_contact(db, email: str, status: str, when) -> None:
    """The contact's status as the change left it live, where when(its status
    now) says the change made it: the dashboard shows it, and its Unblock
    reopens only a 'blocked' contact."""
    contact = db.get_contact_by_email(email)
    if contact and when(contact["status"]):
        db.update_contact_status(
            contact["id"], status, discard_reason="Restored from the journal"
        )


# A host name as a whole word, where export._redact_domain finds one: not the
# end of an address, nor the end of a longer name.
_HOST_NAME = re.compile(
    r"(?<![\w@.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63})(?![\w-]|\.\w)",
    flags=re.IGNORECASE,
)


def _redact_companies(db, companies: set) -> int:
    """The domains of these erased companies (by token) where an erasure
    redacts them live: the activity log's text, as the token, and the daily
    send counts, deleted. A backup made before a colleague's erasure still
    holds them, and the replay erases again only the people the backup holds.
    Returns how many records changed."""
    from modules.database import domain_token
    from modules.export import _redact_domain

    if not companies:
        return 0
    conn = db.conn
    counted = {
        d
        for (d,) in conn.execute("SELECT DISTINCT domain FROM domain_send_counts")
        if domain_token(d) in companies
    }
    named = {
        found.lower()
        for (text,) in conn.execute(
            "SELECT details FROM activity_log WHERE details IS NOT NULL"
        )
        for found in _HOST_NAME.findall(text)
    }
    changed = 0
    for domain in sorted(counted):
        changed += conn.execute(
            "DELETE FROM domain_send_counts WHERE domain = ?", (domain,)
        ).rowcount
    for domain in sorted(d for d in named if domain_token(d) in companies):
        changed += _redact_domain(db, domain)
    conn.commit()
    return changed


def replay_journal(db, path=None, *, primary_mailbox: str | None = None) -> dict:
    """Apply the journal to a restored database. Returns counts.

    Runs with journalling off, so nothing it re-applies is journalled twice.
    A plain line for someone erased later is skipped: the erasure wins. The
    lines are applied in the order they were written, so of a block and its
    lift, address or company, whichever came last holds.

    A send the database does not know is put back (Database.restore_send), so
    the agent does not send it again; one it knows is left alone, so a second
    replay changes nothing. primary_mailbox is the first sending mailbox, as
    the settings name it: it decides which warm-up counter a send made today
    counts on.

    Last, the database is marked as knowing every line it was given, which
    lets the agent start again (unreplayed). A replay that stops on an error
    leaves it behind.
    """
    from modules.database import BOUNCE_REASONS, CLOSED_CONTACT_STATUSES, erasure_token
    from modules.export import DataExporter

    if path is None:
        # What an erasure left pending, finished first if the files are free.
        # Only the database's own: the restore check replays the live journal
        # onto a copy, and must change nothing live.
        finish_pending(db.db_path)
    path = path or journal_path(db.db_path)
    events = read(path)
    pending = [p for p in pending_rewrites(path) if p.get("token")]
    # A rewrite still pending is applied here, in memory, so a replay gives what
    # it gives once the journal is rewritten: her company's domain and how her
    # request ended would otherwise go into the database in clear, to stay.
    for p in pending:
        _tokenise(events, p["token"], p.get("domain_token"))
    # And it counts as journalled, should the journal have refused its line.
    events += [{"kind": "erasure", "token": p["token"]} for p in pending]
    erased = {e["token"] for e in events if e.get("kind") == "erasure" and "token" in e}
    # Every company an erasure since the backup kept only as its token.
    companies = {
        e["token"]
        for e in events
        if e.get("kind") in ("block", "lift") and "domain" not in e and "token" in e
    } | {p["domain_token"] for p in pending if p.get("domain_token")}
    counts = {
        "opt_outs": 0,
        "blocks": 0,
        "lifts": 0,
        "address_blocks": 0,
        "unblocks": 0,
        "suppressions": 0,
        "requests": 0,
        "closed": 0,
        "erased": 0,
        "redacted": 0,
        "sends": 0,
    }
    db.journaling = False
    try:
        for e in events:
            kind = e.get("kind")
            if kind in _SUPPRESSIONS and erasure_token(e.get("email", "")) in erased:
                continue
            if kind == "opt_out" and "email" in e:
                if db.record_opt_out(e["email"], "Restored from the journal")["new"]:
                    counts["opt_outs"] += 1
            elif kind == "address_block" and "email" in e:
                if db.block_address(e["email"]):
                    counts["address_blocks"] += 1
                _mark_contact(
                    db,
                    e["email"],
                    "blocked",
                    lambda s: s not in CLOSED_CONTACT_STATUSES,
                )
            elif kind == "address_unblock" and "email" in e:
                # Only an operator's own block: a replay never lifts an
                # objection, a bounce or an erasure, whatever a line says.
                if db.remove_from_suppression(
                    e["email"], only_reasons=("blocked_via_dashboard",)
                ):
                    counts["unblocks"] += 1
                    _mark_contact(db, e["email"], "pending", lambda s: s == "blocked")
            elif kind == "suppress" and "email" in e and e.get("reason"):
                if db.restore_suppression(e["email"], e["reason"], _db_time(e)):
                    counts["suppressions"] += 1
                if e["reason"] in BOUNCE_REASONS:
                    _mark_contact(db, e["email"], "bounced", _BOUNCEABLE.__contains__)
            elif kind == "block":
                if "domain" in e:
                    db.block_domain(e["domain"], "Restored from the journal")
                else:
                    _drop_plain_block(db, e["token"])
                    _apply(
                        db,
                        "INSERT OR IGNORE INTO suppressed_domains "
                        "(domain, reason, timestamp) "
                        "VALUES (?, 'Restored from the journal', ?)",
                        (e["token"], clock.db_now()),
                    )
                counts["blocks"] += 1
            elif kind == "lift":
                if "domain" in e:
                    db.lift_domain_block(e["domain"])
                else:
                    _drop_plain_block(db, e["token"])
                    _apply(
                        db,
                        "DELETE FROM suppressed_domains WHERE domain = ?",
                        (e["token"],),
                    )
                counts["lifts"] += 1
            elif kind == "dsr":
                if db.restore_dsr(e):
                    counts["requests"] += 1
            elif kind == "dsr_close":
                if db.restore_dsr_close(e):
                    counts["closed"] += 1
            elif kind == "send":
                line = send_line(e, erased)
                if line is None:
                    logger.error("Skipping a send line that cannot be read")
                elif db.restore_send(line, _db_time(e), primary_mailbox):
                    counts["sends"] += 1
            elif kind == "erasure" and "token" in e:
                _apply(
                    db,
                    "INSERT OR IGNORE INTO suppression_list (email, reason, timestamp) "
                    "VALUES (?, 'gdpr_erasure', ?)",
                    (e["token"], clock.db_now()),
                )
        for address in sorted(held_addresses(db)):
            if erasure_token(address) in erased:
                DataExporter(db).gdpr_erase(address)
                counts["erased"] += 1
        counts["redacted"] = _redact_companies(db, companies)
        _mark_replayed(db, events)
    finally:
        db.journaling = True
    db.truncate_wal()
    return counts
