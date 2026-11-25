"""
Omega IFS's human-in-the-loop B2B outreach agent — Main Entry Point.

CLI Commands:
    python main.py run                 Single batch run
    python main.py run --continuous    Continuous loop mode
    python main.py run --dry-run       Dry run (no emails sent)
    python main.py dashboard           Launch the web dashboard
    python main.py security-check      Look for known vulnerabilities in the packages
    python main.py status              Show current database stats
    python main.py add-query "query"   Add a search query to the next run

Flask Dashboard API Endpoints:
    GET  /api/stats       → Aggregate statistics
    GET  /api/activity    → Recent activity log
    GET  /api/contacts    → All contacts
    GET  /api/emails      → All email log entries
    GET  /api/searches    → All search log entries
    POST /api/agent/start → Start the agent (body: {mode: single|continuous|dry_run})
    POST /api/agent/stop  → Stop the agent
    POST /api/query/add   → Add a search query (body: {query: "..."})
"""

import argparse
import hmac
import logging
import re
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Collection
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
_INSTALLED_ROOT = PROJECT_ROOT


def _templates_dir() -> Path:
    """Where the email texts are read and saved. The installed project reads
    CLIENT_TEMPLATES_DIR when it is set (FileHandler); a PROJECT_ROOT moved
    elsewhere - as tests do - keeps its own templates/ folder."""
    if PROJECT_ROOT == _INSTALLED_ROOT:
        return FileHandler().templates
    return PROJECT_ROOT / "templates"
sys.path.insert(0, str(PROJECT_ROOT))

from agent import GuardUnavailable, OutreachAgent
from config import (
    RUNTIME_EDITABLE,
    Settings,
    _ordered_override_items,
    config_error_text,
    effective_settings,
    load_settings,
    validate_override,
)
from modules.database import (
    CLOSED_CONTACT_STATUSES,
    DISCARDED_CONTACT_STATUSES,
    REQUEUED_FROM_A_VERDICT,
    REQUEUED_FROM_DASHBOARD,
    UNSETTLED_SEND,
    Database,
    set_erasure_key,
)
from modules.dispatch_pipeline import AlreadyEmailed
from modules.file_handler import FileHandler
from modules.fit_score import assess as assess_fit
from modules.optout_spool import replay_optout_spool
from modules.review_queue import LEVELS as REVIEW_LEVELS
from modules.review_queue import STAGES as REVIEW_STAGES
from modules.review_queue import ReviewError, ReviewQueue, level_for

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────────────────────────────


class PrivacyFormatter(logging.Formatter):
    """Masks addresses, unsubscribe tokens and IPs in every formatted line,
    the traceback included (modules.compliance.mask_personal)."""

    def format(self, record: logging.LogRecord) -> str:
        from modules.compliance import mask_personal

        return mask_personal(super().format(record))


def setup_logging(
    debug: bool = False,
    name: str = "agent",
    retention_days: int = 30,
    log_dir: Path | None = None,
    to_file: bool = True,
):
    """Configure application logging: console plus a daily file per command.

    One file per command, because two processes rotating one shared file
    fail on Windows. Files older than retention_days are deleted at rotation.
    to_file False keeps to the console: a command that may write nothing
    outside its own folder logs there itself.
    """
    # Prevent Windows console Unicode crashes
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")

    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [logging.StreamHandler(sys.stdout)]
    # Rotating file log so run history survives after the console closes.
    file_log_error = None
    try:
        from logging.handlers import TimedRotatingFileHandler

        if to_file:
            log_dir = log_dir or PROJECT_ROOT / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = TimedRotatingFileHandler(
                log_dir / f"{name}.log",
                when="midnight",
                backupCount=retention_days,
                encoding="utf-8",
            )
            handlers.append(file_handler)
    except (OSError, ImportError) as e:
        # File logging is best-effort; never block startup on it. mkdir and
        # opening the log raise OSError (read-only volume, permissions, a path
        # already taken by a file); the handler import is theoretical. Held and
        # reported below - there is nowhere to log it until basicConfig runs.
        file_log_error = e

    formatter = PrivacyFormatter(fmt, datefmt)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    if file_log_error is not None:
        logger.warning(
            "File logging is disabled (%s) - this run is console-only.",
            file_log_error,
        )
    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # Its request lines carry unsubscribe tokens and the client's IP address.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────
# CLI Commands
# ─────────────────────────────────────────────────────────────────────


def cmd_run(args, settings: Settings):
    """Execute the agent in single or continuous mode."""
    refuse_a_start_before_the_replay(settings)
    refuse_an_unsafe_start(settings)
    agent = OutreachAgent(settings)

    dry_run = args.dry_run or settings.dry_run
    queries = None
    if args.query:
        queries = [args.query]

    if args.continuous:
        # Prefer an explicit --loop-delay; otherwise use the .env value.
        delay = (
            args.loop_delay
            if args.loop_delay is not None
            else settings.loop_delay_seconds
        )

        # Handle Ctrl+C (SIGINT) gracefully so the loop stops cleanly.
        def _handle_sigint(signum, frame):
            logger.info("SIGINT received - requesting graceful shutdown...")
            agent.stop()

        try:
            signal.signal(signal.SIGINT, _handle_sigint)
        except (ValueError, OSError):
            # signal() only works in the main thread; fall back to the
            # KeyboardInterrupt handler inside run_continuous().
            pass

        agent.run_continuous(queries=queries, dry_run=dry_run, loop_delay=delay)
    else:
        agent.run_single(queries=queries, dry_run=dry_run)


def cmd_status(args, settings: Settings):
    """Print current database statistics."""
    db = Database(settings.get_abs_db_path())
    stats = db.get_stats()

    print("\n" + "=" * 50)
    print("  B2B OUTREACH AGENT - STATUS")
    print("=" * 50)
    print(f"  Total Searches    : {stats['total_searches']}")
    print(f"  Total Contacts    : {stats['total_contacts']}")
    print(f"  Pending Contacts  : {stats['pending_contacts']}")
    print(f"  Emails Sent       : {stats['emails_sent']}")
    print(f"  Emails Failed     : {stats['emails_failed']}")
    print(f"  Success Rate      : {stats['success_rate']}%")
    print(f"  Suppressed        : {stats['suppressed']}")
    print("=" * 50)

    # Validate setup
    fh = FileHandler()
    validation = fh.validate_setup(settings.pdf_path)
    print("\n  Setup Validation:")
    for key, ok in validation.items():
        if key == "all_ok":
            continue
        status = "[OK]" if ok else "[MISSING]"
        print(f"    {status} {key.replace('_', ' ').title()}")
    print()


def cmd_add_query(args, settings: Settings):
    """Queue a search query for the next run.

    This used to print instructions telling the operator to hand-edit .env and
    then exit without doing anything - while the dashboard's Add Query box, for
    the same concept, actually queued the search. Two interfaces, one name,
    opposite behaviour. It now does what the dashboard does.
    """
    query = (args.query_text or "").strip()
    if not query:
        print("A query is required.")
        sys.exit(1)

    db = Database(settings.get_abs_db_path())
    search_id = db.log_search(query, source="cli", status="pending")
    print(f'\nQueued: "{query}"  (search #{search_id})')
    print("  It will be picked up by:  python main.py run")
    print("                       or:  python main.py run --continuous")
    print("\nTo make it permanent across runs, add it to TARGET_QUERIES in .env.")
    print()


def _email_arg(text: str) -> str:
    """An argparse type: one email address, trimmed and lower-cased."""
    from modules.database import is_held_address

    email = text.strip().lower()
    if not is_held_address(email):
        raise argparse.ArgumentTypeError(f"{text!r} is not an email address")
    return email


def _received_arg(text: str) -> str:
    """An argparse type for dsr add --received: the day a request arrived
    (YYYY-MM-DD), whose month it runs from, and never a day still to come."""
    from datetime import date, datetime

    from modules import clock

    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a date (YYYY-MM-DD)"
        ) from None
    if day > clock.now().date():
        raise argparse.ArgumentTypeError(f"{text} has not come yet")
    return datetime(
        day.year, day.month, day.day, tzinfo=clock.get_timezone()
    ).isoformat()


def _erasable_arg(text: str) -> str:
    """An argparse type for gdpr-erase: an address, never the token an
    erasure leaves. Refused before the database is opened, as a non-address is."""
    from modules.database import is_erasure_token

    email = _email_arg(text)
    if is_erasure_token(email):
        raise argparse.ArgumentTypeError(
            f"{text!r} is an erasure token: that address is erased already"
        )
    return email


def cmd_gdpr_export(args, settings: Settings):
    try:
        from modules.export import write_access_answer

        db = Database(settings.get_abs_db_path())
        folder = write_access_answer(
            db,
            args.email,
            controller=settings.legal_entity_name or "(legal entity not set)",
            notice_url=settings.privacy_notice_url,
        )
        # The data stays in the folder, out of the terminal's scrollback.
        print(f"Access answer written to: {folder}")
    except ImportError:
        print("Export module not available")


def cmd_backup(args, settings: Settings):
    """Write a checked backup of the database; keeps the newest 14 (L5.7)."""
    from modules.backup import make_backup

    path = make_backup(Database(settings.get_abs_db_path()))
    print(f"  Backup written: {path}")


def cmd_security_check(args, settings: Settings):
    """Look up the installed packages in pip-audit's list of known
    vulnerabilities and record the answer in the activity log. Installs and
    upgrades nothing (docs/DEPLOY.md section 14)."""
    from modules import security_check

    db = Database(settings.get_abs_db_path())
    try:
        report = security_check.check(PROJECT_ROOT / "requirements.txt")
    except security_check.CheckFailed as e:
        detail = f"The security check could not run: {e}."
        print(f"  {detail}")
        db.log_activity("SECURITY_CHECK", detail, status="error")
        sys.exit(1)

    print(f"  Security check: {report.checked} installed packages looked up with pip-audit.")
    if report.not_checked:
        print(f"  Not in pip-audit's list, so not checked: {', '.join(report.not_checked)}.")
    if report.used_not_checked:
        print(
            "  Of those, the agent uses these, so they are not known to be safe: "
            f"{', '.join(report.used_not_checked)}."
        )
    if report.used:
        print("  Known vulnerabilities in packages the agent uses:")
        for finding in report.used:
            print(f"    {security_check.describe(finding)}")
    if report.tools:
        print("  Known vulnerabilities in tools only (not used by the agent):")
        for finding in report.tools:
            print(f"    {security_check.describe(finding)}")
    if not report.findings:
        print("  No known vulnerabilities.")
    print("  Nothing was installed or upgraded. To update, follow docs/DEPLOY.md")
    print("  section 14: update a copy of the install, test it, then swap.")

    if report.used_not_checked:
        db.log_activity(
            "SECURITY_CHECK",
            "pip-audit could not look up these packages the agent uses, so "
            f"they are not known to be safe: {', '.join(report.used_not_checked)}.",
            status="error",
        )
    if report.used:
        db.log_activity(
            "SECURITY_CHECK",
            "Known vulnerabilities in packages the agent uses: "
            + "; ".join(security_check.describe(f) for f in report.used)
            + ". Update a copy of the install (docs/DEPLOY.md section 14).",
            status="error",
        )
        sys.exit(1)
    if report.used_not_checked:
        sys.exit(1)
    if report.tools:
        db.log_activity(
            "SECURITY_CHECK",
            "Known vulnerabilities in tools only, not used by the agent: "
            + "; ".join(security_check.describe(f) for f in report.tools)
            + ".",
            status="warning",
        )
        return
    db.log_activity(
        "SECURITY_CHECK",
        f"No known vulnerabilities in the {report.checked} installed packages.",
        status="success",
    )


def cmd_replay_journal(args, settings: Settings):
    """After restoring a backup: apply every opt-out, company block and lift,
    address block and unblock, bounce, erasure and data request registered or
    closed since, in order, and put back every email sent since, so no one is
    sent the same email twice.

    Then retention, whose erasures are not journalled: what it erased since
    the backup is past its period still, and is erased again.

    Last, the links, which the journal does not hold: each contact left gets its
    record back, so a click on a link sent since the backup still opts them out.

    Until this has run on a restored database, run and the dashboard's Start
    and Direct Send refuse (unreplayed_journal); it says whether they still do.
    """
    from modules.journal import replay_journal
    from modules.unsubscribe import UnsubscribeHandler

    db = Database(settings.get_abs_db_path())
    counts = replay_journal(db, primary_mailbox=settings.smtp_username)
    print("  Journal replayed:")
    for key, n in counts.items():
        print(f"    {key.replace('_', ' '):<12} {n}")
    if settings.retention_enabled:
        from modules.retention import apply_retention, describe

        print(f"  {describe(apply_retention(db, settings))}")
    links = UnsubscribeHandler(db, settings.unsubscribe_secret).record_links_again()
    print(f"  Unsubscribe links recorded again: {links}")
    refusal = unreplayed_journal(settings)
    print(f"  Start still refused: {refusal}." if refusal else "  The agent may start.")


def cmd_restore_check(args, settings: Settings):
    """Rehearse a restore: the newest backup, replayed, in a scratch copy.

    The live database is never opened for writing. Fails if the copy is
    damaged, or if anything erased since the backup survives the replay.
    """
    import shutil
    import tempfile

    from modules.backup import backups_dir
    from modules.database import erasure_token
    from modules.journal import (
        held_addresses,
        held_addresses_at,
        journal_path,
        read,
        replay_journal,
    )
    from modules.unsubscribe import UnsubscribeHandler

    live = settings.get_abs_db_path()
    copies = sorted(backups_dir(live).glob("agent-*.db"))
    if not copies:
        print("  No backup to check - run `main.py backup` first.")
        sys.exit(1)
    scratch = Path(tempfile.mkdtemp(dir=backups_dir(live), prefix="restore-check-"))
    try:
        shutil.copyfile(copies[-1], scratch / "agent.db")
        copy = Database(str(scratch / "agent.db"))  # refuses another erasure key
        verdict = copy.conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = replay_journal(copy, journal_path(live))
        if settings.retention_enabled:
            from modules.retention import apply_retention

            apply_retention(copy, settings)
        links = UnsubscribeHandler(
            copy, settings.unsubscribe_secret
        ).record_links_again()
        erased = {
            e["token"] for e in read(journal_path(live)) if e.get("kind") == "erasure"
        }
        # Anything the live database no longer holds in clear - erased on
        # request, or by retention, which is not journalled - must not be in
        # the replayed copy either.
        still_held = held_addresses_at(live)
        survivors = [
            a
            for a in held_addresses(copy)
            if erasure_token(a) in erased or a not in still_held
        ]
        copy.close_thread_conn()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"  Checked {copies[-1].name}: integrity {verdict}")
    print(
        f"  Replayed: {counts['opt_outs']} opt-out(s), {counts['blocks']} block(s), "
        f"{counts['lifts']} lift(s), {counts['erased']} erasure(s), {links} link(s)"
    )
    if verdict != "ok" or survivors:
        print(f"  Restore check FAILED: {len(survivors)} erased address(es) survive.")
        sys.exit(1)
    print("  Restore check passed.")


def cmd_restore_drill(args, settings: Settings):
    """Restore a backup into a new temporary folder, replay it as a real
    restore would, and prove that no opt-out, company block, address block,
    bounce, erasure, held opt-out or email sent is lost and no one erased
    comes back (docs/DEPLOY.md section 9). What is lost is counted by kind.

    A fresh backup by default; --newest or --backup to drill one already
    made. The live database is only read, and gets one line in the activity
    log. Exits 1 on FAIL, and when it cannot run.
    """
    from modules.restore_drill import DatabaseLocked, NoBackup, log_result, run_drill

    try:
        report = run_drill(settings, newest=args.newest, backup_file=args.backup)
    except DatabaseLocked:
        print(
            "  The database is locked: another program is writing to it (the "
            "agent, the dashboard, a retention run, or a backup or sync tool). "
            "Stop it and run the drill again. Nothing was changed."
        )
        sys.exit(1)
    except NoBackup as e:
        print(f"  {e} Nothing was changed.")
        sys.exit(1)
    log_result(Path(settings.get_abs_db_path()), report)

    print(f"  Restore drill of the {report.how}: {report.backup.name}")
    print(f"  Live database: {report.live}")
    print(f"  Integrity:     {report.integrity}")
    print(f"  Erasure key:   {report.key}")
    print(f"  Start guard:   {report.guard}")
    if report.replayed:
        r = report.replayed
        print(
            f"  Replayed:      {r['opt_outs']} opt-out(s), {r['blocks']} company "
            f"block(s), {r['lifts']} lift(s), {r.get('address_blocks', 0)} address "
            f"block(s), {r.get('unblocks', 0)} unblock(s), "
            f"{r.get('suppressions', 0)} bounce(s) or other suppression(s), "
            f"{r['erased']} erasure(s), {r['links']} link(s); "
            f"{report.held_applied} held opt-out(s) applied"
        )
    print(f"  Sends:         {report.sends()}")
    if report.checks:
        print(f"  {'Kept in the restored copy':<34} {'checked':>8} {'missing':>8}")
        for c in report.checks:
            print(f"    {c.label:<32} {c.checked:>8} {c.missing:>8}")
        print(f"  Erased and back in clear: {report.in_clear}")
    left = "deleted" if report.leftover is None else report.leftover
    print(f"  Temporary folder: {left}")
    print(f"  Activity log: {report.logged}")
    if not report.passed:
        print("  Restore drill FAILED:")
        for problem in report.problems:
            print(f"    - {problem}")
        sys.exit(1)
    print("  Restore drill PASSED.")


def cmd_handover_restore(args):
    """Unpack the owner's handover archive into a new folder: the agent, its
    database and journal, the lists, and ERASURE_KEY and LISTS_KEY in a file
    only this account can read (modules/handover.py; docs/DEPLOY.md, "Handing
    the agent and its data to the client"). Prints no key and logs nothing."""
    import getpass

    from modules.handover import HandoverRefused, restore

    def ask() -> str:
        return getpass.getpass("  Passphrase (given by the owner by phone): ")

    try:
        restore(args.archive, ask, args.into)
    except HandoverRefused as e:
        print(f"\n  Refused: {e}")
        sys.exit(1)


def cmd_retention(args, settings: Settings):
    """Apply the retention periods now, without the agent (decision L5.1).

    For a daily scheduled task, so the periods hold while the agent is not
    run (L1.9). --dry-run counts what would go and changes nothing.
    """
    from modules import clock
    from modules.retention import apply_retention, describe

    if not settings.retention_enabled and not args.dry_run:
        print("  Retention is switched off (RETENTION_ENABLED) - nothing erased.")
        return
    db = Database(settings.get_abs_db_path())
    counts = apply_retention(db, settings, dry_run=args.dry_run)
    if args.dry_run:
        print("  Dry run - nothing was changed. A run now would do:")
    else:
        db.set_state("retention:last_run_date", clock.today_str())
        db.log_activity("RETENTION_APPLIED", describe(counts), status="info")
        print("  Retention applied:")
    for key, n in counts.items():
        print(f"    {key.replace('_', ' '):<26} {n}")


def cmd_company_country(args, settings: Settings):
    """Work out the company country of stored contacts not yet emailed.

    Contacts collected before the company counted for the language have no
    country stored, so an Italian company on a .com address would still get
    the English email. This reads what is stored about each (its website,
    its organisation name and page description), and asks the AI only with
    --ai. A contact already emailed keeps its first email's language, so it
    is left alone. --dry-run counts what would be found and changes nothing.
    """
    from modules.company_country import settle_company_country
    from modules.compliance import language_and_reason

    db = Database(settings.get_abs_db_path())
    ai_gen = None
    if args.ai and not args.dry_run:
        if not settings.company_country_ai:
            print("  COMPANY_COUNTRY_AI is off - the AI is not asked.")
        else:
            from modules.ai_generator import AIGenerator, own_server_options

            ai_gen = AIGenerator(
                **own_server_options(settings),
                personal_data_allowed=bool((settings.ai_dpa_reference or "").strip()),
            )
    counts = dict.fromkeys(
        ("checked", "website", "page", "ai_country", "ai_no_answer", "still_unknown"),
        0,
    )
    italian = 0
    for contact in db.contacts_needing_company_country():
        counts["checked"] += 1
        country, source = settle_company_country(
            settings, db, contact, ai_gen, write=not args.dry_run
        )
        if source == "ai":
            counts["ai_country" if country else "ai_no_answer"] += 1
        elif source:
            counts[source] += 1
        if not country:
            counts["still_unknown"] += 1
        found = {
            **contact,
            "company_country": country,
            "company_country_source": source,
        }
        if language_and_reason(settings, contact["email"], found)[0] == "it":
            italian += 1
    counts["get_italian"] = italian
    if args.dry_run:
        print("  Dry run - nothing was changed. A run now would find:")
    else:
        db.log_activity(
            "COMPANY_COUNTRY",
            ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in counts.items()),
            status="info",
        )
        print("  Company countries worked out:")
    for key, n in counts.items():
        print(f"    {key.replace('_', ' '):<26} {n}")


def cmd_dsr(args, settings: Settings):
    """Data-subject requests (Arts. 12-22): list, show, add, close, erase by id."""
    from modules import clock
    from modules.compliance import mask_personal
    from modules.database import is_held_address

    db = Database(settings.get_abs_db_path())
    if args.action == "list":
        rows = db.list_dsrs(open_only=not args.all)
        now = clock.now().isoformat()
        for r in rows:
            late = "OVERDUE " if r["status"] == "open" and r["due_at"] < now else ""
            print(
                f"  #{r['id']:<4} {late}{r['kind']:<8} {mask_personal(r['email']):<36} "
                f"received {r['received_at'][:10]}  due {r['due_at'][:10]}  {r['status']}"
            )
        if not rows:
            print("  No requests." if args.all else "  No open requests.")
        return
    if args.action == "add":
        rid = db.add_dsr(args.email, args.kind, "manual", received_at=args.received)
        print(f"  Registered request #{rid}, due {db.get_dsr(rid)['due_at'][:10]}.")
        return
    row = db.get_dsr(args.id)
    if row is None:
        print(f"  No request #{args.id}.")
        return
    # An erasure, or retention, keeps the row as the person's token. While
    # the suppression list still holds their address in clear, the request
    # is about that address; with nothing behind the token, they are erased.
    email = db.address_for_erasure_token(row["email"]) or row["email"]
    if args.action in ("export", "erase") and not is_held_address(row["email"]):
        # Filed from the inbox under what its From header held. The erasure
        # crashed on it, and the export answered that nothing was held.
        print(
            f"  Request #{args.id} is not filed under one email address, so "
            f"nothing was done: {args.action} needs one.\n"
            f"  Read it with: dsr show {args.id}. Answer it by hand, then: "
            f"dsr close {args.id} --outcome"
        )
        return
    if args.action == "show":
        # Printed to the operator only, so they can act on it; never logged.
        row = {**row, "email": email}
        for key in (
            "id",
            "kind",
            "email",
            "channel",
            "received_at",
            "due_at",
            "status",
            "closed_at",
            "outcome",
        ):
            print(f"  {key:<12} {row[key]}")
        return
    if args.action == "export":
        from modules.export import write_access_answer

        folder = write_access_answer(
            db,
            email,
            controller=settings.legal_entity_name or "(legal entity not set)",
            notice_url=settings.privacy_notice_url,
            request_id=args.id,
        )
        print(f"Access answer written to: {folder}")
        print("  Close the request once the answer is sent: dsr close --outcome")
        return
    if args.action == "close":
        db.close_dsr(args.id, args.outcome)
        print(f"  Closed request #{args.id}: {args.outcome}")
        return
    if args.action == "erase":
        if row["status"] != "open":
            print(f"  Request #{args.id} is already closed.")
            return
        from modules.database import erasure_token, is_erasure_token
        from modules.journal import finish_by_token, held_addresses

        if is_erasure_token(email):
            # A token does not mean erased. Suppressed by it, the address still
            # held in clear: an erasure that stopped half-way. Erase it, as usual.
            in_clear = [a for a in held_addresses(db) if erasure_token(a) == email]
            if in_clear:
                email = in_clear[0]
            else:
                # Erased, as far as the database goes: finish what it left on
                # files, by the token, and say so only if nothing is left.
                left, recorded = finish_by_token(db.db_path, email)
                db.close_dsr(args.id, "erased" if left else "already erased")
                db.truncate_wal()
                if left:
                    print(f"  Erased: {email} (request #{args.id} closed)")
                    _say_if_files_pending(
                        {"files_pending": left, "pending_recorded": recorded}
                    )
                else:
                    print(f"  Already erased: {email} (request #{args.id} closed)")
                return
        answer = input(
            f"  Erase everything held about {mask_personal(email)} "
            f"(request #{args.id})? Type ERASE to confirm: "
        )
        if answer.strip() != "ERASE":
            print("  Nothing erased.")
            return
        from modules.export import DataExporter

        result = DataExporter(db).gdpr_erase(email)
        db.close_dsr(args.id, "erased")
        # The erasure emptied the database's log; closing the request wrote
        # to it again. Leave it empty.
        db.truncate_wal()
        print(f"  Erased: {result['email']} (request #{args.id} closed)")
        _say_if_files_pending(result)
        return


