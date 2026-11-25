"""Pack a stopped install into one encrypted archive for the client.

the client gets a full copy of the agent: the code, the docs and the manual, and
its own data - the database (contacts found, their status, opt-outs, bounces,
blocks, erasure records, sent-email history), the journal, the held opt-outs
and the client and competitor lists, encrypted as they are. Four things stay
out (EXCLUDED):

  (a) every folder whose name contains "internal", and docs/review: the
      owner's pricing, deal and meeting notes and internal security notes;
  (b) .env and every variant: the owner's own secrets (the client makes its own;
      the blank template .env.example goes, and is scanned like the rest);
  (c) .git: its history may hold deleted files;
  (d) working files: logs, caches, virtual environments, old backups and
      every other database file, and every key or certificate file (*.pem,
      *.key, *.pfx, *.p12, and the file MICROSOFT_CERTIFICATE_PATH names).

The database goes in as a fresh checked backup made by modules/backup.py,
never the live file. A held click on an unsubscribe link the database has no
record of goes in as its address's held opt-out when this install's
UNSUBSCRIBE_SECRET, which stays behind, ties it to an address. ERASURE_KEY and LISTS_KEY travel inside the encrypted
archive only: the database opens only with its erasure key, and without it
the opt-outs and erasures in it would no longer be recognised.

Before it encrypts anything it scans every file it packs, and refuses on:
  - a value of one of the install's secret settings, anywhere, the database
    included;
  - a key, token or private key in the shapes docs/review/tools/scan_private.py
    looks for;
  - a password, secret or API key written into a doc, a settings file or the
    code (not in the tests, whose values are invented);
  - an address the database holds, or a name from the client or competitor
    list, in the code or the docs (they belong in the data only);
  - a spreadsheet outside the data folder.
It prints where, and what kind, never the value.

Usage (from the install folder, with the agent and the dashboard stopped):
    py -3.15 scripts/make_handover.py <archive file outside the install>

It asks for a passphrase twice; the passphrase is never stored or printed.
Beside the archive it writes open_handover.py, the program that opens it on
a computer with no copy of the agent yet (it holds no data and no key).
Send the passphrase by phone, never in the same email as the archive.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.handover import (
    OPENER_NAME,
    PASSPHRASE_MIN_LENGTH,
    README,
    HandoverRefused,
    erasure_key_fingerprint,
    pack,
    seal,
    stored_erasure_fingerprint,
)
from modules.list_crypto import is_encrypted

KEY_SUFFIXES = (".pem", ".key", ".pfx", ".p12")
DATABASE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3")
WORKING_FOLDERS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "logs",
    ".venv",
    "venv",
    ".claude",
    ".agents",
}
WORKING_SUFFIXES = (".pyc", ".pyo", ".partial", ".tmp", ".log")
# A workbook outside the data folder may be a plain copy of a list, or the
# owner's own sheet; its cells are not text this scan can read.
SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".ods", ".csv")
TEMPLATE = ".env.example"

EXCLUDED = {
    "internal": "(a) folders named internal",
    "review": "(a) docs/review",
    "env": "(b) .env and its variants",
    "git": "(c) .git",
    "working": "(d) logs, caches, old backups and other database files",
    "key": "(d) key and certificate files",
}

# The shapes docs/review/tools/scan_private.py looks for (SECRET), with a
# private key's first line and a signed token added. The assignment is held
# to one line: "PASSWORD=" then a new line is a blank setting, not a secret.
SECRET_SHAPES = re.compile(
    r"gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9-]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
)
SECRET_ASSIGNED = re.compile(
    r"(?:password|passwd|secret|api_key)[ \t]*=[ \t]*['\"]?[^\s'\"<>]{6,}",
    re.IGNORECASE,
)
# In code an assignment is a secret only when it is a written-out string:
# `password = settings.smtp_password` is not one.
SECRET_LITERAL = re.compile(
    r"(?:password|passwd|secret|api_key|passphrase)\w*['\"]?[ \t]*[=:][ \t]*"
    r"[rbuf]?['\"][^'\"\s]{6,}['\"]",
    re.IGNORECASE,
)
# Invented values the tests use to prove that real ones are masked, known by
# their SHA-256 so this file does not hold them (EXAMPLE_SECRETS in
# scan_private.py does the same for a reviewer's made-up password).
INVENTED_SECRETS = {
    # tests/test_microsoft_mailboxes_sign_in_without_a_password.py: a toy token
    "8096177e842cb1698b8316b1efdb07e8964028c2e6160b487895cb9f1026716e",
    # tests/test_secrets_stay_out_of_reprs_and_logs.py: four made-up API keys
    "16764074533292e8d4ff34f59e052e36ff55e3e32733cadf233c7a70ea5218e1",
    "731db889b98a99090c1dd6b007398a4b3fac118c6d6ceeca24a5f6ecf9520119",
    "d9c0725107da43a326f687023fd5f4530a42fab73438656cf5ba8876691d2a01",
    "8997e89e107430a6358772e3c63f2d05f3d15c02a6f0cad553bc052adda6df9f",
    # scripts/run_demo.py: the invented demo login, printed in its docstring
    "5bbf45b96aa04c6fcdf013317c351bafcb8c097804434e8141f4c9038e2c9bd9",
}
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Shorter names match ordinary words (scan_private.py uses the same floor).
NAME_MIN_LENGTH = 6
SECRET_MIN_LENGTH = 6


def _say(*parts) -> None:
    print(*parts, flush=True)


def _rel(path: Path, root: Path) -> PurePosixPath | None:
    try:
        return PurePosixPath(path.resolve().relative_to(root.resolve()).as_posix())
    except ValueError:
        return None


def _is_test(rel: PurePosixPath) -> bool:
    return rel.parts[0] == "tests" or rel.name == "conftest.py"


def excluded_group(
    rel: PurePosixPath, is_dir: bool, data: PurePosixPath, certificate
) -> str | None:
    """Which of the four excluded groups rel belongs to, or None to pack it."""
    name = rel.name.lower()
    folders = [p.lower() for p in (rel.parts if is_dir else rel.parts[:-1])]
    if any("internal" in p for p in folders):
        return "internal"
    if tuple(p.lower() for p in rel.parts[:2]) == ("docs", "review"):
        return "review"
    if ".git" in [p.lower() for p in rel.parts]:
        return "git"
    if (
        name.startswith(".env") or name.endswith(".env")
    ) and rel.as_posix() != TEMPLATE:
        return "env"
    if any(p in WORKING_FOLDERS for p in folders):
        return "working"
    if is_dir:
        if rel == data / "backups":
            return "working"
        return None
    if name.endswith(KEY_SUFFIXES) or rel == certificate:
        return "key"
    if name.endswith(DATABASE_SUFFIXES) or name.endswith(WORKING_SUFFIXES):
        return "working"
    if name.startswith("~$") or name in ("thumbs.db", ".ds_store"):
        return "working"
    return None


def collect(root: Path, data: PurePosixPath, certificate) -> tuple[dict, dict]:
    """(files, excluded): every file to pack by its relative path, and how
    many were left out in each group. A link is refused: it could pull in a
    file from outside the install."""
    files: dict[str, Path] = {}
    excluded = dict.fromkeys(EXCLUDED, 0)
    for current, dirs, names in os.walk(root):
        here = Path(current)
        for d in list(dirs):
            path = here / d
            rel = _rel(path, root)
            if path.is_symlink():
                raise HandoverRefused(
                    f"{rel} is a link; move the real folder in or out."
                )
            group = excluded_group(rel, True, data, certificate)
            if group:
                excluded[group] += sum(len(n) for _, _, n in os.walk(path)) or 1
                dirs.remove(d)
        for n in names:
            path = here / n
            rel = _rel(path, root)
            if path.is_symlink():
                raise HandoverRefused(f"{rel} is a link; copy the real file in or out.")
            group = excluded_group(rel, False, data, certificate)
            if group:
                excluded[group] += 1
            else:
                files[rel.as_posix()] = path
    return files, excluded


def secret_values(settings) -> dict[str, str]:
    """The install's secret settings that hold a value of their own, by .env
    name; the passwords inside SENDER_ACCOUNTS too."""
    from config import Settings

    found: dict[str, str] = {}
    for name, field in Settings.model_fields.items():
        if field.repr is not False:
            continue
        value = getattr(settings, name)
        if not isinstance(value, str) or value == field.default:
            continue
        label = (field.alias or name).upper()
        if name == "sender_accounts":
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                for i, entry in enumerate(json.loads(value)):
                    for k in ("password", "imap_password"):
                        if isinstance(entry.get(k), str):
                            found[f"{label}[{i}].{k}"] = entry[k]
            continue
        found[label] = value
    return {k: v for k, v in found.items() if len(v.strip()) >= SECRET_MIN_LENGTH}


def _text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _line(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def scan(
    staged: dict[str, bytes],
    secrets: dict[str, str],
    data: PurePosixPath,
    addresses: set[str],
    names: set[str],
) -> list[str]:
    """Every problem found, as "file: what" - never the value found."""
    problems = []
    needles = {label: value.encode("utf-8") for label, value in secrets.items()}
    name_pattern = (
        re.compile(
            r"(?<![a-z0-9])(?:"
            + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            + r")(?![a-z0-9])"
        )
        if names
        else None
    )
    for name, content in sorted(staged.items()):
        rel = PurePosixPath(name)
        for label, needle in needles.items():
            if needle in content:
                problems.append(f"{name}: holds the value of {label}")
        if rel.suffix.lower() in SPREADSHEET_SUFFIXES and not rel.is_relative_to(data):
            problems.append(
                f"{name}: a spreadsheet outside {data}/ (its contents cannot be "
                "checked; a list belongs in the data folder, encrypted)"
            )
        text = _text(content)
        # An encrypted list is base64 text: random letters that now and then
        # spell a key's shape ("sk-" and 20 more) and say nothing.
        if text is None or is_encrypted(content):
            continue
        checks = [(SECRET_SHAPES, "a key, token or private key")]
        if rel.suffix.lower() == ".py":
            if not _is_test(rel):
                checks.append((SECRET_LITERAL, "a password or secret written out"))
        else:
            checks.append((SECRET_ASSIGNED, "a password or secret written out"))
        for pattern, what in checks:
            for m in pattern.finditer(text):
                if hashlib.sha256(m.group(0).encode()).hexdigest() in INVENTED_SECRETS:
                    continue
                problems.append(f"{name}, line {_line(text, m.start())}: {what}")
        if rel.is_relative_to(data):
            continue
        low = text.lower()
        held = sum(1 for m in EMAIL.finditer(low) if m.group(0) in addresses)
        if held:
            problems.append(f"{name}: {held} address(es) the database holds")
        if name_pattern is not None:
            hits = len(name_pattern.findall(low))
            if hits:
                problems.append(
                    f"{name}: {hits} name(s) from the client or competitor list"
                )
    return problems


def resolve_held_clicks(
    staged: dict[str, bytes],
    spool: str,
    addresses: set[str],
    secret: str,
    scratch_db: Path,
) -> tuple[int, int]:
    """Turn each held click that this install's UNSUBSCRIBE_SECRET ties to an
    address the database holds into that address's held opt-out, in staged.

    A click no recorded link matched is set aside under its token, and a send
    applies it only to the recipient whose token, under the secret of the
    install that sends, is that token. Under the client's new secret none ever is,
    so the person who said no could be emailed. Held by address, it is applied
    at the client's first start whatever the secret. Returns (resolved, left)."""
    from modules.optout_spool import spool_optout
    from modules.unsubscribe import UnsubscribeHandler

    handler = UnsubscribeHandler(None, secret)
    owners = {handler.token_for(address): address for address in addresses}
    held = {
        PurePosixPath(name).stem: name
        for name in staged
        if PurePosixPath(name).parent == PurePosixPath(spool)
        and name.endswith(".unmatched")
    }
    resolved = 0
    for token, name in sorted(held.items()):
        address = owners.get(token)
        if address is None:
            continue
        try:
            entry = json.loads(staged[name])
        except ValueError:
            entry = None
        if not isinstance(entry, dict):
            entry = {}
        made = spool_optout(
            str(scratch_db),
            address,
            entry.get("how") or "held opt-out",
            company=bool(entry.get("company")),
        )
        staged[f"{spool}/{made.name}"] = made.read_bytes()
        del staged[name]
        resolved += 1
    return resolved, len(held) - resolved


