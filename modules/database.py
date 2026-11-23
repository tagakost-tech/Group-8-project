"""
SQLite database operations for the B2B Outreach Agent.
Provides immutable logging of all searches, contacts, and email dispatches.
Uses WAL mode for concurrent read/write performance.

V2: Added tables for lead scoring, warmup, domain throttling, retry queue,
    sequences, open tracking, consent logging, and daily send counts.
"""

import functools
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import clock, journal
from modules.compliance import (
    COUNTRY_SOURCES,
    LANGUAGE_CHOICES,
    RULE_VERSION,
    basis_for,
    notice_language,
)
from modules.private_files import make_private_dir, make_private_file

logger = logging.getLogger(__name__)


# Each stamp column's DEFAULT reads the host's clock, and a host need not be on
# the app's zone: a container sets none. A database already made keeps its
# DEFAULT, as SQLite cannot alter one, so every insert passes clock.db_now().
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Search Queries -- every web search the agent performs
CREATE TABLE IF NOT EXISTS search_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    search_query    TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'google',
    results_count   INTEGER DEFAULT 0,
    urls_scraped    INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    notes           TEXT
);

-- Contacts -- every contact discovered and qualified
CREATE TABLE IF NOT EXISTS contacts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp               TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    search_id               INTEGER NOT NULL,
    name                    TEXT,
    role                    TEXT,
    organization            TEXT,
    email                   TEXT NOT NULL,
    source_url              TEXT,
    qualification_rationale TEXT,
    status                  TEXT DEFAULT 'pending',
    discard_reason          TEXT,
    lead_score              INTEGER DEFAULT 0,
    lead_tier               TEXT DEFAULT 'D',
    is_verified             INTEGER DEFAULT 0,
    verification_details    TEXT,
    last_reply_at           TEXT,
    source_channel          TEXT,
    basis                   TEXT,
    rule_version            TEXT,
    company_country         TEXT,
    company_country_source  TEXT,
    language_choice         TEXT,
    FOREIGN KEY (search_id) REFERENCES search_log(id)
);

-- Email Log -- every email dispatch attempt
CREATE TABLE IF NOT EXISTS email_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    contact_id          INTEGER NOT NULL,
    recipient_email     TEXT NOT NULL,
    subject             TEXT NOT NULL,
    body_preview        TEXT,
    has_attachment      INTEGER DEFAULT 1,
    attachment_filename TEXT,
    delivery_status     TEXT NOT NULL DEFAULT 'pending',
    smtp_response       TEXT,
    error_message       TEXT,
    tracking_id         TEXT,
    opened_at           TEXT,
    retry_count         INTEGER DEFAULT 0,
    sequence_step_id    INTEGER,
    notice_version      TEXT,
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);

-- Suppression List -- emails that must never be contacted again
CREATE TABLE IF NOT EXISTS suppression_list (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT UNIQUE NOT NULL,
    reason      TEXT NOT NULL,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Data-subject requests -- each access or erasure request, with the date it
-- is due (one calendar month after receipt, Art. 12(3)) and how it ended.
-- After an erasure the address is kept only as its token.
CREATE TABLE IF NOT EXISTS dsr_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    due_at      TEXT NOT NULL,
    email       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    channel     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    closed_at   TEXT,
    outcome     TEXT
);

-- Unsubscribe links (step G20) -- the opaque token each link carries, and
-- whose it is, so a click is one lookup. Erasure and retention delete them.
CREATE TABLE IF NOT EXISTS unsubscribe_tokens (
    token       TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Company blocks -- domains no email may go to after one person's objection,
-- erasure or one-click unsubscribe (modules/company_block.py). After an
-- erasure the domain is kept only as its keyed token (domain_token).
CREATE TABLE IF NOT EXISTS suppressed_domains (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT UNIQUE NOT NULL,
    reason      TEXT NOT NULL,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Agent Activity Log -- high-level operational log
CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    action      TEXT NOT NULL,
    details     TEXT,
    status      TEXT DEFAULT 'info'
);

-- Daily Send Counts -- for warmup scheduling
CREATE TABLE IF NOT EXISTS daily_send_counts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT UNIQUE NOT NULL,
    count       INTEGER DEFAULT 0,
    allowance   INTEGER DEFAULT 0
);

-- Domain Send Counts -- per-domain daily rate limiting
CREATE TABLE IF NOT EXISTS domain_send_counts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    domain      TEXT NOT NULL,
    count       INTEGER DEFAULT 0,
    UNIQUE(date, domain)
);

-- Retry Queue -- exponential backoff retry engine
CREATE TABLE IF NOT EXISTS retry_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email_log_id    INTEGER NOT NULL,
    contact_id      INTEGER NOT NULL,
    attempt         INTEGER DEFAULT 0,
    max_attempts    INTEGER DEFAULT 3,
    last_error      TEXT,
    next_retry_at   TEXT,
    status          TEXT DEFAULT 'pending',
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (email_log_id) REFERENCES email_log(id),
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);

-- Sequences -- multi-touch follow-up definitions
CREATE TABLE IF NOT EXISTS sequences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Sequence Steps -- individual steps in a sequence
CREATE TABLE IF NOT EXISTS sequence_steps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id     INTEGER NOT NULL,
    step_number     INTEGER NOT NULL,
    delay_days      INTEGER NOT NULL,
    subject         TEXT NOT NULL,
    template_name   TEXT,
    FOREIGN KEY (sequence_id) REFERENCES sequences(id)
);

-- Sequence Enrollments -- track contacts through sequences
CREATE TABLE IF NOT EXISTS sequence_enrollments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id      INTEGER NOT NULL,
    sequence_id     INTEGER NOT NULL,
    current_step    INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'active',
    enrolled_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    last_sent_at    TEXT,
    completed_at    TEXT,
    FOREIGN KEY (contact_id) REFERENCES contacts(id),
    FOREIGN KEY (sequence_id) REFERENCES sequences(id)
);

-- Open Tracking -- email open events via tracking pixel
CREATE TABLE IF NOT EXISTS open_tracking (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_id     TEXT UNIQUE NOT NULL,
    email_log_id    INTEGER NOT NULL,
    opened_at       TEXT,
    ip_address      TEXT,
    user_agent      TEXT,
    FOREIGN KEY (email_log_id) REFERENCES email_log(id)
);

-- Hot-lead Alerts -- who we already notified a human about (dedupe)
CREATE TABLE IF NOT EXISTS lead_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT UNIQUE NOT NULL,
    notified_to TEXT,
    reply_excerpt TEXT,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Agent State -- small durable key/value store that survives restarts
-- (e.g. the IMAP watermark, so an outage doesn't silently skip mail)
CREATE TABLE IF NOT EXISTS agent_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Consent Log -- GDPR compliance documentation
CREATE TABLE IF NOT EXISTS consent_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL,
    legal_basis     TEXT NOT NULL,
    details         TEXT,
    contact_id      INTEGER,
    channel         TEXT,
    rule_version    TEXT,
    timestamp       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Sends before a restore -- an email the journal records, sent to an address
-- the restored backup does not hold (Database.restore_send). Kept by the
-- address's keyed token alone, never the address, until that address is
-- collected again: add_contact then makes each one its email_log row.
CREATE TABLE IF NOT EXISTS prior_sends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    token               TEXT NOT NULL,
    message_id          TEXT,
    sent_at             TEXT NOT NULL,
    delivery_status     TEXT NOT NULL,
    sequence_step_id    INTEGER,
    sender_account      TEXT,
    notice_version      TEXT
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_prior_sends_token ON prior_sends(token);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_contacts_search ON contacts(search_id);
CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
CREATE INDEX IF NOT EXISTS idx_email_log_contact ON email_log(contact_id);
CREATE INDEX IF NOT EXISTS idx_email_log_status ON email_log(delivery_status);
CREATE INDEX IF NOT EXISTS idx_suppression_email ON suppression_list(email);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_daily_sends_date ON daily_send_counts(date);
CREATE INDEX IF NOT EXISTS idx_domain_sends ON domain_send_counts(date, domain);
CREATE INDEX IF NOT EXISTS idx_retry_status ON retry_queue(status);
CREATE INDEX IF NOT EXISTS idx_retry_next ON retry_queue(next_retry_at);
CREATE INDEX IF NOT EXISTS idx_enrollment_status ON sequence_enrollments(status);
CREATE INDEX IF NOT EXISTS idx_open_tracking_id ON open_tracking(tracking_id);
CREATE INDEX IF NOT EXISTS idx_consent_email ON consent_log(email);
"""


# Suppression reasons that mean "this address rejected our mail". Both must
# count towards the bounce rate: an SMTP 5xx at RCPT and a DSN arriving later
# are the same event learned two different ways, and a list full of dead
# addresses that only ever produced SMTP rejections would otherwise show a 0%
# bounce rate and never trip the circuit breaker. 'bounce_blocked' is a
# bounce on an address an operator had also blocked, in either order.
BOUNCE_REASONS = ("bounce", "smtp_rejected", "bounce_blocked")
BOUNCE_REASONS_SQL = "('" + "','".join(BOUNCE_REASONS) + "')"
# Retention releases a bounce after 24 months (L5.1), but never one an
# operator also blocked: the block was their decision, not the server's.
RELEASABLE_BOUNCE_REASONS_SQL = "('bounce', 'smtp_rejected')"

# email_log states that mean "this send did not go out". 'rejected' (permanent
# SMTP refusal) must count everywhere 'failed' does: the dashboard's headline
# failure count reading 0 while every send is being rejected is precisely the
# broken-looks-quiet failure mode this codebase keeps re-learning.
FAILED_STATUSES = ("failed", "rejected")
FAILED_STATUSES_SQL = "('" + "','".join(FAILED_STATUSES) + "')"

# email_log states that mean "this message reached the server". 'sent_unconfirmed'
# is a send whose acknowledgement was lost: smtplib turns any socket error during
# the post-DATA wait into SMTPServerDisconnected, so the server may well have
# accepted it. It is deliberately NOT resent - a duplicate cold email is worse
# than a missed one - so it must count as sent everywhere, or the same
# quiet-undercount problem FAILED_STATUSES exists to prevent reappears here.
SENT_STATUSES = ("sent", "sent_unconfirmed")
SENT_STATUSES_SQL = "('" + "','".join(SENT_STATUSES) + "')"

# A 'preparing' row is a send going out now, or one whose process stopped
# before recording what the server answered - and the server may have
# accepted it. Every rule that stops a second email counts it as sent until
# settle_interrupted_sends settles it; the counts of delivered mail do not.
POSSIBLY_SENT_STATUSES_SQL = "('" + "','".join((*SENT_STATUSES, "preparing")) + "')"

# The email_log state of a simulated send. The row is the record of the
# rehearsal, but nothing was attempted: it is neither an email sent nor one that
# could have failed, so no count or rate of emails may include it.
DRY_RUN_STATUS = "dry_run"

# What a settled 'preparing' row says about itself (its smtp_response).
INTERRUPTED_SEND_NOTE = (
    "Interrupted after the server may have accepted it: the result was never "
    "recorded. Counted as sent, not resent."
)

# What a send put back from the journal says about itself in email_log
# (restore_send). The journal holds no subject and no text, so the row has
# none to show.
RESTORED_SEND_SUBJECT = "(not kept: this send was restored from the journal)"
RESTORED_SEND_NOTE = (
    "Restored from the journal: sent before the backup was restored. "
    "Counted as sent, not resent."
)

# How the dashboard words a send that has not settled. Its Send button, Direct
# Send and the contact panel are all asked about the same row.
UNSETTLED_SEND = "still going out, or was cut off after it may have reached them"

# Results of refused sends that met a locked database, by database file and
# email_log id. A process that knows nothing went out must not leave a row that
# will read as possibly sent, and only this process knows, so it stays in this
# process's memory until a write gets through. Per file, not per Database: the
# dashboard holds two agents on one file, and either may be the next to write.
_kept_send_results: dict[str, dict[int, tuple]] = {}
_kept_send_results_lock = threading.Lock()

# What a refused send's row says when only the copy held on disk reached it.
HELD_REFUSAL_NOTE = (
    "Refused: the send did not go out. The database was locked when the "
    "server's answer came back, and only that much was kept."
)


def _refused_sends_dir(db_path: str) -> Path:
    """Where a refused send's result is held on disk, beside the database, as
    modules.optout_spool holds opt-outs. Memory alone was not enough: a
    dashboard that sits idle after its Direct Send, or a run that ended under
    the lock, never wrote it, and the next process settled the row as an email
    that may have gone. One file per row, named by its id."""
    return Path(db_path).resolve().parent / "spool" / "refused_sends"


def _refusals_held_on_disk(db_path: str) -> dict[int, str]:
    """The refused sends held on disk for this database: status by row id. A
    file that cannot be read still says the send did not go out."""
    folder = _refused_sends_dir(db_path)
    if not folder.is_dir():
        return {}
    held = {}
    for path in folder.glob("*.json"):
        try:
            email_id = int(path.stem)
        except ValueError:
            continue
        try:
            status = json.loads(path.read_text(encoding="utf-8"))["status"]
        except (OSError, ValueError, KeyError, TypeError):
            status = "failed"
        held[email_id] = status if status in ("failed", "rejected") else "failed"
    return held


def _instant(stamp: str, zone) -> datetime:
    """A stored timestamp as a UTC instant. One stored without an offset is
    read in ``zone``, as the later instant when that hour repeats, so a row is
    never taken for older than it is."""
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        when = when.replace(tzinfo=zone, fold=1)
    return when.astimezone(timezone.utc)


def _written_since(written: str, enrolled_at: str | None, zone) -> bool:
    """Whether a row was written at or after an enrolment began, in whole
    seconds, which is all the row's stamp keeps (an enrolment's stored time
    without an offset is UTC, as get_due_followups reads it). A time it cannot
    read counts as yes, so a step is never sent twice over it."""
    try:
        row = _instant(written, zone).replace(microsecond=0)
        enrolled = _instant(enrolled_at, timezone.utc).replace(microsecond=0)
    except (TypeError, ValueError, OverflowError):
        return True
    return row >= enrolled


def _step_possibly_sent(conn, contact_id: int, step_id: int) -> bool:
    """Whether this follow-up step may already have gone to this contact: a row
    for the step that says sent or unconfirmed, written since they were enrolled.
    An earlier row does not count, as a later enrolment may send the step again."""
    rows = conn.execute(
        "SELECT timestamp FROM email_log "
        "WHERE contact_id = ? AND sequence_step_id = ? "
        f"AND delivery_status IN {SENT_STATUSES_SQL}",
        (contact_id, step_id),
    ).fetchall()
    if not rows:
        return False
    enrolment = conn.execute(
        """SELECT se.enrolled_at FROM sequence_enrollments se
           JOIN sequence_steps ss ON ss.sequence_id = se.sequence_id
           WHERE se.contact_id = ? AND ss.id = ?
           ORDER BY se.id DESC LIMIT 1""",
        (contact_id, step_id),
    ).fetchone()
    enrolled_at = enrolment["enrolled_at"] if enrolment else None
    zone = clock.get_timezone()
    return any(_written_since(r["timestamp"], enrolled_at, zone) for r in rows)


# Email pattern (local@domain.tld) the scraper finds addresses in page text
# with. Not the rule for what an address is: it stops at an apostrophe.
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# One address: an RFC 5322 dot-atom before the "@" (d'amico@, o'brien@) and
# the scraper's domain after it. ASCII only, since a local part in another
# script needs SMTPUTF8, which nothing confirms the recipient's server offers.
_ATEXT = r"[a-zA-Z0-9!#$%&'*+/=?^_`{|}~\-]"
_ADDRESS_RE = re.compile(
    _ATEXT + r"+(?:\." + _ATEXT + r"+)*@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# One address as a mail server or an earlier version may have left it: no
# RFC 5322 special but the dot before the "@", and any script on either side.
_HELD_ADDRESS_RE = re.compile(r"[^\s()<>\[\]:;@\\,\"]+@[\w.\-]+\.[\w\-]{2,}")


def is_address(text: str) -> bool:
    """True when ``text`` is exactly one email address.

    The one rule for what may be stored and emailed. d'amico@brand.it was
    emailed and stored, then refused by erasure and export, and Direct Send
    emailed everyone in "anna@brand.es, bruno@brand.de".
    """
    return bool(_ADDRESS_RE.fullmatch(text or ""))


def is_held_address(text: str) -> bool:
    """True when ``text`` is one address, in any form the database can hold.

    What erasure, export and the restore check act on, so wider than
    is_address: the inbox records an opt-out or a request whatever its sender
    looks like, since refusing one fails open, and a row stored before that
    rule stays. A sender with a local part in another script who asked to be
    erased got a crash, and an access answer saying nothing was held.

    Still exactly one: erasure and export match free text against the address,
    and a blank or partial one matches everything. An empty erasure rewrote
    every activity and SMTP record, and an empty export disclosed them.
    """
    return bool(_HELD_ADDRESS_RE.fullmatch(text or ""))


# The secret behind every erasure token (ERASURE_KEY), set once at start-up
# like the app timezone. None until then, and erasure_token refuses to work
# without it: the alternative is the unkeyed hash this replaced, which anyone
# holding the database could match by hashing guessed addresses.
_erasure_key: bytes | None = None

# The source channel a contact is stored with when its caller names none.
# None in production, so a call site that forgets its channel is refused;
# the test suite sets one, as it sets the erasure key.
_default_source_channel: str | None = None


def set_default_source_channel(channel: str | None) -> None:
    global _default_source_channel
    _default_source_channel = channel


def default_source_channel() -> str | None:
    return _default_source_channel


_COUNTRY_CODE = re.compile(r"[A-Z]{2}")


def _company_country(
    country: str | None, source: str | None, *, allow_unknown: bool
) -> tuple[str | None, str | None]:
    """A company country and its source, checked before they are stored.

    A country with no source, or a source not in COUNTRY_SOURCES, is a
    caller's mistake and raises, as basis_for does for a channel. A source
    with no country is an answer of "unknown" (an AI that could not tell),
    which only set_company_country records. A value that is not a two-letter
    code is stored as no country: every language rule reads the column."""
    if country is None and source is None:
        return None, None
    if source not in COUNTRY_SOURCES:
        raise ValueError(f"unknown company country source: {source!r}")
    if country is None and not allow_unknown:
        raise ValueError("a company country source needs a country")
    code = (country or "").strip().upper()
    if not _COUNTRY_CODE.fullmatch(code):
        return (None, source) if allow_unknown else (None, None)
    return code, source


# agent_state key holding a fingerprint of the key the database was erased
# with. A different key could no longer recognise anyone erased under the old
# one, so they could be collected and emailed again: Database refuses it.
ERASURE_KEY_FINGERPRINT = "erasure_key_fingerprint"

# A new database refuses a shorter key. Once a database records its key, the
# key can never change, so this is the one moment a weak one can be stopped:
# anyone holding a copy of the database could otherwise guess it, and then
# test guessed addresses against every erasure token.
ERASURE_KEY_MIN_LENGTH = 32


def set_erasure_key(key: str) -> None:
    """Set the erasure key for this process (called once at start-up)."""
    key = (key or "").strip()
    if not key:
        raise ValueError(
            "ERASURE_KEY is blank - set it in .env (see docs/DEPLOY.md). It keys "
            "every erasure token, so it must be backed up and never changed."
        )
    global _erasure_key
    _erasure_key = key.encode("utf-8")


def _keyed(text: str) -> str:
    if _erasure_key is None:
        raise RuntimeError(
            "No erasure key: call set_erasure_key(settings.erasure_key) at start-up"
        )
    return hmac.new(_erasure_key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def erasure_token(email: str) -> str:
    """The pseudonym an erased address is replaced with, in every table.

    Stable per address, so the audit trail stays readable and a re-scraped
    address is recognised. Keyed (HMAC-SHA256 with ERASURE_KEY): with the key
    it can only confirm whether a given address was erased; without it, it
    cannot even do that. Still a pseudonym, not anonymisation.
    """
    digest = _keyed((email or "").strip().lower())[:16]
    return f"erased_{digest}@redacted.invalid"


def is_erasure_token(text: str) -> bool:
    """True when ``text`` is an erasure token, not an address to act on.

    No mailbox can exist at redacted.invalid (.invalid is reserved), so this
    is no one's address. Erasing a token again keyed the person's records
    under a token of their token and deleted their own entry on the
    suppression list, so they could be collected and emailed again.
    """
    return (text or "").strip().lower().endswith("@redacted.invalid")


def is_wellformed_token(text) -> bool:
    """True when ``text`` is a token as erasure_token makes one. is_erasure_token
    says only that it is no address; a request can come from any address that
    looks like one, and a token read from a file chooses a folder to delete."""
    return isinstance(text, str) and bool(
        re.fullmatch(r"erased_[0-9a-f]{16}@redacted\.invalid", text)
    )


def token_of(address: str) -> str:
    """The token an address is known by once it is erased, or the token
    itself if that is what is given: the one name a person has both before
    and after an erasure."""
    addr = (address or "").strip().lower()
    return addr if is_erasure_token(addr) else erasure_token(addr)


# How long after a send a bounce report that names no message of ours is taken
# to be about it (Database.bounce_matches_a_send).
BOUNCE_MATCH_DAYS = 30


def _bare_message_id(value) -> str:
    """A Message-ID without its angle brackets and spaces, as two copies of
    one are compared."""
    return (value or "").strip().strip("<>").strip() if isinstance(value, str) else ""


def _journal_who(addr: str) -> dict:
    """Whose a journal line is: an erased person's only as the token, the
    field an erasure's rewrite of earlier lines also leaves."""
    return {"token": addr} if is_erasure_token(addr) else {"email": addr}


