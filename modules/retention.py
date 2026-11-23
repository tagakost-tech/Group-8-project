"""Data retention: keep personal data only as long as outreach needs it.

GDPR Art. 5(1)(e). Run once a day by the agent. Erases with the same routine
GDPR erasure uses (modules.export.anonymise_contact_rows), so "erased" means
one thing everywhere. The periods are the developer's proposals in the
decision sheet (L1.8, L1.16, L5.1), pending counsel:

- contacts not emailed within FIRST_CONTACT_MAX_AGE_DAYS of collection;
- contacts emailed who never replied, RETENTION_DAYS_CONTACTED after the last
  email - unless a follow-up or a retry is still pending for them (for at
  most the longest step delay more);
- contacts who replied or became leads, RETENTION_DAYS_ENGAGED after the last
  exchange: the last email, reply or lead alert;
- an objection (an opt-out or a decline), at the next run.

Clients are no exception. The other records age out on their own clocks
(see _age_out_records). The suppression list keeps every objection, which is
what keeps an opt-out honoured; only a bounce is released.
"""

import logging
import shutil
import time
from datetime import timedelta

from modules import clock
from modules.compliance import mask_personal
from modules.database import (
    POSSIBLY_SENT_STATUSES_SQL,
    RELEASABLE_BOUNCE_REASONS_SQL,
    SENT_STATUSES_SQL,
    Database,
    is_held_address,
    undoes_a_failed_write,
)
from modules.export import anonymise_contact_rows, exports_dir
from modules.optout_spool import expire_unmatched

logger = logging.getLogger(__name__)

# Someone who replied, or whose reply was passed to sales.
ENGAGED_SQL = """(c.status = 'responded' OR EXISTS (
    SELECT 1 FROM lead_alerts l WHERE LOWER(l.email) = LOWER(c.email)))"""

# The last exchange with them: collection, the last email sent, their last
# reply, or the lead alert. MAX over '' for each one missing.
LAST_EXCHANGE_SQL = f"""MAX(
    c.timestamp,
    COALESCE(c.last_reply_at, ''),
    COALESCE((SELECT MAX(e.timestamp) FROM email_log e
              WHERE e.contact_id = c.id
                AND e.delivery_status IN {SENT_STATUSES_SQL}), ''),
    COALESCE((SELECT MAX(l.timestamp) FROM lead_alerts l
              WHERE LOWER(l.email) = LOWER(c.email)), ''))"""

# How long the job holds the write lock before stepping aside, and for how
# long. Committing each address is not enough on its own: the job starts the
# next erasure before a waiting writer's busy handler wakes up (it retries
# every 100 ms at most), so the writer starved. A pause longer than that
# retry interval lets it in.
HOLD_LOCK_S = 1.0
STEP_ASIDE_S = 0.15

# Open-tracking device data is purged sooner than the contact itself: nothing
# reads it, and it is the most intrusive thing held.
TRACKING_DEVICE_DATA_DAYS = 30

# The records held beside the contacts, each on its own clock (decision
# sheet L5.1; the developer's proposals, pending counsel).
ACTIVITY_MASK_DAYS = 180  # addresses in the activity log, then masked
LEAD_ALERT_TEXT_DAYS = 30  # a lead alert's reply text and its recipients
CLOSED_REQUEST_DAYS = 1095  # about 3 years; no legal source, counsel to set
ACCESS_ANSWER_DAYS = 30
SEND_COUNT_DAYS = 30  # daily send counts per recipient domain
BOUNCE_MONTHS = 24