def _list_names(settings) -> set[str]:
    from modules.clients import load_from_xlsx
    from modules.list_crypto import configured_lists

    names = set()
    for label, setting, path in configured_lists(settings):
        try:
            listed = load_from_xlsx(path, key=settings.lists_key)
        except Exception as e:  # noqa: BLE001 - any reason is a refusal, in words
            raise HandoverRefused(
                f"The {label.lower()} ({setting}) cannot be read: {e}. A list "
                "the client cannot read would stop its sending; fix it first."
            ) from None
        names |= {
            n.strip().lower() for n in listed if len(n.strip()) >= NAME_MIN_LENGTH
        }
    return names


def _check_stopped(settings, live: Path) -> None:
    host = settings.dashboard_host
    host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        socket.create_connection((host, settings.dashboard_port), timeout=0.5).close()
    except OSError:
        pass
    else:
        raise HandoverRefused(
            f"The dashboard is running ({host}:{settings.dashboard_port}). Stop "
            "it and the agent, then run this again."
        )
    conn = sqlite3.connect(live, timeout=0.5, isolation_level=None)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        raise HandoverRefused(
            "Another program is using the database (the agent, a scheduled task "
            "or a sync tool). Stop it, then run this again."
        ) from None
    finally:
        conn.close()


def _readme(day: str, db_rel: str, lists: list[tuple[str, str]]) -> str:
    listed = "\n".join(f"  - `{setting}`: `{rel}`" for setting, rel in lists) or (
        "  - none were set up"
    )
    return f"""# For the client: your copy of the outreach agent

Made on {day} by the owner, from the agent as it ran for the client.

## What is inside

- The agent: its program, its settings template (`.env.example`), its tests,
  and its documents (`docs/`). Start with `docs/USER_MANUAL.md` (for the
  people who use it) and `docs/DEPLOY.md` (for IT).
- the client's data, as it was when the agent was stopped:
  - the database, `{db_rel}`: the contacts found and their status, every
    opt-out, bounce and block, the records of erasures and data requests,
    and the history of the emails sent;
  - the journal beside it (`journal.jsonl`) and any opt-outs held in
    `spool/`: what a restore must never lose;
  - the client and competitor lists, encrypted as they were:
{listed}
- Not inside: the owner's own settings file (`.env`), keys and certificates,
  log files, old backups and the owner's working notes. The client makes its own
  `.env`, its own Microsoft certificate and its own backups.

The two keys the data needs, `ERASURE_KEY` and `LISTS_KEY`, were in the
archive. The restore put them in the file `.env.handover-keys` in this
folder, which only the account that ran the restore can read.

Some documents name files under `docs/review/`: those are the developer's
working notes, which stay with the owner.

From now on this copy is the only one that may send. The owner's copy stays
stopped: two copies would not know of each other's emails and opt-outs.

## Install

Follow `docs/DEPLOY.md`, section 1, in this folder, with these changes:

1. Give this folder the permissions IT gives the install folder (only the
   account that runs the agent, and administrators).
2. Make the agent's own Python environment and install the packages:
   `py -3.15 -m venv .venv`, then
   `.venv\\Scripts\\python.exe -m pip install -r requirements.txt`.
3. Make `.env` from `.env.example`. Copy the two lines from
   `.env.handover-keys` into it. **Never change `ERASURE_KEY`**: the
   database recognises the people who were erased or said no only with it.
   Keep a copy of both keys apart from this computer, then delete
   `.env.handover-keys`.
4. Fill in the rest of `.env` with the client's own values (`docs/SETTINGS.md`):
   a new `DASHBOARD_PASSWORD`, a new `UNSUBSCRIBE_SECRET` (links already sent
   keep working: the database records them), the mailbox and, for
   Microsoft 365, the client's own certificate (`docs/DEPLOY.md`, section 12).
   If the lists' paths above are not those in `.env`, set them.

## What to do first

1. `.venv\\Scripts\\python.exe main.py doctor` until it shows no `[FAIL]`.
   "Journal replayed" must pass: the database and the journal match.
2. `.venv\\Scripts\\python.exe main.py lists check`: both lists encrypted
   and readable.
3. `.venv\\Scripts\\python.exe main.py backup`, then schedule the backups
   (`docs/DEPLOY.md`, section 9).
4. Open the dashboard and check that the numbers match what the owner last
   reported. Then start with a dry run.

If anything here fails, stop and call the owner before starting the agent.
"""


