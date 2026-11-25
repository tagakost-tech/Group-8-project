"""
Data export module.
The GDPR access answer (records_held_about, write_access_answer) and erasure.
"""

import json
import logging
import re
import shutil
from pathlib import Path

from modules import clock, journal
from modules.company_block import block_company, blockable_domain
from modules.database import (
    Database,
    domain_token,
    email_domain,
    erasure_token,
    is_erasure_token,
    is_held_address,
    undoes_a_failed_write,
)
from modules.private_files import make_private_dir

logger = logging.getLogger(__name__)


def _table_exists(db: Database, table: str) -> bool:
    return (
        db.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _columns(db: Database, table: str) -> list:
    return [r[1] for r in db.conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _address_rx(email_lower: str) -> re.Pattern:
    """The address as a whole token, case-insensitively.

    A bare substring also matched longer addresses that contain it -
    export@brand.de inside eu-export@brand.de, a@b.com inside a@b.com.au -
    so erasure rewrote, and export disclosed, another person's records. A
    full stop that merely ends the sentence still counts as a boundary.
    """
    return re.compile(
        r"(?<![\w.+-])" + re.escape(email_lower) + r"(?![\w-]|\.\w)", re.IGNORECASE
    )


def _redact(db: Database, table: str, column: str, email_lower: str, token: str) -> int:
    """Replace the address inside free text, keeping the row.

    In Python rather than SQL: SQLite's REPLACE is case-sensitive, and SMTP
    servers echo an address back in whatever casing they like. LIKE only
    selects candidates; its "_" wildcard over-matches harmlessly because the
    substitution itself is exact.
    """
    if column not in _columns(db, table):
        return 0
    rx = _address_rx(email_lower)
    rows = db.conn.execute(
        f"SELECT id, {column} FROM {table} WHERE LOWER({column}) LIKE ?",
        (f"%{email_lower}%",),
    ).fetchall()
    for row_id, text in rows:
        db.conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ?",
            (rx.sub(token, text), row_id),
        )
    return len(rows)


def _redact_domain(db: Database, domain: str) -> int:
    """Replace a company domain inside activity_log details with its token.

    As a whole name only: rossi.it is not the end of mariorossi.it. Nor the
    end of an address: a colleague's own address is theirs, not the erased
    person's, and their own erasure finds it by the address.
    """
    rx = re.compile(
        r"(?<![\w@.-])" + re.escape(domain) + r"(?![\w-]|\.\w)",
        flags=re.IGNORECASE,
    )
    token = domain_token(domain)
    n = 0
    for row_id, text in db.conn.execute(
        "SELECT id, details FROM activity_log WHERE LOWER(details) LIKE ?",
        (f"%{domain}%",),
    ).fetchall():
        cleaned = rx.sub(token, text)
        if cleaned != text:
            db.conn.execute(
                "UPDATE activity_log SET details = ? WHERE id = ?", (cleaned, row_id)
            )
            n += 1
    return n


def anonymise_contact_rows(
    db: Database,
    email: str,
    *,
    status: str = "erased",
    discard_reason: str = "gdpr_erasure",
) -> dict:
    """Remove one address's personal data from every table but suppression_list.

    Shared by GDPR erasure and the retention job, so the two can never
    disagree about what "erased" means. Does not commit: the caller owns the
    transaction. Returns {"contact_ids": [...], "actions": [...]}.
    """
    email_lower = (email or "").strip().lower()
    actions: list[str] = []
    contact_ids: list[int] = []
    identifying: set[str] = set()
    if not is_held_address(email_lower):
        # The retention job passes whatever a contact row holds; a blank one
        # must erase nothing rather than every row's free text.
        logger.warning("Erasure skipped: the input is not an email address")
        return {"contact_ids": contact_ids, "actions": actions}
    if is_erasure_token(email_lower):
        # Erased already: anonymised again, the rows would carry a token of
        # the token, which no check for the person's address matches.
        logger.warning("Erasure skipped: the input is an erasure token")
        return {"contact_ids": contact_ids, "actions": actions}
    token = erasure_token(email_lower)

    def note(action: str) -> None:
        actions.append(action)
        logger.info("%s", action)

    # Anonymize the contact rows in place. A DELETE hits the FOREIGN KEYs
    # from email_log/retry_queue/sequence_enrollments and aborts - which once
    # made erasure crash for every contact we had actually emailed, i.e. for
    # every realistic erasure request.
    if _table_exists(db, "contacts"):
        held = db.conn.execute(
            "SELECT id, name, organization FROM contacts WHERE LOWER(email) = ?",
            (email_lower,),
        ).fetchall()
        contact_ids = [r[0] for r in held]
        identifying = {
            v for r in held for v in (r[1], r[2]) if v and len(v.strip()) >= 3
        }
        result = db.conn.execute(
            """UPDATE contacts
               SET email = ?, name = NULL, role = NULL, organization = NULL,
                   source_url = NULL, qualification_rationale = NULL,
                   company_country = NULL, company_country_source = NULL, language_choice = NULL,
                   verification_details = NULL, status = ?, discard_reason = ?
               WHERE LOWER(email) = ?""",
            (token, status, discard_reason, email_lower),
        )
        if result.rowcount > 0:
            note(f"Anonymized {result.rowcount} contact record(s)")

    if _table_exists(db, "email_log"):
        columns = _columns(db, "email_log")
        total = 0
        for col in [
            c for c in columns if c in ("recipient_email", "to_email", "email")
        ]:
            total += db.conn.execute(
                f"UPDATE email_log SET {col} = ? WHERE LOWER({col}) = ?",
                (token, email_lower),
            ).rowcount

        # The address was not the only personal data in this table. The
        # rendered message is stored too, and every template opens with
        # "Hi {FirstName}," - so body_preview holds the erased person's name.
        # Matched by contact_id because the address column is already
        # overwritten. A marker, not NULL: email_log.subject is NOT NULL, and
        # blanking it aborts the whole erasure after suppression has already
        # been committed - the caller would be told nothing went wrong.
        content_cols = [c for c in columns if c in ("subject", "body_preview")]
        if contact_ids and content_cols:
            placeholders = ",".join("?" for _ in contact_ids)
            sets = ", ".join(f"{c} = ?" for c in content_cols)
            db.conn.execute(
                f"UPDATE email_log SET {sets} WHERE contact_id IN ({placeholders})",
                tuple(["[erased]"] * len(content_cols)) + tuple(contact_ids),
            )
        if total > 0:
            note(f"Anonymized {total} email log record(s)")

        # SMTP servers echo the recipient back ("550 5.1.1 <anna@...>").
        echoed = _redact(db, "email_log", "smtp_response", email_lower, token)
        echoed += _redact(db, "email_log", "error_message", email_lower, token)
        if echoed:
            note(f"Redacted the address from {echoed} SMTP response(s)")

        # Who opened the email, from where: device data about the person.
        if contact_ids and _table_exists(db, "open_tracking"):
            placeholders = ",".join("?" for _ in contact_ids)
            opens = db.conn.execute(
                f"""UPDATE open_tracking SET ip_address = NULL, user_agent = NULL
                    WHERE email_log_id IN (
                        SELECT id FROM email_log WHERE contact_id IN ({placeholders})
                    ) AND (ip_address IS NOT NULL OR user_agent IS NOT NULL)""",
                tuple(contact_ids),
            ).rowcount
            if opens:
                note(f"Cleared device data from {opens} open record(s)")

    if _table_exists(db, "retry_queue"):
        n = _redact(db, "retry_queue", "last_error", email_lower, token)
        if n:
            note(f"Redacted the address from {n} retry record(s)")

    # Proposals written about them, and the person's decision on each. The
    # text goes; the decision stays, as the record of human oversight, and an
    # undecided or unsent proposal can never be sent.
    if contact_ids and _table_exists(db, "review_queue"):
        placeholders = ",".join("?" for _ in contact_ids)
        n = db.conn.execute(
            f"""UPDATE review_queue
                SET proposed_subject = '[erased]', proposed_body = '[erased]',
                    evidence = NULL, final_subject = NULL, final_body = NULL,
                    approved_hash = NULL, reason = NULL, language = NULL,
                    status = CASE WHEN status IN ('pending', 'approved')
                                  THEN 'rejected' ELSE status END
                WHERE contact_id IN ({placeholders})""",
            tuple(contact_ids),
        ).rowcount
        if n:
            note(f"Erased the text of {n} review record(s)")

    # The links we sent them: a click on one later would otherwise resolve to
    # the address and write it back.
    if _table_exists(db, "unsubscribe_tokens"):
        n = db.conn.execute(
            "DELETE FROM unsubscribe_tokens WHERE email = ?", (email_lower,)
        ).rowcount
        if n:
            note(f"Deleted {n} unsubscribe link record(s)")

    # lead_alerts is the sharpest: reply_excerpt is the PROSPECT'S OWN TEXT,
    # up to 500 characters of whatever they wrote back. Deleted rather than
    # anonymized, because unlike suppression_list there is no ongoing purpose
    # that requires keeping it. consent_log records the legitimate-interest
    # basis for contacting them; erasure supersedes that basis.
    for table in ("lead_alerts", "consent_log"):
        if not _table_exists(db, table):
            continue
        result = db.conn.execute(
            f"DELETE FROM {table} WHERE LOWER(email) = ?", (email_lower,)
        )
        if result.rowcount > 0:
            note(f"Deleted {result.rowcount} {table} record(s)")

    # activity_log is the audit trail - what the agent did and why - and is
    # kept under the accountability principle. Keeping the ROW does not need
    # the IDENTIFIER: the address becomes the same token contacts carry, so
    # the record survives and the person does not.
    if _table_exists(db, "activity_log"):
        n = _redact(db, "activity_log", "details", email_lower, token)
        if n:
            note(f"Pseudonymised the address in {n} activity log record(s)")
        # Those same rows, now carrying the token, also name the person and
        # their company. Only those rows: a shared first name in someone
        # else's row is not theirs to erase.
        if identifying:
            named = 0
            for row_id, text in db.conn.execute(
                "SELECT id, details FROM activity_log WHERE details LIKE ?",
                (f"%{token}%",),
            ).fetchall():
                cleaned = text
                for value in identifying:
                    cleaned = re.sub(
                        re.escape(value), "[erased]", cleaned, flags=re.IGNORECASE
                    )
                if cleaned != text:
                    db.conn.execute(
                        "UPDATE activity_log SET details = ? WHERE id = ?",
                        (cleaned, row_id),
                    )
                    named += 1
            if named:
                note(
                    f"Removed the name and company from {named} activity log record(s)"
                )

    # The request register is kept for accountability, but under the token
    # only. The outcome is the operator's free text: it may quote the address
    # (any row) or name the person (their own rows, now carrying the token).
    if _table_exists(db, "dsr_requests"):
        n = db.conn.execute(
            "UPDATE dsr_requests SET email = ? WHERE LOWER(email) = ?",
            (token, email_lower),
        ).rowcount
        _redact(db, "dsr_requests", "outcome", email_lower, token)
        for row_id, outcome in db.conn.execute(
            "SELECT id, outcome FROM dsr_requests "
            "WHERE email = ? AND outcome IS NOT NULL",
            (token,),
        ).fetchall():
            for value in identifying:
                outcome = re.sub(
                    re.escape(value), "[erased]", outcome, flags=re.IGNORECASE
                )
            db.conn.execute(
                "UPDATE dsr_requests SET outcome = ? WHERE id = ?", (outcome, row_id)
            )
        if n:
            note(f"Kept {n} request record(s) as the token")

    return {"contact_ids": contact_ids, "actions": actions}


def records_held_about(db: Database, email_address: str) -> dict:
    """Every record held about one address, by table.

    The GDPR right-of-access export (Art. 15) is exactly this, and the
    dashboard's "why" view reads its timeline from it, so both see the same
    rows. Silent on purpose: the caller decides whether, and how, the address
    is logged.
    """
    result = {}
    email_lower = (email_address or "").strip().lower()
    if not is_held_address(email_lower):
        return result

    # Tables and the columns that might contain the email
    search_config = {
        "contacts": ["email"],
        "email_log": ["recipient_email", "to_email", "email"],
        "suppression_list": ["email"],
        # The recorded legal basis for contacting them, and their own
        # reply text - both omitted before, so the export was incomplete.
        "consent_log": ["email"],
        "lead_alerts": ["email"],
        # Their own requests, and how each was answered.
        "dsr_requests": ["email"],
        # Emails sent to them that a restore put back from the journal while
        # no contact held the address: kept under its token alone.
        "prior_sends": ["token"],
    }

    # Also check sequence enrollments via contact_id
    contact_ids = []

    for table_name, email_columns in search_config.items():
        # Verify table exists
        exists = db.conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name=?""",
            (table_name,),
        ).fetchone()

        if not exists:
            continue

        columns = _columns(db, table_name)
        table_records = []

        for col in email_columns:
            if col in columns:
                # Under the address, or under its token once erased: a
                # person erased earlier is still entitled to know it is held.
                rows = db.conn.execute(
                    f"SELECT * FROM {table_name} WHERE LOWER({col}) IN (?, ?)",
                    (email_lower, erasure_token(email_lower)),
                ).fetchall()

                for row in rows:
                    record = dict(zip(columns, row))
                    if record not in table_records:
                        table_records.append(record)

                        # Collect contact IDs for enrollment lookup
                        if table_name == "contacts" and "id" in record:
                            contact_ids.append(record["id"])

        if table_records:
            result[table_name] = table_records

    # Check sequence enrollments by contact_id
    if contact_ids:
        enrollment_table = db.conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name='sequence_enrollments'"""
        ).fetchone()

        if enrollment_table:
            columns = _columns(db, "sequence_enrollments")
            enrollments = []
            for cid in contact_ids:
                rows = db.conn.execute(
                    "SELECT * FROM sequence_enrollments WHERE contact_id = ?",
                    (cid,),
                ).fetchall()
                for row in rows:
                    enrollments.append(dict(zip(columns, row)))

            if enrollments:
                result["sequence_enrollments"] = enrollments

    # Check open_tracking via email_log
    if "email_log" in result:
        tracking_table = db.conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name='open_tracking'"""
        ).fetchone()

        if tracking_table:
            columns = _columns(db, "open_tracking")
            tracking_records = []
            for log_entry in result["email_log"]:
                log_id = log_entry.get("id")
                if log_id:
                    rows = db.conn.execute(
                        "SELECT * FROM open_tracking WHERE email_log_id = ?",
                        (log_id,),
                    ).fetchall()
                    for row in rows:
                        tracking_records.append(dict(zip(columns, row)))

            if tracking_records:
                result["open_tracking"] = tracking_records

    # Emails proposed to a reviewer about them, and each decision.
    if contact_ids and _table_exists(db, "review_queue"):
        columns = _columns(db, "review_queue")
        placeholders = ",".join("?" for _ in contact_ids)
        rows = db.conn.execute(
            f"SELECT * FROM review_queue WHERE contact_id IN ({placeholders})",
            tuple(contact_ids),
        ).fetchall()
        if rows:
            result["review_queue"] = [dict(zip(columns, r)) for r in rows]

    # Failed-send records about them, by contact.
    if contact_ids and _table_exists(db, "retry_queue"):
        columns = _columns(db, "retry_queue")
        placeholders = ",".join("?" for _ in contact_ids)
        rows = db.conn.execute(
            f"SELECT * FROM retry_queue WHERE contact_id IN ({placeholders})",
            tuple(contact_ids),
        ).fetchall()
        if rows:
            result["retry_queue"] = [dict(zip(columns, r)) for r in rows]

    # Every line the agent wrote that names them. Matched on the address
    # itself, case-insensitively, so another person's rows never leak in.
    if _table_exists(db, "activity_log"):
        columns = _columns(db, "activity_log")
        rx = _address_rx(email_lower)
        rows = db.conn.execute(
            "SELECT * FROM activity_log WHERE LOWER(details) LIKE ?",
            (f"%{email_lower}%",),
        ).fetchall()
        matched = [
            dict(zip(columns, r))
            for r in rows
            if rx.search(dict(zip(columns, r)).get("details") or "")
        ]
        if matched:
            result["activity_log"] = matched

    return result


def exports_dir(db_path: str) -> Path:
    """Where access answers are written: beside the database, under exports/."""
    return Path(db_path).resolve().parent / "exports"


def _answer_folder(db_path: str, token: str) -> Path:
    digest = token.split("_", 1)[1].split("@", 1)[0]
    return exports_dir(db_path) / f"access-{digest}"


def export_folder(db_path: str, email: str) -> Path:
    """Where the access answer for an address is written.

    Named by the address's keyed token, so no address appears in a file
    listing; beside the database, under exports/.
    """
    return _answer_folder(db_path, erasure_token(email))


def remove_access_answer(db_path: str, token: str) -> bool:
    """Delete the access answer prepared for whoever has this erasure token, a
    full copy of their data: its folder is named by the token, so that is all
    this needs. Returns whether there was one. Raises OSError while another
    program holds a file in it open."""
    answer = _answer_folder(db_path, token)
    if answer.exists():
        shutil.rmtree(answer)
        return True
    return False


# The wording is the developer's draft; counsel approves it with the notice
# (decision sheet L3.1 and L5.5) before any answer is sent.
def _cover_letter(held: dict, controller: str, notice_url: str, request_id) -> str:
    sources = sorted(
        {c["source_url"] for c in held.get("contacts", []) if c.get("source_url")}
    )
    counts = "\n".join(
        f"- {table}: {len(rows)} record(s)" for table, rows in sorted(held.items())
    )
    request = f" (request #{request_id})" if request_id else ""
    source_text = (
        "\n".join(f"- {u}" for u in sources)
        if sources
        else "No source page is recorded: the address was entered by hand."
    )
    return f"""# Your personal data held by {controller}