def _cutoff(days: int) -> str:
    return (clock.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def describe(counts: dict) -> str:
    """One line for the activity log: what was done, in counts only."""
    done = [f"{key.replace('_', ' ')} {n}" for key, n in counts.items() if n]
    return "Retention: " + (", ".join(done) or "nothing had outlived its period")


def _age_out_records(db: Database, dry_run: bool = False) -> dict:
    """Age out what is held beside the contacts. Returns counts.

    A dry run rolls back everything since the contacts were erased, so it
    counts exactly what a real run changes, and deletes no file.
    """
    # Masked as the log files are when written; idempotent, since a masked
    # address no longer reads as one, so only changed lines are rewritten.
    masked = 0
    for row_id, details in db.conn.execute(
        "SELECT id, details FROM activity_log "
        "WHERE timestamp < ? AND details IS NOT NULL",
        (_cutoff(ACTIVITY_MASK_DAYS),),
    ).fetchall():
        hidden = mask_personal(details)
        if hidden != details:
            db.conn.execute(
                "UPDATE activity_log SET details = ? WHERE id = ?", (hidden, row_id)
            )
            masked += 1

    # The alert row stays: it stops the same lead being alerted twice.
    lead_alerts = db.conn.execute(
        """UPDATE lead_alerts SET notified_to = NULL, reply_excerpt = NULL
           WHERE timestamp < ?
             AND (notified_to IS NOT NULL OR reply_excerpt IS NOT NULL)""",
        (_cutoff(LEAD_ALERT_TEXT_DAYS),),
    ).rowcount

    # An open request is never deleted, however old.
    requests = db.conn.execute(
        "DELETE FROM dsr_requests WHERE status = 'closed' AND closed_at < ?",
        ((clock.now() - timedelta(days=CLOSED_REQUEST_DAYS)).isoformat(),),
    ).rowcount

    send_counts = db.conn.execute(
        "DELETE FROM domain_send_counts WHERE date < ?",
        ((clock.now() - timedelta(days=SEND_COUNT_DAYS)).strftime("%Y-%m-%d"),),
    ).rowcount

    # A bounce said the address did not work then; an objection is for good.
    bounces = db.conn.execute(
        f"DELETE FROM suppression_list "
        f"WHERE reason IN {RELEASABLE_BOUNCE_REASONS_SQL} AND timestamp < ?",
        (clock.add_months(clock.now(), -BOUNCE_MONTHS).strftime("%Y-%m-%d %H:%M:%S"),),
    ).rowcount
    if dry_run:
        db.conn.rollback()
    else:
        db.conn.commit()

    # Aged from when it was written: when it was sent is not recorded.
    exports = 0
    root = exports_dir(db.db_path)
    oldest_kept = clock.now().timestamp() - ACCESS_ANSWER_DAYS * 86400
    for folder in root.glob("access-*") if root.is_dir() else []:
        written = max(
            (f.stat().st_mtime for f in folder.iterdir()),
            default=folder.stat().st_mtime,
        )
        if written < oldest_kept:
            if not dry_run:
                shutil.rmtree(folder)
            exports += 1

    return {
        "activity_masked": masked,
        "lead_alerts_cleared": lead_alerts,
        "requests_deleted": requests,
        "exports_deleted": exports,
        "held_clicks_expired": expire_unmatched(db.db_path, dry_run),
        "send_counts_deleted": send_counts,
        "bounces_released": bounces,
    }


@undoes_a_failed_write
def apply_retention(db: Database, settings, *, dry_run: bool = False) -> dict:
    """Erase what has outlived its purpose. Returns counts, never addresses.

    A dry run changes nothing: it counts the contacts it would erase, and
    applies the rest inside a transaction it then rolls back.

    An erasure that fails part-way is undone, so the contact is still found
    by the next run and erased whole; the addresses before it stay erased.
    """
    live = """c.status != 'erased'
        AND c.email NOT LIKE 'erased\\_%' ESCAPE '\\'"""
    # An objection ends the purpose at once (decision L1.16). The address
    # stays on the suppression list, which keeps every objection.
    objected = db.conn.execute(
        "SELECT c.id, c.email FROM contacts c "
        f"WHERE {live} AND c.status = 'unsubscribed'"
    ).fetchall()
    common = f"{live} AND c.status != 'unsubscribed' AND NOT {ENGAGED_SQL}"
    # A send in doubt may have reached them: not someone never contacted, whose
    # opt-out link could be erased with them. They wait until it is settled.
    never_emailed = db.conn.execute(
        f"""SELECT c.id, c.email FROM contacts c
            WHERE {common}
              AND c.timestamp < ?
              AND NOT EXISTS (
                  SELECT 1 FROM email_log e
                  WHERE e.contact_id = c.id
                    AND e.delivery_status IN {POSSIBLY_SENT_STATUSES_SQL}
              )""",
        (_cutoff(settings.first_contact_max_age_days),),
    ).fetchall()
    # A follow-up still to come holds the contact: it would be the last email.
    # Only while one can come, though. An enrolment refused on every pass, or
    # left behind when follow-ups were switched off, held its contact for
    # ever, past the period the footer promises. Once the last email is older
    # than the period plus the longest step delay, it holds nothing. A delay
    # that is not a number counts as none, so it cannot stop the whole job.
    # A retry left pending (retries switched off) gets the same limit.
    longest_step = db.conn.execute(
        "SELECT COALESCE(MAX(CAST(delay_days AS INTEGER)), 0) FROM sequence_steps"
    ).fetchone()[0]
    no_reply = db.conn.execute(
        f"""SELECT c.id, c.email FROM contacts c
            WHERE {common}
              AND EXISTS (
                  SELECT 1 FROM email_log e
                  WHERE e.contact_id = c.id AND e.delivery_status IN {SENT_STATUSES_SQL}
              )
              AND (SELECT MAX(e.timestamp) FROM email_log e
                   WHERE e.contact_id = c.id
                     AND e.delivery_status IN {SENT_STATUSES_SQL}) < ?
              AND NOT EXISTS (
                  SELECT 1 FROM sequence_enrollments s
                  WHERE s.contact_id = c.id AND s.status = 'active'
                    AND (SELECT MAX(e.timestamp) FROM email_log e
                         WHERE e.contact_id = c.id
                           AND e.delivery_status IN {SENT_STATUSES_SQL}) >= ?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM retry_queue r
                  WHERE r.contact_id = c.id AND r.status IN ('pending', 'in_flight')
                    AND (SELECT MAX(e.timestamp) FROM email_log e
                         WHERE e.contact_id = c.id
                           AND e.delivery_status IN {SENT_STATUSES_SQL}) >= ?
              )""",
        (
            _cutoff(settings.retention_days_contacted),
            _cutoff(settings.retention_days_contacted + longest_step),
            _cutoff(settings.retention_days_contacted + longest_step),
        ),
    ).fetchall()
    engaged = db.conn.execute(
        f"""SELECT c.id, c.email FROM contacts c
            WHERE {live} AND c.status != 'unsubscribed' AND {ENGAGED_SQL}
              AND {LAST_EXCHANGE_SQL} < ?""",
        (_cutoff(settings.retention_days_engaged),),
    ).fetchall()

    # Committed one address at a time, stepping aside for other writers every
    # HOLD_LOCK_S. Each erasure scans several tables, and one transaction over
    # a large backlog held the write lock for minutes: a dashboard action
    # meanwhile outwaited its busy timeout and failed with "database is
    # locked". Each address is still erased atomically.
    held_since = time.monotonic()
    erasures = (
        [(row, "retention: objection") for row in objected]
        + [
            (row, f"retention: not emailed ({settings.first_contact_max_age_days}d)")
            for row in never_emailed
        ]
        + [
            (row, f"retention: no reply ({settings.retention_days_contacted}d)")
            for row in no_reply
        ]
        + [
            (row, f"retention: engaged ({settings.retention_days_engaged}d)")
            for row in engaged
        ]
    )
    # Counted from what the erasure did: a contact stored under anything but
    # one address is left as it is, and counted from the selection it was
    # reported erased every night. A dry run counts what a run would erase.
    erased: set[int] = set()
    for (contact_id, email), reason in erasures:
        if dry_run:
            if is_held_address(email):
                erased.add(contact_id)
        else:
            result = anonymise_contact_rows(db, email, discard_reason=reason)
            db.conn.commit()
            erased.update(result["contact_ids"])
            if time.monotonic() - held_since >= HOLD_LOCK_S:
                time.sleep(STEP_ASIDE_S)
                held_since = time.monotonic()
        if contact_id not in erased:
            logger.warning(
                "Retention cannot erase contact %d: its address is not one "
                "email address",
                contact_id,
            )

    tracking = db.conn.execute(
        """UPDATE open_tracking SET ip_address = NULL, user_agent = NULL
           WHERE opened_at < ?
             AND (ip_address IS NOT NULL OR user_agent IS NOT NULL)""",
        (_cutoff(TRACKING_DEVICE_DATA_DAYS),),
    ).rowcount

    counts = {
        "erased_never_contacted": sum(c in erased for c, _ in never_emailed),
        "erased_no_reply": sum(c in erased for c, _ in no_reply),
        "erased_engaged": sum(c in erased for c, _ in engaged),
        "erased_objection": sum(c in erased for c, _ in objected),
        "tracking_cleared": tracking,
        **_age_out_records(db, dry_run),
    }
    if dry_run:
        logger.info("Retention dry run, nothing changed: %s", counts)
        return counts
    logger.info("Retention applied: %s", counts)
    # Erased rows are rewritten; empty the log too, which still holds them.
    db.truncate_wal()
    return counts
