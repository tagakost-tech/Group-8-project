"""Opt-outs that could not be written to the database, held until they can.

A one-click unsubscribe arrives once: mailbox providers never re-send it, and
the route must answer 200 whatever happens. When the database was locked (a
retention run or a send pass holding the write lock) the opt-out was logged
and lost, and the person kept being emailed (GDPR Art. 21(3)).

Each failed opt-out is now written here as its own small file, before the
response goes out, and applied by `replay_optout_spool` at start-up and
before every send. One file per request, written to a temporary name and
renamed into place, because the dashboard and the agent are separate
processes: a single shared file appended by one and truncated by the other
fails on Windows with a sharing violation.
"""

import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path

from modules import clock
from modules.company_block import block_company, blockable_domain
from modules.database import erasure_token, is_address
from modules.private_files import make_private_dir

logger = logging.getLogger(__name__)

# A file renamed to *.claimed belongs to the process replaying it. One left
# behind by a process that died mid-replay is taken back after this long.
STALE_CLAIM_SECONDS = 600

# A click on a link the database has no record of (a restore older than the
# link, an erasure since, or not one of ours) is set aside as
# <token>.unmatched for this long (retention deletes it after), so that its
# recipient is refused, and the opt-out applied, if we mean to email them. A
# send looks up only its own recipient's file: stray tokens cost it nothing,
# and none can push a real click out.
UNMATCHED_TOKEN_DAYS = 365

# Anyone can make up a token, so a new click stops being set aside at this many:
# it is refused and none is evicted, since evicting would let made-up tokens push
# real clicks out. Each new token lists the folder to count, so this also bounds
# that listing: milliseconds, whatever the number of addresses held (step G20).
MAX_UNMATCHED_TOKENS = 2000


def spool_dir(db_path: str) -> Path:
    """The holding folder, beside the database it belongs to."""
    return Path(db_path).resolve().parent / "spool" / "optouts"


def spool_optout(
    db_path: str,
    email: str,
    how: str,
    company: bool | str = False,
    request: str | None = None,
    token: str | None = None,
    source_ref: str | None = None,
    received_at: str | None = None,
) -> Path:
    """Hold one opt-out on disk. Raises OSError if even that fails.

    company: True stops the company too (the one-click button, or an emailed
    objector whose block could not be written); "if_emailed" decides at replay,
    for an opt-out by email held before we could tell who sent it.
    request: the kind of a data-subject request found in a reply, registered
    with its deadline when the opt-out is applied.
    token: an unsubscribe link's token, held instead of an address when the
    database was too busy to tell whose it is (email is then None).
    source_ref: the message that request came in, which the next scan reads
    again: whichever registers it first, the other registers nothing.
    received_at: when that message reached the mailbox, which starts the
    request's month; without it, the time it was held does.
    """
    folder = make_private_dir(spool_dir(db_path))
    name = uuid.uuid4().hex
    tmp = folder / f"{name}.tmp"
    entry = {"email": email, "how": how, "company": company, "at": clock.now_iso()}
    if request:
        entry["request"] = request
    if source_ref:
        entry["source_ref"] = source_ref
    if received_at:
        entry["received_at"] = received_at
    if token:
        entry["token"] = token
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    final = folder / f"{name}.json"
    os.replace(tmp, final)
    return final


def hold_optout(db, email: str, how: str, company: bool | str = False) -> Path | None:
    """Hold an opt-out the database could not write; see spool_optout.

    An address erased on request is suppressed for good by its keyed token;
    with its company already blocked when this opt-out would block it, there
    is nothing left to apply, so the erased address is not written to disk
    again. Any other suppression is still held: an operator's block or a
    bounce can be lifted, and the replay upgrades it to an opt-out. That
    takes a read, which a write lock does not stop; when even the read fails,
    the opt-out is held regardless. (A data request is held with
    spool_optout: it is registered on replay whatever else holds.)
    """
    try:
        if db.is_erased(email) and (
            not company or blockable_domain(email) is None or db.company_blocked(email)
        ):
            return None
    except sqlite3.Error:
        pass
    return spool_optout(db.db_path, email, how, company=company)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pending_addresses(db_path: str) -> set[str]:
    """Every address with an opt-out still waiting here, claimed or not."""
    folder = spool_dir(db_path)
    if not folder.is_dir():
        return set()
    found = set()
    for path in folder.iterdir():
        if path.suffix in (".json", ".claimed"):
            entry = _read(path)
            if entry and entry.get("email"):
                found.add(entry["email"].strip().lower())
    return found


def pending_tokens(db_path: str) -> set[str]:
    """Every unsubscribe token held without its address, claimed or not."""
    folder = spool_dir(db_path)
    if not folder.is_dir():
        return set()
    found = set()
    for path in folder.iterdir():
        if path.suffix in (".json", ".claimed"):
            entry = _read(path)
            if entry and entry.get("token") and not entry.get("email"):
                found.add(entry["token"])
    return found