def email_domain(email: str) -> str:
    """The domain of an address, lower-cased; "" for a non-address."""
    email = (email or "").strip().lower()
    return email.rsplit("@", 1)[1] if "@" in email else ""


def domain_token(domain: str) -> str:
    """The keyed token a company block is kept as after an erasure. Its own
    prefix, so it can never equal an address token."""
    return "domain_" + _keyed("domain:" + (domain or "").strip().lower())[:16]


def message_token(reference: str) -> str:
    """The keyed token the request register keeps for the message a request
    came in. Not its Message-ID, which can name the sender's mail server
    (<...@mail.brand.it>): recognising the message is all it is for."""
    return "message_" + _keyed("message:" + (reference or "").strip())[:16]


def erasure_key_fingerprint(key: str | None = None) -> str:
    """A value that identifies the key without revealing it: this process's
    key, or the one given (doctor, which runs without setting one)."""
    if key is None:
        return _keyed("erasure key fingerprint")[:16]
    return hmac.new(
        key.strip().encode("utf-8"), b"erasure key fingerprint", hashlib.sha256
    ).hexdigest()[:16]


def _register_functions(conn: sqlite3.Connection) -> None:
    """Make erasure_token() callable from SQL, so set-based queries can match
    an erased address the same way the Python checks do."""
    conn.create_function("erasure_token", 1, erasure_token, deterministic=True)
    conn.create_function("email_domain", 1, email_domain, deterministic=True)
    conn.create_function("domain_token", 1, domain_token, deterministic=True)


# Contacts who must receive no outreach of any kind: not a first touch, a
# follow-up, a retry or a dashboard re-queue. One definition, because the
# hand-copied tuples drifted - the follow-up loop knew three of these six and
# the retry loop none.
CLOSED_CONTACT_STATUSES = frozenset(
    {"responded", "active_client", "unsubscribed", "erased", "bounced", "blocked"}
)
# Filter verdicts. Automated follow-ups and retries respect them; the
# operator's dashboard "send" re-queues them, and the filters then run again.
DISCARDED_CONTACT_STATUSES = frozenset(
    {"irrelevant", "ai_irrelevant", "low_score", "unverified"}
)
# The discard_reason a dashboard Send writes with 'pending'. The second form
# marks a contact re-queued from a filter verdict that no pass has re-checked
# yet, so a later Send can still say the filters run again.
REQUEUED_FROM_DASHBOARD = "Re-queued from the dashboard"
REQUEUED_FROM_A_VERDICT = f"{REQUEUED_FROM_DASHBOARD} after a filter verdict"


def undoes_a_failed_write(func):
    """For a function that writes on the long-lived connection of the Database
    it is given first, or of its object's ``db``.

    That connection's transaction stays open until someone commits. A write
    that failed part-way left it open: it held the write lock, so every other
    connection waited and gave up, and the next commit on it, for anything,
    saved the half that had been written as if it were whole. Whatever stops
    the function, a Ctrl+C included, what it did not commit is undone before
    the error goes on to the caller. An undo that fails is logged and does
    not take that error's place: the caller acts on what stopped the write.
    """

    @functools.wraps(func)
    def wrapper(first, *args, **kwargs):
        try:
            return func(first, *args, **kwargs)
        except BaseException:
            try:
                getattr(first, "db", first).rollback_thread_conn()
            except sqlite3.Error as undo_err:
                logger.error("Could not undo the failed write (%s)", undo_err)
            raise

    return wrapper


