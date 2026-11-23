"""Application clock.

Centralizes "now" so daily send caps and stored timestamps all use one
timezone. Defaults to Italy (Europe/Rome) because the operating company is
Italian; the agent and dashboard set it from config at startup via
``set_timezone``. Falls back to UTC if the named zone can't be resolved.
"""

import logging
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Rome"

# Set when a requested zone could not be resolved and UTC was substituted, so
# startup checks can surface it instead of the app running silently wrong.
_tz_fallback = False


def _resolve(name: str):
    """Resolve a zone name, falling back to UTC only after saying so loudly.

    The silent fallback that used to live here was a real bug, not a style nit.
    Windows ships no IANA database, so zoneinfo needs the `tzdata` package -
    which was missing from requirements.txt. On a clean Windows install
    ZoneInfo("Europe/Rome") raised, the exception was swallowed, and the whole
    app ran on UTC with no log output whatsoever. A typo in APP_TIMEZONE did
    exactly the same.

    In summer that shifts everything by two hours at once: _within_send_window
    compares now().hour against 8-19, so mail went out 10:00-21:00 Rome - the
    21:00 send being precisely the night-time blast the window exists to
    prevent. The daily cap rolled over at 02:00 instead of midnight, and every
    follow-up fired two hours late.
    """
    global _tz_fallback
    if ZoneInfo is None:
        _tz_fallback = True
        logger.error(
            "zoneinfo is unavailable; falling back to UTC. The send window, "
            "daily caps and follow-up scheduling will all be offset from %s.",
            name,
        )
        return timezone.utc
    try:
        tz = ZoneInfo(name)
        _tz_fallback = False
        return tz
    except (LookupError, ValueError, OSError) as e:
        # ZoneInfoNotFoundError subclasses KeyError (so LookupError) and is what
        # a missing tzdata raises; ValueError covers a malformed name such as ""
        # or a path-like string; OSError covers an unreadable tzdata file.
        _tz_fallback = True
        logger.error(
            "Could not resolve timezone %r (%s) - falling back to UTC. The send "
            "window, daily send caps and follow-up scheduling will all be wrong "
            "by the UTC offset. On Windows this usually means the 'tzdata' "
            "package is missing: pip install tzdata",
            name,
            e,
        )
        return timezone.utc


def timezone_is_fallback() -> bool:
    """True when the configured zone could not be resolved and UTC is in use."""
    return _tz_fallback


_tz = _resolve(DEFAULT_TIMEZONE)


def set_timezone(name: str):
    """Configure the app-wide timezone (called once at startup)."""
    global _tz
    _tz = _resolve(name or DEFAULT_TIMEZONE)
    return _tz


def get_timezone():
    return _tz


def now() -> datetime:
    """Timezone-aware 'now' in the configured app timezone."""
    return datetime.now(_tz)


def today_str() -> str:
    """Local calendar date (YYYY-MM-DD) used for daily send-cap buckets."""
    return now().strftime("%Y-%m-%d")


def add_months(when: datetime, months: int) -> datetime:
    """The same day `months` calendar months later, held to the month's end.

    GDPR deadlines run in calendar months (Art. 12(3): "within one month"),
    so 31 January plus one month is the last day of February.
    """
    import calendar

    month_index = when.month - 1 + months
    year = when.year + month_index // 12
    month = month_index % 12 + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def now_iso() -> str:
    """ISO-8601 timestamp with offset (for module-owned timestamp columns)."""
    return now().isoformat()


def db_now() -> str:
    """Timestamp in SQLite's 'YYYY-MM-DD HH:MM:SS' shape (no offset), so rows
    stay format-compatible with columns defaulting to datetime('now')."""
    return now().strftime("%Y-%m-%d %H:%M:%S")


def db_days_ago(days: int) -> str:
    """db_now() as it read that many days ago: where a window over stored
    timestamps starts."""
    return (now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