def unmatched_tokens(db_path: str) -> set[str]:
    """The held tokens no link we recorded matched, set aside."""
    folder = spool_dir(db_path)
    if not folder.is_dir():
        return set()
    return {path.stem for path in folder.glob("*.unmatched")}


def is_set_aside(db_path: str, token: str) -> bool:
    return (spool_dir(db_path) / f"{token}.unmatched").exists()


def _unmatched_count(folder: Path) -> int:
    with os.scandir(folder) as entries:
        return sum(entry.name.endswith(".unmatched") for entry in entries)


def set_aside_click(db_path: str, token: str, how: str, company: bool = False) -> bool:
    """Set aside a click no record of our links matches, under its own token.

    Returns False if it was refused at MAX_UNMATCHED_TOKENS. Raises OSError if
    it cannot be written. A click on a link already kept is never refused. A
    plain one changes nothing, so that a mail filter opening the link after the
    recipient pressed the one-click button does not undo the company stop.
    """
    folder = make_private_dir(spool_dir(db_path))
    final = folder / f"{token}.unmatched"
    if final.exists():
        if not company:
            return True
        _add_company_stop(final, how, token)
        return True
    elif _unmatched_count(folder) >= MAX_UNMATCHED_TOKENS:
        return False
    tmp = folder / f"{uuid.uuid4().hex}.tmp"
    entry = {
        "email": None,
        "how": how,
        "company": company,
        "at": clock.now_iso(),
        "token": token,
    }
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(tmp, final)
    return True


def _add_company_stop(final: Path, how: str, token: str) -> None:
    """Make a click already kept stop the company too, never moving the date it
    expires by: expire_unmatched goes by the file's mtime, so a repeat click
    that refreshed it would keep the entry for good. The entry keeps the first
    click's "at"; one that cannot be read is rewritten, keeping its mtime."""
    entry = _read(final)
    if entry is not None and entry.get("company"):
        return
    try:
        kept = final.stat()
    except FileNotFoundError:
        kept = None
    if entry is None:
        entry = {"email": None, "how": how, "at": clock.now_iso(), "token": token}
    entry["company"] = True
    tmp = final.parent / f"{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    if kept is not None:
        os.utime(tmp, ns=(kept.st_atime_ns, kept.st_mtime_ns))
    os.replace(tmp, final)


def apply_set_aside(db, email: str, token: str) -> bool:
    """Apply the click set aside for this token to its recipient, about to be
    emailed: their own link, so the address is theirs. Returns True if one
    was applied; a database error propagates and the click stays."""
    path = spool_dir(db.db_path) / f"{token}.unmatched"
    entry = _read(path) if path.exists() else None
    if entry is None:
        return False
    how = entry.get("how") or "held opt-out"
    db.record_opt_out(email, how)
    if entry.get("company"):
        block_company(db, email, how)
    path.unlink(missing_ok=True)
    return True


def expire_unmatched(db_path: str, dry_run: bool = False) -> int:
    """Delete the clicks set aside longer than UNMATCHED_TOKEN_DAYS (run by
    retention). Returns how many."""
    folder = spool_dir(db_path)
    oldest_kept = clock.now().timestamp() - UNMATCHED_TOKEN_DAYS * 86400
    expired = 0
    for path in folder.glob("*.unmatched") if folder.is_dir() else []:
        try:
            if path.stat().st_mtime < oldest_kept:
                if not dry_run:
                    path.unlink()
                expired += 1
        except FileNotFoundError:
            continue
    return expired


def pending_company_domains(db_path: str) -> set[str]:
    """The companies of held opt-outs that stop the whole company. One whose
    company stop is decided at replay ("if_emailed") counts until then."""
    folder = spool_dir(db_path)
    if not folder.is_dir():
        return set()
    found = set()
    for path in folder.iterdir():
        if path.suffix in (".json", ".claimed"):
            entry = _read(path)
            if entry and entry.get("email") and entry.get("company"):
                domain = blockable_domain(entry["email"])
                if domain:
                    found.add(domain)
    return found


def discard_held(db_path: str, token: str) -> int:
    """Remove the unclaimed held opt-outs of whoever has this erasure token,
    matched by the token: after the erasure, the address is held nowhere else.

    A file being replayed (*.claimed) is left alone: its replay finds the
    erasure token and changes nothing. Returns how many files were removed.
    Raises OSError once the rest are done, if another program holds one open: a
    file that cannot even be read may be theirs, and is not passed over.
    """
    folder = spool_dir(db_path)
    if not folder.is_dir():
        return 0
    removed = 0
    refused = None
    for path in folder.glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            email = entry.get("email") if isinstance(entry, dict) else None
            if isinstance(email, str) and erasure_token(email) == token:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            pass
        except ValueError:
            pass  # no held opt-out of anyone's: nothing here reads it either
        except OSError as e:
            refused = e
    if refused:
        raise refused
    return removed