class Database:
    """
    SQLite database manager for the outreach agent.
    All public methods are atomic transactions.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        # Opt-outs, blocks and erasures go to the journal beside the database
        # (modules.journal), which replay turns off while it re-applies them.
        self.journaling = True
        # A data/ folder that already exists keeps its mode (a Docker bind
        # mount is made 0755), so the database file is made owner-only itself.
        if db_path not in ("", ":memory:"):
            make_private_file(db_path)
        self._init_db()
        self._check_erasure_key()

    def _check_erasure_key(self) -> None:
        """Refuse to open a database erased under a different key.

        The first process to open it with a key records the key's
        fingerprint, and refuses a key shorter than ERASURE_KEY_MIN_LENGTH; a
        database that already recorded a shorter one still opens (doctor
        fails on it). A process without a key skips the check, and cannot
        make or match an erasure token either.
        """
        if _erasure_key is None:
            return
        fingerprint = erasure_key_fingerprint()
        stored = self.get_state(ERASURE_KEY_FINGERPRINT)
        if stored is None:
            # No erasure can be made without a key, so any erasure already
            # here predates it: an unkeyed token no key will ever match, or
            # the real address kept on the suppression list. Recording the
            # fingerprint now would leave both behind without a word.
            with self._transaction() as conn:
                (old,) = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM contacts"
                    "        WHERE email LIKE 'erased\\_%' ESCAPE '\\')"
                    " + (SELECT COUNT(*) FROM suppression_list"
                    "    WHERE reason = 'gdpr_erasure')"
                ).fetchone()
            if old:
                raise RuntimeError(
                    f"{self.db_path} holds {old} erasure(s) made before "
                    "ERASURE_KEY existed. Migrate them to keyed tokens before "
                    "opening it with a key, or they will no longer be honoured."
                )
            if len(_erasure_key.decode("utf-8")) < ERASURE_KEY_MIN_LENGTH:
                raise RuntimeError(
                    f"ERASURE_KEY is shorter than {ERASURE_KEY_MIN_LENGTH} "
                    f"characters, and {self.db_path} would keep it for good. Set "
                    "a long random key in .env first, e.g. the output of: "
                    'python -c "import secrets; print(secrets.token_urlsafe(48))"'
                )
            self.set_state(ERASURE_KEY_FINGERPRINT, fingerprint)
        elif stored != fingerprint:
            raise RuntimeError(
                f"ERASURE_KEY is not the key {self.db_path} was erased with. "
                "Restore the original key from its backup: with another key, "
                "people erased earlier would no longer be recognised."
            )

    @property
    def conn(self) -> sqlite3.Connection:
        """Get or create a thread-local connection."""
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
            _register_functions(self._local.conn)
            self._local.conn.execute("PRAGMA journal_mode=WAL;")
            self._local.conn.execute("PRAGMA foreign_keys=ON;")
            # Zero freed content: an erased or expired value must not linger
            # in the file's free space (see truncate_wal for the log file).
            self._local.conn.execute("PRAGMA secure_delete=ON;")
            # Wait up to 5s for a lock instead of failing immediately when the
            # agent thread and a dashboard/manual-send thread write at once.
            self._local.conn.execute("PRAGMA busy_timeout=5000;")
        return self._local.conn

    def truncate_wal(self) -> bool:
        """Copy the write-ahead log into the database and empty it.

        Old page images stay in the log file until their space is reused, so
        a value an erasure or the retention job just overwrote could still be
        read from it. Returns True when the log was emptied. A connection that
        holds the log open makes it wait and give up; that is recorded as
        wal_truncate_pending, for doctor, and the next call finishes the job.
        """
        with self._transaction() as conn:
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        done = busy == 0
        if not done:
            logger.warning("The write-ahead log is in use; emptying it is pending")
        self.set_state("wal_truncate_pending", "0" if done else "1")
        return done

    def close_thread_conn(self) -> None:
        """Close and drop this thread's connection (e.g. on request teardown so
        short-lived web-request threads don't leave SQLite handles open)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as e:
                # Teardown - the handle is dropped below either way, but a close
                # that fails (typically a still-open cursor) is worth a trace.
                logger.debug("Closing thread connection failed: %s", e)
            del self._local.conn

    def rollback_thread_conn(self) -> None:
        """Undo what this thread's connection wrote and did not commit (see
        undoes_a_failed_write). Nothing to undo is not an error."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.rollback()

    def _get_conn(self) -> sqlite3.Connection:
        """Create a new independent connection (legacy)."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        _register_functions(conn)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA secure_delete=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    @contextmanager
    def _transaction(self):
        """Context manager for atomic database transactions using a new connection."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Create all tables on first run, and apply schema migrations for V2."""
        from modules.carbon_passport import SCHEMA_SQL as LOTS_SCHEMA_SQL
        from modules.quoting import SCHEMA_SQL as QUOTES_SCHEMA_SQL
        from modules.review_queue import SCHEMA_SQL as REVIEW_SCHEMA_SQL

        with self._transaction() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.executescript(REVIEW_SCHEMA_SQL)
            conn.executescript(QUOTES_SCHEMA_SQL)
            conn.executescript(LOTS_SCHEMA_SQL)

            # V2 Schema Migrations - add columns if they don't exist
            # Wrap each in a try/except because ALTER TABLE ADD COLUMN
            # throws OperationalError if the column already exists.
            migrations = [
                "ALTER TABLE contacts ADD COLUMN lead_score INTEGER DEFAULT 0;",
                "ALTER TABLE contacts ADD COLUMN lead_tier TEXT DEFAULT 'D';",
                "ALTER TABLE contacts ADD COLUMN is_verified INTEGER DEFAULT 0;",
                "ALTER TABLE contacts ADD COLUMN verification_details TEXT;",
                "ALTER TABLE email_log ADD COLUMN tracking_id TEXT;",
                "ALTER TABLE email_log ADD COLUMN opened_at TEXT;",
                "ALTER TABLE email_log ADD COLUMN retry_count INTEGER DEFAULT 0;",
                "ALTER TABLE email_log ADD COLUMN sequence_step_id INTEGER;",
                "ALTER TABLE email_log ADD COLUMN ab_variant TEXT;",
                "ALTER TABLE email_log ADD COLUMN sender_account TEXT;",
                "ALTER TABLE email_log ADD COLUMN message_id TEXT;",
                # The version of the notice each email carried.
                "ALTER TABLE email_log ADD COLUMN notice_version TEXT;",
                "ALTER TABLE contacts ADD COLUMN qualified_at TEXT;",
                # When they last replied: the engaged retention period runs
                # from the last exchange.
                "ALTER TABLE contacts ADD COLUMN last_reply_at TEXT;",
                # Where the address came from, the basis that gives, and the
                # rules version that assigned it - written when it is stored.
                "ALTER TABLE contacts ADD COLUMN source_channel TEXT;",
                "ALTER TABLE contacts ADD COLUMN basis TEXT;",
                "ALTER TABLE contacts ADD COLUMN rule_version TEXT;",
                # The country the company is in, where that was read, and the
                # operator's language for the contact: an Italian company
                # writing from a .com address gets the Italian email.
                "ALTER TABLE contacts ADD COLUMN company_country TEXT;",
                "ALTER TABLE contacts ADD COLUMN company_country_source TEXT;",
                "ALTER TABLE contacts ADD COLUMN language_choice TEXT;",
                # The basis each live send relied on, and through which channel.
                "ALTER TABLE consent_log ADD COLUMN contact_id INTEGER;",
                "ALTER TABLE consent_log ADD COLUMN channel TEXT;",
                "ALTER TABLE consent_log ADD COLUMN rule_version TEXT;",
                # Why a follow-up sequence was cancelled. Every path that
                # ends one knows, and wrote it only to the log file.
                "ALTER TABLE sequence_enrollments ADD COLUMN cancel_reason TEXT;",
                # The message a request came in, as its token: every inbox
                # scan reads the last day of mail again.
                "ALTER TABLE dsr_requests ADD COLUMN source_ref TEXT;",
            ]
            for mig in migrations:
                try:
                    conn.execute(mig)
                except sqlite3.OperationalError as e:
                    # Only "duplicate column name" means the migration already
                    # applied. The same handler used to swallow "no such table"
                    # and plain SQL typos, so a migration that NEVER applied
                    # looked exactly like one that had - and the first read of
                    # the missing column failed somewhere far away instead.
                    if "duplicate column name" not in str(e).lower():
                        logger.error("Schema migration did not apply: %s (%s)", mig, e)

            # Add the new indexes after columns are guaranteed to exist
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contacts_score ON contacts(lead_score);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_email_log_tracking ON email_log(tracking_id);"
            )
            # A send is known by its Message-ID: a replay of the journal asks
            # once per send it holds (restore_send).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_email_log_message_id "
                "ON email_log(message_id);"
            )
            # One request per message, decided by the database as add_contact
            # is: the agent's scan and the dashboard's replay of a held copy
            # can register the same message at the same moment.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_dsr_requests_source_ref "
                "ON dsr_requests(source_ref);"
            )
            # Addresses are matched case-insensitively with LOWER(column) = ?,
            # which an index on the plain column cannot serve: every such
            # lookup read its whole table. These expression indexes match the
            # queries exactly, so no query had to change.
            for name, table, expr in (
                ("idx_contacts_email_lower", "contacts", "LOWER(email)"),
                (
                    "idx_email_log_recipient_lower",
                    "email_log",
                    "LOWER(recipient_email)",
                ),
                ("idx_consent_email_lower", "consent_log", "LOWER(email)"),
                ("idx_lead_alerts_email_lower", "lead_alerts", "LOWER(email)"),
            ):
                conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({expr});")

            # One-time data migration: suppression is matched case-insensitively
            # now, so fold any legacy mixed-case rows down to lowercase. Done
            # oldest-first with INSERT OR IGNORE semantics via the UNIQUE index:
            # a collision means the lowercase form is already suppressed.
            try:
                mixed = conn.execute(
                    "SELECT id, email FROM suppression_list WHERE email <> LOWER(email)"
                ).fetchall()
                for row in mixed:
                    lowered = row["email"].strip().lower()
                    dup = conn.execute(
                        "SELECT 1 FROM suppression_list WHERE email = ?", (lowered,)
                    ).fetchone()
                    if dup:
                        conn.execute(
                            "DELETE FROM suppression_list WHERE id = ?", (row["id"],)
                        )
                    else:
                        conn.execute(
                            "UPDATE suppression_list SET email = ? WHERE id = ?",
                            (lowered, row["id"]),
                        )
                if mixed:
                    logger.info(
                        "Normalised %d suppression entries to lowercase", len(mixed)
                    )
            except sqlite3.Error as e:
                logger.warning("Suppression-case migration skipped: %s", e)

            # Same for contacts: legacy rows keep the source page's casing, and
            # an exact-match lookup misses them (dropped hot-lead alerts, broken
            # Direct Send). contacts.email has no UNIQUE constraint, so a
            # collision means a genuine duplicate - keep the oldest row.
            try:
                dupes = conn.execute(
                    "SELECT id, email FROM contacts "
                    "WHERE email <> TRIM(LOWER(email)) ORDER BY id"
                ).fetchall()
                merged = skipped = 0
                for row in dupes:
                    lowered = row["email"].strip().lower()
                    keeper = conn.execute(
                        "SELECT id FROM contacts WHERE LOWER(TRIM(email)) = ? "
                        "AND id <> ? ORDER BY id LIMIT 1",
                        (lowered, row["id"]),
                    ).fetchone()
                    if keeper:
                        # Handled by the merge pass below, which repoints the
                        # child rows before deleting so the FKs stay satisfied.
                        skipped += 1
                        continue
                    conn.execute(
                        "UPDATE contacts SET email = ? WHERE id = ?",
                        (lowered, row["id"]),
                    )
                    merged += 1
                if merged or skipped:
                    logger.info(
                        "Normalised %d contact addresses to lowercase "
                        "(%d are duplicates, merged below)",
                        merged,
                        skipped,
                    )
            except sqlite3.Error as e:
                logger.warning("Contact-case migration skipped: %s", e)

            # Merge duplicate contacts, then enforce uniqueness on the address.
            #
            # Without a UNIQUE constraint, add_contact's dedup was a SELECT
            # followed by an INSERT in autocommit - two threads could both pass
            # and both insert, so the same prospect was emailed twice. The
            # constraint is what makes INSERT OR IGNORE authoritative, but it
            # cannot be created while duplicates exist.
            #
            # Duplicates are referenced by email_log, retry_queue and
            # sequence_enrollments (FKs are ON), so the children are repointed
            # onto the oldest row before the extra rows are deleted. Send history
            # is preserved and re-attributed rather than orphaned.
            try:
                groups = conn.execute(
                    "SELECT TRIM(LOWER(email)) AS norm, MIN(id) AS keeper "
                    "FROM contacts GROUP BY norm HAVING COUNT(*) > 1"
                ).fetchall()
                merged_rows = 0
                for g in groups:
                    dupe_ids = [
                        r["id"]
                        for r in conn.execute(
                            "SELECT id FROM contacts "
                            "WHERE TRIM(LOWER(email)) = ? AND id <> ?",
                            (g["norm"], g["keeper"]),
                        ).fetchall()
                    ]
                    if not dupe_ids:
                        continue
                    params = [(g["keeper"], d) for d in dupe_ids]
                    # Table names are literals, not input - no injection surface.
                    for child in ("email_log", "retry_queue", "sequence_enrollments"):
                        conn.executemany(
                            f"UPDATE {child} SET contact_id = ? WHERE contact_id = ?",
                            params,
                        )
                    conn.executemany(
                        "DELETE FROM contacts WHERE id = ?", [(d,) for d in dupe_ids]
                    )
                    merged_rows += len(dupe_ids)

                # Normalise every remaining address so the index is stable.
                conn.execute(
                    "UPDATE contacts SET email = TRIM(LOWER(email)) "
                    "WHERE email <> TRIM(LOWER(email))"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_contacts_email_unique ON contacts(email)"
                )
                if merged_rows:
                    logger.info(
                        "Merged %d duplicate contact row(s) onto their keepers "
                        "and enforced UNIQUE(contacts.email)",
                        merged_rows,
                    )
            except sqlite3.Error as e:
                # Leaving the index absent is safe - add_contact's INSERT OR
                # IGNORE simply stops being authoritative - but it must be loud.
                logger.error("Contact dedup/UNIQUE migration failed: %s", e)

            # Same defect, same fix, one table over. enroll_contact did a
            # SELECT-then-INSERT across two connections with nothing in the
            # schema to stop it, so the agent thread and a dashboard Direct
            # Send could both see "not enrolled" and both insert. Two ACTIVE
            # enrollments means get_due_followups returns the contact twice at
            # every step - they receive the entire follow-up sequence twice,
            # permanently, because the rows never merge back.
            #
            # A PARTIAL index (WHERE status='active') is the right shape here:
            # a contact may legitimately be enrolled again after an earlier
            # enrollment is completed or cancelled.
            try:
                dupes = conn.execute(
                    """SELECT contact_id, sequence_id, MIN(id) AS keeper,
                              COUNT(*) AS n
                       FROM sequence_enrollments WHERE status = 'active'
                       GROUP BY contact_id, sequence_id HAVING n > 1"""
                ).fetchall()
                for d in dupes:
                    # Keep the earliest; the later ones were never meant to
                    # exist. Cancel rather than delete so the history stays.
                    conn.execute(
                        """UPDATE sequence_enrollments
                           SET status = 'cancelled', completed_at = ?,
                               cancel_reason = 'duplicate enrollment'
                           WHERE contact_id = ? AND sequence_id = ?
                           AND status = 'active' AND id <> ?""",
                        (
                            clock.now_iso(),
                            d["contact_id"],
                            d["sequence_id"],
                            d["keeper"],
                        ),
                    )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_enrollment_active_unique "
                    "ON sequence_enrollments(contact_id, sequence_id) "
                    "WHERE status = 'active'"
                )
                if dupes:
                    logger.warning(
                        "Collapsed %d duplicate active enrollment group(s) - those "
                        "contacts were receiving every follow-up twice",
                        len(dupes),
                    )
            except sqlite3.Error as e:
                logger.error("Enrollment dedup/UNIQUE migration failed: %s", e)

    # -----------------------------------------------------------------
    # Search Log Operations
    # -----------------------------------------------------------------

    def log_search(
        self,
        query: str,
        source: str = "google",
        status: str = "pending",
        notes: str | None = None,
    ) -> int:
        """Log a new search query. Returns the search_log ID."""
        with self._transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO search_log (search_query, source, status, notes, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                (query, source, status, notes, clock.db_now()),
            )
            return cursor.lastrowid

    def get_pending_searches(self, limit: int = 10) -> list[dict]:
        """Fetch pending search queries that need to be processed."""
        try:
            with self._get_conn() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT id, search_query, source FROM search_log
                       WHERE status = 'pending'
                       ORDER BY timestamp ASC LIMIT ?""",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.error(f"Failed to get pending searches: {e}")
            return []

    def update_search(
        self,
        search_id: int,
        results_count: int = 0,
        urls_scraped: int = 0,
        status: str = "completed",
        notes: str | None = None,
    ):
        """Update a search log entry with results."""
        with self._transaction() as conn:
            conn.execute(
                """UPDATE search_log
                   SET results_count = ?, urls_scraped = ?, status = ?, notes = ?
                   WHERE id = ?""",
                (results_count, urls_scraped, status, notes, search_id),
            )

    # -----------------------------------------------------------------
    # Contact Operations
    # -----------------------------------------------------------------

    def add_contact(
        self,
        search_id: int,
        email: str,
        name: str | None = None,
        role: str | None = None,
        organization: str | None = None,
        source_url: str | None = None,
        qualification_rationale: str | None = None,
        lead_score: int = 0,
        lead_tier: str = "D",
        source_channel: str | None = None,
        company_country: str | None = None,
        company_country_source: str | None = None,
    ) -> int | None:
        """
        Add a discovered contact.
        Returns contact ID, or None if not one address (is_address), a
        duplicate or suppressed.

        source_channel (modules.compliance.SOURCE_CHANNELS) is stored with the
        basis it gives and the rules version; ValueError without one.
        company_country comes with where it was read (COUNTRY_SOURCES), or
        neither does; ValueError for one without the other.
        """
        channel = source_channel or _default_source_channel
        basis = basis_for(channel)
        country, country_source = _company_country(
            company_country, company_country_source, allow_unknown=False
        )
        # Normalise: scraped addresses carry the source page's casing, so both
        # the suppression and duplicate checks must be case-insensitive or the
        # same person gets re-added (and re-emailed) under a different casing.
        email = (email or "").strip().lower()
        if not is_address(email):
            return None
        with self._transaction() as conn:
            # Check suppression list
            suppressed = conn.execute(
                "SELECT 1 FROM suppression_list WHERE email IN (?, ?)",
                (email, erasure_token(email)),
            ).fetchone()
            if suppressed:
                return None

            # An address erased by the retention job is not suppressed - the
            # person never objected - so without this it would be re-scraped
            # and emailed again: retention would reset the clock, not end it.
            # Matched on the erasure token, so no cleartext is kept.
            erased = conn.execute(
                "SELECT 1 FROM contacts WHERE email = ?", (erasure_token(email),)
            ).fetchone()
            if erased:
                return None

            # A company that said no: storing a colleague it could never email
            # is data kept without a purpose (Art. 5(1)(c)).
            domain = email_domain(email)
            if conn.execute(
                "SELECT 1 FROM suppressed_domains WHERE domain IN (?, ?)",
                (domain, domain_token(domain)),
            ).fetchone():
                return None

            # Deduplicate in the INSERT itself rather than with a preceding
            # SELECT. sqlite3 only opens the write transaction at the DML, so a
            # check-then-insert ran unserialised: the agent worker and a Flask
            # Direct Send could both see "no existing contact", both insert, and
            # both email the same person. INSERT OR IGNORE leans on the UNIQUE
            # index (added in _init_db) so the loser is rejected by the database.
            cursor = conn.execute(
                """INSERT OR IGNORE INTO contacts
                   (search_id, email, name, role, organization, source_url,
                    qualification_rationale, lead_score, lead_tier, timestamp,
                    source_channel, basis, rule_version, company_country,
                    company_country_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    search_id,
                    email,
                    name,
                    role,
                    organization,
                    source_url,
                    qualification_rationale,
                    lead_score,
                    lead_tier,
                    clock.db_now(),
                    channel,
                    basis,
                    RULE_VERSION,
                    country,
                    country_source,
                ),
            )
            if cursor.rowcount == 0:
                # The UNIQUE index rejected it: this address is already a
                # contact. Same outcome the old pre-check reported, but decided
                # atomically so a concurrent caller cannot also get an id.
                return None
            contact_id = cursor.lastrowid
            self._take_up_prior_sends(conn, contact_id, email)
            return contact_id

    def _take_up_prior_sends(self, conn, contact_id: int, email: str) -> None:
        """Give a newly stored contact the sends the journal put back for its
        address while the database did not hold it (restore_send).

        Each becomes an email_log row at its own time and the contact starts
        'emailed', in the transaction that stores it: the address was emailed
        before the restore, so no first email goes to it again. It is enrolled
        in nothing.
        """
        token = erasure_token(email)
        earlier = conn.execute(
            "SELECT * FROM prior_sends WHERE token = ? ORDER BY sent_at, id", (token,)
        ).fetchall()
        if not earlier:
            return
        for send in earlier:
            self._insert_email_row(
                conn,
                contact_id,
                email,
                RESTORED_SEND_SUBJECT,
                has_attachment=False,
                delivery_status=send["delivery_status"],
                smtp_response=RESTORED_SEND_NOTE,
                sequence_step_id=send["sequence_step_id"],
                sender_account=send["sender_account"],
                message_id=send["message_id"],
                notice_version=send["notice_version"],
                timestamp=send["sent_at"],
            )
        conn.execute(
            "UPDATE contacts SET status = 'emailed' WHERE id = ?", (contact_id,)
        )
        conn.execute("DELETE FROM prior_sends WHERE token = ?", (token,))

    def was_lead_alerted(self, email: str) -> bool:
        """True if a human was already notified about this interested lead."""
        with self._transaction() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM lead_alerts WHERE email = ?", (email,)
                ).fetchone()
                is not None
            )

    def record_lead_alert(
        self, email: str, notified_to: str = "", reply_excerpt: str = ""
    ) -> None:
        """Remember that we alerted someone about this lead (prevents repeats)."""
        with self._transaction() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO lead_alerts
                   (email, notified_to, reply_excerpt, timestamp)
                   VALUES (?, ?, ?, ?)""",
                (email, notified_to, (reply_excerpt or "")[:500], clock.db_now()),
            )

    def get_lead_alerts(self, limit: int = 50) -> list[dict]:
        """Recent hot-lead alerts, newest first (for the dashboard)."""
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT la.*, c.organization, c.name
                   FROM lead_alerts la
                   LEFT JOIN contacts c ON c.email = la.email
                   ORDER BY la.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_deliverability_metrics(self, window_days: int = 7) -> dict:
        """Reputation snapshot for the circuit breaker and the dashboard.

        The headline ``bounce_rate`` is measured over a RECENT WINDOW, not all
        time. A lifetime rate is useless as a circuit breaker: after thousands
        of healthy sends a sudden batch of dead addresses barely moves it, so
        the breaker would never fire in the very scenario it exists for (and
        conversely, early bad data would pin it high forever). Lifetime figures
        are still returned for display."""
        # Stored timestamps are local (see modules/clock.py), so the window
        # boundary must be local too.
        since = clock.db_days_ago(int(window_days))
        with self._transaction() as conn:

            def n(sql, *a):
                return conn.execute(sql, a).fetchone()[0]

            sent = n(
                f"SELECT COUNT(*) FROM email_log WHERE delivery_status IN {SENT_STATUSES_SQL}"
            )
            failed = n(
                f"SELECT COUNT(*) FROM email_log "
                f"WHERE delivery_status IN {FAILED_STATUSES_SQL}"
            )
            bounced = n(
                f"SELECT COUNT(*) FROM suppression_list WHERE reason IN {BOUNCE_REASONS_SQL}"
            )
            unsub = n(
                "SELECT COUNT(*) FROM suppression_list WHERE reason='unsubscribe'"
            )
            suppressed = n("SELECT COUNT(*) FROM suppression_list")
            recent_sent = n(
                f"SELECT COUNT(*) FROM email_log WHERE delivery_status IN {SENT_STATUSES_SQL} "
                "AND timestamp >= ?",
                since,
            )
            # A bounce only counts if we actually sent to that address. The old
            # query counted every bounce-reason row in the suppression list,
            # which made the breaker trust data it had no business trusting:
            # before the IMAP classifier was fixed it filed ordinary inbound
            # mail - a newsletter, Nespresso marketing, Google notifications,
            # Italian PEC receipts - as bounces from senders the agent had
            # never emailed. Five such rows produced a 5.1% "bounce rate" over
            # 79 sends and tripped the circuit breaker on a campaign that had
            # not actually bounced at all.
            #
            # A rate is a ratio of two things drawn from the same population.
            # The denominator is sends, so the numerator must be too.
            #
            # LOWER(s.email), although suppression rows are stored lower-case:
            # compared with the bare column, SQLite applies its TEXT affinity
            # and will not use the index on LOWER(recipient_email), so every
            # recent bounce walked every sent row.
            recent_bounced = n(
                f"SELECT COUNT(*) FROM suppression_list s "
                f"WHERE s.reason IN {BOUNCE_REASONS_SQL} "
                f"AND s.timestamp >= ? "
                f"AND EXISTS (SELECT 1 FROM email_log e "
                f"            WHERE LOWER(e.recipient_email) = LOWER(s.email) "
                f"            AND e.delivery_status IN {SENT_STATUSES_SQL})",
                since,
            )
        return {
            "sent": sent,
            "failed": failed,
            "bounced": bounced,
            "unsubscribed": unsub,
            "suppressed": suppressed,
            # Windowed - what the circuit breaker judges on.
            "window_days": int(window_days),
            "recent_sent": recent_sent,
            "recent_bounced": recent_bounced,
            "bounce_rate": round(recent_bounced / recent_sent, 4)
            if recent_sent
            else 0.0,
            "lifetime_bounce_rate": round(bounced / sent, 4) if sent else 0.0,
        }

    def get_domain_bounce_stats(
        self, window_days: int = 7, min_sends: int = 5
    ) -> dict[str, dict]:
        """Bounce rate per RECIPIENT domain over a recent window.

        The global breaker in get_deliverability_metrics judges one number over
        every send, which hides the shape of the problem. If one provider is
        rejecting us at 40% while everything else sits at 1%, the blended rate
        stays under the threshold and we keep hammering that provider - until
        it finally drags the global rate over the line, at which point sending
        stops to EVERY domain, healthy ones included. One bad provider both
        escapes attention and then takes the whole campaign down with it.

        Domains below *min_sends* are returned with ``enough_data=False``: on a
        single send a lone bounce reads as 100%, which would blacklist a
        perfectly good domain forever on one dead address.

        Same window semantics as the global metrics: sends come from email_log,
        bounces from the suppression list, and stored timestamps are local
        (see modules/clock.py) so the boundary is local too.
        """
        since = clock.db_days_ago(int(window_days))
        # instr()/substr() rather than a Python split so the grouping happens in
        # one pass in SQLite; addresses are lower-cased on both sides because
        # email_log keeps whatever casing was scraped while suppression_list
        # normalises (see add_to_suppression).
        domain_of = "substr(LOWER({col}), instr(LOWER({col}), '@') + 1)"
        stats: dict[str, dict] = {}
        with self._transaction() as conn:
            for row in conn.execute(
                f"SELECT {domain_of.format(col='recipient_email')} AS domain, "
                f"COUNT(*) AS sent FROM email_log "
                f"WHERE delivery_status IN {SENT_STATUSES_SQL} "
                f"AND instr(recipient_email, '@') > 0 "
                f"AND timestamp >= ? "
                f"GROUP BY domain",
                (since,),
            ):
                stats[row["domain"]] = {"sent": row["sent"], "bounced": 0}

            for row in conn.execute(
                f"SELECT {domain_of.format(col='s.email')} AS domain, "
                f"COUNT(*) AS bounced FROM suppression_list s "
                f"WHERE s.reason IN {BOUNCE_REASONS_SQL} "
                f"AND instr(s.email, '@') > 0 "
                f"AND s.timestamp >= ? "
                # The same cohort join as recent_bounced above, and for the same
                # reason. Without it any suppression row counts against its
                # domain whether or not we ever wrote to that address - so one
                # misfiled inbound message from it-admin@acme.com backs off
                # acme.com entirely, and every real prospect there stops being
                # emailed. Per-domain this is worse than the global rate that
                # bug produced: it needs far fewer bad rows to cross the
                # threshold, because the denominator is one domain's sends
                # rather than the whole campaign's.
                f"AND EXISTS (SELECT 1 FROM email_log e "
                f"            WHERE LOWER(e.recipient_email) = LOWER(s.email) "
                f"            AND e.delivery_status IN {SENT_STATUSES_SQL}) "
                f"GROUP BY domain",
                (since,),
            ):
                # A bounce whose send fell outside the window still counts
                # against the domain; default the send count rather than
                # dropping the row. Such a domain has sent=0, so enough_data is
                # False and it cannot trigger a backoff on its own.
                stats.setdefault(row["domain"], {"sent": 0, "bounced": 0})
                stats[row["domain"]]["bounced"] = row["bounced"]

        for domain, s in stats.items():
            s["domain"] = domain
            s["enough_data"] = s["sent"] >= max(1, int(min_sends))
            s["bounce_rate"] = round(s["bounced"] / s["sent"], 4) if s["sent"] else 0.0
        return stats

    def get_or_create_manual_search(self) -> int:
        """Return the id of the shared 'manual / direct sends' search row,
        creating it once so manually-added contacts have a parent search_id."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id FROM search_log WHERE source = 'manual' ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                return row["id"] if not isinstance(row, tuple) else row[0]
            cursor = conn.execute(
                """INSERT INTO search_log (search_query, source, status, timestamp)
                   VALUES (?, 'manual', 'completed', ?)""",
                ("Manual / direct sends", clock.db_now()),
            )
            return cursor.lastrowid

    def update_contact_status(
        self, contact_id: int, status: str, discard_reason: str | None = None
    ):
        """Update a contact's status (pending -> emailed/discarded/failed)."""
        with self._transaction() as conn:
            conn.execute(
                """UPDATE contacts SET status = ?, discard_reason = ?
                   WHERE id = ?""",
                (status, discard_reason, contact_id),
            )

    def update_contact_score(self, contact_id: int, lead_score: int, lead_tier: str):
        """Update a contact's lead score and tier."""
        with self._transaction() as conn:
            conn.execute(
                """UPDATE contacts SET lead_score = ?, lead_tier = ?
                   WHERE id = ?""",
                (lead_score, lead_tier, contact_id),
            )

    def update_contact_verification(
        self, contact_id: int, is_verified: bool, details: str | None = None
    ):
        """Update a contact's email verification status."""
        with self._transaction() as conn:
            conn.execute(
                """UPDATE contacts SET is_verified = ?, verification_details = ?
                   WHERE id = ?""",
                (1 if is_verified else 0, details, contact_id),
            )

    def mark_contact_qualified(self, contact_id: int):
        """Record that a contact passed qualification.

        Separate from is_verified, which means "MX confirmed" and feeds the
        dashboard's verified count: with MX checks off, a qualified contact
        has not been verified, and saying so would be a false statistic.
        """
        with self._transaction() as conn:
            conn.execute(
                "UPDATE contacts SET qualified_at = ? WHERE id = ?",
                (clock.db_now(), contact_id),
            )

    def get_pending_contacts(self) -> list[dict]:
        """Get all contacts with status='pending' that are not suppressed."""
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT c.* FROM contacts c
                   WHERE c.status = 'pending'
                     AND email_domain(c.email) NOT IN
                         (SELECT domain FROM suppressed_domains)
                     AND domain_token(email_domain(c.email)) NOT IN
                         (SELECT domain FROM suppressed_domains)
                     AND LOWER(c.email) NOT IN (SELECT email FROM suppression_list)
                     AND erasure_token(c.email) NOT IN
                         (SELECT email FROM suppression_list)
                   ORDER BY c.lead_score DESC, c.timestamp ASC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def get_contact_by_id(self, contact_id: int) -> dict | None:
        """Get a single contact by ID."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM contacts WHERE id = ?", (contact_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_contact_by_email(self, email: str) -> dict | None:
        """Get a contact by email address (case-insensitive).

        Contacts discovered before addresses were normalised keep the source
        page's casing. An exact match would miss them - which silently dropped
        hot-lead alerts and broke Direct Send for those contacts."""
        email = (email or "").strip().lower()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM contacts WHERE LOWER(email) = ?", (email,)
            ).fetchone()
            return dict(row) if row else None

    # -----------------------------------------------------------------
    # Email Log Operations
    # -----------------------------------------------------------------

    @staticmethod
    def _insert_email_row(
        conn,
        contact_id: int,
        recipient_email: str,
        subject: str,
        body_preview: str | None = None,
        has_attachment: bool = True,
        attachment_filename: str | None = None,
        delivery_status: str = "pending",
        smtp_response: str | None = None,
        error_message: str | None = None,
        tracking_id: str | None = None,
        sequence_step_id: int | None = None,
        ab_variant: str | None = None,
        sender_account: str | None = None,
        message_id: str | None = None,
        notice_version: str | None = None,
        timestamp: str | None = None,
    ) -> int:
        cursor = conn.execute(
            """INSERT INTO email_log
               (contact_id, recipient_email, subject, body_preview,
                has_attachment, attachment_filename, delivery_status,
                smtp_response, error_message, tracking_id, sequence_step_id,
                ab_variant, sender_account, message_id, timestamp,
                notice_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                contact_id,
                recipient_email,
                subject,
                body_preview[:500] if body_preview else None,
                1 if has_attachment else 0,
                attachment_filename,
                delivery_status,
                smtp_response,
                error_message,
                tracking_id,
                sequence_step_id,
                ab_variant,
                sender_account,
                message_id,
                timestamp or clock.db_now(),
                notice_version,
            ),
        )
        return cursor.lastrowid

    def log_email(self, *args, **columns) -> int:
        """Log an email dispatch attempt. Returns the email_log ID. The columns
        are _insert_email_row's."""
        with self._transaction() as conn:
            return self._insert_email_row(conn, *args, **columns)

    def claim_send(
        self,
        contact_id: int,
        recipient_email: str,
        subject: str,
        *,
        first_email: bool = True,
        sequence_step_id: int | None = None,
        **columns,
    ) -> int | None:
        """Write a live send's 'preparing' row, if this send may go. Returns the
        row's id, or None when another send holds the contact.

        The row is the claim on the send. The send check reads the same rows,
        but two senders that both pass it would both send: the check and the
        write are one transaction here, under the write lock, so of two
        processes that get this far the second sees the first's row. Nothing
        goes beside a send that has not settled; a first email or its retry
        (``first_email``) goes once; a follow-up step goes once per enrolment.
        """
        with self._transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            in_doubt = conn.execute(
                "SELECT 1 FROM email_log WHERE contact_id = ? "
                "AND delivery_status = 'preparing' LIMIT 1",
                (contact_id,),
            ).fetchone()
            if in_doubt:
                return None
            if (
                first_email
                and conn.execute(
                    f"SELECT 1 FROM email_log WHERE contact_id = ? "
                    f"AND delivery_status IN {SENT_STATUSES_SQL} LIMIT 1",
                    (contact_id,),
                ).fetchone()
            ):
                return None
            if sequence_step_id is not None and _step_possibly_sent(
                conn, contact_id, sequence_step_id
            ):
                return None
            return self._insert_email_row(
                conn,
                contact_id,
                recipient_email,
                subject,
                delivery_status="preparing",
                sequence_step_id=sequence_step_id,
                **columns,
            )

    def update_email_status(
        self,
        email_id: int,
        delivery_status: str,
        smtp_response: str | None = None,
        error_message: str | None = None,
    ):
        """Update an email log entry with delivery result."""
        with self._transaction() as conn:
            conn.execute(
                """UPDATE email_log
                   SET delivery_status = ?, smtp_response = ?, error_message = ?
                   WHERE id = ?""",
                (delivery_status, smtp_response, error_message, email_id),
            )

    def record_refused_send(
        self,
        email_id: int,
        delivery_status: str,
        smtp_response: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Write the result of a send that did not go out. A locked database
        keeps it instead (write_kept_send_results), because a row left
        'preparing' would later be settled as an email that may have gone: the
        person never emailed, and followed up all the same. It is kept in this
        process's memory, and held on disk for any other process, as this one
        may sit idle or end before a write gets through. The copy on disk is
        out of an erasure's reach, so it keeps only the status: the server's
        answer names the address."""
        try:
            self.update_email_status(
                email_id,
                delivery_status,
                smtp_response=smtp_response,
                error_message=error_message,
            )
        except sqlite3.Error as e:
            logger.error(
                "Could not write that send %s did not go out (%s) - kept for "
                "the next write",
                email_id,
                e,
            )
            with _kept_send_results_lock:
                _kept_send_results.setdefault(self.db_path, {})[email_id] = (
                    delivery_status,
                    smtp_response,
                    error_message,
                )
            try:
                folder = _refused_sends_dir(self.db_path)
                make_private_dir(folder)
                tmp = folder / f"{email_id}.tmp"
                tmp.write_text(
                    json.dumps({"status": delivery_status}), encoding="utf-8"
                )
                os.replace(tmp, folder / f"{email_id}.json")
            except OSError as e:
                logger.error(
                    "Could not hold that send %s did not go out on disk either "
                    "(%s) - only this process knows it",
                    email_id,
                    e,
                )

    def write_kept_send_results(self) -> None:
        """Write the results record_refused_send kept, this process's and those
        any process held on disk. Contained: a database still locked leaves
        them for the next try. Only a row still 'preparing' is written: one
        another process has settled since has its contact and follow-ups
        already moved on with it, and stays with them."""
        with _kept_send_results_lock:
            kept = dict(_kept_send_results.get(self.db_path, {}))
        for email_id, status in _refusals_held_on_disk(self.db_path).items():
            kept.setdefault(email_id, (status, None, HELD_REFUSAL_NOTE))
        for email_id, (status, response, error) in kept.items():
            try:
                with self._transaction() as conn:
                    conn.execute(
                        "UPDATE email_log SET delivery_status = ?, "
                        "smtp_response = ?, error_message = ? "
                        "WHERE id = ? AND delivery_status = 'preparing'",
                        (status, response, error, email_id),
                    )
            except sqlite3.Error as e:
                logger.error(
                    "Could not write the kept results of refused sends yet: %s", e
                )
                return
            with _kept_send_results_lock:
                _kept_send_results.get(self.db_path, {}).pop(email_id, None)
            try:
                (_refused_sends_dir(self.db_path) / f"{email_id}.json").unlink(
                    missing_ok=True
                )
            except OSError as e:
                logger.error(
                    "Could not remove the held result of send %s (%s) - the "
                    "next write removes it",
                    email_id,
                    e,
                )

    def record_stopped_send(self, message_id: str | None) -> None:
        """Write that a stopped process handed this message to no server."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE email_log SET delivery_status = 'failed', error_message = ? "
                "WHERE message_id = ? AND delivery_status = 'preparing'",
                ("Stopped before anything was sent to the server.", message_id),
            )

    def journal_send(
        self,
        email: str,
        *,
        message_id: str | None = None,
        sent: str | None = None,
        step_id: int | None = None,
        mailbox: str | None = None,
        notice: str | None = None,
        unconfirmed: bool = False,
    ) -> None:
        """Journal an email that went out, or may have, so a restore from an
        older backup does not send it again (restore_send).

        The line says who, which message (its Message-ID, or the time of its
        email_log row when it has none), when it went (``sent``, for a send
        settled later: the line's own time is the settling's), the follow-up
        step if it was one, the mailbox, the notice version and whether the
        server's answer was lost.
        No subject and no text. Never raises: the message is gone whatever
        becomes of this line.
        """
        if not self.journaling:
            return
        line: dict = {"id": str(message_id)} if message_id else {}
        if sent or not message_id:
            line["sent"] = sent
        if step_id is not None:
            line["step"] = step_id
        if mailbox:
            line["mailbox"] = mailbox
        if notice:
            line["notice"] = notice
        if unconfirmed:
            line["unconfirmed"] = True
        try:
            journal.append(self.db_path, "send", **self._sent_to(email), **line)
        except (RuntimeError, TypeError, ValueError) as e:
            # No erasure key in this process, or a value JSON cannot hold.
            logger.error("Could not journal a send - a restore would miss it: %s", e)

    def _sent_to(self, email: str) -> dict:
        """Whose a send's journal line is. Someone erased while the message
        was going out is named by the token: the erasure rewrote the journal
        before this line was in it. A database that cannot say counts as
        erased, as the replay knows a contact by the token too."""
        addr = (email or "").strip().lower()
        if is_erasure_token(addr):
            return {"token": addr}
        token = erasure_token(addr)
        try:
            with self._transaction() as conn:
                erased = conn.execute(
                    "SELECT 1 FROM suppression_list WHERE email = ? "
                    "UNION ALL SELECT 1 FROM contacts WHERE email = ?",
                    (token, token),
                ).fetchone()
        except sqlite3.Error as e:
            logger.error(
                "Could not read whether a recipient was erased (%s) - the "
                "journal names them by the token",
                e,
            )
            erased = True
        return {"token": token} if erased else {"email": addr}

    def restore_send(
        self, line: dict, timestamp: str, primary_mailbox: str | None = None
    ) -> bool:
        """Put back a send a journal line records, unless the database knows
        it already. Returns True if it was put back.

        It restores what stops a repeat and nothing that starts a send: the
        email_log row, at the time the send went (the line's ``sent``, else
        the time the line was written); the contact
        'emailed', unless it is closed; the follow-up step the line names
        moved on, if the contact's enrolment is still at it; the basis the
        send relied on; and, for a send made today, the day's count for its
        domain and for its mailbox. It enrols no one: whether a follow-up was
        still wanted is not in the journal. A send to an address the database
        does not hold is kept under the address's token (prior_sends), for
        add_contact to find.

        ``primary_mailbox`` decides which warm-up counter a mailbox's sends
        count on (the primary's has no name). Not told, a send counts on
        both: over the cap's side, which is the safe one.
        """
        who = (line.get("email") or line.get("token") or "").strip().lower()
        token = token_of(who)
        message_id = line.get("id")
        sent_at = line.get("sent") or timestamp
        status = "sent_unconfirmed" if line.get("unconfirmed") else "sent"
        mailbox = line.get("mailbox") or ""
        with self._transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if message_id:
                known = conn.execute(
                    "SELECT 1 FROM email_log WHERE message_id = ? "
                    "UNION ALL SELECT 1 FROM prior_sends WHERE message_id = ?",
                    (message_id, message_id),
                ).fetchone()
            else:
                # No Message-ID: the send is the one to this person at that
                # time, whether the row holds the address or its token.
                known = conn.execute(
                    "SELECT 1 FROM prior_sends WHERE token = ? AND sent_at = ?",
                    (token, sent_at),
                ).fetchone() or any(
                    token_of(row["recipient_email"]) == token
                    for row in conn.execute(
                        "SELECT recipient_email FROM email_log WHERE timestamp = ?",
                        (sent_at,),
                    )
                )
            if known:
                return False
            contact = self._contact_sent_to(conn, who, token)
            if contact is None:
                conn.execute(
                    "INSERT INTO prior_sends (token, message_id, sent_at, "
                    "delivery_status, sequence_step_id, sender_account, "
                    "notice_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        token,
                        message_id,
                        sent_at,
                        status,
                        line.get("step"),
                        mailbox or None,
                        line.get("notice"),
                    ),
                )
            else:
                self._insert_email_row(
                    conn,
                    contact["id"],
                    contact["email"],
                    RESTORED_SEND_SUBJECT,
                    has_attachment=False,
                    delivery_status=status,
                    smtp_response=RESTORED_SEND_NOTE,
                    sequence_step_id=line.get("step"),
                    sender_account=mailbox or None,
                    message_id=message_id,
                    notice_version=line.get("notice"),
                    timestamp=sent_at,
                )
                self._restore_what_a_send_left(conn, contact, line)
            if sent_at[:10] == clock.today_str():
                self._count_restored_send(conn, who, mailbox, primary_mailbox)
        return True

    @staticmethod
    def _contact_sent_to(conn, who: str, token: str):
        """The contact a send line names: by the address, or by the token for
        someone the database has erased. A token line can also name someone
        the restored backup still holds in clear, so their token is looked
        for among the addresses."""
        columns = "id, email, status, basis, source_url, source_channel, rule_version"
        contact = conn.execute(
            f"SELECT {columns} FROM contacts WHERE LOWER(email) IN (?, ?)",
            (who, token),
        ).fetchone()
        if contact is None and who == token:
            contact = conn.execute(
                f"SELECT {columns} FROM contacts WHERE erasure_token(email) = ?",
                (token,),
            ).fetchone()
        return contact

    def _restore_what_a_send_left(self, conn, contact, line: dict) -> None:
        """What the send left behind when it went out, for a contact the
        database holds: its status, its follow-up step, its basis. As
        _settle_interrupted_send leaves them, but for the enrolment."""
        cid = contact["id"]
        if contact["status"] not in CLOSED_CONTACT_STATUSES:
            conn.execute(
                "UPDATE contacts SET status = 'emailed', discard_reason = NULL "
                "WHERE id = ?",
                (cid,),
            )
        if line.get("step") is not None:
            try:
                stamp = datetime.fromisoformat(line["at"]).isoformat()
            except (KeyError, TypeError, ValueError):
                stamp = clock.now_iso()
            # Only an enrolment still waiting at the step that was sent: one
            # the backup already moved on, or one that ended, stays as it is.
            at_that_step = conn.execute(
                """SELECT se.id, se.current_step,
                          EXISTS (SELECT 1 FROM sequence_steps nx
                                   WHERE nx.sequence_id = se.sequence_id
                                     AND nx.step_number = se.current_step + 1)
                              AS more
                   FROM sequence_enrollments se
                   WHERE se.status = 'active' AND se.contact_id = ?
                     AND se.current_step = (
                         SELECT sent.step_number FROM sequence_steps sent
                          WHERE sent.id = ? AND sent.sequence_id = se.sequence_id)""",
                (cid, line["step"]),
            ).fetchall()
            for enrolment in at_that_step:
                if enrolment["more"]:
                    conn.execute(
                        "UPDATE sequence_enrollments SET current_step = ?, "
                        "last_sent_at = ? WHERE id = ?",
                        (enrolment["current_step"] + 1, stamp, enrolment["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE sequence_enrollments SET status = 'completed', "
                        "completed_at = ?, last_sent_at = ? WHERE id = ?",
                        (stamp, stamp, enrolment["id"]),
                    )
        if contact["status"] != "erased" and contact["basis"]:
            self._log_consent(
                conn,
                contact["email"],
                contact["basis"],
                contact["source_url"] or "entered by an operator",
                contact_id=cid,
                channel=contact["source_channel"],
                rule_version=contact["rule_version"] or RULE_VERSION,
            )

    @staticmethod
    def _count_restored_send(
        conn, who: str, mailbox: str, primary_mailbox: str | None
    ) -> None:
        """Count a send made today and put back from the journal against
        today's caps, as process_contact counted it: the backup's counts are
        from before it. A line that names only a token has no domain to
        count."""
        day = clock.today_str()
        if not is_erasure_token(who):
            conn.execute(
                "INSERT INTO domain_send_counts (date, domain, count) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(date, domain) DO UPDATE SET count = count + 1",
                (day, email_domain(who)),
            )
        if primary_mailbox is None:
            accounts = {"", mailbox}
        elif mailbox.lower() == primary_mailbox.strip().lower():
            accounts = {""}
        else:
            accounts = {mailbox}
        for account in sorted(accounts):
            conn.execute(
                "INSERT INTO daily_send_counts (date, count, allowance) "
                "VALUES (?, 1, 0) "
                "ON CONFLICT(date) DO UPDATE SET count = count + 1",
                (f"{account}::{day}" if account else day,),
            )

    def update_email_tracking(self, email_id: int, tracking_id: str):
        """Set the tracking ID for an email."""
        with self._transaction() as conn:
            conn.execute(
                "UPDATE email_log SET tracking_id = ? WHERE id = ?",
                (tracking_id, email_id),
            )

    def mark_email_opened(self, tracking_id: str):
        """Mark an email as opened via tracking pixel."""
        with self._transaction() as conn:
            conn.execute(
                # clock.db_now(), not datetime('now'): the latter is UTC while
                # every other timestamp in this table is written in the app
                # timezone. Mixing them made an email appear OPENED TWO HOURS
                # BEFORE IT WAS SENT, and left any opened_at-vs-timestamp
                # comparison or DATE(opened_at) grouping silently wrong.
                """UPDATE email_log SET opened_at = ?
                   WHERE tracking_id = ? AND opened_at IS NULL""",
                (clock.db_now(), tracking_id),
            )

    # -----------------------------------------------------------------
    # Suppression List Operations
    # -----------------------------------------------------------------

    def block_address(self, email: str) -> bool:
        """An operator's dashboard block.

        On an address already suppressed as a bounce, the row becomes
        'bounce_blocked', so the block is not released with the bounce after
        24 months. Any other reason already there - an objection, an erasure
        - is kept: it is the stronger one. Journalled, so a restore from an
        older backup keeps it. Returns True if anything changed.
        """
        addr = (email or "").strip().lower()
        with self._transaction() as conn:
            changed = conn.execute(
                "INSERT OR IGNORE INTO suppression_list (email, reason, timestamp) "
                "VALUES (?, 'blocked_via_dashboard', ?)",
                (addr, clock.db_now()),
            ).rowcount
            changed += conn.execute(
                "UPDATE suppression_list SET reason = 'bounce_blocked' "
                f"WHERE email = ? AND reason IN {RELEASABLE_BOUNCE_REASONS_SQL}",
                (addr,),
            ).rowcount
        if changed and self.journaling:
            journal.append(self.db_path, "address_block", **_journal_who(addr))
        return changed > 0

    def add_to_suppression(self, email: str, reason: str = "manual"):
        """Add an email to the suppression list.

        Addresses are normalised to lowercase: scraped addresses keep whatever
        casing the source page used, so a case-sensitive list would let
        'Info@Brand.de' be re-discovered as 'info@brand.de' and emailed again -
        i.e. a silent right-to-object (GDPR Art. 21) violation. Journalled
        (a bounce, a refusal by the server), so a restore keeps it."""
        addr = (email or "").strip().lower()
        with self._transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO suppression_list (email, reason, timestamp) "
                    "VALUES (?, ?, ?)",
                    (addr, reason, clock.db_now()),
                )
            except sqlite3.IntegrityError:
                return  # Already suppressed
        if self.journaling:
            journal.append(
                self.db_path, "suppress", **_journal_who(addr), reason=reason
            )

    def restore_suppression(self, email: str, reason: str, timestamp: str) -> bool:
        """A suppression a journal line records, at the time it was written:
        that time decides when retention releases a bounce, and whether it
        counts in a domain's bounce backoff. A bounce turns an operator's block
        into 'bounce_blocked', as replace_block does; any other reason already
        there is kept. Returns True if anything changed."""
        addr = (email or "").strip().lower()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT reason FROM suppression_list WHERE email IN (?, ?)",
                (addr, erasure_token(addr)),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO suppression_list (email, reason, timestamp) "
                    "VALUES (?, ?, ?)",
                    (addr, reason, timestamp),
                )
                return True
            if row["reason"] == "blocked_via_dashboard" and reason in BOUNCE_REASONS:
                conn.execute(
                    "UPDATE suppression_list SET reason = 'bounce_blocked', "
                    "timestamp = ? WHERE email = ?",
                    (timestamp, addr),
                )
                return True
            return False

    def block_domain(self, domain: str, reason: str, *, keyed: bool = False) -> bool:
        """Stop all email to a domain. Callers go through
        modules.company_block, which refuses shared mail domains.

        keyed (after an erasure) keeps the domain only as its token, replacing
        any plain block, since a domain such as mariorossi.it names a person.
        Returns True if the domain was not blocked before.
        """
        domain = (domain or "").strip().lower()
        token = domain_token(domain)
        with self._transaction() as conn:
            existed = conn.execute(
                "SELECT 1 FROM suppressed_domains WHERE domain IN (?, ?)",
                (domain, token),
            ).fetchone()
            if keyed:
                conn.execute(
                    "DELETE FROM suppressed_domains WHERE domain = ?", (domain,)
                )
                conn.execute(
                    "INSERT OR IGNORE INTO suppressed_domains "
                    "(domain, reason, timestamp) VALUES (?, ?, ?)",
                    (token, reason, clock.db_now()),
                )
            elif existed is None:
                # Only a new block is written: a plain row beside an existing
                # token would write an erased person's domain back in clear.
                conn.execute(
                    "INSERT INTO suppressed_domains (domain, reason, timestamp) "
                    "VALUES (?, ?, ?)",
                    (domain, reason, clock.db_now()),
                )
        if self.journaling and (keyed or existed is None):
            journal.append(
                self.db_path,
                "block",
                **({"token": token} if keyed else {"domain": domain}),
            )
        return existed is None

    def company_blocked(self, email: str) -> bool:
        """Whether the company domain of this address is blocked."""
        domain = email_domain(email)
        if not domain:
            return False
        with self._transaction() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM suppressed_domains WHERE domain IN (?, ?)",
                    (domain, domain_token(domain)),
                ).fetchone()
                is not None
            )

    def is_blocked(self, email: str) -> bool:
        """Whether no email may go to this address: it, or its company, is out."""
        return self.is_suppressed(email) or self.company_blocked(email)

    def lift_domain_block(self, domain: str) -> str | None:
        """Remove a company block in either form: "plain", "keyed" or None."""
        domain = (domain or "").strip().lower()
        with self._transaction() as conn:
            plain = conn.execute(
                "DELETE FROM suppressed_domains WHERE domain = ?", (domain,)
            ).rowcount
            keyed = conn.execute(
                "DELETE FROM suppressed_domains WHERE domain = ?",
                (domain_token(domain),),
            ).rowcount
        lifted = "plain" if plain else "keyed" if keyed else None
        if lifted and self.journaling:
            journal.append(
                self.db_path,
                "lift",
                **(
                    {"domain": domain}
                    if lifted == "plain"
                    else {"token": domain_token(domain)}
                ),
            )
        return lifted

    def list_domain_blocks(self) -> list[dict]:
        """Every company block; keyed ones show only their token."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT domain, reason, timestamp FROM suppressed_domains "
                "ORDER BY timestamp"
            ).fetchall()
            return [dict(r) for r in rows]

    def register_unsubscribe_token(self, token: str, email: str) -> None:
        """Record the unsubscribe token of a link about to be sent."""
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO unsubscribe_tokens (token, email, created_at) "
                "VALUES (?, ?, ?)",
                (token, (email or "").strip().lower(), clock.db_now()),
            )

    def register_unsubscribe_tokens(self, links) -> int:
        """Record (token, address) pairs in one transaction. Returns how many
        were not on record."""
        now = clock.db_now()
        with self._transaction() as conn:
            return conn.executemany(
                "INSERT OR IGNORE INTO unsubscribe_tokens (token, email, created_at) "
                "VALUES (?, ?, ?)",
                [(token, address, now) for token, address in links],
            ).rowcount

    def contact_addresses(self) -> list[str]:
        """The address of every contact held in clear."""
        with self._transaction() as conn:
            rows = conn.execute("SELECT email FROM contacts").fetchall()
        return [r[0] for r in rows if is_address(r[0]) and not is_erasure_token(r[0])]

    def address_for_unsubscribe_token(self, token: str) -> str | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT email FROM unsubscribe_tokens WHERE token = ?", (token,)
            ).fetchone()
        return row[0] if row else None

    def add_dsr(
        self,
        email: str,
        kind: str,
        channel: str,
        source_ref: str | None = None,
        received_at: str | None = None,
    ) -> int | None:
        """Register a data-subject request; due one calendar month after it
        arrived (Art. 12(3)).

        received_at is when it arrived (ISO 8601), without it now: a request
        read after an outage or typed in later is due a month from its
        arrival, not from when it was got to. Nothing is dated after now.
        source_ref names the message it came in (its Message-ID). Every inbox
        scan reads the last day of mail again, and a message is registered
        once: for one already registered, None is returned. An erased address
        is registered under its token: written in clear, it would undo the
        erasure.
        """
        addr = (email or "").strip().lower()
        received = clock.now()
        if received_at:
            arrived = datetime.fromisoformat(received_at)
            if arrived.tzinfo is None:
                arrived = arrived.replace(tzinfo=clock.get_timezone())
            # In the app's zone before the month is added: in the fixed offset
            # of an ISO string, a request received in winter fell due an hour
            # late in summer time.
            received = min(arrived, received).astimezone(clock.get_timezone())
        with self._transaction() as conn:
            addr = self._registered_as(conn, addr)
            # Deduplicated in the INSERT, on the UNIQUE index, as add_contact
            # is: a SELECT first would not stop another process.
            cursor = conn.execute(
                """INSERT OR IGNORE INTO dsr_requests
                       (received_at, due_at, email, kind, channel, source_ref)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    received.isoformat(),
                    clock.add_months(received, 1).isoformat(),
                    addr,
                    kind,
                    channel,
                    message_token(source_ref) if source_ref else None,
                ),
            )
            request_id = cursor.lastrowid if cursor.rowcount else None
        if request_id and self.journaling:
            # A request typed in by hand is in no inbox to be read again: a
            # restore from an older backup would lose it, deadline and all.
            journal.append(
                self.db_path,
                "dsr",
                **_journal_who(addr),
                request=kind,
                channel=channel,
                received_at=received.isoformat(),
                due_at=clock.add_months(received, 1).isoformat(),
                ref=message_token(source_ref) if source_ref else None,
            )
        return request_id

    @staticmethod
    def _registered_as(conn, addr: str) -> str:
        """The address a request is registered under: an erased person's only
        as the token - written in clear, it would undo the erasure."""
        if conn.execute(
            "SELECT 1 FROM suppression_list WHERE email = ?",
            (erasure_token(addr),),
        ).fetchone():
            return erasure_token(addr)
        return addr

    @staticmethod
    def _dsr_named(conn, line: dict):
        """The request a journal line is about: by the message it came in,
        else by whose it is, what and when it arrived - never by the row id,
        which a restored database numbers differently. Whose means the
        address or its token, as the line and the restored database may be
        on either side of an erasure."""
        if line.get("ref"):
            return conn.execute(
                "SELECT * FROM dsr_requests WHERE source_ref = ?", (line["ref"],)
            ).fetchone()
        who = (line.get("email") or line.get("token") or "").strip().lower()
        return conn.execute(
            "SELECT * FROM dsr_requests WHERE kind = ? AND received_at = ? "
            "AND (email IN (?, ?) OR erasure_token(LOWER(email)) = ?)",
            (line["request"], line["received_at"], who, erasure_token(who), who),
        ).fetchone()

    def restore_dsr(self, line: dict) -> bool:
        """Register a request a journal line records, unless the database
        already holds it. Returns True if it was added."""
        with self._transaction() as conn:
            if self._dsr_named(conn, line):
                return False
            who = (line.get("email") or line.get("token") or "").strip().lower()
            conn.execute(
                """INSERT INTO dsr_requests
                       (received_at, due_at, email, kind, channel, source_ref)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    line["received_at"],
                    line["due_at"],
                    self._registered_as(conn, who),
                    line["request"],
                    line["channel"],
                    line.get("ref"),
                ),
            )
            return True

    def restore_dsr_close(self, line: dict) -> bool:
        """Close a request as a journal line records, if it is held and still
        open. Returns True if it was closed."""
        with self._transaction() as conn:
            row = self._dsr_named(conn, line)
            if row is None:
                return False
            return (
                conn.execute(
                    "UPDATE dsr_requests SET status = 'closed', closed_at = ?, "
                    "outcome = ? WHERE id = ? AND status = 'open'",
                    (line["closed_at"], line.get("outcome"), row["id"]),
                ).rowcount
                > 0
            )

    def get_dsr(self, request_id: int) -> dict | None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM dsr_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_dsrs(self, open_only: bool = False) -> list[dict]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM dsr_requests"
                + (" WHERE status = 'open'" if open_only else "")
                + " ORDER BY due_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def close_dsr(self, request_id: int, outcome: str) -> bool:
        closed_at = clock.now_iso()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM dsr_requests WHERE id = ?", (request_id,)
            ).fetchone()
            closed = (
                conn.execute(
                    "UPDATE dsr_requests SET status = 'closed', closed_at = ?, "
                    "outcome = ? WHERE id = ? AND status = 'open'",
                    (closed_at, outcome, request_id),
                ).rowcount
                > 0
            )
        if closed and self.journaling:
            # Else a restore from an older backup reopens an answered request.
            journal.append(
                self.db_path,
                "dsr_close",
                **_journal_who(row["email"]),
                request=row["kind"],
                received_at=row["received_at"],
                ref=row["source_ref"],
                closed_at=closed_at,
                outcome=outcome,
            )
        return closed

    def suppress_erased(self, email: str) -> bool:
        """Keep an erased address on the suppression list as its token only.

        Any plain entry for it (an earlier opt-out, bounce or block) is
        replaced in the same transaction, so the address is never kept in
        clear. Returns True if the address was not suppressed before.
        """
        addr = (email or "").strip().lower()
        if is_erasure_token(addr):
            # The plain entry it deletes would be the person's own token.
            raise ValueError("an erasure token, not an address: erased already")
        token = erasure_token(addr)
        with self._transaction() as conn:
            had_plain = conn.execute(
                "DELETE FROM suppression_list WHERE email = ?", (addr,)
            ).rowcount
            had_token = conn.execute(
                "UPDATE suppression_list SET reason = 'gdpr_erasure' WHERE email = ?",
                (token,),
            ).rowcount
            if not had_token:
                conn.execute(
                    "INSERT INTO suppression_list (email, reason, timestamp) "
                    "VALUES (?, 'gdpr_erasure', ?)",
                    (token, clock.db_now()),
                )
        if self.journaling:
            # The journal keeps them only as their token from now on. Their
            # earlier lines are rewritten once the erasure is committed
            # (DataExporter.gdpr_erase): this append works beside another
            # program's handle on the file, where replacing the file does not.
            journal.append(self.db_path, "erasure", token=token)
        return not (had_plain or had_token)

    def replace_block(self, email: str, reason: str) -> bool:
        """Turn an operator's dashboard block into *reason* (a bounce).

        A block is the one suppression reason the dashboard's Unblock may
        remove, and the first reason written wins. A hard bounce for an
        address the operator had already blocked would otherwise stay a block:
        uncounted in the bounce rate, and undone by Unblock. The bounce
        handler passes 'bounce_blocked', which retention never releases.
        Returns True if a block was replaced.
        """
        addr = (email or "").strip().lower()
        with self._transaction() as conn:
            replaced = (
                conn.execute(
                    "UPDATE suppression_list SET reason = ?, timestamp = ? "
                    "WHERE email = ? AND reason = 'blocked_via_dashboard'",
                    (reason, clock.db_now(), addr),
                ).rowcount
                > 0
            )
        if replaced and self.journaling:
            journal.append(
                self.db_path, "suppress", **_journal_who(addr), reason=reason
            )
        return replaced

    def record_opt_out(self, email: str, how: str) -> dict:
        """Record that a person opted out, in one transaction: the address is
        suppressed, their contact is marked 'unsubscribed' with *how* as the
        reason, and their active follow-ups end.

        An operator's dashboard block is the one suppression reason Unblock may
        remove. An opt-out from a blocked address used to leave that reason in
        place (the first reason written wins), so Unblock then deleted the row
        and the person became mailable again with no record that they had
        objected (GDPR Art. 21). The block is upgraded to an opt-out here.
        So is a bounce: retention releases bounces after 24 months, and the
        objection must outlive that. An erasure is kept: it is a token.

        Queued retries end too. Only 'pending' ones: an 'in_flight' retry is
        being sent right now, and the send check refuses it on suppression.

        Returns {"new": the opt-out was not already on record, "cancelled":
        follow-up enrolments ended, "retries_cancelled": queued retries ended}.
        """
        addr = (email or "").strip().lower()
        now = clock.now_iso()
        with self._transaction() as conn:
            # Erased already: the token suppresses them, and writing the plain
            # address back (an old unsubscribe link, a late reply) would undo
            # the erasure.
            if conn.execute(
                "SELECT 1 FROM suppression_list WHERE email = ?",
                (erasure_token(addr),),
            ).fetchone():
                return {"new": False, "cancelled": 0, "retries_cancelled": 0}
            row = conn.execute(
                "SELECT reason FROM suppression_list WHERE email = ?", (addr,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO suppression_list (email, reason, timestamp) "
                    "VALUES (?, 'unsubscribe', ?)",
                    (addr, clock.db_now()),
                )
            upgraded = ("blocked_via_dashboard", *BOUNCE_REASONS)
            if row is not None and row["reason"] in upgraded:
                conn.execute(
                    "UPDATE suppression_list SET reason = 'unsubscribe', "
                    "timestamp = ? WHERE email = ?",
                    (clock.db_now(), addr),
                )
            new = row is None or row["reason"] in upgraded
            cancelled = 0
            retries_cancelled = 0
            for (cid,) in conn.execute(
                "SELECT id FROM contacts WHERE LOWER(email) = ?", (addr,)
            ).fetchall():
                conn.execute(
                    "UPDATE contacts SET status = 'unsubscribed', "
                    "discard_reason = ? WHERE id = ?",
                    (how, cid),
                )
                cancelled += conn.execute(
                    "UPDATE sequence_enrollments SET status = 'cancelled', "
                    "completed_at = ?, cancel_reason = ? "
                    "WHERE contact_id = ? AND status = 'active'",
                    (now, how, cid),
                ).rowcount
                retries_cancelled += conn.execute(
                    "UPDATE retry_queue SET status = 'cancelled', last_error = ? "
                    "WHERE contact_id = ? AND status = 'pending'",
                    (f"opted out: {how}", cid),
                ).rowcount
        if new and self.journaling:
            journal.append(self.db_path, "opt_out", email=addr)
        return {
            "new": new,
            "cancelled": cancelled,
            "retries_cancelled": retries_cancelled,
        }

    def is_erased(self, email: str) -> bool:
        """Whether an address was erased on request: suppressed for good by its
        keyed token. Unlike an operator's block or a bounce, nothing lifts it."""
        addr = (email or "").strip().lower()
        with self._transaction() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM suppression_list WHERE email = ?",
                    (erasure_token(addr),),
                ).fetchone()
                is not None
            )

    def address_for_erasure_token(self, token: str) -> str | None:
        """The address an erasure token stands for, while the suppression
        list still holds it in clear. Retention replaces the address with the
        token everywhere else, request rows included, and keeps the objection
        here. None when nothing here matches: only the token is held, so the
        person is erased."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT email FROM suppression_list WHERE erasure_token(email) = ?",
                ((token or "").strip().lower(),),
            ).fetchone()
        return row[0] if row else None

    def is_suppressed(self, email: str) -> bool:
        """Check if an email is on the suppression list (case-insensitive).

        An erased address is on it only as its token, so both forms count.
        """
        addr = (email or "").strip().lower()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM suppression_list WHERE email IN (?, ?)",
                (addr, erasure_token(addr)),
            ).fetchone()
            return row is not None

    def get_suppression_reason(self, email: str) -> str | None:
        """Why an address is suppressed, or None if it is not."""
        addr = (email or "").strip().lower()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT reason FROM suppression_list WHERE email IN (?, ?)",
                (addr, erasure_token(addr)),
            ).fetchone()
            return row["reason"] if row else None

    def remove_from_suppression(
        self, email: str, only_reasons: tuple[str, ...]
    ) -> bool:
        """Un-suppress an address, but ONLY if it was suppressed for one of
        *only_reasons*. Returns True if a row was actually removed.

        ``only_reasons`` is required, not optional, and there is deliberately no
        "remove regardless" path. Most entries on this list are not reversible
        decisions we are entitled to reverse:

          unsubscribe / gdpr_erasure - the person exercised a legal right.
              Re-sending is a right-to-object violation (GDPR Art. 21), which
              is precisely what add_to_suppression's case-folding exists to
              prevent; a careless un-suppress would walk straight around it.
          bounce / smtp_rejected     - the receiving server already told us the
              address is bad. Re-sending guarantees another hard bounce and
              spends sender reputation to learn nothing.

        Only an operator's own dashboard block (``blocked_via_dashboard``) is
        theirs to take back. Journalled with the reason removed, so a restore
        from a backup made before it does not block the address again.
        """
        if not only_reasons:
            raise ValueError("only_reasons must name at least one reason")
        addr = (email or "").strip().lower()
        placeholders = ",".join("?" for _ in only_reasons)
        with self._transaction() as conn:
            row = conn.execute(
                f"SELECT reason FROM suppression_list WHERE email = ? "
                f"AND reason IN ({placeholders})",
                (addr, *only_reasons),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "DELETE FROM suppression_list WHERE email = ? AND reason = ?",
                (addr, row["reason"]),
            )
        if self.journaling:
            journal.append(
                self.db_path,
                "address_unblock",
                **_journal_who(addr),
                reason=row["reason"],
            )
        return True

    # -----------------------------------------------------------------
    # Agent State (durable key/value)
    # -----------------------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Read a persisted agent-state value (None/default if unset)."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT value FROM agent_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    # Runtime config overrides ride on agent_state rather than getting their own
    # table: it is already a durable key/value store with exactly the right
    # semantics, and a new table would mean a migration for no new capability.
    _CFG_PREFIX = "cfg:"

    def get_runtime_overrides(self) -> dict:
        """Operator-set config overrides, decoded. Stored as JSON so an int
        comes back an int rather than the string '5'."""
        out = {}
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT key, value FROM agent_state WHERE key LIKE ?",
                (self._CFG_PREFIX + "%",),
            ).fetchall()
        for row in rows:
            name = row["key"][len(self._CFG_PREFIX) :]
            try:
                out[name] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                # A corrupted or hand-edited row must not take the agent down at
                # startup; skipping it falls back to the .env value.
                logger.warning("Ignoring unreadable config override %r", name)
        return out

    def set_runtime_override(self, key: str, value):
        """Persist one config override. Validation is the caller's job -
        config.validate_override is the only thing that knows the rules."""
        self.set_state(self._CFG_PREFIX + key, json.dumps(value))

    def clear_runtime_override(self, key: str):
        """Drop an override so the .env value applies again."""
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM agent_state WHERE key = ?", (self._CFG_PREFIX + key,)
            )

    def set_state(self, key: str, value: str):
        """Persist an agent-state value, overwriting any previous one."""
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO agent_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at""",
                (key, value, clock.db_now()),
            )

    # -----------------------------------------------------------------
    # Activity Log Operations
    # -----------------------------------------------------------------

    def log_activity(
        self, action: str, details: str | None = None, status: str = "info"
    ):
        """Log a high-level agent activity event."""
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO activity_log (action, details, status, timestamp)
                   VALUES (?, ?, ?, ?)""",
                (action, details, status, clock.db_now()),
            )

    # -----------------------------------------------------------------
    # Consent Log Operations (GDPR)
    # -----------------------------------------------------------------

    def log_consent(
        self,
        email: str,
        legal_basis: str,
        details: str | None = None,
        *,
        contact_id: int | None = None,
        channel: str | None = None,
        rule_version: str | None = None,
    ):
        """Record the basis a live send relied on, once per basis.

        Written after each live send succeeds (process_contact), with the
        contact, the channel and the rules version. A follow-up relies on the
        same basis, so the first record documents it.
        """
        with self._transaction() as conn:
            self._log_consent(
                conn,
                email,
                legal_basis,
                details,
                contact_id=contact_id,
                channel=channel,
                rule_version=rule_version,
            )

    @staticmethod
    def _log_consent(
        conn, email, legal_basis, details, *, contact_id, channel, rule_version
    ) -> None:
        email = (email or "").strip().lower()
        exists = conn.execute(
            "SELECT 1 FROM consent_log WHERE LOWER(email) = ? AND legal_basis = ?",
            (email, legal_basis),
        ).fetchone()
        if exists:
            return
        conn.execute(
            """INSERT INTO consent_log
               (email, legal_basis, details, contact_id, channel, rule_version,
                timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                email,
                legal_basis,
                details,
                contact_id,
                channel,
                rule_version,
                clock.db_now(),
            ),
        )

    # -----------------------------------------------------------------
    # Dashboard Query Methods
    # -----------------------------------------------------------------

    def get_stats(self) -> dict:
        """Get aggregate statistics for the dashboard."""
        with self._transaction() as conn:
            stats = {}

            row = conn.execute("SELECT COUNT(*) as cnt FROM search_log").fetchone()
            stats["total_searches"] = row["cnt"]

            row = conn.execute("SELECT COUNT(*) as cnt FROM contacts").fetchone()
            stats["total_contacts"] = row["cnt"]

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM contacts WHERE status = 'pending'"
            ).fetchone()
            stats["pending_contacts"] = row["cnt"]

            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM email_log "
                f"WHERE delivery_status != '{DRY_RUN_STATUS}'"
            ).fetchone()
            stats["total_emails"] = row["cnt"]

            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM email_log WHERE delivery_status IN {SENT_STATUSES_SQL}"
            ).fetchone()
            stats["emails_sent"] = row["cnt"]

            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM email_log "
                f"WHERE delivery_status IN {FAILED_STATUSES_SQL}"
            ).fetchone()
            stats["emails_failed"] = row["cnt"]

            total = stats["total_emails"]
            sent = stats["emails_sent"]
            stats["success_rate"] = round((sent / total * 100), 1) if total > 0 else 0.0

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM suppression_list"
            ).fetchone()
            stats["suppressed"] = row["cnt"]

            # V2 stats
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM contacts WHERE is_verified = 1"
            ).fetchone()
            stats["verified_contacts"] = row["cnt"]

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM retry_queue WHERE status = 'pending'"
            ).fetchone()
            stats["retries_pending"] = row["cnt"]

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM sequence_enrollments WHERE status = 'active'"
            ).fetchone()
            stats["sequences_active"] = row["cnt"]

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM open_tracking WHERE opened_at IS NOT NULL"
            ).fetchone()
            stats["opens_tracked"] = row["cnt"]

            # True per-status totals for the Contacts filter chips. They used
            # to be derived by counting rendered rows, so with a 200-row page
            # over 1000 contacts the "pending" chip read 41 while the Overview
            # said 437 - two numbers for one quantity, on one screen.
            stats["contact_status_counts"] = {
                r["status"] or "unknown": r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM contacts GROUP BY status"
                )
            }

            return stats

    def get_recent_activity(self, limit: int = 50) -> list[dict]:
        """Get recent activity log entries for the dashboard."""
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT * FROM activity_log
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_errors(self, limit: int = 20, days: int = 14) -> list[dict]:
        """Activity rows with status 'error' from the last ``days`` days,
        newest first.

        Asked for directly. Picked out of the newest rows of every kind, an
        error left the dashboard's panel as soon as enough ordinary rows
        followed it, and the panel then said there were none.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT * FROM activity_log
                   WHERE status = 'error' AND timestamp >= ?
                   ORDER BY timestamp DESC, id DESC LIMIT ?""",
                (clock.db_days_ago(days), limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_activity_since(self, action: str, since: str) -> list[dict]:
        """One kind of activity since a time in log_activity's own shape
        (clock.db_now), oldest first."""
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT * FROM activity_log
                   WHERE action = ? AND timestamp >= ?
                   ORDER BY timestamp, id""",
                (action, since),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_contacts(
        self, limit: int = 200, status: str | None = None
    ) -> list[dict]:
        """Contacts for the dashboard table, newest-and-best first.

        ``status`` filters SERVER-side. Without it the dashboard could only
        filter the page it had already fetched, so with 1000 contacts and a
        limit of 200 the "pending" chip offered 41 rows out of 437 - and said
        41, as though that were the total.
        """
        where = "WHERE c.status = ?" if status else ""
        params = ([status] if status else []) + [limit]
        with self._transaction() as conn:
            rows = conn.execute(
                f"""SELECT c.*, s.search_query
                    FROM contacts c
                    LEFT JOIN search_log s ON c.search_id = s.id
                    {where}
                    ORDER BY c.lead_score DESC, c.timestamp DESC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def set_email_message_id(self, email_id: int, message_id: str):
        """Record the Message-ID we put on an outgoing mail.

        Written after create_email rather than at log_email time, because the
        row is created first (its id becomes the tracking id) and the message
        - and therefore the Message-ID - only exists afterwards.
        """
        with self._transaction() as conn:
            conn.execute(
                "UPDATE email_log SET message_id = ? WHERE id = ?",
                (message_id, email_id),
            )

    def bounce_matches_a_send(self, email: str, message_ids=()) -> bool:
        """Whether a bounce report for email is about an email the agent sent
        to that address.

        message_ids are the Message-IDs the report gives for the email it
        reports on, with or without their angle brackets. With any, one must
        be the Message-ID of a send to the address, however old; a send whose
        Message-ID could not be recorded counts by its date, as below. With
        none, a send to the address in the last BOUNCE_MATCH_DAYS days. A send
        kept by the token, for an address the database does not hold
        (prior_sends), counts too.

        Anyone can write a report that says an address is dead; one about no
        email of ours must suppress no one.
        """
        addr = (email or "").strip().lower()
        wanted = {_bare_message_id(m) for m in message_ids} - {""}
        since = clock.db_days_ago(BOUNCE_MATCH_DAYS)
        try:
            token = token_of(addr)
        except RuntimeError:
            token = None  # no erasure key in this process: no token kept either
        with self._transaction() as conn:
            sends = conn.execute(
                "SELECT message_id, timestamp FROM email_log "
                "WHERE LOWER(recipient_email) = ? "
                f"AND delivery_status IN {POSSIBLY_SENT_STATUSES_SQL} "
                "UNION ALL SELECT message_id, sent_at FROM prior_sends WHERE token = ?",
                (addr, token),
            ).fetchall()
        for message_id, sent_at in sends:
            recorded = _bare_message_id(message_id)
            if wanted and recorded in wanted:
                return True
            if (not wanted or not recorded) and (sent_at or "") >= since:
                return True
        return False

    def get_thread_message_id(self, contact_id: int) -> str | None:
        """The Message-ID of the FIRST email actually delivered to this contact.

        Follow-ups reference this so the whole sequence forms one thread in the
        prospect's client, rather than a chain of pairs or - as before, when no
        Message-ID was set at all - a series of unrelated emails.

        Only DELIVERED rows count - SENT_STATUSES, the same rule every other
        "did this reach the recipient" query in this file uses. A failed or
        dry-run row was never delivered, so referencing it would point at a
        message the prospect never received, and the client would either show
        a broken thread or start a new one.

        'sent_unconfirmed' must be included, and was not at first. It is a send
        whose acknowledgement was lost after DATA: the server most likely
        accepted it, and the agent deliberately does not resend. Excluding it
        meant the prospect had the first email while the follow-up arrived
        unthreaded - and worse, if a later touch got a clean 'sent', THAT
        became the thread root, contradicting the promise above.

        Returns None for a contact first emailed before this existed; the
        caller must then simply not claim a thread.
        """
        with self._transaction() as conn:
            row = conn.execute(
                f"""SELECT message_id FROM email_log
                   WHERE contact_id = ?
                     AND delivery_status IN {SENT_STATUSES_SQL}
                     AND message_id IS NOT NULL
                     AND TRIM(message_id) != ''
                   ORDER BY timestamp ASC, id ASC LIMIT 1""",
                (contact_id,),
            ).fetchone()
            return row["message_id"] if row else None

    def first_sent_languages(self, contact_ids: list[int]) -> dict[int, str]:
        """The language of the first email each contact was sent, for those
        that were sent one: "it" or "en", read from the notice version
        recorded with it (compliance.notice_language).

        Sent means what has_been_emailed means, a 'preparing' send included:
        it may have reached them. A dry run, a failure or a refusal reached
        nobody, so it fixes nothing."""
        languages: dict[int, str] = {}
        ids = [int(i) for i in contact_ids]
        with self._transaction() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"""SELECT contact_id, notice_version FROM (
                           SELECT contact_id, notice_version, ROW_NUMBER() OVER (
                               PARTITION BY contact_id
                               ORDER BY timestamp ASC, id ASC) AS nth
                           FROM email_log
                           WHERE contact_id IN ({marks})
                             AND delivery_status IN {POSSIBLY_SENT_STATUSES_SQL}
                       ) WHERE nth = 1""",
                    chunk,
                ).fetchall()
                for row in rows:
                    languages[row["contact_id"]] = notice_language(
                        row["notice_version"]
                    )
        return languages

    def last_possibly_sent(self, email: str) -> dict | None:
        """When the latest email that may have reached this address went, and
        its language ("it" or "en"), or None while none may have. Read by
        address, not contact: a Direct Send asks about the mailbox the
        operator typed, whichever contact row the earlier email went under."""
        with self._transaction() as conn:
            row = conn.execute(
                f"""SELECT timestamp, notice_version FROM email_log
                     WHERE LOWER(recipient_email) = ?
                       AND delivery_status IN {POSSIBLY_SENT_STATUSES_SQL}
                     ORDER BY timestamp DESC, id DESC LIMIT 1""",
                ((email or "").strip().lower(),),
            ).fetchone()
            if row is None:
                # A send the journal put back while the database did not hold
                # the address (restore_send), known by the token alone.
                row = conn.execute(
                    "SELECT sent_at AS timestamp, notice_version FROM prior_sends "
                    "WHERE token = ? ORDER BY sent_at DESC, id DESC LIMIT 1",
                    (erasure_token(email),),
                ).fetchone()
        if row is None:
            return None
        return {
            "timestamp": row["timestamp"],
            "language": notice_language(row["notice_version"]),
        }

    def first_sent_language(self, contact_id: int) -> str | None:
        """The language of the first email this contact was sent, or None
        while it has been sent none (first_sent_languages)."""
        return self.first_sent_languages([contact_id]).get(contact_id)

    def set_language_choice(self, contact_id: int, choice: str | None) -> bool:
        """Record the operator's language for a contact: "it", "en", or None
        for the rule's own. False when the contact is erased or was already
        sent an email, whose language it keeps.

        The check and the write are one statement, so a send claimed between
        the dashboard reading the contact and this write is not overtaken."""
        if choice is not None and choice not in LANGUAGE_CHOICES:
            raise ValueError(f"unknown language choice: {choice!r}")
        with self._transaction() as conn:
            cursor = conn.execute(
                f"""UPDATE contacts SET language_choice = ?
                   WHERE id = ? AND status != 'erased'
                     AND NOT EXISTS (SELECT 1 FROM email_log WHERE contact_id = ?
                         AND delivery_status IN {POSSIBLY_SENT_STATUSES_SQL})""",
                (choice, contact_id, contact_id),
            )
            return cursor.rowcount > 0

    def set_company_country(
        self, contact_id: int, country: str | None, source: str
    ) -> bool:
        """Record the country of a contact's company and where it was read; a
        None country with source "ai" records that the AI was asked and could
        not tell, so it is not paid to be asked again. Never overwrites what
        was recorded before: the first evidence stands. True when written."""
        country, source = _company_country(country, source, allow_unknown=True)
        with self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE contacts SET company_country = ?, company_country_source = ?
                   WHERE id = ? AND company_country_source IS NULL
                     AND status != 'erased'""",
                (country, source, contact_id),
            )
            return cursor.rowcount > 0

    def contacts_needing_company_country(self) -> list[dict]:
        """Contacts whose company country was never looked for, and which no
        email has gone to: once one has, its language is that email's.

        Only those a first email may still reach, as get_pending_contacts
        reads them: an objector, a client or a bounced address will never be
        written to, and its company is not sent to an AI for a language
        nobody will read. A filter verdict the operator re-queues is pending
        again, and its country is settled when it is qualified."""
        with self._transaction() as conn:
            rows = conn.execute(
                f"""SELECT c.* FROM contacts c
                   WHERE c.company_country_source IS NULL
                     AND c.status = 'pending'
                     AND email_domain(c.email) NOT IN
                         (SELECT domain FROM suppressed_domains)
                     AND domain_token(email_domain(c.email)) NOT IN
                         (SELECT domain FROM suppressed_domains)
                     AND LOWER(c.email) NOT IN (SELECT email FROM suppression_list)
                     AND erasure_token(c.email) NOT IN
                         (SELECT email FROM suppression_list)
                     AND NOT EXISTS (SELECT 1 FROM email_log e
                         WHERE e.contact_id = c.id
                           AND e.delivery_status IN {POSSIBLY_SENT_STATUSES_SQL})
                   ORDER BY c.id"""
            ).fetchall()
            return [dict(r) for r in rows]

    def has_been_emailed(self, contact_id: int) -> bool:
        """True if any email to this contact was delivered, probably was, or
        may have been: a 'preparing' send counts as sent until it is settled."""
        with self._transaction() as conn:
            return (
                conn.execute(
                    f"SELECT 1 FROM email_log WHERE contact_id = ? "
                    f"AND delivery_status IN {POSSIBLY_SENT_STATUSES_SQL} LIMIT 1",
                    (contact_id,),
                ).fetchone()
                is not None
            )

    def send_in_doubt(self, contact_id: int | None) -> bool:
        """True while an email to this contact is 'preparing': going out now
        from some process, or cut off after the server may have accepted it."""
        with self._transaction() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM email_log WHERE contact_id = ? "
                    "AND delivery_status = 'preparing' LIMIT 1",
                    (contact_id,),
                ).fetchone()
                is not None
            )

    def has_confirmed_send(self, contact_id: int) -> bool:
        """True if a server answered that it took an email to this contact, as
        opposed to one that only may have reached them."""
        with self._transaction() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM email_log WHERE contact_id = ? "
                    "AND delivery_status = 'sent' LIMIT 1",
                    (contact_id,),
                ).fetchone()
                is not None
            )

    def step_possibly_sent(self, contact_id: int, step_id: int) -> bool:
        """True if this follow-up step may already have gone to this contact
        since they were enrolled (see _step_possibly_sent)."""
        with self._transaction() as conn:
            return _step_possibly_sent(conn, contact_id, step_id)

    def settle_interrupted_sends(
        self, older_than: timedelta, sequence_id: int | None = None, count_send=None
    ) -> list[dict]:
        """Record as unconfirmed each send whose result was never written.

        dispatch_to_contact writes its row 'preparing' before the message goes
        to the server, and the result after. A process stopped in between -
        killed, Ctrl+C, a reboot - or a result write that met a locked database
        leaves the row 'preparing' although the server may have accepted the
        message, and nothing read it back: the next pass emailed the person
        again. has_been_emailed and send_in_doubt count the row as sent from
        the start. Once it is older than ``older_than`` - longer than any live
        send takes - no process can still be making it, and it is settled as
        the agent settles an unconfirmed send, leaving what such a send leaves:

        - the row becomes 'sent_unconfirmed';
        - the contact 'emailed', unless it was closed meanwhile;
        - the follow-up step on the row, if there is one, moves on;
        - an open retry for the contact succeeds;
        - a contact that was waiting for the campaign's first email or its
          retry, and was never enrolled, is enrolled in ``sequence_id``, as that
          send would have done. Not one Direct Send created: whether its
          operator asked for follow-ups is recorded nowhere, and none goes out
          on a guess;
        - the basis the send relied on is recorded, once per basis;
        - its tracking id is registered, so an open can still be counted;
        - ``count_send(conn, day, mailbox, email)`` counts it against the caps,
          on the day it was made. A send whose result met a locked database was
          already counted by its own process, and is counted twice: over the
          cap's side, which is the safe one.

        Each row is settled in one transaction, opened by its status change
        and holding the activity row that reports it, so a row is settled and
        reported once however many processes try. Returns the rows settled.
        A locked database leaves the rest for a later pass.
        """
        now = clock.now().astimezone(timezone.utc)
        zone = clock.get_timezone()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id, contact_id, recipient_email, timestamp, sequence_step_id, "
                "tracking_id, sender_account FROM email_log "
                "WHERE delivery_status = 'preparing' ORDER BY id"
            ).fetchall()
        with _kept_send_results_lock:
            known = set(_kept_send_results.get(self.db_path, {}))
        known |= set(_refusals_held_on_disk(self.db_path))
        settled = []
        for row in rows:
            # A process knows that one went nowhere, and has yet to get a
            # write through to say so.
            if row["id"] in known:
                continue
            try:
                written = _instant(row["timestamp"], zone)
            except (TypeError, ValueError, OverflowError) as e:
                logger.warning(
                    "email_log row %s is 'preparing' with an unreadable time (%s) "
                    "and is left for an operator",
                    row["id"],
                    e,
                )
                continue
            if now - written <= older_than:
                continue
            try:
                done = self._settle_interrupted_send(row, sequence_id, count_send)
            except sqlite3.Error as e:
                logger.error(
                    "Could not settle email_log row %s yet (%s) - it and the rows "
                    "after it wait for a later pass",
                    row["id"],
                    e,
                )
                break
            if done:
                settled.append(done)
        return settled

    def _settle_interrupted_send(
        self, row, sequence_id: int | None, count_send
    ) -> dict | None:
        """settle_interrupted_sends for one row: the row as settled, or None if
        it is no longer 'preparing'."""
        stamp = clock.now_iso()
        cid = row["contact_id"]
        with self._transaction() as conn:
            # First: the status change takes the write lock, so nothing below
            # can change under it, and a row already settled ends here.
            if not conn.execute(
                "UPDATE email_log SET delivery_status = 'sent_unconfirmed', "
                "smtp_response = ?, error_message = NULL "
                "WHERE id = ? AND delivery_status = 'preparing'",
                (INTERRUPTED_SEND_NOTE, row["id"]),
            ).rowcount:
                return None
            # Read again under the lock: an erasure that committed after the row
            # was read has replaced the address, and must not see it written back.
            email = conn.execute(
                "SELECT recipient_email FROM email_log WHERE id = ?", (row["id"],)
            ).fetchone()["recipient_email"]
            its = conn.execute(
                "SELECT message_id, notice_version FROM email_log WHERE id = ?",
                (row["id"],),
            ).fetchone()
            contact = conn.execute(
                "SELECT c.status, c.basis, c.source_url, c.source_channel, "
                "c.rule_version, s.source FROM contacts c "
                "LEFT JOIN search_log s ON s.id = c.search_id WHERE c.id = ?",
                (cid,),
            ).fetchone()
            active = conn.execute(
                """SELECT se.id, se.current_step,
                          EXISTS (SELECT 1 FROM sequence_steps nx
                                   WHERE nx.sequence_id = se.sequence_id
                                     AND nx.step_number = se.current_step + 1)
                              AS more
                   FROM sequence_enrollments se
                   JOIN sequence_steps ss
                     ON ss.sequence_id = se.sequence_id
                    AND ss.step_number = se.current_step
                   WHERE se.contact_id = ? AND se.status = 'active' AND ss.id = ?""",
                (cid, row["sequence_step_id"]),
            ).fetchall()
            for e in active:
                # The one step this row sent, moved on as advance_enrollment
                # moves it. A row with no step sent none, and a step its own
                # process has already moved on is no longer the current one.
                if e["more"]:
                    conn.execute(
                        "UPDATE sequence_enrollments SET current_step = ?, "
                        "last_sent_at = ? WHERE id = ?",
                        (e["current_step"] + 1, stamp, e["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE sequence_enrollments SET status = 'completed', "
                        "completed_at = ?, last_sent_at = ? WHERE id = ?",
                        (stamp, stamp, e["id"]),
                    )
            conn.execute(
                "UPDATE retry_queue SET status = 'succeeded', attempt = attempt + 1 "
                "WHERE contact_id = ? AND status IN ('pending', 'in_flight')",
                (cid,),
            )
            if contact is not None and contact["status"] not in CLOSED_CONTACT_STATUSES:
                conn.execute(
                    "UPDATE contacts SET status = 'emailed', discard_reason = NULL "
                    "WHERE id = ?",
                    (cid,),
                )
                if (
                    sequence_id
                    and contact["status"] in ("pending", "failed")
                    and contact["source"] != "manual"
                    and not conn.execute(
                        "SELECT 1 FROM sequence_enrollments WHERE contact_id = ? LIMIT 1",
                        (cid,),
                    ).fetchone()
                ):
                    conn.execute(
                        "INSERT OR IGNORE INTO sequence_enrollments "
                        "(contact_id, sequence_id, current_step, status, enrolled_at) "
                        "VALUES (?, ?, 0, 'active', ?)",
                        (cid, sequence_id, stamp),
                    )
            # An erasure deleted the record of the basis, and superseded it, and
            # left no address to count a domain by.
            erased = contact is None or contact["status"] == "erased"
            if not erased and contact["basis"]:
                self._log_consent(
                    conn,
                    email,
                    contact["basis"],
                    contact["source_url"] or "entered by an operator",
                    contact_id=cid,
                    channel=contact["source_channel"],
                    rule_version=contact["rule_version"] or RULE_VERSION,
                )
            if row["tracking_id"]:
                conn.execute(
                    "INSERT OR IGNORE INTO open_tracking (tracking_id, email_log_id) "
                    "VALUES (?, ?)",
                    (row["tracking_id"], row["id"]),
                )
            if count_send:
                count_send(
                    conn,
                    row["timestamp"][:10],
                    row["sender_account"],
                    None if erased else email,
                )
            details = (
                f"Send to {email} interrupted after the server "
                "may have accepted it - recorded as unconfirmed, not resending "
                f"(started {row['timestamp']})"
            )
            conn.execute(
                "INSERT INTO activity_log (action, details, status, timestamp) "
                "VALUES ('EMAIL_INTERRUPTED', ?, 'warning', ?)",
                (details, clock.db_now()),
            )
        # After the commit, as every journal line: it now counts as sent, so a
        # restore from a backup older than this must not send it again.
        self.journal_send(
            email,
            message_id=(its["message_id"] or "").strip() or None,
            sent=row["timestamp"],
            step_id=row["sequence_step_id"],
            mailbox=row["sender_account"],
            notice=its["notice_version"],
            unconfirmed=True,
        )
        return {**dict(row), "recipient_email": email}

    def get_last_email_log_id(self, contact_id: int) -> int:
        """The newest email_log row for this contact, or 0 if there is none."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id FROM email_log WHERE contact_id = ? ORDER BY id DESC LIMIT 1",
                (contact_id,),
            ).fetchone()
            return row[0] if row else 0

    def get_all_emails(self, limit: int = 200) -> list[dict]:
        """Get all email log entries for the dashboard table."""
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT e.*, c.name, c.organization, c.email as contact_email
                   FROM email_log e
                   LEFT JOIN contacts c ON e.contact_id = c.id
                   ORDER BY e.timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_searches(self, limit: int = 100) -> list[dict]:
        """Get all search log entries for the dashboard table."""
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT * FROM search_log
                   ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def searched_among(self, queries) -> set[str]:
        """Which of ``queries`` have been searched at any time.

        The free generator's exclusion list is only the last 500 searches, so
        it cannot tell a query searched long ago from one never searched. The
        query generator puts SUPPLECO's own phrases first only while they are
        new; judged by the 500-row window, every phrase came back to the front
        of the queue as soon as it left it, ahead of categories never searched.
        """
        queries = list(dict.fromkeys(q for q in queries if q))
        if not queries:
            return set()
        with self._transaction() as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT search_query FROM search_log
                   WHERE search_query IN ({",".join("?" * len(queries))})""",
                queries,
            ).fetchall()
            return {r[0] for r in rows}

    def count_pending_contacts(self) -> int:
        """Fast count of pending (unsuppressed) contacts awaiting outreach."""
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS cnt FROM contacts c
                   WHERE c.status = 'pending'
                     AND email_domain(c.email) NOT IN
                         (SELECT domain FROM suppressed_domains)
                     AND domain_token(email_domain(c.email)) NOT IN
                         (SELECT domain FROM suppressed_domains)
                     AND LOWER(c.email) NOT IN (SELECT email FROM suppression_list)
                     AND erasure_token(c.email) NOT IN
                         (SELECT email FROM suppression_list)"""
            ).fetchone()
            return row["cnt"] if row else 0

    def get_search_outcomes(self, limit: int = 500) -> list[dict]:
        """Per-search outcome signal for the learning loop: the query text, how
        many contacts it found, how many verified, how many replied, and how
        many a reviewer approved or rejected."""
        with self._transaction() as conn:
            rows = conn.execute(
                """SELECT s.search_query AS search_query,
                          s.results_count AS results_count,
                          COUNT(DISTINCT CASE WHEN c.is_verified = 1 THEN c.id END) AS verified,
                          COUNT(DISTINCT CASE WHEN c.status = 'responded' THEN c.id END) AS responded,
                          COUNT(DISTINCT CASE WHEN r.status IN ('approved', 'sent')
                                              THEN c.id END) AS approved,
                          COUNT(DISTINCT CASE WHEN r.status = 'rejected'
                                              THEN c.id END) AS rejected
                   FROM search_log s
                   LEFT JOIN contacts c ON c.search_id = s.id
                   LEFT JOIN review_queue r
                          ON r.contact_id = c.id AND r.stage = 'first_email'
                   GROUP BY s.id
                   ORDER BY s.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_ab_stats(self) -> list[dict]:
        """Per-A/B-variant sent/opened counts and open rate for reporting."""
        with self._transaction() as conn:
            rows = conn.execute(
                f"""SELECT COALESCE(ab_variant, 'default') AS variant,
                          COUNT(*) AS sent,
                          SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened
                   FROM email_log
                   WHERE delivery_status IN {SENT_STATUSES_SQL}
                   GROUP BY COALESCE(ab_variant, 'default')
                   ORDER BY sent DESC"""
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["open_rate"] = (
                    round((d["opened"] / d["sent"] * 100), 1) if d["sent"] else 0.0
                )
                result.append(d)
            return result
