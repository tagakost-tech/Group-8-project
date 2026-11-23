"""Unit tests for LoopOrchestrator (modules/loop_orchestration.py).

Verifies:
- Execution lifecycle: run_single, run_continuous, and clean shutdown signaling via stop().
- Interruptible sleep (interruptible_sleep): verifies sliced sleep exits promptly when _running becomes False.
- Send window timing: within_send_window and seconds_until_send_window.
- Reputation circuit breaker: pause on high bounces or DNSBL listing, and affirmative safe_to_resume evaluation.
- IMAP inbox check triggers and hot-lead intent classification / operator alerting.
- GDPR data retention daily cycle (maybe_apply_retention).
- Dynamic runtime override hot-reloading (apply_runtime_overrides).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from modules import clock
from modules.database import Database
from modules.loop_orchestration import LoopOrchestrator


class DummyAgent:
    """Agent dummy providing required state and hooks for LoopOrchestrator."""

    def __init__(self, db: Database, settings: Any):
        self.db = db
        self.settings = settings
        self._running = True
        self._sending_paused = False
        self._pause_reason = None
        self._sending_domain = "suppleco.com"
        self._last_imap_check = 0.0
        self._last_imap_ok = 0.0
        self._guard_logged: dict[str, bool] = {}
        self._domain_backoff: dict[str, dict] = {}
        self._accounts: list[dict] = []
        self._account_keys: list[str] = ["main"]
        self._stats: dict[str, int] = {
            "searches": 0,
            "contacts": 0,
            "scored": 0,
            "verified": 0,
            "emails_sent": 0,
            "emails_failed": 0,
            "retries_queued": 0,
            "domain_skipped": 0,
            "warmup_skipped": 0,
            "low_score_skipped": 0,
            "irrelevant_skipped": 0,
        }
        self.warmup = None
        self.domain_throttle = None
        self.retry_queue = None
        self.imap_monitors = []
        self.query_generator = None
        self.learner = None
        self.ai_gen = None
        self.search = MagicMock()
        self.email_sender = MagicMock()
        self._bl_checker = None

        self.orchestrator = LoopOrchestrator(self)

        # Execution tracking
        self.pending_contacts_processed = 0
        self.sequences_processed = 0
        self.retries_processed = 0
        self.retention_runs = 0

    def stop(self):
        self.orchestrator.stop()

    def _interruptible_sleep(self, seconds: float):
        self.orchestrator.interruptible_sleep(seconds)

    def _within_send_window(self) -> bool:
        return self.orchestrator.within_send_window()

    def _seconds_until_send_window(self) -> float:
        return self.orchestrator.seconds_until_send_window()

    def _next_loop_delay(self, loop_delay: float) -> float:
        return self.orchestrator.next_loop_delay(loop_delay)

    def _safe_to_resume(self) -> bool:
        return self.orchestrator.safe_to_resume()

    def _bounce_guard_tripped(self) -> bool:
        return self.orchestrator.bounce_guard_tripped()

    def _blacklist_guard_tripped(self) -> bool:
        return self.orchestrator.blacklist_guard_tripped()

    def _maybe_check_imap(self):
        self.orchestrator.maybe_check_imap()

    def _reply_is_interested(self, subject: str, body: str) -> bool:
        return self.orchestrator.reply_is_interested(subject, body)

    def _handle_interested_replies(self, entries: list):
        self.orchestrator.handle_interested_replies(entries)

    def _send_lead_alert(self, entry: dict, recipients: list) -> bool:
        return self.orchestrator.send_lead_alert(entry, recipients)

    def _maybe_apply_retention(self, dry_run: bool):
        self.orchestrator.maybe_apply_retention(dry_run)

    def _apply_runtime_overrides(self):
        self.orchestrator.apply_runtime_overrides()

    def _resync_settings_consumers(self, key: str):
        self.orchestrator.resync_settings_consumers(key)

    def _discovery_focus_hint(self) -> str:
        return self.orchestrator.discovery_focus_hint()

    def _queries_for_a_standalone_run(self):
        return self.orchestrator.queries_for_a_standalone_run()

    def _print_summary(self):
        self.orchestrator.print_summary()

    def refresh_lists_if_changed(self):
        pass

    def _reconcile_interrupted_sends(self):
        pass

    def _process_pending_contacts(self, dry_run: bool = False):
        self.pending_contacts_processed += 1

    def _process_sequences(self, dry_run: bool = False):
        self.sequences_processed += 1

    def _process_retries(self):
        self.retries_processed += 1

    def _qualify_contacts(self, contacts: list[dict]) -> list[dict]:
        return contacts

    def _email_contacts(self, qualified: list[dict], dry_run: bool = False):
        pass


@pytest.fixture
def orch_fixture(tmp_path, settings_factory):
    test_db = Database(str(tmp_path / "loop_test.db"))
    st = settings_factory(
        send_window_enabled=True,
        send_window_start_hour=9,
        send_window_end_hour=17,
        send_window_days="0,1,2,3,4",
        bounce_guard_enabled=True,
        bounce_guard_min_sends=10,
        max_bounce_rate=0.08,
        retention_enabled=True,
        imap_check_interval_minutes=5,
    )
    agent = DummyAgent(test_db, st)
    return agent, agent.orchestrator


# ---------------------------------------------------------------------------
# 1. Execution lifecycle (stop, interruptible_sleep)
# ---------------------------------------------------------------------------


def test_stop_signals_clean_shutdown(orch_fixture):
    agent, orch = orch_fixture
    assert agent._running is True
    orch.stop()
    assert agent._running is False


def test_interruptible_sleep_exits_promptly_when_stopped(orch_fixture):
    agent, orch = orch_fixture
    agent._running = True

    start = time.time()

    def _stop_soon():
        time.sleep(0.05)
        agent._running = False

    t = threading.Thread(target=_stop_soon)
    t.start()

    # Request 30 seconds: sliced sleep must exit after the 1-second slice (< 2.0s) instead of 30s
    orch.interruptible_sleep(30.0)
    elapsed = time.time() - start

    t.join()
    assert elapsed < 2.0


def test_interruptible_sleep_non_positive_duration_returns_immediately(orch_fixture):
    _agent, orch = orch_fixture
    start = time.time()
    orch.interruptible_sleep(0.0)
    orch.interruptible_sleep(-5.0)
    assert (time.time() - start) < 0.2


# ---------------------------------------------------------------------------
# 2. Send window timing
# ---------------------------------------------------------------------------


def test_within_send_window_when_disabled(orch_fixture):
    agent, orch = orch_fixture
    agent.settings.send_window_enabled = False
    assert orch.within_send_window() is True


def test_within_send_window_business_hours(orch_fixture, monkeypatch):
    agent, orch = orch_fixture
    agent.settings.send_window_enabled = True

    # Monday 11:00 AM (weekday 0, hour 11) -> within [9, 17)
    monday_open = datetime(2026, 10, 5, 11, 0, 0, tzinfo=clock.get_timezone())
    monkeypatch.setattr(clock, "now", lambda: monday_open)
    assert orch.within_send_window() is True

    # Monday 20:00 PM (hour 20) -> outside [9, 17)
    monday_closed = datetime(2026, 10, 5, 20, 0, 0, tzinfo=clock.get_timezone())
    monkeypatch.setattr(clock, "now", lambda: monday_closed)
    assert orch.within_send_window() is False

    # Sunday 12:00 PM (weekday 6) -> outside mon-fri
    sunday_noon = datetime(2026, 10, 4, 12, 0, 0, tzinfo=clock.get_timezone())
    monkeypatch.setattr(clock, "now", lambda: sunday_noon)
    assert orch.within_send_window() is False


def test_seconds_until_send_window_calculation(orch_fixture, monkeypatch):
    agent, orch = orch_fixture
    agent.settings.send_window_enabled = True

    # When open, returns 0.0
    monday_open = datetime(2026, 10, 5, 11, 0, 0, tzinfo=clock.get_timezone())
    monkeypatch.setattr(clock, "now", lambda: monday_open)
    assert orch.seconds_until_send_window() == 0.0

    # Monday 08:00 AM, opens at 09:00 AM -> 3600 seconds until open
    monday_morning = datetime(2026, 10, 5, 8, 0, 0, tzinfo=clock.get_timezone())
    monkeypatch.setattr(clock, "now", lambda: monday_morning)
    secs = orch.seconds_until_send_window()
    assert 3590.0 <= secs <= 3610.0


# ---------------------------------------------------------------------------
# 3. Reputation circuit breaker
# ---------------------------------------------------------------------------


def test_bounce_guard_trips_when_bounce_rate_exceeds_threshold(orch_fixture):
    agent, orch = orch_fixture
    agent.settings.bounce_guard_enabled = True
    agent.settings.bounce_guard_min_sends = 20
    agent.settings.max_bounce_rate = 0.05

    # Mock DB deliverability metrics: 100 sent, 10 bounced (10% > 5%)
    agent.db.get_deliverability_metrics = MagicMock(
        return_value={
            "recent_sent": 100,
            "recent_bounced": 10,
            "bounce_rate": 0.10,
            "window_days": 7,
        }
    )

    assert orch.bounce_guard_tripped() is True
    logs = agent.db.get_recent_activity(10)
    assert any(l["action"] == "BOUNCE_GUARD" for l in logs)


def test_bounce_guard_ignores_when_under_min_sends(orch_fixture):
    agent, orch = orch_fixture
    agent.settings.bounce_guard_min_sends = 50

    # Only 5 sends total, even if 100% bounce, not enough data
    agent.db.get_deliverability_metrics = MagicMock(
        return_value={
            "recent_sent": 5,
            "recent_bounced": 5,
            "bounce_rate": 1.0,
            "window_days": 7,
        }
    )

    assert orch.bounce_guard_tripped() is False


def test_safe_to_resume_evaluates_dnsbl_delisting(orch_fixture):
    agent, orch = orch_fixture
    agent._pause_reason = "blacklist"

    # Mock BlacklistChecker
    mock_checker = MagicMock()
    mock_checker.check_domain.return_value = []
    mock_checker.summarize.return_value = {
        "status": "clean",
        "checked": 15,
        "total": 15,
    }
    agent._bl_checker = mock_checker

    assert orch.safe_to_resume() is True

    # If domain is still listed
    mock_checker.summarize.return_value = {
        "status": "listed",
        "checked": 15,
        "total": 15,
    }
    assert orch.safe_to_resume() is False


# ---------------------------------------------------------------------------
# 4. IMAP inbox check triggers & hot-lead alerting
# ---------------------------------------------------------------------------


def test_reply_is_interested_intent_classification(orch_fixture):
    _agent, orch = orch_fixture
    # Clear interested reply
    assert (
        orch.reply_is_interested(
            "Re: Partnership inquiry",
            "We are very interested in your contract manufacturing capabilities. Please send prices.",
        )
        is True
    )

    # Clear opt-out / not interested
    assert (
        orch.reply_is_interested(
            "Re: Partnership inquiry",
            "Please remove us from your email list, not interested at all.",
        )
        is False
    )


def test_handle_interested_replies_escalates_to_human_operator(orch_fixture):
    agent, orch = orch_fixture
    agent.settings.lead_alert_email = "operator@suppleco.com"

    sid = agent.db.log_search("test")
    agent.db.add_contact(sid, "interested_buyer@pharmaco.de", organization="PharmaCo")

    agent.email_sender.create_email.return_value = MagicMock()
    agent.email_sender.send_email.return_value = (True, "250 OK")

    reply_entry = {
        "email": "interested_buyer@pharmaco.de",
        "subject": "Re: Collaboration",
        "body": "Yes we would like to schedule a call next week.",
    }

    orch.handle_interested_replies([reply_entry])

    # Verified lead alert was recorded in db
    assert agent.db.was_lead_alerted("interested_buyer@pharmaco.de") is True
    logs = agent.db.get_recent_activity(10)
    assert any(l["action"] == "LEAD_ALERT" for l in logs)


# ---------------------------------------------------------------------------
# 5. GDPR data retention daily cycle (maybe_apply_retention)
# ---------------------------------------------------------------------------


def test_maybe_apply_retention_runs_once_per_calendar_day(orch_fixture, monkeypatch):
    agent, orch = orch_fixture
    agent.settings.retention_enabled = True

    calls = []
    from modules import retention

    monkeypatch.setattr(
        retention,
        "apply_retention",
        lambda db, st: (
            calls.append(True)
            or {
                "erased_never_contacted": 3,
                "erased_no_reply": 1,
                "tracking_cleared": 0,
            }
        ),
    )

    # First call runs retention
    orch.maybe_apply_retention(dry_run=False)
    assert len(calls) == 1

    # Second call on same calendar day is deduplicated
    orch.maybe_apply_retention(dry_run=False)
    assert len(calls) == 1

    # Dry run skips entirely
    agent.db.set_state("retention:last_run_date", None)
    orch.maybe_apply_retention(dry_run=True)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 6. Dynamic runtime override hot-reloading (apply_runtime_overrides)
# ---------------------------------------------------------------------------


def test_apply_runtime_overrides_updates_settings_and_consumers(orch_fixture):
    agent, orch = orch_fixture

    # Setup domain throttle consumer
    agent.domain_throttle = MagicMock()
    agent.domain_throttle.max_per_domain_per_day = 3

    # Add override to database
    agent.db.set_runtime_override("max_per_domain_per_day", 15)

    orch.apply_runtime_overrides()

    assert agent.settings.max_per_domain_per_day == 15
    assert agent.domain_throttle.max_per_domain_per_day == 15
    logs = agent.db.get_recent_activity(10)
    assert any(l["action"] == "CONFIG_CHANGED" for l in logs)


# ---------------------------------------------------------------------------
# 7. Execution lifecycle: run_single & run_continuous
# ---------------------------------------------------------------------------


def test_run_single_executes_batch_cleanly(orch_fixture):
    agent, orch = orch_fixture
    agent.search.search_and_extract.return_value = [
        {"id": 1, "email": "test@lead.com", "name": "Lead"}
    ]

    orch.run_single(queries=["pharma distributors"], dry_run=True)

    assert agent._stats["searches"] == 1
    assert agent._stats["contacts"] == 1
    assert agent.pending_contacts_processed == 1


class _SecondIteration(BaseException):
    """BaseException, not Exception: run_continuous catches Exception per
    iteration and carries on, which would swallow this."""


def test_run_continuous_stops_when_flag_cleared(orch_fixture):
    """A stop during an iteration ends the loop before the next one.

    The original asserted `_running is False` afterwards - which the loop's
    own `finally` guarantees whatever happens, and a loop that ignored the
    flag would have hung the suite rather than failed it. A second iteration
    now raises, so a broken stop fails at once.
    """
    agent, orch = orch_fixture
    iterations = []

    # Counted on the override refresh, which runs unconditionally at the top
    # of every iteration; the backlog step is itself skipped once the flag is
    # cleared, so it cannot see a second pass.
    def _count_iteration():
        iterations.append(1)
        if len(iterations) > 1:
            raise _SecondIteration

    def _stop_loop(dry):
        agent._running = False

    agent._apply_runtime_overrides = _count_iteration
    agent._process_pending_contacts = _stop_loop

    orch.run_continuous(queries=["test query"], dry_run=True, loop_delay=0.01)
    assert iterations == [1]