def _say_if_files_pending(result: dict) -> None:
    """The database erasure is done; some files that name them could not be
    changed yet. A promise of what comes next only if something stands behind it."""
    left = result.get("files_pending")
    if not left:
        return
    what = ", ".join(left)
    if result.get("pending_recorded"):
        print(
            f"\n  ! Not changed yet: {what}. The file may be held open by another\n"
            "    program (a sync or backup tool, an editor), or the disk may be full;\n"
            "    the log says why. It is pending, and doctor reports it until done.\n"
            "    Once it is free, run `main.py backup`, which finishes it."
        )
    else:
        print(
            f"\n  ! Not changed yet: {what}. The file may be held open by another\n"
            "    program, or the disk may be full; the log says why.\n"
            "    This could not be recorded either, so nothing will report or finish\n"
            "    it: once the disk has room, run `main.py gdpr-erase <address>` again."
        )


def cmd_suppress_domain(args, settings: Settings):
    """Company-wide blocks (decision L1.6): list them, or lift one with a reason."""
    db = Database(settings.get_abs_db_path())
    if args.action == "list":
        blocks = db.list_domain_blocks()
        # A keyed block (left by an erasure) shows nothing that names the person.
        plain = [b for b in blocks if not b["domain"].startswith("domain_")]
        for b in plain:
            print(f"  {b['domain']:<40} {b['reason']:<34} {b['timestamp']}")
        keyed = len(blocks) - len(plain)
        if keyed:
            print(f"  ... and {keyed} more kept only as tokens (after an erasure)")
        if not blocks:
            print("  No company is blocked.")
        return
    domain = args.domain.strip().lower()
    lifted = db.lift_domain_block(domain)
    if lifted is None:
        print(f"  {domain} is not blocked.")
        return
    # A block kept only as a token was left by an erasure, and its domain may
    # name the erased person: the log must not write it back in clear.
    named = domain if lifted == "plain" else "a company blocked after an erasure"
    db.log_activity(
        "DOMAIN_UNBLOCKED", f"Company block lifted for {named}: {args.reason}"
    )
    print(f"  Lifted the block on {domain}. Reason recorded: {args.reason}")


def cmd_gdpr_erase(args, settings: Settings):
    try:
        from modules.export import DataExporter

        db = Database(settings.get_abs_db_path())
        exporter = DataExporter(db)
        result = exporter.gdpr_erase(args.email)
        print(f"\nErased: {result['email']}")
        for action in result["actions"]:
            print(f"  - {action}")
        _say_if_files_pending(result)
        recipients = settings.lead_alert_email.strip() or "none configured"
        print(
            "\nNot reached by this command - clear these by hand:\n"
            "  - the sending mailbox: Sent Items, Inbox and Junk Email\n"
            f"  - the mailboxes of the lead-alert recipients: {recipients}\n"
            "  - the log files in logs/: each mention keeps its first letter and\n"
            "    domain until the file is deleted after LOG_RETENTION_DAYS days\n"
            "Tell each recipient of the data that it was erased (GDPR Art. 19)."
        )
    except ImportError:
        print("Export module not available")


class DoctorCheck(NamedTuple):
    """One preflight result.

    ok is tri-state: None means "could not be assessed" (printed SKIP), which
    is not a failure in its own right. blocking marks a check whose failure
    means live sending is refused, not merely inadvisable.
    """

    label: str
    ok: bool | None
    detail: str
    blocking: bool = False


# doctor's one question to an own AI server: no personal data, and an answer
# that shows whether the model does as it is told.
AI_CHECK_QUESTION = "Reply with the single word YES and nothing else."


