"""
Retry Queue Engine for the B2B Outreach Agent.

Provides exponential-backoff retry logic for failed email deliveries.
Failed sends are queued in the ``retry_queue`` table and automatically
scheduled for re-attempt with increasing delays:

    Attempt 1 ->  ~5 min delay
    Attempt 2 -> ~30 min delay
    Attempt 3 ->  ~3 hr  delay  (then exhausted)
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from modules.database import Database

logger = logging.getLogger(__name__)


class RetryQueue:
    """
    Manages a persistent retry queue with exponential backoff.

    Parameters
    ----------
    db : Database
        The application database instance.
    max_retries : int
        Maximum number of retry attempts before marking as exhausted.
    base_delay_minutes : float
        Base delay in minutes for the first retry.  Subsequent retries
        multiply by 6 each time (exponential backoff).
    """

    def __init__(
        self,
        db: Database,
        max_retries: int = 3,
        base_delay_minutes: float = 5.0,
    ):
        self.db = db
        self.max_retries = max_retries
        self.base_delay_minutes = base_delay_minutes

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        """Return the current UTC datetime as an ISO-8601 string."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _compute_next_retry(self, attempt: int) -> str:
        """
        Compute the next retry timestamp using exponential backoff.

        delay = base_delay_minutes * (6 ** attempt)

        attempt 0 -> base_delay          (5 min)
        attempt 1 -> base_delay * 6      (30 min)
        attempt 2 -> base_delay * 36     (180 min / 3 hr)
        """
        delay_minutes = self.base_delay_minutes * (6**attempt)
        next_time = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        return next_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_to_retry(self, email_log_id: int, contact_id: int, error: str) -> int:
        """
        Queue a failed email for retry.

        Parameters
        ----------
        email_log_id : int
            The ``email_log.id`` of the failed send.
        contact_id : int
            The ``contacts.id`` of the target contact.
        error : str
            Description of the failure.

        Returns
        -------
        int
            The new retry_queue row ID.
        """
        now = self._now_iso()
        next_retry = self._compute_next_retry(attempt=0)

        with self.db._transaction() as conn:
            # One open retry per contact. A failing retry send re-entered
            # _process_contact's failure branch, which called this again and
            # created a SECOND row at attempt=0 while the first was incremented.
            # Every child restarted the counter, so max_attempts never terminated
            # the family - and each row was a real SMTP send to the same person.
            # in_flight counts as open: a claimed row is still going to be sent,
            # so queuing another here would reintroduce the duplicate it fixes.
            existing = conn.execute(
                "SELECT id, attempt FROM retry_queue "
                "WHERE contact_id = ? AND status IN ('pending', 'in_flight')",
                (contact_id,),
            ).fetchone()
            if existing:
                logger.debug(
                    "Retry already queued for contact_id=%s (row %s, attempt %s) "
                    "- not queuing another",
                    contact_id,
                    existing["id"],
                    existing["attempt"],
                )
                return existing["id"]

            cursor = conn.execute(
                """INSERT INTO retry_queue
                   (email_log_id, contact_id, attempt, max_attempts,
                    last_error, next_retry_at, status, created_at)
                   VALUES (?, ?, 0, ?, ?, ?, 'pending', ?)""",
                (email_log_id, contact_id, self.max_retries, error, next_retry, now),
            )
            row_id = cursor.lastrowid

        logger.info(
            "Queued retry for email_log_id=%d, contact_id=%d, next_retry=%s [OK]",
            email_log_id,
            contact_id,
            next_retry,
        )
        return row_id

    def get_due_retries(self) -> list[dict]:
        """
        Get all pending retries whose scheduled time has arrived.

        Returns
        -------
        list[dict]
            Retry rows where ``next_retry_at <= now`` and
            ``status = 'pending'``.
        """
        now = self._now_iso()

        # Claim the rows in the same transaction that reads them. Previously they
        # were handed out still marked 'pending' and only updated after the send
        # returned, so any failure in between - a crash, a service restart, an
        # exception after SMTP accepted the message - left the row due and the
        # next pass sent the same email again. Marking them 'in_flight' first
        # makes delivery at-most-once per claim; mark_retry_failed/`_success`
        # move them on from there, and reset_stale_in_flight recovers the rest.
        with self.db._transaction() as conn:
            rows = conn.execute(
                """SELECT * FROM retry_queue
                   WHERE status = 'pending' AND next_retry_at <= ?
                   ORDER BY next_retry_at ASC""",
                (now,),
            ).fetchall()
            # Only the rows this UPDATE won. Another process can claim one
            # between the read and the write, and handing it out here too is
            # how both of them sent it.
            won = [
                r
                for r in rows
                if conn.execute(
                    "UPDATE retry_queue SET status = 'in_flight' "
                    "WHERE id = ? AND status = 'pending'",
                    (r["id"],),
                ).rowcount
            ]

        results = [dict(r) for r in won]
        logger.debug("Due retries: %d claimed", len(results))
        return results

    def reset_stale_in_flight(self, older_than_minutes: int = 30) -> int:
        """Return rows stranded in 'in_flight' by a crash back to 'pending'.

        A claimed row whose send never reported an outcome would otherwise sit
        in_flight forever. This is the deliberate trade: a crash now costs a
        delayed retry rather than a duplicate email.

        Staleness is judged on next_retry_at - the time the row became due -
        since a claim does not stamp its own timestamp. A row claimed normally
        is resolved within seconds, so anything still in_flight long after it
        came due was abandoned.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=abs(older_than_minutes))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.db._transaction() as conn:
            cursor = conn.execute(
                "UPDATE retry_queue SET status = 'pending' "
                "WHERE status = 'in_flight' AND next_retry_at <= ?",
                (cutoff,),
            )
            recovered = cursor.rowcount
        if recovered:
            logger.warning(
                "Recovered %d retry row(s) stranded in_flight by an earlier crash",
                recovered,
            )
        return recovered

    def mark_retry_success(self, retry_id: int) -> None:
        """
        Mark a retry as successfully completed.

        Parameters
        ----------
        retry_id : int
            The ``retry_queue.id`` to update.
        """
        with self.db._transaction() as conn:
            conn.execute(
                """UPDATE retry_queue
                   SET status = 'succeeded', attempt = attempt + 1
                   WHERE id = ?""",
                (retry_id,),
            )
        logger.info("Retry %d marked as succeeded [OK]", retry_id)

    def release(self, retry_ids: list[int]) -> None:
        """Return claimed rows this pass did not resolve to 'pending',
        without spending an attempt."""
        if not retry_ids:
            return
        placeholders = ",".join("?" for _ in retry_ids)
        with self.db._transaction() as conn:
            conn.execute(
                f"UPDATE retry_queue SET status = 'pending' "
                f"WHERE status = 'in_flight' AND id IN ({placeholders})",
                tuple(retry_ids),
            )

    def cancel_retry(self, retry_id: int, reason: str) -> None:
        """End a retry without spending an attempt: the contact is closed,
        so there is nothing left to try."""
        with self.db._transaction() as conn:
            conn.execute(
                "UPDATE retry_queue SET status = 'cancelled', last_error = ? "
                "WHERE id = ?",
                (reason, retry_id),
            )
        logger.info("Retry %d cancelled: %s", retry_id, reason)

    def mark_retry_failed(self, retry_id: int, error: str) -> None:
        """
        Record a failed retry attempt.

        Increments the attempt counter and either schedules the next
        retry (with exponential backoff) or marks the entry as
        ``'exhausted'`` if all attempts are used up.

        Parameters
        ----------
        retry_id : int
            The ``retry_queue.id`` to update.
        error : str
            Description of the latest failure.
        """
        exhausted = None
        with self.db._transaction() as conn:
            row = conn.execute(
                "SELECT attempt, max_attempts, contact_id FROM retry_queue "
                "WHERE id = ?",
                (retry_id,),
            ).fetchone()

            if row is None:
                logger.warning("Retry %d not found [FAIL]", retry_id)
                return

            new_attempt = row["attempt"] + 1

            if new_attempt >= row["max_attempts"]:
                # All retries exhausted
                conn.execute(
                    """UPDATE retry_queue
                       SET attempt = ?, last_error = ?, status = 'exhausted'
                       WHERE id = ?""",
                    (new_attempt, error, retry_id),
                )
                logger.info(
                    "Retry %d exhausted after %d attempts [FAIL]",
                    retry_id,
                    new_attempt,
                )
                exhausted = (row["contact_id"], new_attempt)
            else:
                # Schedule next retry with exponential backoff. Back to
                # 'pending' as well: get_due_retries() claimed the row, and a
                # row left 'in_flight' is only freed by the crash-recovery
                # reset, half an hour after its backoff has elapsed.
                next_retry = self._compute_next_retry(new_attempt)
                conn.execute(
                    """UPDATE retry_queue
                       SET attempt = ?, last_error = ?, next_retry_at = ?,
                           status = 'pending'
                       WHERE id = ?""",
                    (new_attempt, error, next_retry, retry_id),
                )
                logger.info(
                    "Retry %d failed (attempt %d/%d), next retry at %s",
                    retry_id,
                    new_attempt,
                    row["max_attempts"],
                    next_retry,
                )

        # Outside the transaction (log_activity opens its own connection).
        # Giving up on a contact after N attempts was previously visible only in
        # the log file, so from the dashboard the contact simply stopped moving
        # with no explanation.
        if exhausted:
            contact_id, attempts = exhausted
            try:
                self.db.log_activity(
                    "RETRY_EXHAUSTED",
                    f"Gave up on contact {contact_id} after {attempts} "
                    f"attempts: {error}",
                    status="error",
                )
            except sqlite3.Error as e:
                logger.warning("Could not log retry exhaustion: %s", e)

    def get_retry_stats(self) -> dict:
        """
        Return aggregate retry queue statistics.

        Returns
        -------
        dict
            ``{pending, succeeded, exhausted, cancelled, total}``
        """
        with self.db._transaction() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM retry_queue"
            ).fetchone()
            # in_flight rows are claimed but not yet resolved - still outstanding
            # work, so they belong in "pending" or the dashboard under-reports
            # the queue while a batch is being sent.
            pending_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM retry_queue "
                "WHERE status IN ('pending', 'in_flight')"
            ).fetchone()
            succeeded_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM retry_queue WHERE status = 'succeeded'"
            ).fetchone()
            exhausted_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM retry_queue WHERE status = 'exhausted'"
            ).fetchone()
            # Without its own count a cancelled retry showed only in the
            # total, and the panel's numbers did not add up.
            cancelled_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM retry_queue WHERE status = 'cancelled'"
            ).fetchone()

        stats = {
            "pending": pending_row["cnt"],
            "succeeded": succeeded_row["cnt"],
            "exhausted": exhausted_row["cnt"],
            "cancelled": cancelled_row["cnt"],
            "total": total_row["cnt"],
        }

        logger.debug(
            "Retry stats: %d pending, %d succeeded, %d exhausted, %d total",
            stats["pending"],
            stats["succeeded"],
            stats["exhausted"],
            stats["total"],
        )
        return stats
