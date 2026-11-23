"""
Analytics and open tracking module.
Provides email open tracking, chart data generation, and funnel analytics.
"""

import base64
import logging
import sqlite3
import uuid
from datetime import timedelta

from modules import clock
from modules.database import DRY_RUN_STATUS, SENT_STATUSES_SQL, Database

logger = logging.getLogger(__name__)


class Analytics:
    """Email analytics with open tracking and chart data generation."""

    # 1x1 transparent GIF pixel (43 bytes)
    TRACKING_PIXEL = base64.b64decode(
        "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    )

    def __init__(self, db: Database):
        self.db = db

    def generate_tracking_id(
        self, email_log_id: int, tracking_id: str | None = None
    ) -> str:
        """
        Create a unique tracking ID for an email and store the mapping.

        Args:
            email_log_id: ID of the email log entry to track.
            tracking_id: Optional existing tracking ID to use.

        Returns:
            A unique tracking ID string (UUID4).
        """
        if tracking_id is None:
            tracking_id = str(uuid.uuid4())

        try:
            with self.db._transaction() as conn:
                conn.execute(
                    """INSERT INTO open_tracking (tracking_id, email_log_id)
                       VALUES (?, ?)""",
                    (tracking_id, email_log_id),
                )
        except sqlite3.Error:
            # The caller embeds this pixel in an email that is about to be sent,
            # so the ID escapes into the world whether or not the row was
            # written. Without the row every later hit on the pixel is discarded
            # as "Unknown tracking ID" and the campaign reads as 0% opens - so
            # this is logged loudly, with a traceback, rather than as a warning.
            logger.exception(
                "Failed to store tracking ID '%s' for email log %s - opens for "
                "this email will be recorded against no one",
                tracking_id,
                email_log_id,
            )

        logger.info(
            "Generated tracking ID '%s' for email log %s", tracking_id, email_log_id
        )
        return tracking_id

    def record_open(
        self,
        tracking_id: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ):
        """
        Log an email open event.

        Args:
            tracking_id: The tracking ID from the pixel URL.
            ip_address: Kept for compatibility. Do not pass the reader's IP:
                it is personal data with no use here.
            user_agent: Likewise - device data nothing reads.
        """
        # clock.db_now(), not an ISO-with-offset UTC string. This value goes
        # into open_tracking.opened_at AND email_log.opened_at, where every
        # other timestamp is app-local "YYYY-MM-DD HH:MM:SS". Three different
        # formats in one column made an email look opened before it was sent,
        # and the "+00:00" form also breaks SQLite's datetime() parsing.
        now = clock.db_now()

        try:
            with self.db._transaction() as conn:
                # Check if tracking ID exists
                row = conn.execute(
                    "SELECT id, opened_at, email_log_id FROM open_tracking WHERE tracking_id = ?",
                    (tracking_id,),
                ).fetchone()

                if not row:
                    logger.warning("Unknown tracking ID: %s", tracking_id)
                    return

                if row[1] is None:
                    # First open
                    conn.execute(
                        """UPDATE open_tracking
                           SET opened_at = ?, ip_address = ?, user_agent = ?
                           WHERE tracking_id = ?""",
                        (now, ip_address, user_agent, tracking_id),
                    )
                    logger.info("First open recorded for %s", tracking_id)
                    # Mirror into email_log: the Emails table's Opens column
                    # and the A/B panel both read email_log.opened_at. Without
                    # this the A/B table reported a confident 0.0% open rate
                    # for every variant forever - not "no data", a wrong answer.
                    conn.execute(
                        """UPDATE email_log SET opened_at = ?
                           WHERE id = ? AND opened_at IS NULL""",
                        (now, row[2]),
                    )
                else:
                    # Subsequent open - insert a new row to track multiple opens
                    new_id = str(uuid.uuid4())
                    email_log_id = row[2]
                    conn.execute(
                        """INSERT INTO open_tracking
                           (tracking_id, email_log_id, opened_at, ip_address, user_agent)
                           VALUES (?, ?, ?, ?, ?)""",
                        (new_id, email_log_id, now, ip_address, user_agent),
                    )
                    logger.info(
                        "Subsequent open recorded for email log %s", email_log_id
                    )
                    # Backfill: opens recorded before the mirror-write existed
                    # left email_log.opened_at NULL; a later open repairs it.
                    # The IS NULL guard keeps the FIRST timestamp authoritative.
                    conn.execute(
                        """UPDATE email_log SET opened_at = ?
                           WHERE id = ? AND opened_at IS NULL""",
                        (now, email_log_id),
                    )
        except sqlite3.Error as e:
            logger.error("Failed to record open: %s", e)

    def get_chart_data(self, days: int = 30) -> dict:
        """
        Return data formatted for Chart.js consumption.
        Uses efficient GROUP BY queries instead of per-day loops.

        Args:
            days: Number of days to look back for trend data.

        Returns:
            Dict with: emails_per_day, success_rate_trend,
            contacts_by_status, lead_funnel.
        """
        try:
            with self.db._transaction() as conn:
                # clock.now(), not datetime.now(timezone.utc): every timestamp
                # this query buckets was written by clock.db_now() in app-local
                # (Rome) time. Rome's date rolls over one or two hours before
                # UTC's, so an email logged in that window carried a local date
                # that was not in all_days, and DROPPED OFF the chart until UTC
                # caught up. tests/test_timestamp_consistency.py exists to stop
                # exactly this, but its guard greps for the literal
                # "datetime.now()" and so never saw the timezone.utc form.
                end_date = clock.now()
                # Include today in the window: the last `days` calendar days
                # ending on (and including) today, so the current day always
                # appears on the chart and advances automatically each day.
                start_date = end_date - timedelta(days=days - 1)
                start_str = start_date.strftime("%Y-%m-%d")

                # Build full date range for labels (oldest -> today, inclusive)
                all_days = []
                for i in range(days):
                    day = start_date + timedelta(days=i)
                    all_days.append(day.strftime("%Y-%m-%d"))

                # --- Emails per day (single GROUP BY query) ---
                # SENT_STATUSES, the rule behind the Emails Sent card. Counting
                # every row drew dry runs and failed sends as emails sent, on a
                # chart beside a card that said 0.
                rows = conn.execute(
                    f"""SELECT DATE(timestamp) as day, COUNT(*) as cnt
                       FROM email_log
                       WHERE DATE(timestamp) >= ?
                         AND delivery_status IN {SENT_STATUSES_SQL}
                       GROUP BY DATE(timestamp)
                       ORDER BY day""",
                    (start_str,),
                ).fetchall()
                emails_by_day = {r[0]: r[1] for r in rows}
                emails_per_day_data = [emails_by_day.get(d, 0) for d in all_days]

                # --- Success rate trend (single GROUP BY query) ---
                # A failed send belongs in the denominator and a dry run does
                # not: it could not have failed, so counting it as an attempt
                # drew a day of clean sends beside a rehearsal as half failed.
                rows = conn.execute(
                    f"""SELECT DATE(timestamp) as day,
                              COUNT(*) as total,
                              SUM(CASE WHEN delivery_status IN {SENT_STATUSES_SQL} THEN 1 ELSE 0 END) as success
                       FROM email_log
                       WHERE DATE(timestamp) >= ?
                         AND delivery_status != '{DRY_RUN_STATUS}'
                       GROUP BY DATE(timestamp)
                       ORDER BY day""",
                    (start_str,),
                ).fetchall()
                rate_by_day = {}
                for r in rows:
                    total = r[1]
                    success = r[2]
                    rate_by_day[r[0]] = (
                        round((success / total) * 100, 1) if total > 0 else 0.0
                    )
                success_data = [rate_by_day.get(d, 0.0) for d in all_days]

                # --- Contacts by status (single query) ---
                status_rows = conn.execute(
                    """SELECT status, COUNT(*) FROM contacts
                       GROUP BY status ORDER BY COUNT(*) DESC"""
                ).fetchall()
                contacts_labels = [r[0] or "unknown" for r in status_rows]
                contacts_data = [r[1] for r in status_rows]

                # --- Lead funnel (4 simple counts) ---
                total_contacts = conn.execute(
                    "SELECT COUNT(*) FROM contacts"
                ).fetchone()[0]

                # Contacts mail actually went out to, by the rule behind the
                # Emails Sent card. Any row used to count, so a dry run showed
                # contacts as Emailed who had never been written to.
                emailed = conn.execute(
                    f"""SELECT COUNT(DISTINCT contact_id) FROM email_log
                       WHERE contact_id IS NOT NULL
                         AND delivery_status IN {SENT_STATUSES_SQL}"""
                ).fetchone()[0]

                # Counted by CONTACT, like every other stage. Counting distinct
                # email_log_id here counted MESSAGES, and a contact on a
                # follow-up sequence has several - so one prospect who opened
                # both their first mail and its follow-up scored 2 against an
                # "Emailed" of 1, and the funnel drew Opened taller than
                # Emailed. A funnel that widens is self-evidently broken to the
                # operator reading it, which is the only reason it was ever
                # going to be noticed.
                opened = conn.execute(
                    f"""SELECT COUNT(DISTINCT el.contact_id)
                       FROM open_tracking ot
                       JOIN email_log el ON el.id = ot.email_log_id
                       WHERE ot.opened_at IS NOT NULL
                         AND el.contact_id IS NOT NULL
                         AND el.delivery_status IN {SENT_STATUSES_SQL}"""
                ).fetchone()[0]

                replied = conn.execute(
                    "SELECT COUNT(*) FROM contacts WHERE status = 'responded'"
                ).fetchone()[0]

                funnel_labels = ["Found", "Emailed", "Opened", "Replied"]
                funnel_data = [total_contacts, emailed, opened, replied]

            chart_data = {
                "emails_per_day": {
                    "labels": all_days,
                    "data": emails_per_day_data,
                },
                "success_rate_trend": {
                    "labels": all_days,
                    "data": success_data,
                },
                "contacts_by_status": {
                    "labels": contacts_labels,
                    "data": contacts_data,
                },
                "lead_funnel": {
                    "labels": funnel_labels,
                    "data": funnel_data,
                },
            }

            logger.debug("Generated chart data for %d days", days)
            return chart_data

        except sqlite3.Error as e:
            logger.error("Failed to generate chart data: %s", e)
            return {
                "emails_per_day": {"labels": [], "data": []},
                "success_rate_trend": {"labels": [], "data": []},
                "contacts_by_status": {"labels": [], "data": []},
                "lead_funnel": {
                    "labels": ["Found", "Emailed", "Opened", "Replied"],
                    "data": [0, 0, 0, 0],
                },
            }