def run_doctor_checks(
    settings: Settings,
    live_smtp: bool = False,
    network: bool = True,
    log_dir: Path | None = None,
    only: Collection[str] | None = None,
    for_page: bool = False,
) -> list[DoctorCheck]:
    """Every preflight check, as data. cmd_doctor only prints them.

    network=False reports the resolver-dependent checks as SKIP, so tests can
    run the rest without depending on DNS. log_dir is where the log files are
    scanned for anything written in clear. only, when given, names the checks
    to run: the others are not run at all, so a caller polling a few cheap
    checks never pays for a log scan or an AI client it does not show.
    for_page writes the details for the dashboard: a path relative to the
    project, or a file name, and no exception text, which carries absolute
    paths and the account folder. The terminal keeps both.
    """
    checks: list[DoctorCheck] = []

    def shown(path) -> str:
        if not for_page:
            return str(path)
        path = Path(path)
        try:
            return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
        except (ValueError, OSError):
            return path.name

    def check(label, fn, blocking=False):
        if only is not None and label not in only:
            return
        try:
            ok, detail = fn()
        except Exception as e:
            # Deliberately broad: `fn` is an arbitrary probe (filesystem, DNS,
            # sqlite, optional imports, settings validation) and the whole point
            # of `doctor` is to report every check even when one explodes. The
            # failure is never hidden - it goes into the report line as `detail`
            # - and logger.exception keeps the traceback for the surprises.
            logger.exception("Preflight check %r raised", label)
            ok, detail = False, f"error: {e}"
            if for_page:
                detail = "could not be checked - see the server log"
        # bool(None) is False, which turned "not assessed" into a WARN.
        checks.append(
            DoctorCheck(label, None if ok is None else bool(ok), detail, blocking)
        )

    def skip(label, reason):
        if only is None or label in only:
            checks.append(DoctorCheck(label, None, reason))

    fh = FileHandler()

    check(
        ".env file present",
        lambda: ((PROJECT_ROOT / ".env").exists(), str(PROJECT_ROOT / ".env")),
    )

    def _queries_available():
        """TARGET_QUERIES is one of three sources, not the only one.

        Warning purely on an empty TARGET_QUERIES reported a problem that was
        not one: an installation with queued searches, or simply with heuristic
        discovery enabled, has plenty to search. The check now asks the real
        question - is there anything for the agent to do - so the WARN means
        "it will sit idle", which is worth acting on.
        """
        configured = len(settings.get_target_queries())
        try:
            queued = len(Database(settings.get_abs_db_path()).get_pending_searches(50))
        except (sqlite3.Error, OSError):
            # A DB that cannot be opened is already reported by its own check;
            # this one just falls back to the other two sources.
            queued = 0
        sources = []
        if configured:
            sources.append(f"{configured} in TARGET_QUERIES")
        if queued:
            sources.append(f"{queued} queued")
        if settings.heuristic_discovery_enabled:
            sources.append("heuristic discovery")
        if not sources:
            return False, "nothing to search - the agent would sit idle"
        return True, ", ".join(sources)

    check("Something to search", _queries_available)
    check(
        "Email template present",
        lambda: (fh.get_email_template() is not None, "templates/email_template.html"),
    )
    italian_template = fh.get_email_template("email_template_it.html")
    italian_present = not _blank_template(fh, "email_template_it.html")
    check(
        "Italian email template present",
        lambda: (
            italian_present,
            "templates/email_template_it.html"
            if italian_present
            else "templates/email_template_it.html missing - Italian recipients "
            "get the English first email",
        ),
    )

    def _sender_title():
        # Asked only where a first-email template says what its sender is
        # responsible for; there a blank title refuses the live first email.
        from modules.compliance import italian_countries

        templates = [fh.get_email_template()]
        # An A/B arm is an English first email too, as first_email_needs_title
        # counts it: one that names the title holds every English one back.
        if settings.ab_testing_enabled:
            templates += [t for _n, t in fh.get_template_variants()]
        if italian_countries(settings):
            templates.append(italian_template)
        if not any("{{sender_title}}" in (t or "") for t in templates):
            return None, "no first-email template uses {{sender_title}}"
        accounts = settings.get_sender_accounts()
        missing = [
            a["username"] or "the primary mailbox"
            for a in accounts
            if not a["sender_title"]
        ]
        if missing:
            return False, (
                f"SENDER_TITLE blank for {', '.join(missing)} - live first "
                "emails from it are refused"
            )
        return True, ", ".join(dict.fromkeys(a["sender_title"] for a in accounts))

    check("Sender title", _sender_title, blocking=True)
    if settings.attach_profile:

        def _profile_pdf():
            # The first email says "please find attached": without the PDF
            # the live first email is refused.
            path, problem = fh.pdf_problem(settings.pdf_path)
            if problem:
                return False, (
                    f"{shown(path)} {problem} - live first emails are refused "
                    "(ATTACH_PROFILE is on)"
                )
            return True, shown(path)

        check("PDF attachment present", _profile_pdf, blocking=True)
        skip("Profile link", "not used - the PDF is attached")
    else:
        skip("PDF attachment present", "not attached - ATTACH_PROFILE is off")
        check(
            "Profile link",
            lambda: (
                (settings.profile_url or "").lower().startswith("https://"),
                settings.profile_url
                or "PROFILE_URL blank - first emails offer no company profile",
            ),
        )
    check(
        "Profile sent as a link, not attached",
        lambda: (
            not settings.attach_profile,
            "linked"
            if not settings.attach_profile
            else "attached to every first email - a large attachment on a cold "
            "email is a spam signal; set ATTACH_PROFILE=false and PROFILE_URL",
        ),
    )

    recipients = settings.get_lead_alert_recipients()
    check(
        "Hot-lead alerts",
        lambda: (
            bool(recipients),
            ", ".join(recipients)
            or "LEAD_ALERT_EMAIL blank - interested replies notify nobody",
        ),
    )

    def _list_file(raw: str, consequence: str):
        path = PROJECT_ROOT / raw
        if path.is_file():
            return True, shown(path)
        return False, f"{shown(path)} not found - {consequence}"

    def _screening_list(kind: str, raw: str, missing: str, consequence: str):
        """Read a screening list as the agent's loader does, not only find it:
        an empty or unreadable workbook passed a check for the file alone,
        while the send check refuses live sends for it."""
        ok, detail = _list_file(raw, missing)
        if not ok:
            return ok, detail
        from modules.clients import ClientRegister, load_from_xlsx
        from modules.list_crypto import ListRefused

        try:
            register = ClientRegister(
                load_from_xlsx(PROJECT_ROOT / raw, key=settings.lists_key)
            )
        except ListRefused as e:
            # Its words name a file and a setting, never a path or a key.
            return False, f"{detail} refused: {e} - {consequence}"
        except Exception as e:  # the agent's loader contains any error too
            logger.warning("The %s list cannot be read", kind, exc_info=True)
            why = "" if for_page else f" ({e})"
            return False, f"{detail} cannot be read{why} - {consequence}"
        if not register.matchable:
            return False, (
                f"{detail} holds no company the screen can match - {consequence}"
            )
        return True, f"{detail} ({register.matchable} companies)"

    if not settings.client_filter_enabled:
        skip("Client list file", "client filter off")
    elif not (settings.client_list_path or "").strip():
        check(
            "Client list file",
            lambda: (False, "CLIENT_LIST_PATH blank - customers are not screened"),
            blocking=True,
        )
    else:
        # Blocking: live sends are refused while the screen has no list, and
        # while its list screens no company.
        check(
            "Client list file",
            lambda: _screening_list(
                "client",
                settings.client_list_path,
                "existing customers are not screened",
                "customers are not screened",
            ),
            blocking=True,
        )
    competitor_path = (settings.competitor_list_path or "").strip()
    if settings.competitor_filter_enabled and competitor_path:
        # Blocking, on the client list's rule: live sends are refused while a
        # configured competitor list does not load or screens no company.
        check(
            "Competitor list file",
            lambda: _screening_list(
                "competitor",
                competitor_path,
                "no competitor is screened by name",
                "no competitor is screened by name",
            ),
            blocking=True,
        )
    else:
        skip("Competitor list file", "none configured")

    from modules.list_crypto import configured_lists

    def _lists_encrypted():
        from modules.list_crypto import is_encrypted, plain_copies

        if not settings.lists_key:
            return False, (
                "LISTS_KEY blank - the client and competitor lists are plain "
                "files, and any copy of the folder shows them (docs/DEPLOY.md)"
            )
        plain, copies = [], []
        for _label, _setting, path in configured_lists(settings):
            try:
                with open(path, "rb") as f:
                    if not is_encrypted(f.read(64)):
                        plain.append(shown(path))
            except OSError:
                continue
            copies += [shown(copy) for copy in plain_copies(path)]
        if plain:
            return False, f"not encrypted: {', '.join(plain)} - the agent refuses it"
        if copies:
            return False, (
                f"plain copies still on disk: {', '.join(copies)} - delete them"
            )
        return True, "LISTS_KEY set - the lists are read only encrypted"

    if configured_lists(settings):
        check("Lists encrypted", _lists_encrypted)
    else:
        skip("Lists encrypted", "no list configured")

    def _warmup():
        s = settings
        ramp = f"{s.warmup_start_daily}/day, +{s.warmup_increment}/day, cap {s.warmup_max_daily}"
        if not s.warmup_enabled:
            return False, "OFF - a new mailbox sends its full rate from day one"
        if s.warmup_start_daily > 10 or s.warmup_increment > 5:
            return False, (
                f"{ramp} - a new mailbox should start at 10/day or less and "
                "grow by 5/day or less"
            )
        return True, ramp

    check("Warm-up ramp", _warmup)

    if settings.imap_enabled:

        def _imap_credentials():
            accounts = settings.get_sender_accounts()
            missing = [
                a.get("username") or "(primary)"
                for a in accounts
                if not (
                    a.get("imap_username")
                    and (a.get("imap_password") or a.get("microsoft"))
                )
            ]
            if missing:
                return False, f"no IMAP login for {', '.join(missing)}"
            return True, f"{len(accounts)} inbox(es)"

        check("IMAP credentials", _imap_credentials)

        def _inbox_read():
            from modules.imap_monitor import inbox_read

            return inbox_read(
                Database(settings.get_abs_db_path()),
                [a["imap_username"] for a in settings.get_sender_accounts()],
                settings.imap_check_interval_minutes,
            )

        # Blocking: the send check refuses live sends on the same rule.
        check("Inbox read", _inbox_read, blocking=True)

        def _inbox_gaps():
            from modules.imap_monitor import inbox_gaps

            return inbox_gaps(
                Database(settings.get_abs_db_path()),
                [a["imap_username"] for a in settings.get_sender_accounts()],
            )

        # Not blocking: live sends go on; the mail skipped needs a person.
        check("Inbox gap", _inbox_gaps)
    else:
        skip("IMAP credentials", "IMAP off")
        skip("Inbox read", "IMAP off")
        skip("Inbox gap", "IMAP off")

    def _dashboard_password():
        refusal = dashboard_password_refusal(settings)
        if refusal:
            return False, refusal
        return True, "set"

    # Blocking: the dashboard refuses to start on the same rule.
    check("Dashboard password", _dashboard_password, blocking=True)

    def _country_lock():
        from modules.compliance import enabled_countries

        countries = sorted(enabled_countries(settings))
        if not countries:
            return False, (
                "ENABLED_COUNTRIES is blank - no live email may go out until "
                "counsel enables a country (decision L1.1)"
            )
        unknown = (
            "; no-country domains (.com, .eu) ALLOWED"
            if settings.unknown_country_allowed
            else "; no-country domains (.com, .eu) refused"
        )
        return True, f"enabled: {', '.join(countries)}{unknown}"

    check("Country lock", _country_lock, blocking=True)

    def _recorded_key_fingerprint():
        from modules.database import ERASURE_KEY_FINGERPRINT

        path = Path(settings.get_abs_db_path())
        if not path.exists():
            return None
        raw = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            row = raw.execute(
                "SELECT value FROM agent_state WHERE key = ?",
                (ERASURE_KEY_FINGERPRINT,),
            ).fetchone()
        finally:
            raw.close()
        return row[0] if row else None

    def _erasure_key():
        from modules.database import ERASURE_KEY_MIN_LENGTH

        key = settings.erasure_key.strip()
        if not key:
            return False, (
                "ERASURE_KEY is blank - erasure cannot be keyed, so the agent "
                "and dashboard refuse to start (see docs/DEPLOY.md)"
            )
        if len(key) >= ERASURE_KEY_MIN_LENGTH:
            return True, "set (back it up apart from the database; never change it)"
        try:
            kept = _recorded_key_fingerprint() is not None
        except sqlite3.Error:
            kept = False
        if kept:
            return False, (
                f"ERASURE_KEY is shorter than {ERASURE_KEY_MIN_LENGTH} characters, "
                "so it could be guessed. This database already keeps it and it "
                "cannot be changed: people erased under it would no longer be "
                "recognised. The agent and the dashboard refuse to start: "
                "start again with a new database and a long random key "
                "(docs/DEPLOY.md, section 6)"
            )
        return False, (
            f"ERASURE_KEY is shorter than {ERASURE_KEY_MIN_LENGTH} characters - "
            "a new database refuses it. Set a long random key, e.g. the output "
            'of: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )

    check("Erasure key", _erasure_key, blocking=True)

    def _db_check():
        db = Database(settings.get_abs_db_path())
        stats = db.get_stats()
        return True, f"{stats['total_contacts']} contacts, {stats['emails_sent']} sent"

    check("Database accessible", _db_check)

    def _smtp_credentials():
        """Non-empty is not the same as real.

        This passed on `your-name@yourcompany.com` - the value shipped in
        .env.example - and reported [OK]. Everything downstream then agreed:
        the SPF check resolved `yourcompany.com`, found a record belonging to
        somebody else entirely, and also said [OK]. A preflight that greenlights
        the template placeholder is worse than no preflight, because it is the
        one thing an operator trusts before handing the agent over.
        """
        user = (settings.smtp_username or "").strip()
        detail = f"{user or '(missing)'} @ {settings.smtp_host}:{settings.smtp_port}"
        microsoft = settings.get_sender_accounts()[0]["microsoft"]
        if microsoft:
            detail += ", Microsoft sign-in (no password)"
        if not user or not (settings.smtp_password or microsoft):
            return False, detail
        placeholders = (
            "your-name@",
            "yourcompany.com",
            "example.com",
            "changeme",
            "your-email",
            "user@domain",
        )
        hit = next((p for p in placeholders if p in user.lower()), None)
        if hit:
            return False, f"{detail} - still the .env.example placeholder ({hit!r})"
        return True, detail

    check("SMTP credentials set", _smtp_credentials)

    def _microsoft_signin():
        from modules import microsoft_signin as ms

        if not settings.microsoft_signin_on():
            if any(
                ms.is_microsoft_mail_host(a["smtp_host"])
                or (settings.imap_enabled and ms.is_microsoft_mail_host(a["imap_host"]))
                for a in settings.get_sender_accounts()
            ):
                return False, (
                    "a Microsoft 365 mailbox signs in with a password. Microsoft "
                    "already refuses passwords for IMAP and turns password SMTP "
                    "off by default at the end of December 2026, and a stored "
                    "password lets whoever copies it send mail as the client. Set "
                    'up Microsoft sign-in (docs/DEPLOY.md, "Connect to '
                    'Microsoft 365 without a password")'
                )
            return None, "not used - no mailbox on Microsoft 365"
        problems = settings.microsoft_signin_problems()
        if problems:
            return False, "; ".join(problems)
        return ms.expires_soon(
            settings.get_abs_microsoft_certificate_path(),
            settings.microsoft_certificate_passphrase,
        )

    check("Microsoft sign-in", _microsoft_signin)

    if live_smtp:

        def _smtp_login():
            import smtplib
            import ssl

            from modules import microsoft_signin as ms

            signin = (
                ms.from_settings(settings)
                if settings.get_sender_accounts()[0]["microsoft"]
                else None
            )
            try:
                token = signin.token() if signin is not None else None
            except ms.SignInRefused as e:
                return False, f"no token: {e}"

            def _login(s):
                if token is None:
                    s.login(settings.smtp_username, settings.smtp_password)
                    return
                try:
                    ms.smtp_authenticate(s, settings.smtp_username, token)
                except smtplib.SMTPAuthenticationError as e:
                    server = e.smtp_error
                    if isinstance(server, bytes):
                        server = server.decode("utf-8", "replace")
                    raise ms.SignInRefused(
                        f"{ms.explain_mailbox_refusal('SMTP', server)} "
                        f"(server said: {e.smtp_code} {server})"
                    ) from None

            ctx = ssl.create_default_context()
            try:
                if settings.smtp_port == 465:
                    with smtplib.SMTP_SSL(
                        settings.smtp_host, settings.smtp_port, context=ctx, timeout=15
                    ) as s:
                        _login(s)
                else:
                    with smtplib.SMTP(
                        settings.smtp_host, settings.smtp_port, timeout=15
                    ) as s:
                        s.ehlo()
                        s.starttls(context=ctx)
                        s.ehlo()
                        _login(s)
            except ms.SignInRefused as e:
                return False, str(e)
            return True, "login OK" if token is None else "login OK (Microsoft token)"

        check("SMTP login (live)", _smtp_login)

        def _microsoft_login():
            import imaplib

            from modules import microsoft_signin as ms

            signin = ms.from_settings(settings)
            if signin is None:
                return None, "not used - Microsoft sign-in is not set up"
            try:
                signin.token()
            except (ms.SignInRefused, ms.SignInUnavailable) as e:
                return False, f"no token: {e}"
            if not settings.imap_enabled:
                return True, "token issued (IMAP off, so no inbox was opened)"
            inboxes = {}
            for account in settings.get_sender_accounts():
                if account["microsoft"]:
                    inboxes.setdefault(account["imap_username"], account)
            for name, account in inboxes.items():
                try:
                    ms.check_imap(
                        signin, account["imap_host"], account["imap_port"], name
                    )
                except imaplib.IMAP4.error as e:
                    return False, (
                        f"{name}: {ms.explain_mailbox_refusal('IMAP', str(e))} "
                        f"(server said: {e})"
                    )
            return True, (
                f"token issued; IMAP sign-in and NOOP OK for {len(inboxes)} inbox(es)"
            )

        check("Microsoft sign-in (live)", _microsoft_login)

    def _dns_check():
        import dns.resolver

        answers = dns.resolver.resolve("gmail.com", "MX")
        return len(answers) > 0, f"DNS OK ({len(answers)} MX for gmail.com)"

    if network:
        check("DNS resolution (dnspython)", _dns_check)
    else:
        skip("DNS resolution (dnspython)", "not checked - network checks off")

    def _deliverability_check():
        from modules.deliverability import DeliverabilityLinter

        tpl = fh.get_email_template()
        if not tpl:
            return False, "template missing"
        issues = DeliverabilityLinter.lint(tpl)
        return (len(issues) == 0, "no issues" if not issues else "; ".join(issues[:3]))

    check("Email template deliverability", _deliverability_check)

    sender_domain = (
        settings.smtp_username.rsplit("@", 1)[-1]
        if "@" in settings.smtp_username
        else ""
    )
    if sender_domain:
        # The dashboard's own lookup, so the two give the same verdict in the
        # same words. A lookup that could not be made is a SKIP: doctor read a
        # timeout as "no record", and so did a missing dnspython.
        auth_records: dict = {}

        def _auth_record(name):
            if not auth_records:
                found = _lookup_auth_records(sender_domain)
                auth_records.update(zip(("SPF", "DMARC"), found))
            return auth_records[name], auth_record_words(name, auth_records[name])

        def _spf():
            return _auth_record("SPF")

        def _dmarc():
            return _auth_record("DMARC")

        # Only meaningful for a domain we will actually send from. Against the
        # .env.example placeholder these resolved yourcompany.com - a real
        # registered domain belonging to somebody else - and reported its SPF
        # record as [OK], which reads as "your sending setup is fine" when
        # nothing about the sending setup has been configured at all.
        if not network:
            skip("SPF / DMARC", "not checked - network checks off")
        elif _smtp_credentials()[0]:
            check(f"SPF record ({sender_domain})", _spf)
            check(f"DMARC record ({sender_domain})", _dmarc)

            def _dkim():
                found, selector = _lookup_dkim(sender_domain)
                return found, dkim_words(found, selector)

            check(f"DKIM key ({sender_domain})", _dkim)
        else:
            skip(
                "SPF / DMARC",
                f"not checked - {sender_domain} is not a configured sending domain",
            )

    placeholder = settings.sender_name.strip().lower() in (
        "",
        "your name",
        "pharmaceutical outreach",
        # Found in a live .env with doctor reporting [OK]: it would have
        # signed every email.
        "the name of the sender",
    ) or settings.company_name.strip().lower() in (
        "",
        "your pharmaceutical company",
        "pharmacorp",
    )
    check(
        "Sender identity set (not placeholder)",
        lambda: (not placeholder, f"{settings.sender_name} / {settings.company_name}"),
    )

    def _legal_identity():
        missing = settings.missing_legal_identity()
        if missing:
            return False, f"{', '.join(missing)} blank - live sends refused"
        return True, settings.legal_entity_name

    check("Legal identity (email footer)", _legal_identity, blocking=True)

    def _legal_footer_in_templates():
        from modules.compliance import missing_placeholders

        tpl_dir = _templates_dir()
        files = sorted(tpl_dir.glob("email_template*.html")) + sorted(
            tpl_dir.glob("followup_*.html")
        )
        gaps = [
            f"{p.name} lacks {', '.join(m)}"
            for p in files
            if (m := missing_placeholders(p.read_text(encoding="utf-8")))
        ]
        if gaps:
            return False, "; ".join(gaps)
        return True, f"{len(files)} template(s) checked"

    check("Legal footer in templates", _legal_footer_in_templates, blocking=True)

    check(
        "Open tracking (ePrivacy consent)",
        lambda: (
            not settings.open_tracking_enabled,
            "off"
            if not settings.open_tracking_enabled
            else "ON - pixels need the recipient's prior consent (ePrivacy "
            "Art. 5(3)); cold prospects have given none",
        ),
    )

    def _opt_out_route():
        from modules.compliance import opt_out_route

        return opt_out_route(settings)

    # Blocking: the send check refuses live sends on the same rule.
    check("Opt-out route", _opt_out_route, blocking=True)

    def _retention():
        if not settings.retention_enabled:
            return False, "OFF - personal data is kept indefinitely (GDPR Art. 5(1)(e))"
        try:
            last = Database(settings.get_abs_db_path()).get_state(
                "retention:last_run_date"
            )
        except (sqlite3.Error, OSError):
            last = None
        return True, (
            f"{settings.first_contact_max_age_days}d to the first email / "
            f"{settings.retention_days_contacted}d no reply / "
            f"{settings.retention_days_engaged}d engaged; last run {last or 'never'}"
        )

    check("Data retention", _retention)

    # ── The GDPR machinery (step G17) ──
    def _state(key):
        try:
            return Database(settings.get_abs_db_path()).get_state(key)
        except (sqlite3.Error, OSError, RuntimeError):
            return None

    def _recent(key, how):
        from datetime import timedelta

        from modules import clock

        last = _state(key)
        if not last:
            return False, f"never - {how}"
        if last[:10] < (clock.now() - timedelta(days=2)).strftime("%Y-%m-%d"):
            return False, f"last {last[:10]} - {how}"
        return True, f"last {last[:10]}"

    if settings.retention_enabled:
        check(
            "Retention run",
            lambda: _recent(
                "retention:last_run_date",
                "schedule `main.py retention` daily (DEPLOY.md section 8)",
            ),
        )
    else:
        skip("Retention run", "retention off")
    check(
        "Backup",
        lambda: _recent(
            "backup:last_run", "schedule `main.py backup` daily (DEPLOY.md section 9)"
        ),
    )

    def _logs_masked():
        from modules.compliance import mask_personal

        folder = log_dir or PROJECT_ROOT / "logs"
        lines = files = 0
        for path in sorted(folder.glob("*.log*")) if folder.is_dir() else []:
            text = path.read_text(encoding="utf-8", errors="replace")
            clear = sum(1 for line in text.splitlines() if mask_personal(line) != line)
            if clear:
                lines += clear
                files += 1
        if lines:
            return False, (
                f"{lines} line(s) in {files} file(s) under {folder} hold an "
                "address, token, key, password or IP in clear - delete those files"
            )
        return True, "nothing in clear"

    check("Log files masked", _logs_masked)

    def _held_optouts():
        from modules.optout_spool import (
            MAX_UNMATCHED_TOKENS,
            pending_addresses,
            pending_tokens,
            unmatched_tokens,
        )

        path = settings.get_abs_db_path()
        unmatched = unmatched_tokens(path)
        held = pending_addresses(path) | pending_tokens(path)
        if held:
            return False, (
                f"{len(held)} opt-out(s) held while the database was locked - "
                "applied at the next start; those addresses are refused meanwhile"
            )
        if len(unmatched) >= MAX_UNMATCHED_TOKENS:
            return False, (
                f"{len(unmatched)} held clicks match no link we recorded, the most "
                "we keep: a new one is refused until retention deletes the oldest, "
                "after a year. Links on record still work. Most likely a flood of "
                "made-up links"
            )
        if unmatched:
            return True, (
                f"{len(unmatched)} held click(s) match no link we recorded; each "
                "is set aside for a year and applied if its recipient comes up"
            )
        return True, "none"

    check("Held opt-outs", _held_optouts)

    check(
        "Database log truncated",
        lambda: (
            (
                False,
                (
                    "an erasure's copy may linger in the database's log - "
                    "run `main.py retention` or restart"
                ),
            )
            if _state("wal_truncate_pending") == "1"
            else (True, "yes")
        ),
    )

    def _erasures_finished():
        from modules.journal import (
            _company_left_in_clear,
            half_way_erasures,
            pending_folder,
            pending_rewrites,
            unreadable_pending,
        )

        path = settings.get_abs_db_path()
        unreadable = unreadable_pending(path)
        waiting = len(pending_rewrites(path)) - len(unreadable)
        problems = []
        if waiting:
            problems.append(
                f"{waiting} erasure(s) with files not changed yet: the journal, an "
                "access answer or a held opt-out may still name them. Another "
                "program may hold the file, or the disk may be full (the log says "
                "why) - once it is free, run `main.py backup`"
            )
        if unreadable:
            problems.append(
                f"unreadable in {shown(pending_folder(path))}, so nothing can finish "
                f"them: {', '.join(unreadable)} - look, then delete them"
            )
        could_not = None
        if not (settings.erasure_key or "").strip():
            could_not = "could not check for half-way erasures: no erasure key"
        else:
            try:
                half_way = half_way_erasures(path, timeout=0 if for_page else 5.0)
            except sqlite3.Error as e:
                busy = getattr(e, "sqlite_errorcode", None) in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                )
                if for_page and busy:
                    # The page polls this: it says so at once, as the
                    # Data-subject requests row does, rather than wait again
                    # on the journal check's own read of the same database.
                    busy_words = "could not be checked - the database is busy"
                    if problems:
                        return False, "; ".join(problems + [busy_words])
                    return None, busy_words
                could_not = "could not check for half-way erasures" + (
                    " - see the server log" if for_page else f": {e}"
                )
                if for_page:
                    logger.warning("Could not check for half-way erasures: %s", e)
            else:
                if half_way:
                    problems.append(
                        f"{half_way} address(es) still held in clear though erased: "
                        "an erasure stopped half-way - run it again with "
                        "`main.py dsr erase <id>` or `main.py gdpr-erase <address>`"
                    )
            companies = _company_left_in_clear(path)
            if companies is None:
                could_not = "; ".join(
                    filter(
                        None,
                        [could_not, "could not check the journal for erased companies"],
                    )
                )
            elif companies:
                problems.append(
                    f"{len(companies)} erased company domain(s) still named in "
                    "clear in the journal - once it is free, run `main.py backup`"
                )
        if problems:
            return False, "; ".join(problems + ([could_not] if could_not else []))
        if could_not:
            return None, could_not
        return True, "yes"

    check("Erasures finished", _erasures_finished)

    def _requests():
        from modules import clock

        # Read only, as _key_matches does: a Database writes its schema when
        # it opens, which waits out a write lock another process holds. The
        # page polls this, so it does not wait even for a read.
        path = Path(settings.get_abs_db_path())
        if not path.exists():
            return True, "no database yet"
        try:
            raw = sqlite3.connect(
                f"{path.as_uri()}?mode=ro", uri=True, timeout=0 if for_page else 5.0
            )
            try:
                open_requests = raw.execute(
                    "SELECT id, due_at FROM dsr_requests WHERE status = 'open' "
                    "ORDER BY due_at"
                ).fetchall()
            finally:
                raw.close()
        except sqlite3.Error as e:
            if e.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return None, "could not be checked - the database is busy"
            return None, "database unreadable"
        now = clock.now().isoformat()
        late = [rid for rid, due in open_requests if due < now]
        if late:
            return False, "overdue: " + ", ".join(f"#{i}" for i in late) + (
                " - answer them now (`main.py dsr list`)"
            )
        return True, f"{len(open_requests)} open, none overdue"

    check("Data-subject requests", _requests)

    def _ai_agreement():
        """COMPANY_COUNTRY_AI is not one of the AI switches, as the footer's
        AI sentence is about writing emails and reading replies; but with a
        key it sends a company's name, domain and page description, so
        doctor counts it here."""
        from modules.ai_generator import AIGenerator, own_server_options
        from modules.compliance import ai_in_use, italian_countries

        country_ai = (
            settings.company_country_ai
            and bool(italian_countries(settings))
            and AIGenerator(**own_server_options(settings)).is_configured
        )
        if not (ai_in_use(settings) or country_ai):
            return None, "no AI sees recipients' data"
        if not (settings.ai_dpa_reference or "").strip():
            return False, (
                "an AI sees recipients' data and AI_DPA_REFERENCE is blank - "
                "record where the provider's processing agreement is filed"
            )
        return True, settings.ai_dpa_reference

    check("AI processing agreement", _ai_agreement)

    def _key_matches():
        from modules.database import ERASURE_KEY_FINGERPRINT, erasure_key_fingerprint

        if not (settings.erasure_key or "").strip():
            return None, "no key set - see Erasure key"
        # Read without opening a Database: with the wrong key set, as main()
        # sets it for doctor too, that refuses to open, and the check then
        # passed as if no key had been recorded.
        path = Path(settings.get_abs_db_path())
        if not path.exists():
            return True, "no database yet"
        try:
            raw = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            try:
                row = raw.execute(
                    "SELECT value FROM agent_state WHERE key = ?",
                    (ERASURE_KEY_FINGERPRINT,),
                ).fetchone()
            finally:
                raw.close()
        except sqlite3.Error as e:
            return None, f"database unreadable: {e}"
        stored = row[0] if row else None
        if stored is None:
            return True, "no key recorded yet"
        if stored != erasure_key_fingerprint(settings.erasure_key):
            return False, (
                "ERASURE_KEY differs from the key this database was erased with "
                "- the agent refuses to start; restore the original key"
            )
        return True, "matches"

    check("Erasure key matches the database", _key_matches, blocking=True)

    def _journal_replayed():
        from modules.journal import unreplayed

        try:
            behind = unreplayed(
                settings.get_abs_db_path(), timeout=0 if for_page else 5.0
            )
        except sqlite3.Error as e:
            busy = getattr(e, "sqlite_errorcode", None) in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            )
            if not (for_page and busy):
                raise
            # The page polls this, as it does the erasures row: it says so at
            # once. Start and Direct Send wait, and refuse if still busy.
            return None, "could not be checked - the database is busy"
        if behind:
            return False, _behind_the_journal(behind)
        return True, "the database knows every journal line"

    # Blocking: run, the dashboard's Start and Direct Send refuse on it.
    check("Journal replayed", _journal_replayed, blocking=True)

    check(
        "Share capital (Cod. Civ. 2250)",
        lambda: (
            bool((settings.legal_share_capital or "").strip()),
            settings.legal_share_capital
            or "blank - required for an S.p.A./S.r.l.; set LEGAL_SHARE_CAPITAL",
        ),
    )

    def _ai_generator():
        """The generator the agent builds, from the same settings, so the
        row names the model the agent will ask.

        SKIP, not WARN, when the AI is off: the feature is optional, and a
        WARN flipped "All checks passed" for a setup with nothing wrong.
        The own server is not asked here, so "configured" alone read as
        "server OK" with the server down; the row that asks it is named."""
        from modules.ai_generator import AIGenerator, own_server_options

        ai = AIGenerator(**own_server_options(settings))
        if not ai.is_configured:
            return None, (
                "off - LLM_PROVIDER is not openai_compatible (AI features disabled)"
            )
        return True, (
            f"openai_compatible: configured, model {ai.MODEL_NAME} "
            "(not contacted here - see AI server reachable)"
        )

    check("AI generator (optional)", _ai_generator)

    # Every row below is at most a warning: each AI use falls back to the
    # path without AI, so a model server that is down never stops a send.
    if settings.llm_provider == "openai_compatible":
        server = urlsplit(settings.llm_base_url).netloc
        path = urlsplit(settings.llm_base_url).path

        def _ai_server():
            """The whole address, not host:port alone: a path left off or
            wrong is the likeliest slip, and with host:port it read the same
            as a correct address while every call answered 404.

            Not assessed (SKIP), never OK: this row only repeats the settings,
            and an [OK] here read as "server OK" while it was down or while
            nothing was asked at all. "AI server reachable" asks it."""
            named = f"{settings.llm_base_url}, model {settings.llm_model}"
            if path.lower().endswith("/v1"):
                return None, f"{named} (settings only - see AI server reachable)"
            return False, (
                f"{named} - LLM_BASE_URL's path is {path or 'empty'}; the "
                "server's API usually starts at /v1 (for Ollama "
                "http://<host>:11434/v1)"
            )

        check("AI server", _ai_server)

        def _ai_server_is_local():
            """Resolved again here, as before every AI call: the settings
            were checked when they loaded, and a DNS answer can change."""
            from config import own_server_host_problem

            problem = own_server_host_problem(
                urlsplit(settings.llm_base_url).hostname or ""
            )
            if problem:
                return False, (
                    f"LLM_BASE_URL {problem}; until then no AI question is sent"
                )
            return True, (
                "every address its host resolves to is on this machine or the "
                "private network"
            )

        check(
            "AI server stays on this machine or private network", _ai_server_is_local
        )

        # Set by the list row: the question goes only to a server that
        # answered it.
        list_row = {"reached": False}
        _pull_or_rename = (
            f"pull {settings.llm_model} on the server, or set LLM_MODEL to a "
            "name it lists"
        )

        def _ai_server_reachable():
            """One GET of the server's list of models, which carries no
            personal data, so IT can tell a stopped server or a model never
            pulled from a working one before the first send pass."""
            import httpx

            from modules.ai_generator import (
                AddressNotLocal,
                model_listed,
                own_server_models,
            )

            try:
                models = own_server_models(
                    settings.llm_base_url, settings.llm_api_key
                )
            except AddressNotLocal as e:
                return False, f"not asked: {e}"
            except (httpx.HTTPError, ValueError, TypeError) as e:
                if isinstance(e, httpx.TransportError):
                    step = (
                        "check that the model server is running and that its "
                        "host, port and firewall let this machine reach it"
                    )
                elif (
                    isinstance(e, httpx.HTTPStatusError)
                    and e.response.status_code == 404
                ):
                    step = (
                        f"LLM_BASE_URL path {path or '(none)'} answered 404 - "
                        "for Ollama it ends in /v1"
                    )
                else:
                    step = (
                        "something answered there: check that LLM_BASE_URL is "
                        "the model server's own address"
                    )
                return False, (
                    f"not reachable or not understood at {server} ({e}); {step}; "
                    "until then every AI use falls back to the path without AI"
                )
            list_row["reached"] = True
            if model_listed(settings.llm_model, models):
                return True, f"reachable, {settings.llm_model} is listed"
            listed = ", ".join(models[:5]) or "none"
            return False, (
                f"reachable, but {settings.llm_model} is not in its list "
                f"(it lists: {listed}) - {_pull_or_rename}; until then every AI "
                "use falls back to the path without AI"
            )

        def _ai_server_answers():
            """One fixed question with no personal data, by the call every
            AI use makes: the same address, model, key, reasoning setting
            and timeout. A model can be listed and still answer nothing in
            time: Qwen 3.5 on Ollama was listed, and reasoned for over three
            minutes on a one-word question.

            Not asked unless the list row reached the server, and then it
            waits up to LLM_TIMEOUT_SECONDS. None of the answers refuses
            live sending: every AI use falls back to the path without AI."""
            import httpx

            from modules.ai_generator import (
                AIGenerator,
                own_server_options,
                server_reason,
            )

            if not list_row["reached"]:
                return None, "not asked - the server did not answer the list row"
            ai = AIGenerator(**own_server_options(settings))
            started = time.monotonic()
            try:
                text = ai.ask_once(AI_CHECK_QUESTION)
            except httpx.TimeoutException:
                return False, (
                    f"no answer within {settings.llm_timeout_seconds} s "
                    "(LLM_TIMEOUT_SECONDS) - try LLM_REASONING_EFFORT=none, a "
                    "longer LLM_TIMEOUT_SECONDS, or a smaller or faster model"
                )
            except httpx.HTTPStatusError as e:
                reason = " ".join(server_reason(e).split())
                if "reasoning" in reason.lower():
                    hint = (
                        " - LLM_REASONING_EFFORT can be left blank, so that no "
                        "reasoning_effort is sent"
                    )
                elif e.response.status_code == 404 and (
                    "model" in reason.lower() and "not found" in reason.lower()
                ):
                    hint = f" - {_pull_or_rename}"
                else:
                    hint = ""
                return False, (
                    f"refused: HTTP {e.response.status_code} "
                    f"{reason[:120]!r}{hint}"
                )
            except httpx.HTTPError as e:
                return False, f"no answer ({e})"
            except (ValueError, KeyError, IndexError, TypeError) as e:
                return False, (
                    f"answered, but not in the OpenAI shape ({type(e).__name__})"
                )
            took = time.monotonic() - started
            if text.strip().strip(".!").upper() == "YES":
                return True, f"answered as asked in {took:.1f} s"
            return False, (
                f"answered in {took:.1f} s, but not as asked: {text.strip()[:40]!r}"
                " - the model may not follow instructions well enough for "
                "this work"
            )

        if network:
            check("AI server reachable", _ai_server_reachable)
            check("AI server answers", _ai_server_answers)
        else:
            skip("AI server reachable", "not checked - network checks off")
            skip("AI server answers", "not asked - network checks off")
    elif (settings.llm_base_url or "").strip():
        check(
            "AI server",
            lambda: (
                False,
                (
                    "LLM_BASE_URL is set but LLM_PROVIDER is blank, so the AI "
                    "is off - the address is not used"
                ),
            ),
        )

    return checks


# The doctor checks the dashboard's "Ready to send" card shows, and nothing
# else: an allow-list, so a check added to the doctor later never reaches the
# page without someone deciding it belongs there.
READINESS_CHECKS = (
    "Sender title",
    "PDF attachment present",
    "Italian email template present",
    "Client list file",
    "Competitor list file",
    "Inbox read",
    "Country lock",
    "Legal identity (email footer)",
    "Opt-out route",
    "Legal footer in templates",
    "SMTP credentials set",
    "Journal replayed",
)

# The doctor reports these as warnings, but a live send cannot go without
# them: with no SMTP login every live email fails at the server.
REFUSES_EVERY_LIVE_SEND = {
    "SMTP credentials set": "every live email would fail at the SMTP login",
}

# The doctor's privacy checks the "Privacy and inbox" card shows, from the
# same run. Their details carry counts, request ids, record file names and
# the operator's own mailboxes, never a recipient's address.
PRIVACY_CHECKS = (
    "Erasures finished",
    "Held opt-outs",
    "Data-subject requests",
    "Inbox gap",
)


def _competitor_count(settings: Settings) -> dict:
    """The named-competitor list as the agent's loader reads it."""
    raw = (settings.competitor_list_path or "").strip()
    if not (settings.competitor_filter_enabled and raw):
        return {"state": "off", "count": None}
    from modules.clients import ClientRegister, load_from_xlsx

    try:
        register = ClientRegister(
            load_from_xlsx(PROJECT_ROOT / raw, key=settings.lists_key)
        )
    except Exception:  # the agent's loader contains any error too
        logger.warning("The competitor list cannot be read", exc_info=True)
        return {"state": "unreadable", "count": None}
    return {"state": "ok", "count": register.matchable}


