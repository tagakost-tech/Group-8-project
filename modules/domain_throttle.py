"""
Per-Domain Send Rate Limiter for the B2B Outreach Agent.

Enforces a configurable maximum number of emails per domain per day
to avoid triggering spam filters and maintain sender reputation.

Tracks per-domain daily counts in the ``domain_send_counts`` table.
"""

import logging

from modules import clock
from modules.database import Database

logger = logging.getLogger(__name__)


class DomainThrottle:
    """
    Limits outbound email volume on a per-recipient-domain basis.

    Parameters
    ----------
    db : Database
        The application database instance.
    max_per_domain_per_day : int
        Maximum emails allowed to a single domain in one calendar day.
    """

    def __init__(self, db: Database, max_per_domain_per_day: int = 3):
        self.db = db
        self.max_per_domain_per_day = max_per_domain_per_day

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _today_str() -> str:
        """Return today's date in the configured app timezone (Italy), so the
        per-domain daily cap resets at local midnight."""
        return clock.today_str()

    @staticmethod
    def extract_domain(email: str) -> str:
        """
        Extract the domain part from an email address.

        Parameters
        ----------
        email : str
            A valid email address.

        Returns
        -------
        str
            The lowercased domain (everything after the last ``@``).
        """
        if "@" not in email:
            return email.lower().strip()
        return email.rsplit("@", 1)[-1].lower().strip()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_send_to_domain(self, domain: str) -> bool:
        """
        Check whether we are still under the daily limit for *domain*.

        Parameters
        ----------
        domain : str
            The recipient domain to check (e.g. ``"example.com"``).

        Returns
        -------
        bool
            True if the daily count is below the configured maximum.
        """
        domain = domain.lower().strip()
        today = self._today_str()

        with self.db._transaction() as conn:
            row = conn.execute(
                "SELECT count FROM domain_send_counts WHERE date = ? AND domain = ?",
                (today, domain),
            ).fetchone()
            current = row["count"] if row else 0

        allowed = current < self.max_per_domain_per_day
        logger.debug(
            "Domain '%s' today: %d/%d %s",
            domain,
            current,
            self.max_per_domain_per_day,
            "[OK]" if allowed else "[FAIL] limit reached",
        )
        return allowed

    def record_domain_send(
        self, domain: str, *, day: str | None = None, conn=None
    ) -> None:
        """
        Increment the daily send counter for *domain*.

        Creates a new row for today if one does not exist.

        Parameters
        ----------
        domain : str
            The recipient domain (e.g. ``"example.com"``).
        day, conn :
            For a send settled later: the day it was made, and the transaction
            that settles it.
        """
        domain = domain.lower().strip()
        day = day or self._today_str()

        # A single atomic UPSERT. The previous SELECT-then-INSERT ran
        # unserialised - sqlite3 only opens its write transaction at the DML -
        # so the agent thread and a dashboard Direct Send to the SAME domain
        # (the common case here, since this cap exists precisely because
        # several contacts share one company domain) could both see "no row
        # yet", both INSERT, and the loser would die on UNIQUE(date, domain).
        #
        # That exception surfaced AFTER the mail had already gone out, so the
        # operator was told "Send failed" for an email sitting in the
        # prospect's inbox - and a retry sent a real duplicate. warmup.py's
        # record_send had this same bug and this same fix; it was never
        # carried across to here.
        upsert = """INSERT INTO domain_send_counts (date, domain, count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(date, domain) DO UPDATE SET count = count + 1"""
        if conn is not None:
            conn.execute(upsert, (day, domain))
        else:
            with self.db._transaction() as own:
                own.execute(upsert, (day, domain))

        logger.debug("Recorded send to domain '%s' for %s [OK]", domain, day)

    def get_domain_stats(self) -> list[dict]:
        """
        Return today's send counts for all domains.

        Returns
        -------
        list[dict]
            Each dict has keys: ``domain``, ``count``, ``limit``,
            ``remaining``.
        """
        today = self._today_str()

        with self.db._transaction() as conn:
            rows = conn.execute(
                "SELECT domain, count FROM domain_send_counts WHERE date = ? ORDER BY count DESC",
                (today,),
            ).fetchall()

        stats = []
        for row in rows:
            count = row["count"]
            stats.append(
                {
                    "domain": row["domain"],
                    "count": count,
                    "limit": self.max_per_domain_per_day,
                    "remaining": max(0, self.max_per_domain_per_day - count),
                }
            )

        logger.debug("Domain stats for %s: %d domains tracked", today, len(stats))
        return stats