def make_handover(
    root: Path, settings, archive: Path, passphrase: str, *, say=_say
) -> dict:
    """Pack the install at root into archive. Returns the counts it printed.
    Raises HandoverRefused, in words, before anything is written to archive."""
    from modules import clock, journal
    from modules.backup import backups_dir, make_backup
    from modules.database import Database, set_erasure_key
    from modules.list_crypto import configured_lists
    from modules.optout_spool import spool_dir

    root = Path(root).resolve()
    archive = Path(archive).resolve()
    if len(passphrase) < PASSPHRASE_MIN_LENGTH:
        raise HandoverRefused(
            f"The passphrase must be at least {PASSPHRASE_MIN_LENGTH} characters: "
            "for example four or five unrelated words."
        )
    if _rel(archive, root) is not None:
        raise HandoverRefused("Write the archive outside the install folder.")
    if archive.exists():
        raise HandoverRefused(f"{archive} exists already; name a new file.")
    if not archive.parent.is_dir():
        raise HandoverRefused(f"The folder {archive.parent} does not exist.")

    live = Path(settings.get_abs_db_path())
    db_rel = _rel(live, root)
    if db_rel is None:
        raise HandoverRefused(
            "DB_PATH is outside the install folder; move it in first."
        )
    if not live.is_file():
        raise HandoverRefused(f"There is no database at {db_rel}.")
    data = db_rel.parent
    if data == PurePosixPath("."):
        raise HandoverRefused("Keep the database in a folder of its own (data/).")
    lists = []
    for label, setting, path in configured_lists(settings):
        rel = _rel(path, root)
        if rel is None:
            raise HandoverRefused(
                f"The {label.lower()} ({setting}) is outside the install folder; "
                "move it into data/ and set its path, then run this again."
            )
        if not path.is_file():
            raise HandoverRefused(f"The {label.lower()} ({setting}) is missing.")
        lists.append((setting, rel.as_posix()))
    cert = (settings.microsoft_certificate_path or "").strip()
    certificate = (
        _rel(Path(settings.get_abs_microsoft_certificate_path()), root)
        if cert
        else None
    )

    try:
        set_erasure_key(settings.erasure_key)
    except ValueError:
        raise HandoverRefused(
            "ERASURE_KEY is blank. The database's opt-outs and erasures are "
            "recognised only with the key it was made with: put that key back "
            "in .env first."
        ) from None
    _check_stopped(settings, live)
    behind = journal.unreplayed(live)
    if behind:
        raise HandoverRefused(
            f"The database is behind its journal by {behind} line(s). Run "
            "`py -3.15 main.py replay-journal` first."
        )
    names = _list_names(settings)

    say("  Taking a checked backup of the database...")
    try:
        backup = make_backup(Database(str(live)))
    except (RuntimeError, sqlite3.Error, OSError) as e:
        raise HandoverRefused(
            f"The backup of the database failed ({e}). Run "
            "`py -3.15 main.py doctor`, fix what it shows, then run this again."
        ) from None
    if journal.pending_rewrites(live):
        raise HandoverRefused(
            "An erasure still has files to finish (doctor shows it). Run "
            "`py -3.15 main.py backup` once more after closing what holds them."
        )
    journal_file = journal.journal_path(live)
    journal_rel = _rel(journal_file, root).as_posix()

    files, excluded = collect(root, data, certificate)
    staged = {rel: path.read_bytes() for rel, path in files.items()}
    staged[db_rel.as_posix()] = backup.read_bytes()
    staged[README] = _readme(clock.today_str(), db_rel.as_posix(), lists).encode()

    scratch = Path(tempfile.mkdtemp(dir=backups_dir(live), prefix="handover-"))
    try:
        copy = scratch / "agent.db"
        copy.write_bytes(staged[db_rel.as_posix()])
        if journal_rel in staged:
            (scratch / journal.JOURNAL_NAME).write_bytes(staged[journal_rel])
        if stored_erasure_fingerprint(copy) not in (
            None,
            erasure_key_fingerprint(settings.erasure_key),
        ):
            raise HandoverRefused(
                "ERASURE_KEY is not the key the database was made with. Restore "
                "the original key in .env before handing anything over."
            )
        if journal.unreplayed(copy):
            raise HandoverRefused(
                "Something wrote to the journal after the backup: the agent or "
                "the dashboard is still running. Stop both, then run this again."
            )
        conn = sqlite3.connect(copy)
        try:
            contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            suppressed = conn.execute(
                "SELECT COUNT(*) FROM suppression_list"
            ).fetchone()[0]
            addresses = {
                str(r[0]).strip().lower()
                for r in conn.execute(
                    "SELECT email FROM contacts UNION SELECT email FROM suppression_list"
                )
                if r[0]
                and "@" in str(r[0])
                and not str(r[0]).endswith("@redacted.invalid")
            }
        finally:
            conn.close()
        resolved, unresolved = resolve_held_clicks(
            staged,
            _rel(spool_dir(str(live)), root).as_posix(),
            addresses,
            settings.unsubscribe_secret,
            copy,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if resolved:
        say(
            f"  {resolved} held click(s) on an unsubscribe link matched a contact: "
            "packed as that address's held opt-out, applied at the client's first start."
        )
    if unresolved:
        say(
            f"  Note: {unresolved} held click(s) on an unsubscribe link match no "
            "link the database recorded and no address it holds. Each is matched "
            "to its recipient only under this install's UNSUBSCRIBE_SECRET, which "
            "does not travel: under the client's new one it is never applied "
            "(docs/DEPLOY.md section 15)."
        )
    now = journal_file.read_bytes() if journal_file.exists() else None
    if now != staged.get(journal_rel):
        raise HandoverRefused(
            "The journal changed while the archive was made: the agent or the "
            "dashboard is still running. Stop both, then run this again."
        )

    problems = scan(staged, secret_values(settings), data, addresses, names)
    if problems:
        for problem in problems:
            say(f"    {problem}")
        raise HandoverRefused(
            f"{len(problems)} problem(s) above: nothing was written. Remove the "
            "secret or the client data from those files, or move them out of the "
            "install folder, then run this again."
        )

    keys = {"ERASURE_KEY": settings.erasure_key, "LISTS_KEY": settings.lists_key}
    made = clock.now().isoformat(timespec="seconds")
    blob = seal(pack(staged, keys, made), passphrase)
    partial = archive.with_name(archive.name + ".partial")
    try:
        with open(partial, "xb") as f:
            f.write(blob)
        os.replace(partial, archive)
    finally:
        partial.unlink(missing_ok=True)
    opener = archive.parent / OPENER_NAME
    shutil.copyfile(ROOT / "modules" / "handover.py", opener)

    counts = {
        "files": len(staged),
        "contacts": contacts,
        "suppressed": suppressed,
        "megabytes": round(len(blob) / 1024**2, 1),
        **{f"left out {EXCLUDED[g]}": n for g, n in excluded.items()},
    }
    say(f"  Archive written: {archive}")
    say(f"  Beside it: {opener} (the program that opens it; no data, no key).")
    say(
        f"  Inside: {len(staged)} files ({counts['megabytes']} MB encrypted); the "
        f"database holds {contacts} contact(s) and {suppressed} suppressed address(es)."
    )
    for group, n in excluded.items():
        say(f"  Left out, {EXCLUDED[group]}: {n} file(s)")
    say("  Send the archive and open_handover.py to the client's IT. Give the passphrase")
    say("  by phone, never in the same email or message as the archive.")
    return counts


def _ask_passphrase() -> str:
    first = getpass.getpass(
        f"  Passphrase for the archive (at least {PASSPHRASE_MIN_LENGTH} characters): "
    )
    second = getpass.getpass("  The same passphrase again: ")
    if first != second:
        raise HandoverRefused("The two passphrases differ. Nothing was written.")
    return first


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Pack this stopped install into one encrypted archive for the client."
    )
    parser.add_argument(
        "archive", type=Path, help="The archive to write, outside the install"
    )
    args = parser.parse_args(argv)

    from config import config_error_text, load_settings

    try:
        settings = load_settings()
    except (ValueError, OSError) as e:
        _say(f"  Refused: the settings do not load: {config_error_text(e)}")
        return 1
    try:
        make_handover(ROOT, settings, args.archive, _ask_passphrase())
    except HandoverRefused as e:
        _say(f"  Refused: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