def _hand_back(claimed: Path, path: Path) -> None:
    try:
        os.replace(claimed, path.with_suffix(".json"))
    except OSError:
        pass


def _set_aside(claimed: Path, token: str) -> None:
    """Never refused at MAX_UNMATCHED_TOKENS: this click was told it is recorded."""
    final = claimed.parent / f"{token}.unmatched"
    if not (token.isascii() and token.isalnum()):
        claimed.unlink(missing_ok=True)
    elif final.exists():
        # A click on a link already kept: see set_aside_click.
        held = _read(claimed) or {}
        if held.get("company"):
            _add_company_stop(final, held.get("how") or "held opt-out", token)
        claimed.unlink(missing_ok=True)
    else:
        os.replace(claimed, final)


def _claimable(folder: Path) -> list[Path]:
    now = time.time()
    paths = []
    for path in sorted(folder.iterdir()):
        if path.suffix == ".json":
            paths.append(path)
        elif path.suffix == ".claimed":
            try:
                if now - path.stat().st_mtime > STALE_CLAIM_SECONDS:
                    paths.append(path)
            except OSError:
                continue
    return paths


def flag_unless_one_address(db, addr: str, what: str) -> None:
    """Ask a person to look at what was filed under anything but one address.

    Filed all the same, since refusing an opt-out or a request fails open.
    But the agent cannot email it, and it may not be the sender at all:
    the real address may be somewhere else in the From header. The inbox and
    the replay of what it held both ask: held, it once went unreviewed.
    """
    if not is_address(addr):
        db.log_activity(
            "SENDER_UNPARSED",
            f"{what} filed under {addr}, which is not one address the agent "
            "can email - review manually",
            status="error",
        )


def replay_optout_spool(db, resolve=None) -> int:
    """Apply every held opt-out to the database. Returns how many were applied.

    Safe to run from the dashboard and the agent at once: a file is claimed by
    renaming it first, and a rename the other process won is skipped.
    resolve: turns a held unsubscribe token into its address
    (UnsubscribeHandler.verify_token); without it, such a file waits.
    """
    folder = spool_dir(db.db_path)
    if not folder.is_dir():
        return 0
    applied = 0
    for path in _claimable(folder):
        claimed = path.with_suffix(".claimed")
        try:
            os.replace(path, claimed)
            # Refresh the age, so the other process does not take a live claim
            # back as stale.
            os.utime(claimed)
        except (FileNotFoundError, PermissionError):
            continue
        entry = _read(claimed)
        if not entry or not (entry.get("email") or entry.get("token")):
            logger.error("Unreadable held opt-out %s left in place", claimed.name)
            continue
        email = entry.get("email")
        try:
            if not email:
                if resolve is None:
                    _hand_back(claimed, path)
                    continue
                email = resolve(entry["token"])
                if email is None:
                    logger.warning(
                        "A held unsubscribe matched no link we recorded; set aside"
                    )
                    _set_aside(claimed, entry["token"])
                    continue
            db.record_opt_out(email, entry.get("how") or "held opt-out")
            flag_unless_one_address(
                db, email, "Data request" if entry.get("request") else "Opt-out"
            )
            company = entry.get("company")
            if company == "if_emailed":
                contact = db.get_contact_by_email(email)
                company = contact is not None and db.has_been_emailed(contact["id"])
            if company:
                block_company(db, email, entry.get("how") or "held opt-out")
            # Last: everything above is safe to repeat if this fails and the
            # file is handed back, and a request must not be registered twice.
            request_id = None
            if entry.get("request"):
                request_id = db.add_dsr(
                    email,
                    entry["request"],
                    "reply",
                    entry.get("source_ref"),
                    received_at=entry.get("received_at") or entry.get("at"),
                )
        except sqlite3.Error as e:
            # Still locked: hand the file back and try again next time.
            logger.warning("Held opt-out not applied yet: %s", e)
            try:
                os.replace(claimed, path.with_suffix(".json"))
            except OSError:
                pass
            break
        try:
            claimed.unlink()
        except FileNotFoundError:
            pass
        applied += 1
        if request_id is not None:
            # The row the inbox scan writes, when the request is registered
            # here instead: the scan that reads it again registers nothing.
            try:
                row = db.get_dsr(request_id)
                db.log_activity(
                    "DSR_RECEIVED",
                    f"{row['kind']} request from {row['email']}: "
                    "answer within one month",
                    status="warning",
                )
            except sqlite3.Error as e:
                logger.warning("Could not log a held request: %s", e)
    if applied:
        logger.info("Applied %d held opt-out(s)", applied)
    return applied