def _first_email_texts(settings: Settings, fh: FileHandler) -> dict:
    """The first-email texts each language's recipients can get, as
    process_contact chooses them: {language: [(file name, text), ...]}.

    With A/B testing on, an English recipient gets any arm, the main text
    among them. Italian recipients get the English text while
    email_template_it.html is missing, so there is an Italian group only with
    the file and a country in ITALIAN_COUNTRIES.
    """
    from modules.compliance import italian_countries

    english = fh.get_email_template()
    arms = fh.get_template_variants() if settings.ab_testing_enabled else []
    if arms:
        texts = {"English": [(f"{name}.html", text) for name, text in arms]}
    else:
        texts = {
            "English": [("email_template.html", english)] if english is not None else []
        }
    italian = fh.get_email_template("email_template_it.html")
    if not _blank_template(fh, "email_template_it.html") and italian_countries(
        settings
    ):
        texts["Italian"] = [("email_template_it.html", italian)]
    return texts


def _blank_template(fh: FileHandler, name: str) -> bool:
    """True when the template is missing or holds only white space: the send
    path then uses the English text, so the dashboard must say so too."""
    return not (fh.get_email_template(name) or "").strip()


def italian_first_email_missing(fh: FileHandler) -> bool:
    """True when process_contact sends Italian recipients the English first
    email: email_template_it.html is missing or blank."""
    return _blank_template(fh, "email_template_it.html")


def italian_followups_missing(fh: FileHandler) -> list[str]:
    """The follow-up steps whose Italian text is missing or blank, so
    backlog_draining sends Italian recipients the English one: "1", "2"."""
    return [
        step
        for step in ("1", "2")
        if _blank_template(fh, f"followup_{step}_it.html")
    ]


def language_as_sent(
    language: str,
    reason: str,
    words: str,
    first_missing: bool,
    followups_missing: list[str],
) -> tuple[str, str]:
    """The language a contact's emails go in, and why, as the send path
    chooses it: with no Italian first email on disk, an Italian contact not
    yet emailed gets the English one, and every follow-up goes in English;
    with one Italian follow-up missing, that follow-up goes in English."""
    if language != "it":
        return language, words
    if first_missing:
        if reason != "sent":
            return "en", (
                "Italian, but templates/email_template_it.html is missing, so the "
                "English first email goes"
            )
        return language, (
            f"{words}, but templates/email_template_it.html is missing, so its "
            "follow-ups go in English"
        )
    if len(followups_missing) == 2:
        return language, (
            f"{words}, but templates/followup_1_it.html and followup_2_it.html "
            "are missing, so follow-ups 1 and 2 go in English"
        )
    if followups_missing:
        step = followups_missing[0]
        return language, (
            f"{words}, but templates/followup_{step}_it.html is missing, so "
            f"follow-up {step} goes in English"
        )
    return language, words


def _mailbox_name(account: dict) -> str:
    return account["username"] or "the primary mailbox"


def _first_email_rules(settings: Settings, fh: FileHandler, checks: list) -> list:
    """Rewrite the Sender title and Legal footer rows from the rules the send
    path applies, and return what holds back some first emails but not all.

    The doctor fails these checks on any gap, but a gap often stops only some
    first emails: send_verdict sends through a mailbox that has a title, and
    process_contact refuses only a chosen text without {{legal_footer}}. So a
    row refuses (blocking) only when, with the other, it leaves no language a
    live first email can go out in.
    """
    rows = {c["label"]: c for c in checks}
    texts = _first_email_texts(settings, fh)
    refused = {language: [] for language in texts}
    partial = {}

    accounts = settings.get_sender_accounts()
    untitled = [_mailbox_name(a) for a in accounts if not a["sender_title"]]
    titled = [_mailbox_name(a) for a in accounts if a["sender_title"]]
    needing = [
        language
        for language, files in texts.items()
        if any("{{sender_title}}" in text for _name, text in files)
    ]
    title = rows.get("Sender title")
    if title is not None:
        if not needing:
            title.update(
                ok=None, detail="no first-email template uses {{sender_title}}"
            )
        elif not untitled:
            title.update(
                ok=True,
                detail=", ".join(dict.fromkeys(a["sender_title"] for a in accounts)),
            )
        elif titled:
            title.update(
                ok=False,
                detail=f"SENDER_TITLE blank for {', '.join(untitled)} - first emails "
                f"go out through {', '.join(titled)} instead",
            )
        else:
            for language in needing:
                refused[language].append("Sender title")
            if len(needing) == len(texts):
                consequence = (
                    "the first email says what its sender is responsible for, "
                    "so live first emails are refused"
                )
            else:
                consequence = partial["Sender title"] = (
                    f"{' and '.join(needing)} first emails are held back until "
                    "a mailbox has a SENDER_TITLE"
                )
            title.update(
                ok=False,
                detail=f"SENDER_TITLE is blank on every mailbox - {consequence}",
            )

    footer = rows.get("Legal footer in templates")
    if footer is not None and footer["ok"] is False:
        said = []
        for language, files in texts.items():
            lacking = [n for n, text in files if "{{legal_footer}}" not in text]
            if lacking and len(lacking) == len(files):
                refused[language].append("Legal footer in templates")
                said.append(f"{language} first emails are refused")
            elif lacking:
                said.append(
                    f"some {language} first emails are refused ({', '.join(lacking)})"
                )
        if said:
            partial["Legal footer in templates"] = "; ".join(said)
        footer["detail"] = (
            "; ".join(said) if said else "first emails are not refused for this"
        ) + f": {footer['detail']}"

    stopped = bool(texts) and all(refused.values())
    for label in ("Sender title", "Legal footer in templates"):
        if label in rows:
            rows[label]["blocking"] = stopped and any(
                label in why for why in refused.values()
            )
    return [] if stopped else list(partial.values())


def readiness_report(settings: Settings) -> dict:
    """Whether a live first email can go out, from the doctor's checks and
    the first-email rules of the send path.

    can_send_live is False only when no live first email can go, and
    held_back says what stops some of them. Read-only: the doctor only reads. send_blocker() and first_email_blocker()
    are not called, because they write activity rows and change the agent's
    state. The doctor reads the files on disk, so a running agent whose
    reload failed may still hold an older client list.
    """
    doctor = [
        {"label": c.label, "ok": c.ok, "detail": c.detail, "blocking": c.blocking}
        for c in run_doctor_checks(
            settings,
            network=False,
            only=READINESS_CHECKS + PRIVACY_CHECKS,
            for_page=True,
        )
    ]
    checks = [c for c in doctor if c["label"] in READINESS_CHECKS]
    for c in checks:
        consequence = REFUSES_EVERY_LIVE_SEND.get(c["label"])
        if consequence and c["ok"] is False:
            c["blocking"] = True
            c["detail"] = f"{c['detail']} - {consequence}"
    fh = FileHandler()
    held_back = _first_email_rules(settings, fh, checks)
    return {
        "checks": checks,
        "privacy": [c for c in doctor if c["label"] in PRIVACY_CHECKS],
        "can_send_live": not any(c["blocking"] and c["ok"] is False for c in checks),
        "held_back": held_back,
        "dry_run": settings.dry_run,
        "mailboxes": [
            {"address": a["username"], "title": a["sender_title"]}
            for a in settings.get_sender_accounts()
        ],
        "italian_followups": {
            name: fh.get_email_template(f"{name}.html") is not None
            for name in ("followup_1_it", "followup_2_it")
        },
        "competitor": _competitor_count(settings),
    }


TEMPLATE_EDITOR_FILES = {
    "email_template": "email_template.html",
    "followup_1": "followup_1.html",
    "followup_2": "followup_2.html",
    "email_template_it": "email_template_it.html",
    "followup_1_it": "followup_1_it.html",
    "followup_2_it": "followup_2_it.html",
}


def cmd_doctor(args, settings: Settings):
    """Run preflight checks and report configuration health before a run.

    Exits 1 when a blocking check fails, so a deploy script can stop on it.
    """
    checks = run_doctor_checks(settings, live_smtp=getattr(args, "smtp", False))

    print("\n" + "=" * 64)
    print("  B2B OUTREACH AGENT - DOCTOR (preflight checks)")
    print("=" * 64)
    all_ok = True
    blocked = False
    for c in checks:
        # Tri-state. None means "could not be assessed", which is not a failure
        # in its own right - the thing it depends on is already reporting one,
        # and raising a second WARN for the same root cause just buries it.
        if c.ok is None:
            mark = "[SKIP]"
        elif c.ok:
            mark = "[OK]  "
        elif c.blocking:
            mark = "[FAIL]"
            all_ok = False
            blocked = True
        else:
            mark = "[WARN]"
            all_ok = False
        print(f"  {mark} {c.label:<38} {c.detail}")
    print("=" * 64)
    if blocked:
        print("  Live sending is refused until every [FAIL] above is fixed.")
    print(
        "  All checks passed."
        if all_ok
        else "  Some checks need attention (see [WARN] above)."
    )
    print()
    if blocked:
        sys.exit(1)


def cmd_ai_benchmark(args, settings: Settings):
    """Score the configured AI model on the fixed exam of the agent's own
    questions (modules/ai_benchmark.py). Exits 1 when the cases are refused,
    the AI cannot be asked, or a model's run stopped before its end."""
    from modules.ai_benchmark import run_command

    status = run_command(args, settings)
    if status:
        sys.exit(status)


def cmd_lists(args, settings: Settings):
    """Encrypt a plain client or competitor list with LISTS_KEY, check the
    configured lists, or decrypt one to a file named by --out
    (modules/list_crypto.py). Exits 1 when it did not do what was asked."""
    from modules.list_crypto import run_command

    status = run_command(args, settings)
    if status:
        sys.exit(status)


def cmd_ai_proof(args, settings: Settings):
    """Run the whole agent with the configured AI on a few invented cases,
    sending nothing (modules/ai_proof.py). Exits 1 when the run is refused,
    stopped, or reaches for the network beyond the AI server."""
    from modules.ai_proof import run_command

    status = run_command(args, settings)
    if status:
        sys.exit(status)


# ─────────────────────────────────────────────────────────────────────
# Flask Dashboard
# ─────────────────────────────────────────────────────────────────────

# The deliverability panel's SPF/DMARC answer for a sending domain is kept this
# long: DNS records change rarely, and asking on every refresh held the whole
# dashboard up while the panels beside it waited.
AUTH_RECORDS_TTL_SECONDS = 600
# One entry per sending domain; a handful covers every address ever configured.
AUTH_RECORDS_MAX_DOMAINS = 16
# Both lookups together never hold the request longer than this.
AUTH_RECORDS_DEADLINE_SECONDS = 3.0
_NO_SENDING_DOMAIN_NOTE = "No sending domain is set, so SPF and DMARC were not checked."


def _lookup_auth_records(domain: str) -> tuple:
    """(spf, dmarc) for the domain: True found, False absent, None unknown.

    The two TXT lookups run side by side and share one deadline, so a
    resolver that hangs costs the dashboard AUTH_RECORDS_DEADLINE_SECONDS in
    total, not that much per record. A lookup still running at the deadline
    is left to finish on its own daemon thread and counts as unknown.
    """
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        # dnspython is an optional extra; the panel says the records could
        # not be checked rather than failing the whole request.
        logger.debug("dnspython is not installed - skipping the SPF/DMARC check")
        return None, None
    try:
        res = dns.resolver.Resolver()
    except (dns.exception.DNSException, OSError) as e:
        # Building the Resolver reads the host resolver config and raises
        # NoResolverConfiguration (a DNSException) or OSError when there is
        # none to read.
        logger.warning("SPF/DMARC check unavailable: %s", e)
        return None, None
    res.lifetime = res.timeout = AUTH_RECORDS_DEADLINE_SECONDS

    found = {}

    def _has(key, name, marker):
        try:
            for rr in res.resolve(name, "TXT"):
                if marker in b"".join(rr.strings).decode("utf-8", "ignore"):
                    found[key] = True
                    return
            found[key] = False
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            # The name or its TXT records do not exist: the record is absent.
            found[key] = False
        except (dns.exception.DNSException, OSError) as e:
            # A timeout or an unreachable resolver says nothing about the
            # domain, so it is reported as unknown, not as missing.
            logger.debug("TXT lookup for %s failed: %s", name, e)

    lookups = [
        threading.Thread(target=_has, args=(key, name, marker), daemon=True)
        for key, name, marker in (
            ("spf", domain, "v=spf1"),
            ("dmarc", f"_dmarc.{domain}", "v=DMARC1"),
        )
    ]
    deadline = time.monotonic() + AUTH_RECORDS_DEADLINE_SECONDS
    for t in lookups:
        t.start()
    for t in lookups:
        t.join(max(0.0, deadline - time.monotonic()))
    return found.get("spf"), found.get("dmarc")


# DKIM keys live under a selector the mail provider chooses, and DNS cannot
# list them, so the usual ones are tried: Microsoft 365, Google Workspace and
# the common defaults of hosting panels and sending services.
DKIM_SELECTORS = (
    "selector1", "selector2", "google", "default", "dkim", "mail", "k1", "s1", "s2",
)


def _lookup_dkim(domain: str) -> tuple:
    """(found, selector) for the domain's DKIM key: found True with the first
    selector that answers, False when none of DKIM_SELECTORS has a key, None
    when the lookups could not be made. Same deadline as SPF and DMARC."""
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        return None, None
    try:
        res = dns.resolver.Resolver()
    except (dns.exception.DNSException, OSError):
        return None, None
    res.lifetime = res.timeout = AUTH_RECORDS_DEADLINE_SECONDS
    answers: dict = {}

    def _try(selector):
        try:
            for rr in res.resolve(f"{selector}._domainkey.{domain}", "TXT"):
                text = b"".join(rr.strings).decode("utf-8", "ignore")
                if "p=" in text and ("v=DKIM1" in text or "k=rsa" in text or "k=ed25519" in text):
                    answers[selector] = True
                    return
            answers[selector] = False
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            answers[selector] = False
        except (dns.exception.DNSException, OSError):
            pass

    lookups = [
        threading.Thread(target=_try, args=(sel,), daemon=True) for sel in DKIM_SELECTORS
    ]
    deadline = time.monotonic() + AUTH_RECORDS_DEADLINE_SECONDS
    for t in lookups:
        t.start()
    for t in lookups:
        t.join(max(0.0, deadline - time.monotonic()))
    for sel in DKIM_SELECTORS:
        if answers.get(sel):
            return True, sel
    if len(answers) == len(DKIM_SELECTORS):
        return False, None
    return None, None


def dkim_words(found: bool | None, selector: str | None) -> str:
    if found is None:
        return "DKIM could not be checked just now"
    if found:
        return f"DKIM key found (selector {selector})"
    return (
        "no DKIM key found under the usual selectors - ask the mail provider "
        "which selector it signs with"
    )


def auth_record_words(name: str, found: bool | None) -> str:
    """One record's verdict from _lookup_auth_records, in the words doctor and
    the dashboard both use."""
    if found is None:
        return f"{name} could not be checked just now"
    return f"{name} record found" if found else f"no {name} record found"


def _auth_records_note(spf: bool | None, dmarc: bool | None) -> str | None:
    """The dashboard's line under its SPF and DMARC marks: the records that
    could not be checked, or None when both were."""
    unchecked = [
        name for name, found in (("SPF", spf), ("DMARC", dmarc)) if found is None
    ]
    if not unchecked:
        return None
    return auth_record_words(" and ".join(unchecked), None) + "."


