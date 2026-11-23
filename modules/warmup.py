"""
Send Volume Warm-up Scheduler for the B2B Outreach Agent.

Implements a linear warm-up ramp for email sending volume:
  Day 1 = start_daily, Day 2 = start_daily + increment, ...
  up to a configurable maximum daily cap.

Tracks daily send counts in the ``daily_send_counts`` table and
provides helpers to check/enforce the current day's allowance.
"""

import logging

from modules import clock
from modules.database import Database

logger = logging.getLogger(__name__)


class WarmupScheduler:
    """
    Manages send volume warm-up to protect sender reputation.

    Parameters
    ----------
    db : Database
        The application database instance.
    warmup_enabled : bool
        Master switch; when False, ``can_send()`` always returns True.
    warmup_start_daily : int
        Number of emails allowed on Day 1.
    warmup_increment : int
        Additional emails added per subsequent day.
    warmup_max_daily : int
        Hard ceiling on daily sends regardless of warm-up day.
    """

    def __init__(
        self,
        db: Database,
        warmup_enabled: bool = True,
        warmup_start_daily: int = 5,
        warmup_increment: int = 3,
        warmup_max_daily: int = 100,
    ):
        self.db = db
        self.warmup_enabled = warmup_enabled
        self.warmup_start_daily = warmup_start_daily
        self.warmup_increment = warmup_increment
        self.warmup_max_daily = warmup_max_daily

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _today_str() -> str:
        """Return today's date in the configured app timezone, so the daily
        allowance resets at local (Italy) midnight rather than UTC."""
        return clock.today_str()

    def _key(self, account: str = "", day: str | None = None) -> str:
        """Storage key for a day's counter (today's unless named), namespaced
        per account. The primary mailbox ("") keeps the plain date (backward
        compatible); extra mailboxes use 'username::date', so each account has
        its own counter and ramp."""
        day = day or self._today_str()
        return f"{account}::{day}" if account else day

    def _day_number(self, conn, account: str = "") -> int:
        """Warm-up day number for this account (Day 1 = its first sending day),
        i.e. the count of distinct calendar days it has recorded."""
        rows = conn.execute("SELECT date FROM daily_send_counts").fetchall()
        if account:
            prefix = f"{account}::"
            dates = {
                r["date"].split("::", 1)[1]
                for r in rows
                if r["date"].startswith(prefix)
            }
        else:
            dates = {r["date"] for r in rows if "::" not in r["date"]}
        days = len(dates)
        if self._today_str() not in dates:
            days += 1  # today would be a new day
        return max(days, 1)

    # ------------------------------------------------------------------
    # Public API  (account="" = the primary mailbox)
    # ------------------------------------------------------------------

    def get_daily_allowance(self, account: str = "") -> int:
        """Today's per-account allowed volume: start + (day-1)*increment, capped."""
        with self.db._transaction() as conn:
            day = self._day_number(conn, account)
        allowance = self.warmup_start_daily + (day - 1) * self.warmup_increment
        return min(allowance, self.warmup_max_daily)

    def get_remaining_today(self, account: str = "") -> int:
        """How many more emails this account may send today."""
        allowance = self.get_daily_allowance(account)
        key = self._key(account)
        with self.db._transaction() as conn:
            row = conn.execute(
                "SELECT count FROM daily_send_counts WHERE date = ?", (key,)
            ).fetchone()
            sent = row["count"] if row else 0
        return max(0, allowance - sent)

    def record_send(
        self, account: str = "", *, day: str | None = None, conn=None
    ) -> None:
        """Increment this account's send counter for today.

        Done as a single atomic UPSERT. The previous SELECT-then-INSERT ran as
        two separate statements on two separate connections, so the agent thread
        and a dashboard Direct Send could both see "no row yet", both INSERT,
        and one would die on the UNIQUE constraint - losing the count and
        aborting the whole batch *after* the email had already gone out.

        A send settled later counts on the ``day`` it was made, inside the
        transaction (``conn``) that settles it."""
        key = self._key(account, day)
        allowance = self.get_daily_allowance(account)
        upsert = """INSERT INTO daily_send_counts (date, count, allowance)
                    VALUES (?, 1, ?)
                    ON CONFLICT(date) DO UPDATE SET count = count + 1"""
        if conn is not None:
            conn.execute(upsert, (key, allowance))
            return
        with self.db._transaction() as own:
            own.execute(upsert, (key, allowance))

    def can_send(self, account: str = "") -> bool:
        """True if warm-up is off OR this account is below its daily allowance."""
        if not self.warmup_enabled:
            return True
        return self.get_remaining_today(account) > 0

    def get_warmup_status(self, account: str = "") -> dict:
        """Snapshot of one account's warm-up state:
        ``{day_number, daily_allowance, sent_today, remaining, is_warmed_up}``."""
        key = self._key(account)
        with self.db._transaction() as conn:
            day = self._day_number(conn, account)
            row = conn.execute(
                "SELECT count FROM daily_send_counts WHERE date = ?", (key,)
            ).fetchone()
            sent_today = row["count"] if row else 0

        # The same allowance the SENDER will use, not a second copy of the
        # formula. Both used to compute start + (day-1)*increment capped at
        # max_daily independently, so any change to the ramp had to be made
        # twice - and if it were made once, the dashboard would report a figure
        # the sender was not honouring. The operator trusts this number.
        allowance = self.get_daily_allowance(account)
        return {
            "day_number": day,
            "daily_allowance": allowance,
            "sent_today": sent_today,
            "remaining": max(0, allowance - sent_today),
            "is_warmed_up": allowance >= self.warmup_max_daily,
        }

    def get_combined_status(self, accounts: list) -> dict:
        """Sum the warm-up state across several account keys (e.g. all sending
        mailboxes) for the dashboard's total daily capacity."""
        keys = accounts or [""]
        parts = [self.get_warmup_status(a) for a in keys]
        total = {
            "accounts": len(keys),
            "daily_allowance": sum(p["daily_allowance"] for p in parts),
            "sent_today": sum(p["sent_today"] for p in parts),
            "remaining": sum(p["remaining"] for p in parts),
            "day_number": max(p["day_number"] for p in parts),
            "is_warmed_up": all(p["is_warmed_up"] for p in parts),
        }
        return total