This is the answer to your access request{request} under Article 15 of the
GDPR. The data itself is in records.json, beside this letter.

## Purposes

We use your business contact details to offer {controller}'s contract
manufacturing of food supplements to your company, and to honour any
objection or request you make to us.

## Data held

Your business email address, and your name and job title where your company
published them; your company's name and the page where we found your
address; the emails we sent you and whether they were delivered; and, if
you replied, your reply. In detail:

{counts or "- nothing"}

## Where it came from

{source_text}

## Recipients

Microsoft, which runs our mailbox, and our sales staff if you replied with
interest. Nothing is transferred outside the European Economic Area beyond
Microsoft's own processing, under its standard contractual clauses.

## How long we keep it

For the periods set out in our privacy notice: {notice_url or "(not yet published)"}

## Automated decisions and scoring

We score each contact, partly from the job title and the form of the email
address, to decide in which order to email. No decision with legal or
similarly significant effect is made about you.

## Your rights

You may ask us for rectification of your data, for its erasure, or to
restrict its use, and you may object at any time to its use for direct
marketing. You may also lodge a complaint with a supervisory authority; in
Italy, the Garante per la protezione dei dati personali.
"""


def write_access_answer(
    db: Database,
    email: str,
    *,
    controller: str,
    notice_url: str,
    request_id: int | None = None,
) -> Path:
    """Write records.json and the Art. 15 cover letter; return the folder."""
    held = records_held_about(db, email)
    folder = make_private_dir(export_folder(db.db_path, email))
    (folder / "records.json").write_text(
        json.dumps(held, indent=2, default=str), encoding="utf-8"
    )
    (folder / "cover.md").write_text(
        _cover_letter(held, controller, notice_url, request_id), encoding="utf-8"
    )
    return folder


class DataExporter:
    """Handles data export operations including GDPR compliance."""

    def __init__(self, db: Database):
        self.db = db

    def gdpr_export(self, email_address: str) -> dict:
        """
        Export ALL data held on a specific email address across all tables.

        This supports the GDPR right of access (Article 15).

        Args:
            email_address: The email address to search for.

        Returns:
            Dict mapping table names to lists of matching records.
        """
        if not is_held_address((email_address or "").strip().lower()):
            raise ValueError("not an email address")
        result = records_held_about(self.db, email_address)

        total_records = sum(len(v) for v in result.values())
        logger.info(
            "GDPR export for '%s': %d records across %d tables",
            email_address,
            total_records,
            len(result),
        )
        return result

    @undoes_a_failed_write
    def gdpr_erase(self, email_address: str) -> dict:
        """
        Delete/anonymize all PII for an email address (GDPR right to erasure).

        Actions taken:
        - Suppress the address FIRST (so nothing downstream can re-email it,
          even if a later step fails)
        - Remove the address's personal data from every other table - see
          anonymise_contact_rows, shared with the retention job
        - Cancel active sequence enrollments
        - Only then change the files that name them: rewrite the journal's
          earlier lines as tokens, delete their access answer and held
          opt-outs. A program holding one open can delay these, never stop them

        Args:
            email_address: The email address to erase.

        Returns:
            Dict summarizing all actions taken. files_pending names, in words
            that name no one, the files that could not be changed yet, and
            journal_pending says whether the journal is one of them. If any
            are left, pending_recorded says whether a record of them stands
            (doctor reports it, and the next backup finishes it); if not,
            nothing will: the erasure must be run again.
        """
        if not is_held_address((email_address or "").strip().lower()):
            raise ValueError("not an email address")
        email_lower = email_address.strip().lower()
        if is_erasure_token(email_lower):
            # Erased already: erasing the token deleted the person's own
            # suppression entry and blocked redacted.invalid as a company.
            raise ValueError("an erasure token, not an address: erased already")
        token = erasure_token(email_lower)
        # The token, never the address: this summary is printed and logged.
        summary = {
            "email": token,
            "actions": [],
            "timestamp": clock.now_iso(),
        }

        # 0. Suppress before anything else, in its own committed transaction:
        # the request means "never contact me again", and that must hold even
        # if a later anonymization step throws. Suppressed as the keyed token,
        # which the suppression checks match against the real address - so a
        # re-scraped address is still refused, and none is kept in clear.
        # Their company too, kept only as a keyed token (decision L1.6).
        block_company(self.db, email_lower, "gdpr_erasure", keyed=True)
        if self.db.suppress_erased(email_lower):
            summary["actions"].append(
                "Added to suppression list (reason: gdpr_erasure)"
            )
        else:
            summary["actions"].append("Already on suppression list")

        erased = anonymise_contact_rows(self.db, email_lower)
        summary["actions"].extend(erased["actions"])
        contact_ids = erased["contact_ids"]

        # Cancel active sequence enrollments
        if contact_ids and _table_exists(self.db, "sequence_enrollments"):
            # Must match what sequences.py writes to this same column;
            # see the comment there.
            now = clock.now_iso()
            total_cancelled = 0
            for cid in contact_ids:
                result = self.db.conn.execute(
                    """UPDATE sequence_enrollments
                       SET status = 'cancelled', completed_at = ?,
                           cancel_reason = 'contact erased on request'
                       WHERE contact_id = ? AND status = 'active'""",
                    (now, cid),
                )
                total_cancelled += result.rowcount

            if total_cancelled > 0:
                action = f"Cancelled {total_cancelled} active sequence enrollment(s)"
                summary["actions"].append(action)
                logger.info("%s", action)

        # A retry of a failed first email would otherwise stay scheduled.
        if contact_ids and _table_exists(self.db, "retry_queue"):
            placeholders = ",".join("?" for _ in contact_ids)
            n = self.db.conn.execute(
                f"""UPDATE retry_queue SET status = 'cancelled',
                        last_error = 'contact erased on request'
                    WHERE contact_id IN ({placeholders}) AND status = 'pending'""",
                tuple(contact_ids),
            ).rowcount
            if n:
                summary["actions"].append(f"Cancelled {n} queued retry(ies)")

        # Their company's daily send counts name its domain, which may name
        # them (mariorossi.it); the company is blocked now, so they serve no
        # purpose. Shared mail domains count many unrelated people: kept.
        domain = blockable_domain(email_lower)
        if domain and _table_exists(self.db, "domain_send_counts"):
            n = self.db.conn.execute(
                "DELETE FROM domain_send_counts WHERE domain = ?", (domain,)
            ).rowcount
            if n:
                summary["actions"].append(
                    f"Deleted {n} daily send count(s) for their company"
                )
        # For the same reason the activity log keeps that domain only as the
        # token its block is kept as: a backoff or a lifted block names the
        # domain without the address, and retention masks addresses only.
        if domain and _table_exists(self.db, "activity_log"):
            n = _redact_domain(self.db, domain)
            if n:
                summary["actions"].append(
                    f"Pseudonymised their company's domain in {n} activity log "
                    "record(s)"
                )

        # (Suppression already happened in step 0, before any risky write.)
        # The record of the steps on files below goes first, by the tokens
        # alone: a kill at any moment after this commit leaves it for doctor to
        # report, and the next erasure, backup or replay to finish. Not earlier:
        # an erasure stopped before here has nothing on file to finish.
        address_domain = email_domain(email_lower)
        domain_tok = domain_token(address_domain) if address_domain else None
        journalled = self.db.journaling
        record = (
            journal.keep_pending(self.db.db_path, token, domain_tok)
            if journalled
            else None
        )
        self.db.conn.commit()

        # The files that name them, only now that the database holds nothing:
        # on Windows another program can hold any of them open, and rewriting
        # the journal first stopped the erasure half-way. These never raise.
        done, left = journal.finish_erasure(
            self.db.db_path, token, domain_tok, record, journalled=journalled
        )
        summary["actions"].extend(done)
        summary["files_pending"] = left
        summary["journal_pending"] = journal.THE_JOURNAL in left
        summary["pending_recorded"] = bool(left and record)

        self.db.log_activity(
            "GDPR_ERASURE",
            f"Erased on request: {token} ({len(summary['actions'])} action(s))",
        )
        # The rows are rewritten; now the files: the log's old page images
        # still hold the address until it is emptied.
        self.db.truncate_wal()
        logger.info(
            "GDPR erasure complete for %s: %d action(s)",
            token,
            len(summary["actions"]),
        )
        return summary