def create_dashboard_app(settings: Settings):
    """Build and return the Flask dashboard app (without running it).

    Separated from cmd_dashboard so the app can be constructed in tests via
    app.test_client() without starting a live server."""
    from urllib.parse import urlsplit

    from flask import (
        Flask,
        Response,
        jsonify,
        redirect,
        render_template,
        request,
        session,
    )

    from modules.rate_limit import RequestLimiter

    refusal = dashboard_password_refusal(settings)
    if refusal:
        raise ValueError(f"The dashboard will not start: {refusal}.")

    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
    )
    # A request naming any other host is refused (400), so a forged Host
    # header - DNS rebinding, a misrouted proxy - does not reach the dashboard.
    app.config["TRUSTED_HOSTS"] = _trusted_hosts(settings)
    # FLASK_DEBUG in the environment would otherwise switch it on.
    app.config["DEBUG"] = False
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    from modules import clock

    clock.set_timezone(settings.app_timezone)
    set_erasure_key(settings.erasure_key)

    db = Database(settings.get_abs_db_path())
    from modules.unsubscribe import UnsubscribeHandler

    replay_optout_spool(
        db, resolve=UnsubscribeHandler(db, settings.unsubscribe_secret).verify_token
    )
    agent = None
    agent_thread = None
    send_agent = None  # dedicated agent for manual/direct sends (lazy, reused)
    # Flask runs threaded, so the "is it already running?" check and the thread
    # start must be one atomic step. Without this, two near-simultaneous POSTs
    # to /api/agent/start both passed the check and two campaign loops ran
    # against the same pending set - emailing every prospect twice.
    agent_lock = threading.Lock()

    # ── HTTP Basic Auth ──
    # Always enforced: the app is not built without a password (above).
    # Recipient-facing endpoints stay public so tracking pixels and unsubscribe
    # links keep working from external inboxes.
    _AUTH_EXEMPT_PREFIXES = ("/track/", "/unsubscribe/", "/healthz")
    login_failures = RequestLimiter(
        LOGIN_FAILURES_ALLOWED, window=LOGIN_FAILURE_WINDOW_SECONDS
    )

    # ── Sign-in page and sessions ──
    # People sign in on a page and get a session cookie that lasts a working
    # day; scripts and the tests may still send HTTP Basic credentials. The
    # signing key is derived from the passwords themselves, so changing any
    # password signs everyone out.
    import hashlib
    from datetime import timedelta as _session_span

    from flask.sessions import SecureCookieSessionInterface

    class _SessionCookies(SecureCookieSessionInterface):
        def get_cookie_secure(self, app):
            # Secure over HTTPS, directly or behind the host's TLS proxy; a
            # plain-HTTP run on this machine still keeps its session.
            return request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"

    app.session_interface = _SessionCookies()
    app.secret_key = hashlib.sha256(
        "\0".join(
            (
                "omega-session",
                settings.dashboard_password or "",
                settings.dashboard_reviewers or "",
                settings.erasure_key or "",
            )
        ).encode("utf-8")
    ).digest()
    app.config.update(
        SESSION_COOKIE_NAME="omega_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=_session_span(hours=8),
    )

    def _accounts() -> list:
        return [
            (settings.dashboard_username, settings.dashboard_password),
            *settings.reviewer_accounts().items(),
        ]

    def _fingerprint(name: str, password: str) -> str:
        return hmac.new(
            app.secret_key, f"{name}\0{password}".encode(), hashlib.sha256
        ).hexdigest()

    def _session_user() -> str | None:
        """The signed-in person, while their session and password still hold."""
        name, mark = session.get("user"), session.get("fp")
        if not name or not mark:
            return None
        for account, password in _accounts():
            if account == name and hmac.compare_digest(mark, _fingerprint(account, password)):
                return account
        return None

    def _wants_the_page() -> bool:
        """A person's browser opening a page, as opposed to a script or a
        fetch: they get the sign-in page instead of the browser's own box."""
        return (
            request.method == "GET"
            and "text/html" in (request.headers.get("Accept") or "")
            and not request.path.startswith(("/api/", "/static/"))
        )

    def _safe_next(target: str | None) -> str:
        return target if target in ("/", "/classic") else "/"

    @app.before_request
    def _require_dashboard_auth():
        if any(request.path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return None  # public endpoints
        if request.path in ("/login", "/static/app/app.css") or _session_user():
            return None
        # The connection's own address: no trusted-proxy setting exists, so a
        # header the caller writes, such as X-Forwarded-For, is never read.
        client = request.remote_addr or ""
        # While locked, the password is not even compared: answering a right
        # guess with 200 would let the guessing go on at full speed.
        locked_for = login_failures.retry_after(client)
        if locked_for is not None:
            return Response(
                "Too many wrong passwords. Try again later.",
                429,
                {"Retry-After": str(locked_for)},
            )
        auth = request.authorization
        # Constant-time comparison so response timing can't be used to guess
        # the credentials character-by-character. As UTF-8 bytes: compare_digest
        # raises TypeError on str holding any non-ASCII character, so an
        # accented password crashed every page instead of protecting it.
        # The administrator, or one of the named reviewers. Every pair is
        # compared, so the time taken does not say which name exists.
        def matches(name: str, password: str) -> bool:
            same_name = hmac.compare_digest(
                (auth.username or "").encode("utf-8"), name.encode("utf-8")
            )
            same_password = hmac.compare_digest(
                (auth.password or "").encode("utf-8"), password.encode("utf-8")
            )
            return same_name and same_password

        accounts = [
            (settings.dashboard_username, settings.dashboard_password),
            *settings.reviewer_accounts().items(),
        ]
        # A list, not a generator: any() would stop at the first match.
        compared = [matches(n, pw) for n, pw in accounts] if auth else []
        ok = any(compared)
        if not ok:
            # A request with no credentials is the browser asking for the
            # login box, not a guess.
            if auth is not None:
                login_failures.hit(client)
                if login_failures.retry_after(client) is not None:
                    logger.warning(
                        "Dashboard login: %d wrong passwords from %s within %d "
                        "minutes; that address is refused until the oldest "
                        "leaves the window",
                        LOGIN_FAILURES_ALLOWED,
                        client,
                        LOGIN_FAILURE_WINDOW_SECONDS // 60,
                    )
            if _wants_the_page():
                return Response(
                    render_template("login.html", next=request.path, error=None),
                    401,
                )
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Outreach Dashboard"'},
            )
        return None

    def _origin_of(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    @app.before_request
    def _reject_cross_origin_writes():
        """Refuse state-changing requests that do not come from this origin.

        Defence in depth behind `_json_body`. A page on another origin can make
        the operator's browser POST here, and HTTP Basic credentials ride along
        automatically - so without this an attacker's page could start a
        campaign or send mail from the operator's own mailbox.

        The Origin header decides, or the Referer when a client sends no
        Origin; a request with neither is refused too, so a script must send
        one. The exempt prefixes are recipient-facing (the one-click
        unsubscribe POST comes server-to-server from mailbox providers) and
        must stay reachable from outside.
        """
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        if any(request.path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return None
        sent = request.headers.get("Origin") or request.headers.get("Referer")
        if not sent:
            logger.warning("Rejected %s with no Origin or Referer", request.path)
            return jsonify(
                {"error": "Refused: the request did not say which page sent it."}
            ), 403
        if _origin_of(sent) != _origin_of(request.host_url):
            logger.warning("Rejected cross-origin %s from %s", request.path, sent)
            return jsonify({"error": "Cross-origin request rejected"}), 403
        return None

    @app.errorhandler(413)
    def _too_large(_error):
        return jsonify(
            {
                "error": "The request is larger than "
                f"{MAX_REQUEST_BYTES // (1024 * 1024)} MB, the most the "
                "dashboard accepts."
            }
        ), 413

    @app.after_request
    def _security_headers(resp):
        # The dashboard can start campaigns and send mail, so it must not be
        # frameable: otherwise an attacker's page could overlay it and trick the
        # operator into clicking Start Agent or Send.
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", DASHBOARD_CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        # Nothing to other sites. Within the dashboard the browser keeps the
        # real Origin on a form post (sign-in, sign-out); under no-referrer it
        # sends "null", which the cross-origin check refuses.
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        # Over HTTPS (directly or behind the host's TLS proxy), browsers are
        # told to never fall back to plain HTTP, where a password could be read.
        if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return resp

    # Writes that send email, start or stop the agent, or change what it does
    # and to whom: the administrator's. A reviewer decides on proposals.
    _ADMIN_ONLY_WRITES = (
        "/api/config",
        "/api/template",
        "/api/agent/start",
        "/api/agent/stop",
        "/api/send-manual",
        "/api/queries/add",
        "/api/strategy/queue",
        "/api/autonomy",
        "/api/leads/import",
    )

    @app.before_request
    def _require_admin_for_control():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        if request.path not in _ADMIN_ONLY_WRITES:
            return None
        if hmac.compare_digest(
            _reviewer().encode("utf-8"), settings.dashboard_username.encode("utf-8")
        ):
            return None
        return jsonify({"error": "Only the administrator can do that."}), 403

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if _session_user():
                return redirect(_safe_next(request.args.get("next")))
            return render_template("login.html", next=_safe_next(request.args.get("next")), error=None)
        client = request.remote_addr or ""
        target = _safe_next(request.form.get("next"))
        locked = login_failures.retry_after(client)
        if locked is not None:
            return render_template(
                "login.html", next=target, error="Too many wrong passwords. Try again later."
            ), 429
        name = (request.form.get("username") or "").strip()
        given = (request.form.get("password") or "").encode("utf-8")
        found = None
        # Every account is compared, so the time taken does not say which exists.
        for account, password in _accounts():
            same = hmac.compare_digest(name.encode("utf-8"), account.encode("utf-8"))
            same &= hmac.compare_digest(given, password.encode("utf-8"))
            if same:
                found = (account, password)
        if found is None:
            login_failures.hit(client)
            return render_template(
                "login.html", next=target, error="That name and password do not match."
            ), 401
        session.clear()
        session.permanent = True
        session["user"], session["fp"] = found[0], _fingerprint(*found)
        logger.info("Dashboard sign-in: %s", found[0])
        return redirect(target)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect("/login")

    @app.route("/healthz")
    def healthz():
        """For the host's health check: no login, no personal data."""
        try:
            db.conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return jsonify({"status": "error", "database": "unreachable"}), 503
        return jsonify({"status": "ok"})

    def _json_body():
        """Return the parsed JSON body, or None if the request isn't JSON.

        These routes used `get_json(force=True)`, which parses the body whatever
        the Content-Type. That turned them from preflight-protected JSON
        endpoints into targets a plain cross-origin HTML form could reach
        (`enctype="text/plain"` is a CORS-simple request, so no preflight
        fires). Requiring the header means a cross-origin caller must clear a
        preflight first. /api/template and /api/contacts/<id>/action already
        used plain `request.json` and were protected for exactly this reason.
        """
        if not request.is_json:
            return None
        data = request.get_json(silent=True)
        # `or {}` only rescued FALSY values. A JSON array, string or number is
        # truthy, so it was handed straight to callers that immediately do
        # .get(...) - and every write route 500'd on `[1,2,3]` with an
        # AttributeError. Verified against all six. The type check belongs here,
        # once, rather than in each route.
        return data if isinstance(data, dict) else {}

    def _not_text(data: dict, *keys: str) -> str | None:
        """The first of `keys` the body holds as something other than a
        string, or None. Read as text, a number or a list crashed the route
        (`.strip()` on an int) and answered 500 instead of saying what was
        wrong."""
        return next(
            (k for k in keys if data.get(k) is not None and not isinstance(data[k], str)),
            None,
        )

    def _update_config():
        """Apply operator config changes.

        Three things stand between the request body and the running agent:

        1. RUNTIME_EDITABLE, an ALLOW-list. A field is not editable until
           someone adds it deliberately, so a new Settings field never becomes
           remotely writable by accident. Credentials are not on it and must
           not be added - .env is a deploy concern, and a dashboard write path
           to SMTP passwords turns one XSS into account takeover.
        2. config.validate_override, which rebuilds the whole model so the
           field validators AND the cross-field ones run. Plain assignment
           would skip them entirely.
        3. The values are persisted, not applied to this process's Settings.
           The agent re-reads them at the top of its loop. Mutating the
           dashboard's copy would leave the two disagreeing about what the
           configuration actually is.

        Cross-origin writes are already blocked by _reject_cross_origin_writes
        and the JSON content-type requirement, same as every other write route.
        """
        body = _json_body()
        if body is None:
            return jsonify({"error": "Expected application/json."}), 415

        updates = body.get("settings")
        if not isinstance(updates, dict) or not updates:
            return jsonify({"error": "Expected a non-empty 'settings' object."}), 400

        rejected = sorted(set(updates) - RUNTIME_EDITABLE)
        if rejected:
            return jsonify(
                {
                    "error": "Not editable at runtime: " + ", ".join(rejected),
                    "editable": sorted(RUNTIME_EDITABLE),
                }
            ), 400

        # Validate every value against a single candidate before persisting any
        # of them, so a bad third field cannot leave the first two applied.
        is_reset = bool(body.get("reset"))
        candidate = effective_settings(settings, db)
        target_updates = {
            k: getattr(settings, k) if is_reset else v
            for k, v in updates.items()
        }
        for key, value in _ordered_override_items(candidate, target_updates):
            try:
                candidate = validate_override(candidate, key, value)
            except (ValueError, TypeError) as e:
                # pydantic's ValidationError subclasses ValueError, so this
                # covers both a value the validators reject and the
                # "not editable" guard; TypeError covers a value of the wrong
                # shape entirely (a list where an int belongs).
                return jsonify(
                    {"error": f"{key}: {config_error_text(e, names=False)}"}
                ), 400

        try:
            for key, value in updates.items():
                if body.get("reset"):
                    db.clear_runtime_override(key)
                else:
                    db.set_runtime_override(key, value)
        except sqlite3.Error:
            logger.exception("Could not persist the config change")
            return jsonify({"error": "Could not save the change."}), 500

        applied = {k: getattr(candidate, k, None) for k in updates}
        logger.info("Config updated via dashboard: %s", applied)
        return jsonify(
            {
                "status": "ok",
                "applied": applied,
                "note": "The agent picks this up on its next loop.",
            }
        )

    def _clamp_limit(value: int, default: int, maximum: int = 1000) -> int:
        """Bound a caller-supplied row limit.

        `?limit=-1` reached SQLite as `LIMIT -1`, which means *unlimited* - so it
        dumped the entire prospect database (names, emails, orgs, scores) in one
        response. Large positive values were an easy memory/CPU spike.
        """
        if value is None or value <= 0:
            return default
        return min(value, maximum)

    @app.teardown_request
    def _close_db_conn(exc):
        # Each web request runs on its own thread; close that thread's SQLite
        # connection when the request ends so handles don't accumulate.
        db.close_thread_conn()

    # ── Dashboard Page ──
    @app.route("/")
    def index():
        return render_template("app.html")

    @app.route("/classic")
    def classic():
        return render_template("dashboard.html")

    # ── API: Stats ──
    @app.route("/api/stats")
    def api_stats():
        return jsonify(db.get_stats())

    # ── API: Activity Log ──
    @app.route("/api/activity")
    def api_activity():
        limit = _clamp_limit(request.args.get("limit", 50, type=int), 50)
        return jsonify(db.get_recent_activity(limit))

    # ── API: Contacts ──
    @app.route("/api/contacts")
    def api_contacts():
        # Shape kept as a bare list: the front end and any other consumer index
        # it directly. Totals live on /api/stats, which the dashboard already
        # polls, so the status chips can show real numbers without a second
        # request per refresh.
        limit = _clamp_limit(request.args.get("limit", 200, type=int), 200)
        status = (request.args.get("status") or "").strip() or None
        contacts = db.get_all_contacts(limit, status=status)
        from modules.compliance import language_and_reason, language_reason_words

        # The first email's language, for every listed contact in one query.
        sent = db.first_sent_languages([c["id"] for c in contacts])
        first_missing = italian_first_email_missing(FileHandler())
        followups_missing = italian_followups_missing(FileHandler())
        from modules.outcomes import stages_by_contact

        outcome_of = stages_by_contact(db)
        # Marked here, from the one definition, so the page needs no copy of
        # the closed statuses, or of the language rule, to drift.
        for contact in contacts:
            contact["closed"] = contact.get("status") in CLOSED_CONTACT_STATUSES
            contact["outcome"] = outcome_of.get(contact["id"])
            if contact.get("status") != "erased":
                fit = assess_fit(contact)
                contact["fit_score"] = fit["total"]
                contact["sector"] = fit["sector"]
                contact["company_type"] = fit["company_type"]
            contact["language_locked"] = contact["id"] in sent
            # An erased contact's address is a token, which would read "en".
            if contact.get("status") == "erased":
                contact["language"] = contact["language_reason"] = None
                continue
            email = contact.get("email") or ""
            language, reason = language_and_reason(
                settings, email, contact, sent.get(contact["id"])
            )
            contact["language"], contact["language_reason"] = language_as_sent(
                language,
                reason,
                language_reason_words(language, reason, email, contact),
                first_missing,
                followups_missing,
            )
        return jsonify(contacts)

    # ── API: Email Log ──
    @app.route("/api/emails")
    def api_emails():
        limit = _clamp_limit(request.args.get("limit", 200, type=int), 200)
        return jsonify(db.get_all_emails(limit))

    # ── API: Searches ──
    @app.route("/api/searches")
    def api_searches():
        limit = _clamp_limit(request.args.get("limit", 100, type=int), 100)
        return jsonify(db.get_all_searches(limit))

    # ── V2 APIs ──
    @app.route("/api/charts")
    def api_charts():
        try:
            from modules.analytics import Analytics

            analytics = Analytics(db)
            return jsonify(analytics.get_chart_data())
        except ImportError:
            return jsonify({"error": "Analytics module not available"}), 501

    @app.route("/api/warmup")
    def api_warmup():
        try:
            from modules.warmup import WarmupScheduler

            warmup = WarmupScheduler(
                db,
                warmup_enabled=settings.warmup_enabled,
                warmup_start_daily=settings.warmup_start_daily,
                warmup_increment=settings.warmup_increment,
                warmup_max_daily=settings.warmup_max_daily,
            )
            accts = settings.get_sender_accounts()
            keys = ["" if i == 0 else a["username"] for i, a in enumerate(accts)]
            if len(keys) > 1:
                st = warmup.get_combined_status(keys)
                st["accounts"] = len(keys)
                return jsonify(st)
            return jsonify(warmup.get_warmup_status())
        except ImportError:
            return jsonify({"error": "Warmup module not available"}), 501

    @app.route("/api/retries")
    def api_retries():
        try:
            from modules.retry_queue import RetryQueue

            rq = RetryQueue(
                db, settings.retry_max_attempts, settings.retry_base_delay_minutes
            )
            return jsonify(rq.get_retry_stats())
        except ImportError:
            return jsonify({"error": "Retry module not available"}), 501

    @app.route("/api/sequences")
    def api_sequences():
        try:
            from modules.sequences import SequenceEngine

            seq = SequenceEngine(db)
            return jsonify(seq.get_sequence_stats())
        except ImportError:
            return jsonify({"error": "Sequences module not available"}), 501

    @app.route("/api/abtest")
    def api_abtest():
        return jsonify(db.get_ab_stats())

    @app.route("/api/leads")
    def api_leads():
        """Interested prospects a human was alerted about."""
        return jsonify(
            {
                "configured": bool(settings.get_lead_alert_recipients()),
                "recipients": settings.get_lead_alert_recipients(),
                "alerts": db.get_lead_alerts(25),
            }
        )

    # Sending domain -> (checked at, spf, dmarc). Kept on the app so each
    # dashboard (and each test's app) starts with an empty memory.
    auth_memory: dict = {}
    app.config["AUTH_RECORDS_MEMORY"] = auth_memory
    auth_memory_lock = threading.Lock()

    def _auth_records(domain: str, recheck: bool = False) -> tuple:
        """(spf, dmarc, note) for the sending domain, from memory when fresh.

        A 'could not be checked' answer is remembered too: a resolver that is
        down would otherwise cost every refresh the full deadline again.
        """
        from modules import clock

        if not domain:
            return None, None, _NO_SENDING_DOMAIN_NOTE
        now = clock.now()
        with auth_memory_lock:
            held = auth_memory.get(domain)
        if (
            held is not None
            and not recheck
            and (now - held[0]).total_seconds() < AUTH_RECORDS_TTL_SECONDS
        ):
            spf, dmarc = held[1], held[2]
        else:
            spf, dmarc = _lookup_auth_records(domain)
            with auth_memory_lock:
                auth_memory.pop(domain, None)
                while len(auth_memory) >= AUTH_RECORDS_MAX_DOMAINS:
                    auth_memory.pop(next(iter(auth_memory)))
                auth_memory[domain] = (now, spf, dmarc)
        return spf, dmarc, _auth_records_note(spf, dmarc)

    dkim_memory: dict = {}

    def _dkim_record(domain: str, recheck: bool = False) -> tuple:
        """(found, selector) for the sending domain, remembered like SPF/DMARC."""
        from modules import clock

        if not domain:
            return None, None
        now = clock.now()
        with auth_memory_lock:
            held = dkim_memory.get(domain)
        fresh = held is not None and (
            (now - held[0]).total_seconds() < AUTH_RECORDS_TTL_SECONDS
        )
        if fresh and not recheck:
            return held[1], held[2]
        found, selector = _lookup_dkim(domain)
        with auth_memory_lock:
            if len(dkim_memory) >= AUTH_RECORDS_MAX_DOMAINS:
                dkim_memory.pop(next(iter(dkim_memory)))
            dkim_memory[domain] = (now, found, selector)
        return found, selector

    @app.route("/api/deliverability/dkim")
    def api_deliverability_dkim():
        """The sending domain's DKIM key: asked apart from the panel, so its
        lookups never hold the SPF and DMARC answer up."""
        domain = (
            settings.smtp_username.split("@")[-1]
            if "@" in settings.smtp_username
            else ""
        )
        found, selector = _dkim_record(domain, recheck=bool(request.args.get("recheck")))
        return jsonify(
            {
                "domain": domain,
                "dkim": found,
                "selector": selector,
                "note": dkim_words(found, selector) if domain else None,
            }
        )

    @app.route("/api/deliverability")
    def api_deliverability():
        from modules import clock

        m = db.get_deliverability_metrics()
        sending_domain = (
            settings.smtp_username.split("@")[-1]
            if "@" in settings.smtp_username
            else ""
        )

        spf, dmarc, dns_note = _auth_records(
            sending_domain, recheck=bool(request.args.get("recheck"))
        )

        eff = effective_settings(settings, db)
        now = clock.now()
        window_open = (not eff.send_window_enabled) or (
            now.weekday() in eff.get_send_window_days()
            and eff.send_window_start_hour
            <= now.hour
            < eff.send_window_end_hour
        )
        # Must mirror agent._bounce_guard_tripped exactly: it judges the RECENT
        # window, so gating on the lifetime total here would show "healthy" on a
        # dashboard whose agent has already stopped sending (or the reverse).
        guard_tripped = (
            eff.bounce_guard_enabled
            and m["recent_sent"] >= eff.bounce_guard_min_sends
            and m["bounce_rate"] > eff.max_bounce_rate
        )

        return jsonify(
            {
                "metrics": m,
                "thresholds": {
                    "max_bounce_rate": eff.max_bounce_rate,
                    "provider_bounce_limit": 0.02,
                    "provider_spam_limit": 0.003,
                },
                "guard": {
                    "enabled": eff.bounce_guard_enabled,
                    "tripped": guard_tripped,
                },
                "send_window": {
                    "enabled": eff.send_window_enabled,
                    "open": window_open,
                    "start": eff.send_window_start_hour,
                    "end": eff.send_window_end_hour,
                    "now_local": now.strftime("%Y-%m-%d %H:%M %Z"),
                },
                "sending": {
                    "domain": sending_domain,
                    "from_address": settings.smtp_username,
                    "spf": spf,
                    "dmarc": dmarc,
                    "dns_note": dns_note,
                    "personal_gmail": sending_domain
                    in ("gmail.com", "googlemail.com", "yahoo.com", "outlook.com"),
                    "attach_profile": settings.attach_profile,
                    "profile_url": settings.profile_url or None,
                },
                "blacklist": _blacklist_status(sending_domain),
            }
        )

    # Cached across requests so the dashboard doesn't re-query DNSBLs on every
    # refresh (the checker keeps its own TTL).
    _bl_checker = {"obj": None}

    def _blacklist_status(domain: str) -> dict:
        """DNSBL verdict for the sending domain (blank/off -> not_configured)."""
        if not settings.blacklist_check_enabled or not domain:
            return {"status": "off"}
        try:
            from modules.blacklist import BlacklistChecker

            if _bl_checker["obj"] is None:
                _bl_checker["obj"] = BlacklistChecker()
            chk = _bl_checker["obj"]
            return chk.summarize(chk.check_domain(domain))
        except (ImportError, OSError) as e:
            # BlacklistChecker._query already turns every DNS-level failure into
            # an UNAVAILABLE row, so the only things that reach here are the
            # optional dnspython import and a socket-level failure underneath
            # it. Both mean "we could not ask", which is not "listed".
            logging.getLogger(__name__).warning(f"Blacklist check failed: {e}")
            return {"status": "unavailable", "listed_on": [], "results": []}

    @app.route("/api/blacklist")
    def api_blacklist():
        """On-demand blacklist re-check for the sending domain."""
        domain = (
            settings.smtp_username.split("@")[-1]
            if "@" in settings.smtp_username
            else ""
        )
        if _bl_checker["obj"] is not None:
            _bl_checker["obj"]._cache.clear()  # force a fresh lookup
        return jsonify({"domain": domain, **_blacklist_status(domain)})

    @app.route("/api/domains")
    def api_domains():
        try:
            from modules.domain_throttle import DomainThrottle

            eff = effective_settings(settings, db)
            dt = DomainThrottle(db, eff.max_per_domain_per_day)
            return jsonify(dt.get_domain_stats())
        except ImportError:
            return jsonify({"error": "Domain throttle module not available"}), 501

    @app.route("/api/learning")
    def api_learning():
        try:
            from modules.learning import PerformanceLearner
            from modules.query_generator import EUROPEAN_COUNTRIES, VERTICAL_SEEDS

            learner = PerformanceLearner(db, VERTICAL_SEEDS, EUROPEAN_COUNTRIES)
            return jsonify(learner.snapshot())
        except ImportError:
            return jsonify({"error": "Learning module not available"}), 501

    @app.route("/api/errors")
    def api_errors():
        limit = _clamp_limit(request.args.get("limit", 20, type=int), 20)
        return jsonify(db.get_recent_errors(limit))

    @app.route("/api/config", methods=["GET", "POST"])
    def api_config():
        # Read-only view of feature flags + non-secret settings (no credentials).
        if request.method == "POST":
            return _update_config()
        overrides = {}
        try:
            overrides = db.get_runtime_overrides()
        except sqlite3.Error:
            logger.exception("Could not read config overrides")
        eff = effective_settings(settings, db)
        return jsonify(
            {
                "editable": sorted(RUNTIME_EDITABLE),
                # Which values differ from .env, so the UI can mark them and
                # offer a reset rather than leaving the operator guessing where
                # a number came from.
                "overridden": sorted(overrides),
                "current": {k: getattr(eff, k, None) for k in RUNTIME_EDITABLE},
                "features": {
                    "relevance_filter": settings.relevance_filter_enabled,
                    "ai_personalization": settings.ai_personalization_enabled,
                    "ai_relevance": settings.ai_relevance_enabled,
                    "ai_strategist": settings.ai_strategist_enabled,
                    "sequences": settings.sequences_enabled,
                    "imap_monitor": settings.imap_enabled,
                    "ab_testing": settings.ab_testing_enabled,
                    "lead_scoring": settings.lead_scoring_enabled,
                    "email_verification": settings.verify_emails,
                    "warmup": settings.warmup_enabled,
                    "retry_queue": settings.retry_enabled,
                    "open_tracking": settings.open_tracking_enabled,
                    "learning": settings.learning_enabled,
                },
                "settings": {
                    "sender_name": eff.sender_name,
                    "company_name": eff.company_name,
                    "emails_per_hour": eff.emails_per_hour,
                    "delay_between_emails": eff.delay_between_emails,
                    "send_jitter_seconds": eff.send_jitter_seconds,
                    "max_per_domain_per_day": eff.max_per_domain_per_day,
                    "loop_delay_seconds": eff.loop_delay_seconds,
                    "max_pending_backlog": eff.max_pending_backlog,
                    "min_lead_score_to_email": eff.min_lead_score_to_email,
                    "base_url": eff.base_url,
                    "target_queries": eff.get_target_queries(),
                    "llm_provider": eff.llm_provider,
                    "own_server_configured": bool(
                        eff.llm_base_url and eff.llm_model
                    ),
                },
            }
        )

    @app.route("/api/template", methods=["GET", "POST"])
    def api_template():
        data = None
        if request.method == "POST":
            # Through the shared helper, which is the only place that knows a
            # parsed JSON body still has to BE an object.
            data = _json_body()
            if data is None:
                return jsonify({"error": "Expected application/json."}), 415
            # A name that is not text is refused below as an unknown template.
            if _not_text(data, "content"):
                return jsonify({"error": "content must be text."}), 400
            name = data.get("name") or "email_template"
        else:
            name = request.args.get("name") or "email_template"
        # Looked up in a fixed map, so the name never builds a path.
        file_name = TEMPLATE_EDITOR_FILES.get(name) if isinstance(name, str) else None
        if file_name is None:
            return jsonify(
                {"error": "Unknown template. Reload the page and choose one from the list."}
            ), 400
        template_path = _templates_dir() / file_name
        if request.method == "POST":
            new_content = data.get("content") or ""
            # An empty body is never a legitimate save. The editor shows an empty
            # textarea when its GET failed, which looks identical to "not loaded
            # yet", so an unguarded save silently truncated the template - the
            # agent then falls back to a generic default and nothing surfaces it.
            if not new_content.strip():
                return jsonify(
                    {
                        "error": "Refusing to save an empty template. Reload the page and try again."
                    }
                ), 400
            from modules.compliance import missing_placeholders

            missing = missing_placeholders(new_content)
            if missing:
                return jsonify(
                    {
                        "error": "Refusing to save: the template must keep "
                        + " and ".join(missing)
                        + " - they carry the legally required sender identity "
                        "and the opt-out link."
                    }
                ), 400
            try:
                # Keep the previous version recoverable before overwriting.
                if template_path.exists():
                    backup_path = template_path.with_suffix(".html.bak")
                    backup_path.write_text(
                        template_path.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                template_path.write_text(new_content, encoding="utf-8")
                return jsonify({"status": "ok"})
            except (OSError, UnicodeError):
                # Both sides are pathlib text I/O: OSError for permissions, a
                # read-only volume or a missing templates/ directory, and
                # UnicodeError if the file on disk is not decodable as UTF-8.
                # The response stays generic on purpose - the exception carries
                # an absolute filesystem path, and it goes to the log only.
                logger.exception("Template save failed")
                return jsonify({"error": "Could not write template."}), 500

        # GET. A missing text other than the first email opens empty, to be
        # written and saved: meanwhile Italian recipients get the English
        # text, and an English follow-up step sends the first email's. `preview` is what
        # the send path would put in each placeholder, in the text's language.
        from modules.dispatch_pipeline import preview_placeholders

        preview = preview_placeholders(
            effective_settings(settings, db), "it" if name.endswith("_it") else "en"
        )
        if name != "email_template" and not template_path.exists():
            return jsonify({"content": "", "missing": True, "preview": preview})
        try:
            content = template_path.read_text(encoding="utf-8")
            return jsonify({"content": content, "preview": preview})
        except Exception:
            logger.exception("Could not read the template")
            return jsonify({"error": "Could not read the template."}), 500

    @app.route("/api/contacts/<int:contact_id>/why")
    def api_contact_why(contact_id):
        """Why this contact was or wasn't emailed: the dashboard's detail panel.

        Read-only, and behind the dashboard login like every other route: the
        timeline names the contact and quotes the agent's log about them.
        """
        from modules.contact_explainer import explain_contact

        try:
            why = explain_contact(db, contact_id)
        except Exception:
            logger.exception("Could not explain contact %s", contact_id)
            return jsonify({"error": "Could not load the explanation."}), 500
        if why is None:
            return jsonify({"error": "No such contact."}), 404
        if why["status"] != "erased":
            contact = db.get_contact_by_id(contact_id)
            why["facts"].append(("Language", _language_fact(contact)))
        return jsonify(why)

    def _language_fact(contact: dict | None) -> str:
        """The language of every email to this contact and why, in plain
        words: what the contacts table says, and what the send will do."""
        from modules.compliance import language_for, language_reason_words

        email = (contact or {}).get("email") or ""
        language, reason = language_for(settings, db, contact, email)
        return language_as_sent(
            language,
            reason,
            language_reason_words(language, reason, email, contact),
            italian_first_email_missing(FileHandler()),
            italian_followups_missing(FileHandler()),
        )[1]

    @app.route("/api/contacts/<int:contact_id>/language", methods=["POST"])
    def api_contact_language(contact_id):
        """The operator's language for a contact: "it", "en", or "auto" for
        the rule's own. Refused once the contact was sent an email: a thread
        keeps the language of its first email."""
        from modules.compliance import language_for, language_reason_words

        data = _json_body()
        if data is None:
            return jsonify({"error": "Expected application/json."}), 415
        value = data.get("language")
        if not isinstance(value, str) or value not in ("it", "en", "auto"):
            return jsonify({"error": "Choose it, en or auto."}), 400
        try:
            contact = db.get_contact_by_id(contact_id)
            if not contact:
                return jsonify({"error": "No such contact."}), 404
            if contact["status"] == "erased":
                return jsonify({"error": "An erased contact is never emailed."}), 409
            if not db.set_language_choice(
                contact_id, None if value == "auto" else value
            ):
                sent = db.first_sent_language(contact_id)
                name = "Italian" if sent == "it" else "English"
                return jsonify(
                    {
                        "error": f"Already emailed in {name}: a contact keeps "
                        "the language of its first email."
                    }
                ), 409
            email = contact["email"]
            language, reason = language_for(settings, db, {"id": contact_id}, email)
            stored = db.get_contact_by_id(contact_id)
            words = language_reason_words(language, reason, email, stored)
            db.log_activity(
                "LANGUAGE_SET", f"Contact {contact_id}: {words}", status="info"
            )
            # The toast must name the language the send will use, as the list does.
            fh = FileHandler()
            language, words = language_as_sent(
                language,
                reason,
                words,
                italian_first_email_missing(fh),
                italian_followups_missing(fh),
            )
        except Exception:
            logger.exception("Could not set the language of contact %s", contact_id)
            return jsonify({"error": "Could not set the language."}), 500
        return jsonify({"ok": True, "language": language, "reason": words})

    @app.route("/api/contacts/<int:contact_id>/action", methods=["POST"])
    def api_contact_action(contact_id):
        data = _json_body()
        if data is None:
            return jsonify({"error": "Expected application/json."}), 415
        action = data.get("action")

        if action == "block":
            # Add to suppression list. Recorded as the operator's doing, on the
            # contact and in the activity log - it used to clear the reason and
            # leave nothing to say a person, not the agent, had closed it.
            try:
                contact = db.get_contact_by_id(contact_id)
                if not contact:
                    return jsonify({"error": "No such contact."}), 404
                status = contact["status"]
                if status in CLOSED_CONTACT_STATUSES:
                    # A closed status says why outreach ended, and other rules
                    # read it: retention erases an objector at the next run by
                    # 'unsubscribed' and keeps a reply by 'responded', and
                    # Unblock reopens only 'blocked'. Overwriting it kept an
                    # objector's data a year longer, and Block then Undo put a
                    # contact who had replied back in the queue.
                    before = db.get_suppression_reason(contact["email"])
                    db.block_address(contact["email"])
                    if db.get_suppression_reason(contact["email"]) != before:
                        db.log_activity(
                            "CONTACT_BLOCKED",
                            f"{contact['email']} blocked from the dashboard; "
                            f"kept as {status}",
                            status="warning",
                        )
                    reply = {
                        "status": "ok",
                        "closed": status,
                        "message": f"Already closed ({status}): the status "
                        "stays, and the address is suppressed.",
                    }
                    # Unblock can take back only the row this Block added.
                    if before is None:
                        reply["undo"] = "unblock"
                    return jsonify(reply)
                db.block_address(contact["email"])
                db.update_contact_status(
                    contact_id, "blocked", discard_reason="Blocked from the dashboard"
                )
                db.log_activity(
                    "CONTACT_BLOCKED",
                    f"{contact['email']} blocked from the dashboard",
                    status="warning",
                )
                return jsonify({"status": "ok", "undo": "unblock"})
            except Exception:
                logger.exception("Could not update the contact")
                return jsonify({"error": "Could not update the contact."}), 500

        elif action == "unblock":
            # Reverses a dashboard block, and ONLY a dashboard block. An
            # unsubscribe or erasure request is the person exercising a legal
            # right and is not ours to undo; a bounce or SMTP rejection is the
            # receiving server telling us the address is dead. Un-suppressing
            # either of those puts mail back on the wire that must not go out,
            # so the reason is checked rather than assumed from the status
            # column - a contact can read "blocked" while its suppression row
            # says "unsubscribe".
            try:
                contact = db.get_contact_by_id(contact_id)
                if not contact:
                    return jsonify({"error": "No such contact."}), 404
                # Only a blocked contact can be unblocked. Only the page hid
                # the button, so the API turned a contact who had replied, a
                # client or a discard verdict back into a backlog entry.
                status = contact["status"]
                if status != "blocked" and status not in CLOSED_CONTACT_STATUSES:
                    return jsonify({"error": "This contact is not blocked."}), 409
                reason = db.get_suppression_reason(contact["email"])
                removed = db.remove_from_suppression(
                    contact["email"], only_reasons=("blocked_via_dashboard",)
                )
                if not removed and reason is not None:
                    logger.info(
                        "Refused to unblock %s: suppressed as %r, not a dashboard block",
                        contact["email"],
                        reason,
                    )
                    return jsonify(
                        {
                            "error": "This address is suppressed because of "
                            f"{reason}, which cannot be undone from here."
                        }
                    ), 409
                if status != "blocked":
                    # Block on a closed contact only added the suppression row,
                    # so that row is all Unblock takes back.
                    if not removed:
                        return jsonify({"error": "This contact is not blocked."}), 409
                    db.log_activity(
                        "CONTACT_UNBLOCKED",
                        f"{contact['email']} unblocked from the dashboard; "
                        f"kept as {status}",
                        status="info",
                    )
                    return jsonify(
                        {
                            "status": "ok",
                            "message": "Dashboard block removed. The contact "
                            f"stays {status}.",
                        }
                    )
                # Removed, or there was no suppression row to reverse: either
                # way the status should agree with reality.
                db.update_contact_status(
                    contact_id, "pending", discard_reason="Unblocked from the dashboard"
                )
                db.log_activity(
                    "CONTACT_UNBLOCKED",
                    f"{contact['email']} unblocked from the dashboard",
                    status="info",
                )
                return jsonify({"status": "ok"})
            except Exception:
                logger.exception("Could not unblock the contact")
                return jsonify({"error": "Could not unblock the contact."}), 500

        elif action == "mark_client":
            # Deliberately operator-driven. The agent finds PROSPECTS; a
            # prospect becomes a client when they buy, which happens in the
            # sales process and is invisible from here. Auto-appending anyone
            # whose reply looked positive would fill the company's client
            # master list with people who merely answered an email.
            try:
                contact = db.get_contact_by_id(contact_id)
                if not contact:
                    return jsonify({"error": "No such contact."}), 404
                org = (contact["organization"] or "").strip()
                if not org:
                    return jsonify(
                        {
                            "error": "This contact has no company name, so there "
                            "is nothing to add to the client list."
                        }
                    ), 400

                from modules.clients import append_to_xlsx
                from modules.list_crypto import ListRefused

                path = PROJECT_ROOT / settings.client_list_path
                if not path.exists():
                    return jsonify(
                        {"error": f"Client list not found: {path.name}"}
                    ), 404
                try:
                    added = append_to_xlsx(path, org, key=settings.lists_key)
                except ListRefused as e:
                    logger.error("Could not mark the contact as a client: %s", e)
                    return jsonify({"error": str(e)}), 409
                db.update_contact_status(
                    contact_id, "active_client", discard_reason=f"Active client: {org}"
                )
                db.log_activity(
                    "CLIENT_ADDED",
                    f"{org} marked as an active client by the operator "
                    f"(from {contact['email']})",
                    status="success",
                )
                # Agents screen against a register loaded at startup; without
                # this, colleagues at the new client stayed fair game until
                # a restart.
                for live in (agent, send_agent):
                    if live is not None:
                        live.load_client_register()
                return jsonify(
                    {
                        "status": "ok",
                        "added": added,
                        "organization": org,
                        "note": (
                            f"{org} added to the client list."
                            if added
                            else f"{org} was already on the client list."
                        ),
                    }
                )
            except Exception:
                logger.exception("Could not mark the contact as a client")
                return jsonify({"error": "Could not update the client list."}), 500

        elif action == "send_now":
            # Re-queues the contact for the next loop, which runs the filters
            # again: a verdict is re-checked here, not overridden. A closed
            # contact - replied, client, opted out, bounced, erased - is not
            # the operator's to reopen.
            try:
                contact = db.get_contact_by_id(contact_id)
                if not contact:
                    return jsonify({"error": "No such contact."}), 404
                status = contact["status"]
                if status in CLOSED_CONTACT_STATUSES or db.is_suppressed(
                    contact["email"]
                ):
                    return jsonify(
                        {
                            "error": f"This contact is closed ({status}) or on the "
                            "do-not-contact list, so it cannot be re-queued."
                        }
                    ), 409
                if db.send_in_doubt(contact_id):
                    return jsonify(
                        {
                            "error": f"An email to this contact is {UNSETTLED_SEND}, "
                            "so it cannot be re-queued until it settles. A send "
                            "that was cut off is recorded in the activity log."
                        }
                    ), 409
                # The campaign refuses to send a first email twice, so
                # re-queueing would answer "ok" and then send nothing.
                if db.has_been_emailed(contact_id):
                    gone = (
                        "has already gone"
                        if db.has_confirmed_send(contact_id)
                        else "may already have gone"
                    )
                    return jsonify(
                        {
                            "error": f"The first email {gone} to this "
                            "contact, so it cannot be sent again. Follow-ups "
                            "continue from the sequence."
                        }
                    ), 409
                # A press while an earlier one still waits on the filters
                # faces the same verdict as the first.
                verdict_pending = status in DISCARDED_CONTACT_STATUSES or (
                    status == "pending"
                    and contact["discard_reason"] == REQUEUED_FROM_A_VERDICT
                )
                db.update_contact_status(
                    contact_id,
                    "pending",
                    discard_reason=(
                        REQUEUED_FROM_A_VERDICT
                        if verdict_pending
                        else REQUEUED_FROM_DASHBOARD
                    ),
                )
                db.log_activity(
                    "CONTACT_REQUEUED",
                    f"{contact['email']} re-queued from the dashboard (was {status})",
                    status="info",
                )
                reply = {"status": "ok"}
                # Decided here, not on the page: the row on screen can be older
                # than the verdict.
                if verdict_pending:
                    reply["message"] = (
                        "Re-queued, not sent: the filters run again before "
                        "anything is sent."
                    )
                return jsonify(reply)
            except Exception:
                logger.exception("Could not perform that action")
                return jsonify({"error": "Could not perform that action."}), 500

        return jsonify({"error": "Unknown action"}), 400

    # Only while open tracking is on: off, the public pixel address answers
    # 404 like any unknown page, and writes nothing.
    if settings.open_tracking_enabled:
        track_limiter = RequestLimiter(TRACK_REQUESTS_PER_MINUTE)

        @app.route("/track/<tracking_id>")
        def track_open(tracking_id):
            retry_after = track_limiter.hit(request.remote_addr or "")
            if retry_after is not None:
                return "", 429, {"Retry-After": str(retry_after)}
            try:
                import io

                from flask import send_file

                from modules.analytics import Analytics

                analytics = Analytics(db)
                # The time of the open only. The reader's IP address and
                # User-Agent are device data that nothing here reads.
                analytics.record_open(tracking_id)
                return send_file(
                    io.BytesIO(Analytics.TRACKING_PIXEL), mimetype="image/gif"
                )
            except ImportError:
                return "", 204

    unsubscribe_limiter = RequestLimiter(settings.unsubscribe_requests_per_minute)

    def _a_real_link(handler, token) -> bool:
        """Whether a one-click over the limit is for a link we could have sent:
        one the database records, or one our key made that a restore may have
        lost. A database too busy to say counts as real, since the opt-out is
        then held on disk rather than lost."""
        if handler.is_our_token(token):
            return True
        try:
            return handler.verify_token(token) is not None
        except sqlite3.Error:
            return True

    @app.route("/unsubscribe/<token>", methods=["GET", "POST"])
    def unsubscribe(token):
        # POST is the RFC 8058 one-click path. Gmail/Yahoo/Apple send it because we
        # advertise List-Unsubscribe-Post in create_email(), and they expect a 2xx
        # with no body. A 405 or an HTML page reads as a failed opt-out and counts
        # against the sending domain.
        one_click = request.method == "POST"
        # The connection's own address: no trusted-proxy setting exists, so a
        # header the caller writes, such as X-Forwarded-For, is never read.
        retry_after = unsubscribe_limiter.hit(request.remote_addr or "")
        try:
            from modules.unsubscribe import UnsubscribeHandler

            handler = UnsubscribeHandler(db, settings.unsubscribe_secret)
            if retry_after is not None and not (
                one_click and _a_real_link(handler, token)
            ):
                return (
                    (
                        "<html><body><h2>Too many requests</h2>"
                        "<p>Please try again in a minute.</p></body></html>"
                    ),
                    429,
                    {"Retry-After": str(retry_after)},
                )
            success, message = handler.process_unsubscribe(token, one_click=one_click)
            if one_click:
                if not success:
                    logger.warning("One-click unsubscribe rejected: %s", message)
                return "", 200
            if success:
                if message == handler.NO_MATCH:
                    return handler.NO_MATCH_HTML
                if message == handler.NOT_KEPT:
                    return handler.NOT_KEPT_HTML
                return handler.confirmation_page(token, settings)
            else:
                return f"<html><body><h2>Error</h2><p>{message}</p></body></html>", 400
        except ImportError:
            return "Unsubscribe not configured", 501
        except Exception:
            # Intentionally broad. Mailbox providers do not re-deliver a failed
            # one-click unsubscribe, so ANY unhandled error here loses the opt-out
            # permanently - a locked DB must not become a lost compliance action.
            # logger.exception keeps the traceback rather than swallowing it.
            logger.exception("Unsubscribe failed for token")
            if one_click:
                return "", 200
            return (
                "<html><body><h2>Error</h2><p>Could not process request.</p></body></html>",
                500,
            )

    def _guard_refusal(e: GuardUnavailable) -> str:
        """Record that no agent was built, where the error panel shows it, and
        give the words for the page."""
        message = f"Nothing was started or sent - {e}"
        logger.error("%s", message)
        try:
            db.log_activity("GUARD_UNAVAILABLE", str(e), status="error")
        except sqlite3.Error as log_err:
            logger.error("Could not record GUARD_UNAVAILABLE (%s)", log_err)
        return message

    def _unreplayed_refusal() -> str | None:
        """The words for the page while the database is behind its journal,
        recorded as a refused start is; or None."""
        refusal = unreplayed_journal(settings)
        if refusal is None:
            return None
        record_unreplayed(settings, refusal)
        return f"Nothing was started or sent - {refusal}."

    # ── API: Start Agent ──
    @app.route("/api/agent/start", methods=["POST"])
    def api_agent_start():
        nonlocal agent, agent_thread

        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        mode = data.get("mode", "single")
        # Any other mode ran a live single batch, and the reply said it had
        # started in the mode asked for: a mistyped "dry-run" sent real email.
        if mode not in ("single", "continuous", "dry_run"):
            return jsonify(
                {"error": "Unknown mode: use single, continuous or dry_run"}
            ), 400
        # DRY_RUN=true makes every run a simulation, as `main.py run` does.
        # The Start button posts "continuous", which always ran live, so a
        # pilot rehearsed with DRY_RUN=true sent real email.
        dry_run = settings.dry_run or mode == "dry_run"

        with agent_lock:
            if agent_thread and agent_thread.is_alive():
                return jsonify({"error": "Agent is already running"}), 409
            refusal = _unreplayed_refusal()
            if refusal:
                return jsonify({"error": refusal}), 409

            try:
                agent = OutreachAgent(settings)
            except GuardUnavailable as e:
                return jsonify({"error": _guard_refusal(e)}), 503

            # Bind the instance as a default argument so the thread runs the
            # agent it was created for. Reading the `agent` nonlocal at execution
            # time meant both threads of a double-start drove whichever instance
            # was assigned last, while the first was orphaned - untracked by
            # agent_thread and unreachable by /api/agent/stop.
            def run_agent(_agent=agent):
                try:
                    if mode == "continuous":
                        _agent.run_continuous(
                            dry_run=dry_run, loop_delay=settings.loop_delay_seconds
                        )
                    else:
                        _agent.run_single(dry_run=dry_run)
                except Exception as e:
                    # Deliberately broad: this is the top of a daemon thread.
                    # Anything that escapes here is swallowed by threading's
                    # default hook and printed to stderr with no activity row,
                    # so the dashboard would show the agent silently stopped
                    # with nothing to explain why. Catching it is what makes
                    # AGENT_ERROR visible; .exception keeps the traceback.
                    logging.getLogger(__name__).exception("Agent error")
                    # What the run wrote on its own connection and did not
                    # commit holds the write lock until this thread ends:
                    # undone first, or the record below waits and gives up.
                    try:
                        _agent.db.rollback_thread_conn()
                    except sqlite3.Error as undo_err:
                        logging.getLogger(__name__).error(
                            "Could not undo the failed run's writes (%s)", undo_err
                        )
                    try:
                        db.log_activity("AGENT_ERROR", str(e), status="error")
                    except sqlite3.Error as log_err:
                        # A locked database fails this record too; the log
                        # file keeps the error, and the thread ends cleanly.
                        logging.getLogger(__name__).error(
                            "Could not record AGENT_ERROR (%s)", log_err
                        )

            agent_thread = threading.Thread(target=run_agent, daemon=True)
            agent_thread.start()

        return jsonify({"status": "started", "mode": mode})

    # ── API: Manual / Direct Send ──
    @app.route("/api/send-manual", methods=["POST"])
    def api_send_manual():
        nonlocal send_agent
        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        wrong = _not_text(data, "email", "name", "organization", "role", "channel")
        if wrong:
            return jsonify({"ok": False, "message": f"{wrong} must be text."}), 400
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "message": "Email address is required."}), 400
        refusal = _unreplayed_refusal()
        if refusal:
            return jsonify({"ok": False, "message": refusal}), 409

        # Reuse the running campaign agent if there is one (shared in-memory
        # state); otherwise lazily build a dedicated one for direct sends.
        active = agent if (agent_thread and agent_thread.is_alive()) else None
        if active is None:
            if send_agent is None:
                try:
                    send_agent = OutreachAgent(settings)
                except GuardUnavailable as e:
                    return jsonify({"ok": False, "message": _guard_refusal(e)}), 503
            send_agent._apply_runtime_overrides()
            active = send_agent

        # DRY_RUN=true makes Send a preview too. Only the request's flag was
        # read, so Send emailed the address while DRY_RUN said nothing goes out.
        dry_run = bool(data.get("dry_run")) or settings.dry_run
        try:
            ok, message = active.send_single_email(
                email=email,
                name=(data.get("name") or "").strip() or None,
                organization=(data.get("organization") or "").strip() or None,
                role=(data.get("role") or "").strip() or None,
                enroll_followup=bool(data.get("enroll_followup")),
                dry_run=dry_run,
                channel=(data.get("channel") or "").strip() or None,
                confirm_repeat=data.get("confirm_repeat") is True,
            )
        except Exception as e:
            logging.getLogger(__name__).error(f"Manual send error: {e}")
            logger.exception("Manual send failed")
            return jsonify(
                {"ok": False, "message": "Send failed - see the server log."}
            ), 500

        # The page asks the operator, and sends again with confirm_repeat
        # only on a yes.
        if isinstance(ok, AlreadyEmailed):
            return jsonify(
                {
                    "ok": False,
                    "already_emailed": True,
                    "last_sent_on": ok.on,
                    "message": message,
                }
            ), 409

        # A Send that DRY_RUN made a preview sent nothing, so it answers as a
        # refused send does: answered ok, the page showed it as sent.
        if dry_run and not data.get("dry_run"):
            ok = False
            message += " Only a preview: DRY_RUN is on, so nothing was sent."
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    # ── API: Stop Agent ──
    @app.route("/api/agent/stop", methods=["POST"])
    def api_agent_stop():
        nonlocal agent
        if _json_body() is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        if agent:
            agent.stop()
            return jsonify({"status": "stop_requested"})
        return jsonify({"error": "No agent running"}), 404

    # ── API: Add Query ──
    @app.route("/api/queries/add", methods=["POST"])
    def api_add_query():
        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        if _not_text(data, "query"):
            return jsonify({"error": "query must be text."}), 400
        query = (data.get("query") or "").strip()
        if not query:
            return jsonify({"error": "Query is required"}), 400

        try:
            search_id = db.log_search(query, source="dashboard", status="pending")
            return jsonify({"status": "ok", "search_id": search_id, "query": query})
        except Exception:
            logger.exception("Could not add the query")
            return jsonify({"error": "Could not add the query."}), 500

    # ── API: Review - the agent proposes, a person decides ──
    # The decision rules live in modules/review_queue.py; these routes only
    # name the person deciding (their dashboard login) and add the checks a
    # reviewer should see before the draft: what is uncertain, and whether the
    # company's own page holds text written to steer an AI.
    _INSTRUCTION_LIKE = re.compile(
        r"(?i)(ignore (?:all |any )?(?:previous|prior|above)|disregard (?:the|all|any)"
        r"|you are now|system prompt|as an ai\b|new instructions|instructions?\s*:"
        r"|<\s*/?\s*(?:script|iframe)\b)"
    )

    def _reviewer() -> str:
        signed_in = _session_user()
        if signed_in:
            return signed_in
        auth = request.authorization
        return (auth.username or "").strip() if auth else ""

    def _review_flags(item: dict) -> list[str]:
        ev = item.get("evidence") or {}
        flags = []
        if not (ev.get("company_country") or item.get("company_country")):
            flags.append("country not known")
        if (ev.get("lead_tier") or item.get("lead_tier")) in ("C", "D"):
            flags.append("low lead tier")
        if not ev.get("found_on_their_site"):
            flags.append("nothing read from their site")
        if not ev.get("role"):
            flags.append("no named role: the greeting is generic")
        if item.get("ai_written"):
            flags.append("written by the AI: check every claim")
        page = " ".join(
            str(v) for v in (ev.get("found_on_their_site"), ev.get("organization")) if v
        )
        if _INSTRUCTION_LIKE.search(page):
            flags.append(
                "their page holds instruction-like text: possible prompt injection"
            )
        return flags

    def _review_public(item: dict) -> dict:
        shown = {k: v for k, v in item.items() if k != "approved_hash"}
        # A single item read by id carries no contact columns; the screen
        # still says whom the email is for.
        if "email" not in shown and item.get("contact_id"):
            contact = db.get_contact_by_id(item["contact_id"]) or {}
            for key in ("email", "organization", "lead_score", "lead_tier", "company_country"):
                shown.setdefault(key, contact.get(key))
        shown["flags"] = _review_flags(item)
        if item.get("contact_id"):
            from modules.outcomes import stage_of

            shown["outcome"] = stage_of(db, item["contact_id"])
        ev = item.get("evidence") or {}
        if "lookalike" not in shown:
            from modules.lookalike import Lookalike

            shown["lookalike"] = Lookalike.from_db(db).score(
                [" ".join(str(x or "") for x in (ev.get("organization"), ev.get("found_on_their_site")))]
            )[0]
        shown["fit"] = assess_fit(
            {
                "organization": ev.get("organization") or shown.get("organization"),
                "qualification_rationale": ev.get("found_on_their_site") or ev.get("their_words"),
                "company_country": ev.get("company_country") or shown.get("company_country"),
            }
        )
        return shown

    def _review_queue() -> ReviewQueue:
        return ReviewQueue(db, effective_settings(settings, db).review_daily_cap)

    def _sectors() -> list[dict]:
        from modules.client_profile import PROFILE

        sectors = []
        for s in PROFILE.sectors:
            if not (s.get("id") and s.get("name")):
                continue
            queries = _sector_queries(s)
            sectors.append(
                {
                    "id": s.get("id"),
                    "name": s.get("name"),
                    "type": s.get("type"),
                    "why": s.get("why_omega_fits") or s.get("why") or "",
                    "barriers": s.get("barriers") or "",
                    "buyer_roles": s.get("buyer_roles") or [],
                    "countries": s.get("priority_countries") or [],
                    "evidence": [
                        e
                        for e in (s.get("demand_evidence") or [])
                        if isinstance(e, dict)
                        and str(e.get("source_url", "")).startswith(("https://", "http://"))
                    ],
                    "queries": queries,
                    **_sector_outcomes(queries),
                }
            )
        return sectors

    def _sector_outcomes(queries: list[str]) -> dict:
        """When the sector's searches were last queued, and what people
        decided about the companies they found."""
        if not queries:
            return {"queued_at": None, "approved": 0, "rejected": 0, "found": 0}
        marks = ",".join("?" for _ in queries)
        try:
            row = db.conn.execute(
                f"""SELECT MAX(s.timestamp),
                           COUNT(DISTINCT c.id),
                           COUNT(DISTINCT CASE WHEN r.status IN ('approved', 'sent')
                                               THEN c.id END),
                           COUNT(DISTINCT CASE WHEN r.status = 'rejected' THEN c.id END)
                    FROM search_log s
                    LEFT JOIN contacts c ON c.search_id = s.id
                    LEFT JOIN review_queue r
                           ON r.contact_id = c.id AND r.stage = 'first_email'
                    WHERE s.search_query IN ({marks})""",
                tuple(queries),
            ).fetchone()
        except sqlite3.Error:
            logger.exception("Could not read a sector's outcomes")
            return {"queued_at": None, "approved": 0, "rejected": 0, "found": 0}
        return {"queued_at": row[0], "found": row[1], "approved": row[2], "rejected": row[3]}

    def _sector_queries(sector: dict) -> list[str]:
        keywords = sector.get("search_keywords") or sector.get("keywords") or {}
        if isinstance(keywords, list):
            return [k for k in keywords if isinstance(k, str)][:30]
        return [
            k
            for lang in ("en", "it", "de")
            for k in keywords.get(lang, [])
            if isinstance(k, str)
        ][:30]

    def _review_error(e: Exception):
        return jsonify({"error": str(e)}), 400

    _REVIEW_VIEWS = {
        "waiting": None,
        "approved": ("approved",),
        "decided": ("sent", "rejected"),
    }

    @app.route("/api/review")
    def api_review():
        eff = effective_settings(settings, db)
        view = request.args.get("view", "waiting")
        if view not in _REVIEW_VIEWS:
            return jsonify({"error": f"view is one of {sorted(_REVIEW_VIEWS)}"}), 400
        q = _review_queue()
        try:
            statuses = _REVIEW_VIEWS[view]
            items = q.pending(limit=200) if statuses is None else q.listing(statuses)
            metrics = q.metrics()
            counts = q.counts()
        except sqlite3.Error:
            logger.exception("Could not read the review queue")
            return jsonify({"error": "Could not read the review queue."}), 500
        from modules.lookalike import Lookalike

        likeness = Lookalike.from_db(db).score(
            [
                " ".join(
                    str(x or "")
                    for x in ((i.get("evidence") or {}).get("organization"), (i.get("evidence") or {}).get("found_on_their_site"))
                )
                for i in items
            ]
        )
        for i, like in zip(items, likeness):
            i["lookalike"] = like
        return jsonify(
            {
                "view": view,
                "items": [_review_public(i) for i in items],
                "pending": [_review_public(i) for i in items] if statuses is None else [],
                "metrics": metrics,
                "counts": counts,
                "levels": {s: level_for(eff, s) for s in REVIEW_STAGES},
                "reviewer": _reviewer(),
                "is_admin": _reviewer() == settings.dashboard_username,
                "sectors": _sectors(),
            }
        )

    _STATUS_WORDS = {
        "pending": "Not contacted yet",
        "emailed": "Emailed",
        "responded": "Replied",
        "active_client": "Client",
        "unsubscribed": "Opted out",
        "bounced": "Bounced",
        "blocked": "Blocked",
        "rejected_in_review": "Rejected in review",
        "irrelevant": "Not a fit",
        "ai_irrelevant": "Not a fit",
        "low_score": "Low score",
        "unverified": "Address not verified",
    }

    @app.route("/api/results")
    def api_results():
        """What the outreach has found so far, for the three plain charts:
        leads per sector, leads per status, average fit per sector."""
        try:
            contacts = db.get_all_contacts(2000)
        except sqlite3.Error:
            logger.exception("Could not read the contacts")
            return jsonify({"error": "Could not read the contacts."}), 500
        per_sector, per_status, fit_sum = {}, {}, {}
        for c in contacts:
            if c.get("status") == "erased":
                continue
            fit = assess_fit(c)
            sector = fit["sector"] or "No sector matched"
            per_sector[sector] = per_sector.get(sector, 0) + 1
            fit_sum[sector] = fit_sum.get(sector, 0) + fit["total"]
            word = _STATUS_WORDS.get(c.get("status"), c.get("status") or "Unknown")
            per_status[word] = per_status.get(word, 0) + 1
        ordered = lambda d: sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
        return jsonify(
            {
                "per_sector": ordered(per_sector),
                "per_status": ordered(per_status),
                "fit_per_sector": ordered(
                    {k: round(fit_sum[k] / per_sector[k]) for k in per_sector}
                ),
            }
        )

    def _csv_cell(value) -> str:
        """A spreadsheet runs a cell that starts with = + - @ as a formula:
        a scraped company name must never become one."""
        text = "" if value is None else str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text

    @app.route("/api/export/leads.csv")
    def api_export_leads():
        """The leads a person approved, as a CSV that HubSpot imports as
        companies and contacts. Omega's CRM or King can read the same file."""
        import csv
        import io

        try:
            rows = db.conn.execute(
                """SELECT c.*, r.status AS review_status, r.decided_at, r.reviewer
                   FROM contacts c JOIN review_queue r
                     ON r.contact_id = c.id AND r.stage = 'first_email'
                   WHERE r.status IN ('approved', 'sent') AND c.status != 'erased'
                   ORDER BY r.decided_at DESC"""
            ).fetchall()
        except sqlite3.Error:
            logger.exception("Could not export the leads")
            return jsonify({"error": "Could not export the leads."}), 500
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(
            [
                "Company name", "Company Domain Name", "Email", "First Name", "Last Name",
                "Job Title", "Country/Region", "Industry", "Company type", "Fit score",
                "Lead Status", "Source URL", "Approved by", "Approved on",
            ]
        )
        for r in rows:
            c = dict(r)
            fit = assess_fit(c)
            first, _, last = (c.get("name") or "").partition(" ")
            email = c.get("email") or ""
            w.writerow(
                [
                    _csv_cell(v)
                    for v in (
                        c.get("organization"), email.rsplit("@", 1)[-1], email, first, last,
                        c.get("role"), c.get("company_country"), fit["sector"],
                        fit["company_type"], fit["total"],
                        "IN_PROGRESS" if c.get("review_status") == "sent" else "NEW",
                        c.get("source_url"), c.get("reviewer"), c.get("decided_at"),
                    )
                ]
            )
        db.log_activity(
            "LEADS_EXPORTED",
            f"{len(rows)} approved lead(s) exported as CSV by {_reviewer()}",
            status="info",
        )
        return Response(
            "\ufeff" + out.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="omega-leads.csv"'},
        )

    @app.route("/api/review/paste-reply", methods=["POST"])
    def api_paste_reply():
        """A reply that reached someone's own mailbox, pasted in. An opt-out
        or a refusal is applied at once, as the inbox reader would; anything
        else becomes an answer for a person to review."""
        from modules import reply_intent
        from modules.company_block import block_company

        nonlocal send_agent
        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        if _not_text(data, "email", "subject", "body"):
            return jsonify({"error": "email, subject and body must be text."}), 400
        email = (data.get("email") or "").strip().lower()
        body = data.get("body") or ""
        subject = (data.get("subject") or "").strip()
        if not body.strip():
            return jsonify({"error": "Paste the reply's text."}), 400
        contact = db.get_contact_by_email(email)
        if not contact or not db.has_been_emailed(contact["id"]):
            return jsonify({"error": "That address is not a company Omega has emailed."}), 400
        fresh = reply_intent.strip_quoted(body)
        if reply_intent.is_optout(subject, fresh) or reply_intent.is_decline(subject, body):
            label = "opt-out" if reply_intent.is_optout(subject, fresh) else "not interested"
            db.add_to_suppression(email, reason=f"{label} reply, pasted by {_reviewer()}")
            block_company(db, email, f"{label} reply, pasted by {_reviewer()}")
            db.log_activity(
                "PASTED_REPLY_OBJECTION",
                f"{email}: {label}; the company is not emailed again",
                status="warning",
            )
            return jsonify({"label": label, "applied": "The company will not be emailed again."})
        active = agent if (agent_thread and agent_thread.is_alive()) else send_agent
        if active is None:
            try:
                active = send_agent = OutreachAgent(settings)
            except GuardUnavailable as e:
                return jsonify({"error": _guard_refusal(e)}), 503
        active.loop_orchestrator.propose_reply_answers(
            [{"email": email, "subject": subject or "Re:", "body": body}]
        )
        label = {
            reply_intent.INTERESTED: "interested",
            reply_intent.NOT_INTERESTED: "not interested",
        }.get(reply_intent.classify(subject, body), "unclear")
        return jsonify({"label": label, "applied": "An answer waits in Review."})

    # ── API: outcomes - where each conversation ended up ──
    @app.route("/api/contacts/<int:contact_id>/outcome", methods=["POST"])
    def api_contact_outcome(contact_id):
        from modules import outcomes

        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        stage = data.get("stage") or None
        try:
            outcomes.set_stage(db, contact_id, stage, _reviewer())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except LookupError as e:
            return jsonify({"error": str(e)}), 404
        return jsonify({"contact_id": contact_id, "stage": stage})

    @app.route("/api/funnel")
    def api_funnel():
        from modules import outcomes

        try:
            return jsonify(outcomes.funnel(db))
        except sqlite3.Error:
            logger.exception("Could not count the funnel")
            return jsonify({"error": "Could not count the funnel."}), 500

    @app.route("/api/leads/import", methods=["POST"])
    def api_leads_import():
        """A CSV of leads: checked first (dry_run), then imported."""
        from modules import lead_import

        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        text = data.get("csv")
        if not isinstance(text, str) or not text.strip():
            return jsonify({"error": "The file is empty."}), 400
        try:
            result = lead_import.run(
                db,
                text,
                channel=str(data.get("channel") or "third_party"),
                filename=str(data.get("filename") or "")[:120],
                dry_run=data.get("dry_run") is not False,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        if result["added"]:
            db.log_activity(
                "LEADS_IMPORTED",
                f"{result['added']} leads imported from a CSV by {_reviewer()}",
                status="success",
            )
        return jsonify(result)

    @app.route("/api/review/summary")
    def api_review_summary():
        """Counts only, for the sidebar badge and the overview: cheap enough
        to ask on every poll."""
        try:
            return jsonify({"counts": _review_queue().counts(), "metrics": _review_queue().metrics()})
        except sqlite3.Error:
            logger.exception("Could not count the review queue")
            return jsonify({"error": "Could not count the review queue."}), 500

    @app.route("/api/review/<int:item_id>/open", methods=["POST"])
    def api_review_open(item_id):
        try:
            item = _review_queue().open(item_id, _reviewer())
        except ReviewError as e:
            return _review_error(e)
        return jsonify({"item": _review_public(item)})

    @app.route("/api/review/<int:item_id>/approve", methods=["POST"])
    def api_review_approve(item_id):
        nonlocal send_agent
        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        bad = _not_text(data, "subject", "body", "note")
        if bad:
            return jsonify({"error": f"{bad} must be text."}), 400
        try:
            item = _review_queue().approve(
                item_id,
                _reviewer(),
                subject=data.get("subject"),
                body=data.get("body"),
                note=data.get("note") or "",
            )
        except ReviewError as e:
            return _review_error(e)
        # An answer to a reply goes now: the prospect is waiting, and a
        # person has just approved it. First emails and follow-ups go with the
        # next batch, within the sending limits.
        sent = None
        if item["stage"] == "reply":
            active = agent if (agent_thread and agent_thread.is_alive()) else None
            if active is None:
                if send_agent is None:
                    try:
                        send_agent = OutreachAgent(settings)
                    except GuardUnavailable as e:
                        return jsonify(
                            {"item": _review_public(item), "sent": False, "message": _guard_refusal(e)}
                        )
                active = send_agent
                active._apply_runtime_overrides()
            ok, message = active.send_reply_answer(item_id, dry_run=settings.dry_run)
            sent = {"ok": ok, "message": message}
            item = _review_queue().get(item_id) or item
        return jsonify({"item": _review_public(item), "sent": sent})

    @app.route("/api/review/<int:item_id>/reject", methods=["POST"])
    def api_review_reject(item_id):
        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        if _not_text(data, "reason"):
            return jsonify({"error": "reason must be text."}), 400
        try:
            item = _review_queue().reject(
                item_id, _reviewer(), data.get("reason") or ""
            )
        except ReviewError as e:
            return _review_error(e)
        return jsonify({"item": _review_public(item)})

    @app.route("/api/review/<int:item_id>/reopen", methods=["POST"])
    def api_review_reopen(item_id):
        try:
            item = _review_queue().reopen(item_id, _reviewer())
        except ReviewError as e:
            return _review_error(e)
        return jsonify({"item": _review_public(item)})

    @app.route("/api/autonomy", methods=["POST"])
    def api_autonomy():
        """Change who decides at one stage. Only the administrator's login may,
        with a reason, and the change is journalled: moving a stage to "auto"
        is the one switch that takes people out of the loop."""
        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        if _reviewer() != settings.dashboard_username:
            return jsonify(
                {"error": "Only the administrator may change who decides."}
            ), 403
        if _not_text(data, "stage", "level", "reason"):
            return jsonify({"error": "stage, level and reason must be text."}), 400
        stage = (data.get("stage") or "").strip()
        level = (data.get("level") or "").strip().lower()
        reason = (data.get("reason") or "").strip()
        if stage not in REVIEW_STAGES or level not in REVIEW_LEVELS:
            return jsonify(
                {
                    "error": f"stage is one of {REVIEW_STAGES}, level one of {REVIEW_LEVELS}."
                }
            ), 400
        if not reason:
            return jsonify(
                {"error": "Say why: a change of who decides needs a reason."}
            ), 400
        try:
            db.set_runtime_override(f"autonomy_{stage}", level)
            db.log_activity(
                "AUTONOMY_CHANGED",
                f"{stage} set to {level} by {_reviewer()}: {reason}",
                status="warning",
            )
        except sqlite3.Error:
            logger.exception("Could not save the autonomy change")
            return jsonify({"error": "Could not save the change."}), 500
        return jsonify({"status": "ok", "stage": stage, "level": level})

    @app.route("/api/strategy/queue", methods=["POST"])
    def api_strategy_queue():
        data = _json_body()
        if data is None:
            return jsonify({"error": "Content-Type: application/json required"}), 415
        sector = next((s for s in _sectors() if s["id"] == data.get("sector")), None)
        if sector is None:
            return jsonify({"error": "Unknown sector."}), 400
        try:
            for query in sector["queries"]:
                db.log_search(query, source="strategy", status="pending")
            db.log_activity(
                "STRATEGY_QUEUED",
                f"{len(sector['queries'])} searches queued for {sector['name']} by {_reviewer()}",
                status="info",
            )
        except sqlite3.Error:
            logger.exception("Could not queue the sector's searches")
            return jsonify({"error": "Could not queue the searches."}), 500
        return jsonify({"status": "ok", "queued": len(sector["queries"])})

    # ── API: Agent Status ──
    @app.route("/api/agent/status")
    def api_agent_status():
        running = agent_thread.is_alive() if agent_thread else False
        # A live thread is not the same as a working pipeline: a reputation
        # guard can pause SENDING while the loop keeps running. Without this the
        # dashboard shows a reassuring green while nothing is going out.
        paused = bool(agent and getattr(agent, "_sending_paused", False))
        pause_reason = getattr(agent, "_pause_reason", None) if agent else None
        return jsonify(
            {
                "running": running,
                "sending_paused": paused,
                "pause_reason": pause_reason,
                "dry_run": settings.dry_run,
                "stats": db.get_stats(),
            }
        )

    # ── API: Ready to send ──
    @app.route("/api/readiness")
    def api_readiness():
        try:
            return jsonify(readiness_report(settings))
        except Exception:  # the page shows the message, the log the cause
            logger.exception("Could not run the readiness checks")
            return jsonify(
                {
                    "error": "Could not run the checks just now - see the server "
                    "log, or run main.py doctor."
                }
            ), 503

    # ── API: Connect Omega - what is left before a live email can go ──
    @app.route("/api/setup")
    def api_setup():
        try:
            report = readiness_report(settings)
        except Exception:  # the page shows the message, the log the cause
            logger.exception("Could not run the setup checks")
            return jsonify({"error": "Could not run the checks just now."}), 503
        by_label = {c["label"]: c for c in report["checks"]}
        domain = (
            settings.smtp_username.split("@")[-1] if "@" in settings.smtp_username else ""
        )
        steps = []

        def step(key, title, how, checks=(), required=True, status=None, detail=None):
            found = [by_label[label] for label in checks if label in by_label]
            if status is None:
                oks = [c["ok"] for c in found]
                if not found:
                    status = "unknown"
                elif any(ok is False for ok in oks):
                    status = "todo"
                elif all(ok is True for ok in oks):
                    status = "done"
                else:
                    status = "off"
            if detail is None:
                detail = "; ".join(c["detail"] for c in found)
            steps.append(
                {"key": key, "title": title, "status": status, "detail": detail,
                 "how": how, "required": required}
            )

        step("mailbox", "Sending mailbox",
             "Omega's IT creates an outreach mailbox on omegaifs.it and enters its "
             "SMTP login, or connects Microsoft 365 without a password (docs/DEPLOY.md, section 12).",
             ["SMTP credentials set"])
        if domain:
            spf, dmarc, _note = _auth_records(domain)
            dkim, selector = _dkim_record(domain)
            marks = {"SPF": spf, "DKIM": dkim, "DMARC": dmarc}
            step("domain", f"Domain authentication ({domain})",
                 "Omega's IT publishes SPF, DKIM and DMARC records for the sending "
                 "domain, so mail is not marked as spam.",
                 status="done" if all(marks.values())
                 else "todo" if False in marks.values() else "unknown",
                 detail=", ".join(
                     f"{k} {'found' if v else 'missing' if v is False else 'not checked'}"
                     for k, v in marks.items()
                 ) + (f" (DKIM selector {selector})" if selector else ""))
        else:
            step("domain", "Domain authentication",
                 "Set the sending mailbox first; then SPF, DKIM and DMARC are checked on its domain.",
                 status="todo", detail="no sending domain yet")
        step("legal", "Legal details in every email",
             "Company name, registered office, registration number and the privacy page, "
             "shown in each email's footer.",
             ["Legal identity (email footer)", "Legal footer in templates"])
        step("optout", "One-click opt-out",
             "A public https address for the unsubscribe link (BASE_URL).",
             ["Opt-out route"])
        step("customers", "Existing customers excluded",
             "Omega's customer list (and competitors, optional), so they are never cold-emailed.",
             ["Client list file", "Competitor list file"])
        step("countries", "Countries enabled",
             "The countries Omega's lawyer has cleared for B2B email (ENABLED_COUNTRIES).",
             ["Country lock"])
        step("inbox", "Replies read automatically",
             "IMAP access to the mailbox, so a reply, bounce or opt-out stops the follow-ups.",
             ["Inbox read"], required=False)
        ai_on = bool(settings.llm_base_url) and settings.ai_personalization_enabled
        step("ai", "AI writer",
             "An OpenAI-compatible model endpoint (LLM_BASE_URL). Without one, the agent uses "
             "Omega's own template, which a person still approves.",
             required=False, status="done" if ai_on else "off",
             detail="drafts personalised by the model" if ai_on else "off: Omega's template is used")
        required = [x for x in steps if x["required"]]
        return jsonify(
            {
                "steps": steps,
                "done": sum(x["status"] == "done" for x in required),
                "required": len(required),
                "can_send_live": report["can_send_live"],
                "dry_run": report["dry_run"],
            }
        )

    # ── API: open data-subject requests ──
    @app.route("/api/data-requests")
    def api_data_requests():
        """The open register, soonest due first. No address, token or message
        reference: the page shows no requester, and answering stays in the
        CLI (main.py dsr ...)."""
        from datetime import datetime

        try:
            rows = db.list_dsrs(open_only=True)
        except (sqlite3.Error, RuntimeError):
            logger.exception("Could not read the data-request register")
            return jsonify(
                {"error": "Could not read the data requests just now - try again."}
            ), 503
        now = clock.now()
        out = []
        for r in rows:
            due = datetime.fromisoformat(r["due_at"])
            out.append(
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "channel": r["channel"],
                    "received_at": r["received_at"],
                    "due_at": r["due_at"],
                    "days_left": (due.date() - now.date()).days,
                    "overdue": due < now,
                }
            )
        return jsonify(out)

    return app


_LOOPBACK = ("127.0.0.1", "localhost", "::1")

DASHBOARD_PASSWORD_MIN_LENGTH = 12
# Wrong passwords one address may try in the window before every request from
# it is answered 429 until the oldest of them leaves the window.
LOGIN_FAILURES_ALLOWED = 10
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
# The largest body the dashboard reads is a template save: the shipped ones
# are about 3 KB, so 1 MB leaves room for any real email and none for a flood.
MAX_REQUEST_BYTES = 1024 * 1024
TRACK_REQUESTS_PER_MINUTE = 60

# The page's libraries are served from static/vendor (versions, sources and
# hashes in its README), so the policy below allows nothing from another
# server: a CDN that is hacked, or blocked by the office network, cannot
# change or break the page.
# 'unsafe-inline' stays for styles only: the page and its script set style
# attributes, and the template preview shows the email's own inline styles.
DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _trusted_hosts(settings: Settings) -> list[str]:
    """The host names the dashboard answers to."""
    from urllib.parse import urlparse

    hosts = set(_LOOPBACK)
    public = urlparse(settings.base_url or "").hostname
    if public:
        hosts.add(public)
    if settings.dashboard_host not in ("", "0.0.0.0", "::"):
        hosts.add(settings.dashboard_host)
    return sorted(hosts)


def dashboard_password_refusal(settings: Settings) -> str | None:
    """Why the dashboard must not start as configured, or None.

    It needs a password wherever it listens. On loopback too: any other user
    or program on this machine reaches 127.0.0.1, and the dashboard sends mail
    as the client and shows every contact.
    """
    password = settings.dashboard_password or ""
    if not password.strip():
        return (
            "DASHBOARD_PASSWORD is blank - anyone who can reach the dashboard, "
            "on this machine or beyond, could send mail, read the contacts and "
            "change settings"
        )
    if len(password) < DASHBOARD_PASSWORD_MIN_LENGTH:
        return (
            f"DASHBOARD_PASSWORD is shorter than {DASHBOARD_PASSWORD_MIN_LENGTH} "
            "characters - too easy to guess; choose a longer one"
        )
    return None


# The blocking doctor checks the agent and the dashboard run again at every
# start. The other security rules refuse earlier, for every command: the
# secrets, the AI address and Microsoft sign-in when the settings load, a blank
# ERASURE_KEY when it is set, the dashboard password in cmd_dashboard.
START_CHECKS = ("Erasure key", "Erasure key matches the database")


def refuse_an_unsafe_start(settings: Settings) -> None:
    """Stop, in words, if a START_CHECKS check fails. Without it a database
    that kept a short key ran on, and one erased under another key stopped
    on a traceback."""
    failed = [
        c
        for c in run_doctor_checks(settings, network=False, only=START_CHECKS)
        if c.ok is False
    ]
    if failed:
        for c in failed:
            print(f"  Refusing to start - {c.label}: {c.detail}.")
        print("  `main.py doctor` shows every check.")
        sys.exit(1)


def _behind_the_journal(behind: int) -> str:
    return (
        f"the database is behind its journal by {behind} line(s): it was "
        "restored from a backup, or it lost writes. Run "
        "`py -3.15 main.py replay-journal` first: until then the emails sent "
        "since would go out again and the opt-outs made since would be lost"
    )


def unreplayed_journal(settings: Settings) -> str | None:
    """Why nothing may be started or sent because the database is behind its
    journal (modules.journal.unreplayed), or None. A check that cannot be
    made refuses too: a restored database taken for an up-to-date one emails
    again everyone emailed since the backup."""
    from modules.journal import unreplayed

    try:
        behind = unreplayed(settings.get_abs_db_path())
    except (sqlite3.Error, OSError, ValueError) as e:
        # ValueError: a journal that is not UTF-8 any more.
        return f"whether the database is behind its journal could not be checked ({e})"
    return _behind_the_journal(behind) if behind else None


def record_unreplayed(settings: Settings, refusal: str) -> None:
    """A start refused before the replay, in the log file and the activity
    log, where the dashboard's errors panel shows it."""
    logger.error("Refusing to start - %s", refusal)
    try:
        Database(settings.get_abs_db_path()).log_activity(
            "JOURNAL_NOT_REPLAYED", refusal, status="error"
        )
    except (sqlite3.Error, OSError, RuntimeError) as e:
        logger.error("Could not record JOURNAL_NOT_REPLAYED (%s)", e)


def refuse_a_start_before_the_replay(settings: Settings) -> None:
    """Stop, in words, while the database is behind its journal."""
    refusal = unreplayed_journal(settings)
    if refusal:
        print(f"  Refusing to start - {refusal}.")
        record_unreplayed(settings, refusal)
        sys.exit(1)


def dev_server_refusal(settings: Settings) -> str | None:
    """Why Flask's development server must not serve this dashboard, or None.

    It is built for one developer on one computer: it is not hardened
    against slow or malformed requests from a network. So it may listen on
    this computer only, and not behind a public BASE_URL either, which means
    a tunnel or proxy brings the outside in."""
    from urllib.parse import urlparse

    if settings.dashboard_host not in _LOOPBACK:
        return (
            f"DASHBOARD_HOST is {settings.dashboard_host}, so other computers "
            "can reach the dashboard"
        )
    public = urlparse(settings.base_url or "").hostname
    if public and public not in _LOOPBACK:
        return (
            f"BASE_URL names {public}, so a tunnel or proxy brings other "
            "computers to the dashboard"
        )
    return None


# Waitress answers this many requests at once; the dashboard's own page sends
# a handful together, and the agent runs in a thread of its own.
DASHBOARD_THREADS = 8


def cmd_dashboard(args, settings: Settings):
    """Launch the web dashboard on waitress, or on Flask's development server
    with --dev-server, on this computer only."""
    refusal = dashboard_password_refusal(settings)
    if refusal:
        print(f"  Refusing to start: {refusal}.")
        sys.exit(1)
    dev_server = getattr(args, "dev_server", False)
    if dev_server:
        exposed = dev_server_refusal(settings)
        if exposed:
            print(
                f"  Refusing to start: --dev-server is Flask's development "
                f"server, for this computer only, and {exposed}. Start the "
                "dashboard without --dev-server."
            )
            sys.exit(1)
    else:
        try:
            from waitress import serve
        except ImportError:
            print("  Refusing to start: waitress, the dashboard's web server, is not")
            print("  installed. Install it: py -3.15 -m pip install -r requirements.txt")
            sys.exit(1)
    refuse_an_unsafe_start(settings)
    try:
        app = create_dashboard_app(settings)
    except ImportError:
        print("Flask is required for the dashboard. Install it:")
        print("  pip install flask")
        sys.exit(1)

    host = settings.dashboard_host
    port = settings.dashboard_port
    print(f"\n{'=' * 50}")
    print("  B2B Outreach Agent Dashboard")
    print(f"  Running at: http://{host}:{port}")
    if dev_server:
        print("  On Flask's development server (--dev-server)")
    print(f"{'=' * 50}\n")

    if dev_server:
        # Never Werkzeug's debugger, whatever DEBUG says: it runs any code it
        # is sent. DEBUG still makes the log verbose.
        app.run(host=host, port=port, debug=False, use_debugger=False, use_reloader=False)
        return
    # No trusted proxy is named, so waitress drops X-Forwarded-* headers and
    # the login lockout counts the connection's own address. The body limit
    # is the app's own, refused here before the body is read.
    serve(
        app,
        host=host,
        port=port,
        threads=DASHBOARD_THREADS,
        max_request_body_size=MAX_REQUEST_BYTES,
        expose_tracebacks=False,
        ident="",
    )


# ─────────────────────────────────────────────────────────────────────
# Argument Parser
# ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="B2B Outreach Agent",
        description="Human-in-the-loop B2B outreach agent for Omega IFS, an Italian "
        "maker of special cold-formed fasteners and precision parts.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── run ──
    run_parser = subparsers.add_parser("run", help="Execute the outreach agent")
    run_parser.add_argument(
        "--continuous", action="store_true", help="Run in continuous loop mode"
    )
    run_parser.add_argument(
        "--dry-run", action="store_true", help="Execute without actually sending emails"
    )
    run_parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Override configured queries with a single query",
    )
    run_parser.add_argument(
        "--loop-delay",
        type=float,
        default=None,
        help="Seconds between loops in continuous mode "
        "(default: LOOP_DELAY_SECONDS from .env, or 300)",
    )

    # ── dashboard ──
    dashboard_parser = subparsers.add_parser(
        "dashboard", help="Launch the web monitoring dashboard"
    )
    dashboard_parser.add_argument(
        "--dev-server",
        action="store_true",
        help="Use Flask's development server instead of waitress. Refused "
        "unless the dashboard is reachable from this computer only.",
    )

    # ── status ──
    subparsers.add_parser("status", help="Show current database statistics")

    # ── doctor ──
    doctor_parser = subparsers.add_parser(
        "doctor", help="Run preflight configuration checks"
    )
    doctor_parser.add_argument(
        "--smtp", action="store_true", help="Also attempt a live SMTP login (network)"
    )

    # ── ai-benchmark ──
    from modules import ai_benchmark

    bench_parser = subparsers.add_parser(
        "ai-benchmark",
        help="Score the configured AI model on a fixed exam of the agent's questions",
    )
    bench_parser.add_argument(
        "--questions",
        type=ai_benchmark.questions_arg,
        default=list(ai_benchmark.QUESTIONS),
        help="Which questions, separated by commas: "
        + ",".join(ai_benchmark.QUESTIONS)
        + " (default: all)",
    )
    bench_parser.add_argument(
        "--repeat",
        type=ai_benchmark.repeat_arg,
        default=ai_benchmark.DEFAULT_REPEAT,
        help=f"Times each case is asked, 1 to {ai_benchmark.MAX_REPEAT} "
        f"(default {ai_benchmark.DEFAULT_REPEAT})",
    )
    bench_parser.add_argument(
        "--quick", action="store_true", help="Ask a fixed small subset of the cases"
    )
    bench_parser.add_argument(
        "--models",
        type=ai_benchmark.models_arg,
        help="Own server only: the same exam for each named model, then a comparison",
    )
    bench_parser.add_argument(
        "--cases",
        type=Path,
        help="A folder of the company's own cases (<question>.json); needs AI_DPA_REFERENCE",
    )
    bench_parser.add_argument(
        "--out",
        type=Path,
        help="Write report.md and results.json in this folder (with --blind: the "
        "blind cases and their key)",
    )
    bench_parser.add_argument(
        "--check", action="store_true", help="Validate the case files; ask no AI"
    )
    bench_parser.add_argument(
        "--blind",
        choices=ai_benchmark.QUESTIONS,
        help="Print that question's cases as JSON, numbered and without anything "
        "that shows the right answer",
    )

    # ── ai-proof ──
    from modules import ai_proof

    proof_parser = subparsers.add_parser(
        "ai-proof",
        help="Run the whole agent with the configured AI on invented cases; sends nothing",
    )
    proof_parser.add_argument(
        "--companies",
        type=ai_proof.count_arg,
        default=ai_proof.DEFAULT_COMPANIES,
        help=f"Invented companies to qualify, 0 to {ai_proof.MAX_CASES} "
        f"(default {ai_proof.DEFAULT_COMPANIES})",
    )
    proof_parser.add_argument(
        "--replies",
        type=ai_proof.count_arg,
        default=ai_proof.DEFAULT_REPLIES,
        help=f"Invented replies to judge, 0 to {ai_proof.MAX_CASES} "
        f"(default {ai_proof.DEFAULT_REPLIES})",
    )
    proof_parser.add_argument(
        "--out", type=Path, help="Also write proof.txt and the agent's log in this folder"
    )
    proof_parser.add_argument(
        "--work-dir",
        type=Path,
        help="A folder that does not exist yet, for the run's database (default: a "
        "new temporary folder); it is removed afterwards",
    )

    # ── add-query ──
    aq_parser = subparsers.add_parser("add-query", help="Add a search query")
    aq_parser.add_argument("query_text", type=str, help="The search query to add")

    # ── gdpr-export ──
    export_parser = subparsers.add_parser(
        "gdpr-export", help="Export data for an email"
    )
    export_parser.add_argument("email", type=_email_arg, help="Email address to export")

    # ── gdpr-erase ──
    erase_parser = subparsers.add_parser("gdpr-erase", help="Erase data for an email")
    erase_parser.add_argument(
        "email", type=_erasable_arg, help="Email address to erase"
    )

    # ── backup, replay-journal, restore-check, restore-drill ──
    subparsers.add_parser("backup", help="Write a checked backup of the database")
    subparsers.add_parser(
        "security-check",
        help="Look up the installed packages in pip-audit's list of known "
        "vulnerabilities; installs nothing",
    )
    subparsers.add_parser(
        "replay-journal", help="After a restore: apply what happened since"
    )
    subparsers.add_parser(
        "restore-check", help="Rehearse a restore of the newest backup"
    )
    drill_parser = subparsers.add_parser(
        "restore-drill",
        help="Restore a backup in a temporary folder and prove nothing is lost",
    )
    drill_source = drill_parser.add_mutually_exclusive_group()
    drill_source.add_argument(
        "--newest",
        action="store_true",
        help="Drill the newest backup instead of taking a fresh one",
    )
    drill_source.add_argument(
        "--backup",
        type=Path,
        default=None,
        help="Drill this backup file (an off-machine copy, for example)",
    )

    # ── retention ──
    retention_parser = subparsers.add_parser(
        "retention", help="Apply the retention periods now, without the agent"
    )
    retention_parser.add_argument(
        "--dry-run", action="store_true", help="Count what would go; change nothing"
    )

    # ── company-country ──
    company_country_parser = subparsers.add_parser(
        "company-country",
        help="Work out the company country of stored contacts not yet emailed",
    )
    company_country_parser.add_argument(
        "--ai",
        action="store_true",
        help="Also ask the AI (COMPANY_COUNTRY_AI, a key and AI_DPA_REFERENCE)",
    )
    company_country_parser.add_argument(
        "--dry-run", action="store_true", help="Count what would be found; change nothing"
    )

    # ── dsr ──
    dsr_parser = subparsers.add_parser(
        "dsr", help="Data-subject requests: list, show, add, close, erase"
    )
    dsr_actions = dsr_parser.add_subparsers(dest="action", required=True)
    dsr_list = dsr_actions.add_parser("list", help="Open requests, masked")
    dsr_list.add_argument("--all", action="store_true", help="Closed ones too")
    dsr_show = dsr_actions.add_parser("show", help="One request, full address")
    dsr_show.add_argument("id", type=int)
    dsr_add = dsr_actions.add_parser("add", help="Register a request received by hand")
    dsr_add.add_argument("email", type=_email_arg)
    dsr_add.add_argument(
        "--kind", choices=["access", "erasure", "other"], required=True
    )
    dsr_add.add_argument(
        "--received",
        type=_received_arg,
        help="The day it arrived (YYYY-MM-DD), if not today: its month runs from then",
    )
    dsr_export = dsr_actions.add_parser("export", help="Write the access answer")
    dsr_export.add_argument("id", type=int)
    dsr_close = dsr_actions.add_parser("close", help="Record how a request ended")
    dsr_close.add_argument("id", type=int)
    dsr_close.add_argument("--outcome", required=True)
    dsr_erase = dsr_actions.add_parser("erase", help="Erase the requester's data")
    dsr_erase.add_argument("id", type=int)

    # ── lists ──
    lists_parser = subparsers.add_parser(
        "lists", help="Encrypt the client and competitor lists (LISTS_KEY)"
    )
    lists_actions = lists_parser.add_subparsers(dest="action", required=True)
    encrypt_parser = lists_actions.add_parser(
        "encrypt", help="Encrypt a plain list into <file>.enc and check it"
    )
    encrypt_parser.add_argument("file", type=Path, help="The plain workbook")
    encrypt_parser.add_argument(
        "--delete-plain",
        action="store_true",
        help="Then overwrite and delete the plain file",
    )
    lists_actions.add_parser(
        "check", help="Say whether each configured list is encrypted and readable"
    )
    decrypt_parser = lists_actions.add_parser(
        "decrypt", help="Write a plain copy of an encrypted list (a warning)"
    )
    decrypt_parser.add_argument("file", type=Path, help="The encrypted list")
    decrypt_parser.add_argument(
        "--out", type=Path, required=True, help="Where to write the plain copy"
    )

    # ── handover-restore ──
    handover_parser = subparsers.add_parser(
        "handover-restore",
        help="Unpack the owner's handover archive into a new folder (asks the passphrase)",
    )
    handover_parser.add_argument("archive", type=Path, help="The handover archive")
    handover_parser.add_argument(
        "--into",
        type=Path,
        default=None,
        help="A new or empty folder to unpack into (default: beside the archive, "
        "named after it)",
    )

    # ── suppress-domain ──
    sd_parser = subparsers.add_parser(
        "suppress-domain", help="List or lift company-wide blocks"
    )
    sd_actions = sd_parser.add_subparsers(dest="action", required=True)
    sd_actions.add_parser("list", help="Show every company block")
    lift_parser = sd_actions.add_parser("lift", help="Lift one company's block")
    lift_parser.add_argument("domain", type=str, help="The company's domain")
    lift_parser.add_argument(
        "--reason", required=True, help="Why the block is lifted (it is logged)"
    )

    return parser


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────


def _invalid_setting_names(error) -> list[str]:
    """The .env names of the settings a configuration error is about. A
    cross-field error has no single setting, and a SettingsError or OSError
    names none, so the list can be empty."""
    try:
        details = error.errors()
    except AttributeError:
        return []
    return sorted({str(d["loc"][0]).upper() for d in details if d.get("loc")})


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Before any setting is read: the computer it unpacks onto has no .env yet,
    # and the settings of the install it runs from are not the archive's.
    if args.command == "handover-restore":
        cmd_handover_restore(args)
        return

    # Load settings
    try:
        settings = load_settings()
    except (ValueError, OSError) as e:
        # load_settings() is Settings(), i.e. pydantic-settings: a missing or
        # out-of-range value raises ValidationError and a malformed .env raises
        # SettingsError, both subclasses of ValueError, as is the UnicodeError
        # from a non-UTF-8 .env. OSError covers a .env that cannot be read.
        # Anything else is a bug in config.py and should show its traceback
        # rather than be reported to the operator as "check your .env".
        # Never the value of a setting: a misspelt key can hold a password.
        print(f"\n[ERROR] Configuration error: {config_error_text(e)}")
        env = PROJECT_ROOT / ".env"
        if env.exists():
            # Never the copy hint here: in PowerShell `copy` is Copy-Item, which
            # overwrites without asking, so following it after a typo wiped the
            # filled .env - ERASURE_KEY included, which the database then
            # refuses to replace.
            names = _invalid_setting_names(e)
            where = ", ".join(names) if names else "the setting named above"
            print(f"  Correct {where} in {env}.")
            print(
                "  Do not copy .env.example over it: that erases every value "
                "in it, ERASURE_KEY included."
            )
        else:
            print("  There is no .env file yet. Create it from the template:")
            print("  copy .env.example .env")
        sys.exit(1)

    # ai-proof writes nothing outside its own work folder, where it keeps the
    # agent's log for the run.
    setup_logging(
        settings.debug, name=args.command, retention_days=settings.log_retention_days,
        to_file=args.command != "ai-proof",
    )

    # Every command but doctor and lists works on the database, and so needs the
    # key that makes and matches erasure tokens. doctor runs without it, to
    # report it; lists does not open the database.
    if args.command not in ("doctor", "lists"):
        try:
            set_erasure_key(settings.erasure_key)
        except ValueError as e:
            print(f"\n[ERROR] {e}")
            sys.exit(1)
    elif settings.erasure_key.strip():
        set_erasure_key(settings.erasure_key)

    # Ensure directories exist
    fh = FileHandler()
    fh.ensure_directories()

    # Dispatch command
    commands = {
        "run": cmd_run,
        "dashboard": cmd_dashboard,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "ai-benchmark": cmd_ai_benchmark,
        "ai-proof": cmd_ai_proof,
        "add-query": cmd_add_query,
        "gdpr-export": cmd_gdpr_export,
        "gdpr-erase": cmd_gdpr_erase,
        "suppress-domain": cmd_suppress_domain,
        "dsr": cmd_dsr,
        "retention": cmd_retention,
        "company-country": cmd_company_country,
        "backup": cmd_backup,
        "replay-journal": cmd_replay_journal,
        "restore-check": cmd_restore_check,
        "restore-drill": cmd_restore_drill,
        "lists": cmd_lists,
        "security-check": cmd_security_check,
    }

    handler = commands.get(args.command)
    if handler:
        try:
            handler(args, settings)
        except GuardUnavailable as e:
            # No agent was built, so nothing was sent. Recorded too: a start
            # by a scheduled task or in a container prints where no one reads,
            # and the dashboard would show an agent that is merely not running.
            print(f"\n  Refusing to start - {e}")
            logger.error("Refusing to start - %s", e)
            try:
                Database(settings.get_abs_db_path()).log_activity(
                    "GUARD_UNAVAILABLE", str(e), status="error"
                )
            except (sqlite3.Error, OSError, RuntimeError) as log_err:
                logger.error("Could not record GUARD_UNAVAILABLE (%s)", log_err)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
