"""Autonomous continuous loop lifecycle, standalone runs, pacing, and monitoring.

Manages daemon loop execution (run_continuous), standalone batch run (run_single),
shutdown signaling (stop), interruptible sleep (interruptible_sleep), send window
timing (within_send_window, seconds_until_send_window, next_loop_delay), reputation
pause/resume (safe_to_resume, bounce_guard_tripped, blacklist_guard_tripped),
IMAP inbox checks (maybe_check_imap), hot-lead escalation (handle_interested_replies,
send_lead_alert), data retention pruning (maybe_apply_retention), and runtime config
syncing (apply_runtime_overrides, resync_settings_consumers).
"""

from __future__ import annotations

import html
import logging
import random
import sqlite3
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from config import RUNTIME_EDITABLE, effective_settings
from modules import clock

if TYPE_CHECKING:
    from agent import OutreachAgent

logger = logging.getLogger(__name__)


class LoopOrchestrator:
    """Manages the autonomous lifecycle, scheduling, monitoring, and operator alerts."""

    def __init__(self, agent: OutreachAgent):
        self.agent = agent

    def stop(self) -> None:
        """Signal the agent to stop after the current operation."""
        self.agent._running = False
        logger.info("Stop signal received - will finish current operation")
        # The flag is cleared first: the SIGINT handler runs this on the main
        # thread, which may be holding the write lock mid-transaction, so the
        # log write can time out and must never cost the Stop itself.
        try:
            self.agent.db.log_activity("AGENT_STOP_REQUESTED", "Stop signal received")
        except sqlite3.Error as e:
            logger.warning("Could not record the stop request: %s", e)

    def interruptible_sleep(self, seconds: float) -> None:
        """Sleep in short increments so stop()/SIGINT takes effect promptly.

        A plain time.sleep(loop_delay) would keep the agent asleep for the
        full delay even after a stop was requested; this checks _running
        roughly once per second instead.
        """
        end = time.time() + max(0.0, seconds)
        while self.agent._running:
            remaining = end - time.time()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))

    _interruptible_sleep = interruptible_sleep

    def within_send_window(self) -> bool:
        """True if the current local (Italy) time is inside the configured
        business-hours send window. Night-time / weekend blasts read as
        automated and depress reply rates, so we hold sends outside it."""
        if not self.agent.settings.send_window_enabled:
            return True

        now = clock.now()
        if now.weekday() not in self.agent.settings.get_send_window_days():
            return False
        return (
            self.agent.settings.send_window_start_hour
            <= now.hour
            < self.agent.settings.send_window_end_hour
        )

    _within_send_window = within_send_window

    def seconds_until_send_window(self) -> float:
        """Seconds until the send window next opens, or 0.0 if it is open now.

        Walks forward day by day rather than doing arithmetic on the weekday
        set, because the set can be any subset (weekends only, Tue+Thu) and
        because stepping through midnight in a DST-aware zone is not a fixed
        number of hours. Eight iterations covers a full week plus today.
        """
        if (
            not self.agent.settings.send_window_enabled
            or self.agent._within_send_window()
        ):
            return 0.0

        now = clock.now()
        days = self.agent.settings.get_send_window_days()
        if not days:
            return 0.0  # no valid day: let the normal loop delay apply
        start_hour = self.agent.settings.send_window_start_hour
        for step in range(8):
            day = now + timedelta(days=step)
            if day.weekday() not in days:
                continue
            opens = day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            # Timestamps, not opens - now: two datetimes sharing a tzinfo
            # subtract as wall-clock times, so across the spring change the
            # agent slept an hour too long and missed the first sending hour.
            wait = opens.timestamp() - now.timestamp()
            if wait > 0:
                return wait
        return 0.0

    _seconds_until_send_window = seconds_until_send_window

    def next_loop_delay(self, loop_delay: float) -> float:
        """How long to sleep before the next loop.

        Outside the send window the loop has almost nothing to do - it cannot
        send first touches, follow-ups or retries - yet it woke every
        loop_delay seconds anyway, which over a 13-hour night is ~156 no-op
        passes. Sleep until the window opens instead.

        The cap is what makes this safe. _maybe_check_imap is deliberately NOT
        gated on the send window: reading the inbox is how bounces get
        suppressed, opt-outs honoured and hot leads escalated, and those must
        keep working overnight. So never sleep past the next IMAP check -
        otherwise this optimisation would silently switch off opt-out
        processing until morning, which is a legal problem, not a latency one.
        """
        wait = self.agent._seconds_until_send_window()
        if wait <= 0:
            return loop_delay
        if self.agent.imap_monitors:
            interval = max(60, self.agent.settings.imap_check_interval_minutes * 60)
            due_in = max(0.0, self.agent._last_imap_check + interval - time.time())
            wait = min(wait, max(due_in, 60.0))
        return max(loop_delay, wait)

    _next_loop_delay = next_loop_delay

    def safe_to_resume(self) -> bool:
        """Whether a paused agent may start sending again.

        Deliberately requires AFFIRMATIVE evidence of health, not merely the
        absence of a trip. The naive "not tripped" check was self-clearing:
        pausing stops sends, so the 7-day `recent_sent` count drains below
        bounce_guard_min_sends, the guard then reports "not tripped" for lack of
        data, and the agent resumed straight back into the list that caused the
        bounces."""
        if self.agent._pause_reason == "blacklist":
            # Resume only on a CLEAN verdict with full list coverage. An
            # UNAVAILABLE (refused/rate-limited lookup) is not evidence of
            # delisting - and it is the normal answer from a public resolver.
            try:
                from modules.blacklist import CLEAN, BlacklistChecker

                if self.agent._bl_checker is None:
                    self.agent._bl_checker = BlacklistChecker()
                v = self.agent._bl_checker.summarize(
                    self.agent._bl_checker.check_domain(self.agent._sending_domain)
                )
                return v["status"] == CLEAN and v["checked"] == v["total"]
            except ImportError:
                # The checker already downgrades every failed lookup to
                # 'unavailable', so the optional dnspython import is the only
                # thing that can escape it - and a missing resolver is no more
                # evidence of delisting than an UNAVAILABLE verdict is.
                logger.warning(
                    "Cannot verify delisting (dnspython is not installed) - "
                    "sending stays paused."
                )
                return False

        # A bounce pause stays until a human looks at it: the reputation damage
        # is already done, the offending contacts are still queued, and (as
        # above) the window cannot honestly clear itself while sending is
        # stopped. Restarting the agent from the dashboard clears it.
        return False

    _safe_to_resume = safe_to_resume

    def blacklist_guard_tripped(self) -> bool:
        """True when the sending domain is on a spam blocklist.

        Detection existed only on the dashboard before, which meant the
        unattended agent kept sending into a black hole: every send "succeeds"
        at the SMTP layer while no mail is delivered. Cached (1h TTL) so this is
        cheap to call per loop."""
        if (
            not self.agent.settings.blacklist_check_enabled
            or not self.agent._sending_domain
        ):
            self.agent._guard_logged.pop("blacklist", None)
            return False
        try:
            from modules.blacklist import LISTED, BlacklistChecker

            if self.agent._bl_checker is None:
                self.agent._bl_checker = BlacklistChecker()
            verdict = self.agent._bl_checker.summarize(
                self.agent._bl_checker.check_domain(self.agent._sending_domain)
            )
        except ImportError as e:
            # A refused, hijacked or timed-out lookup is already reported as an
            # 'unavailable' verdict, which does not pause. Only a checker that
            # cannot be loaded raises out here, and that is not a verdict: the
            # agent refuses to start without it, so this is one lost since.
            # Sending pauses, as _safe_to_resume keeps it paused.
            logger.error(
                "BLACKLIST GUARD: the blocklist check cannot be loaded (%s) - "
                "pausing all sending.",
                e,
            )
            if not self.agent._guard_logged.get("blacklist"):
                try:
                    self.agent.db.log_activity(
                        "BLACKLIST_GUARD",
                        f"Paused sending: the blocklist check cannot be loaded ({e})",
                        status="error",
                    )
                    self.agent._guard_logged["blacklist"] = True
                except sqlite3.Error as log_err:
                    logger.error("Could not log the blocklist pause: %s", log_err)
            return True
        if verdict["status"] != LISTED:
            self.agent._guard_logged.pop("blacklist", None)  # clear the latch
        if verdict["status"] == LISTED:
            listed = ", ".join(verdict["listed_on"])
            logger.error(
                f"BLACKLIST GUARD: {self.agent._sending_domain} is listed on "
                f"{listed} - pausing all sending."
            )
            if not self.agent._guard_logged.get("blacklist"):
                self.agent.db.log_activity(
                    "BLACKLIST_GUARD",
                    f"Paused sending: {self.agent._sending_domain} listed on {listed}",
                    status="error",
                )
                self.agent._guard_logged["blacklist"] = True
            return True
        return False

    _blacklist_guard_tripped = blacklist_guard_tripped

    def bounce_guard_tripped(self) -> bool:
        """Circuit breaker: True when the recent bounce rate is high enough to
        threaten sender reputation (mailbox providers want bounces < 2%). Once
        tripped, the agent stops sending until an operator intervenes."""
        if not self.agent.settings.bounce_guard_enabled:
            return False
        try:
            m = self.agent.db.get_deliverability_metrics()
        except Exception as e:
            # Fail CLOSED. This guard is the only thing between a bad list and a
            # burned sending domain; if we can't evaluate it (locked DB, schema
            # drift) we must not assume everything is fine and keep sending.
            logger.exception("BOUNCE GUARD could not read metrics")
            # Best-effort only: the failure being handled is usually a DB
            # problem, so this write can fail for the same reason. It must never
            # stop us returning the safe answer.
            if not self.agent._guard_logged.get("bounce_error"):
                try:
                    self.agent.db.log_activity(
                        "BOUNCE_GUARD_ERROR",
                        f"Pausing sends - cannot evaluate bounce rate: {e}",
                        status="error",
                    )
                    self.agent._guard_logged["bounce_error"] = True
                except sqlite3.Error as exc:
                    logger.debug("Could not log the bounce-guard failure: %s", exc)
            return True
        self.agent._guard_logged.pop("bounce_error", None)  # metrics readable again
        if m["recent_sent"] < self.agent.settings.bounce_guard_min_sends:
            # Not enough recent signal to judge. NOTE: this is not "healthy" -
            # see _safe_to_resume(), which must not treat it as recovery.
            self.agent._guard_logged.pop("bounce", None)
            return False
        if m["bounce_rate"] > self.agent.settings.max_bounce_rate:
            logger.error(
                f"BOUNCE GUARD tripped: bounce rate {m['bounce_rate']:.1%} exceeds "
                f"{self.agent.settings.max_bounce_rate:.1%} "
                f"({m['recent_bounced']}/{m['recent_sent']} in the last "
                f"{m['window_days']} days). Pausing all sends to protect reputation."
            )
            if not self.agent._guard_logged.get("bounce"):
                self.agent.db.log_activity(
                    "BOUNCE_GUARD",
                    f"Paused sending: bounce rate {m['bounce_rate']:.1%} "
                    f"> {self.agent.settings.max_bounce_rate:.1%} (last {m['window_days']}d)",
                    status="error",
                )
                self.agent._guard_logged["bounce"] = True
            return True
        self.agent._guard_logged.pop("bounce", None)  # recovered
        return False

    _bounce_guard_tripped = bounce_guard_tripped

    def maybe_apply_retention(self, dry_run: bool) -> None:
        """Run the data-retention job at most once per calendar day.

        Never on a dry run: a preview must not erase anything. A failure is
        logged and the day is not marked done, so the next loop retries. It
        is also recorded as an error, once a day: an erasure that fails is
        undone and tried again on every loop, and a job that never finishes
        would otherwise show only in the log file.
        """
        if dry_run or not self.agent.settings.retention_enabled:
            return
        today = clock.today_str()
        try:
            if self.agent.db.get_state("retention:last_run_date") == today:
                return
            from modules.retention import apply_retention, describe

            counts = apply_retention(self.agent.db, self.agent.settings)
            self.agent.db.set_state("retention:last_run_date", today)
            self.agent.db.log_activity(
                "RETENTION_APPLIED", describe(counts), status="info"
            )
            self.agent._guard_logged.pop("retention", None)
        except sqlite3.Error as e:
            logger.error("Retention run failed - will retry next loop: %s", e)
            if self.agent._guard_logged.get("retention") != today:
                try:
                    self.agent.db.log_activity(
                        "RETENTION_FAILED",
                        f"The retention run stopped on an error ({e}). What it "
                        "did not finish is tried again on every loop.",
                        status="error",
                    )
                    self.agent._guard_logged["retention"] = today
                except sqlite3.Error as log_err:
                    logger.error("Could not record RETENTION_FAILED (%s)", log_err)

    _maybe_apply_retention = maybe_apply_retention

    def first_contact_capacity(self) -> int | None:
        """How many waiting contacts the mailboxes can email within the
        first-contact limit: today's allowance across mailboxes times the
        sending days within it (decision L1.8). Today's allowance only, since
        warm-up may never reach its later steps. None while warm-up is off,
        which sets no daily limit.
        """
        if not self.agent.warmup:
            return None
        settings = self.agent.settings
        per_day = sum(
            self.agent.warmup.get_daily_allowance(account=k)
            for k in self.agent._account_keys
        )
        days = range(settings.first_contact_max_age_days)
        if settings.send_window_enabled:
            allowed = settings.get_send_window_days()
            today = clock.now()
            days = [d for d in days if (today + timedelta(days=d)).weekday() in allowed]
        return per_day * len(days)

    def collection_paused(self, backlog: int) -> str | None:
        """Why nothing more may be collected now, or None.

        A contact collected now that could not be emailed within the
        first-contact limit would only be held without a notice and erased.
        """
        reachable = self.first_contact_capacity()
        if reachable is not None and backlog >= reachable:
            return (
                f"{backlog} contacts are waiting, and the mailboxes can email "
                f"{reachable} within {self.agent.settings.first_contact_max_age_days} "
                "days of collection"
            )
        return None

    def apply_runtime_overrides(self) -> None:
        """Pull operator config changes in without a restart.

        Re-read at the top of every loop rather than at construction, because
        the whole point is that an operator can retune pacing or a threshold
        during a long run and have it take effect.

        Every value goes through config.validate_override, which rebuilds the
        model. This Settings does not set validate_assignment, so a bare
        `self.settings.max_bounce_rate = 99` would succeed silently and turn
        every validator in config.py into decoration. A rejected value is
        logged and skipped: one bad row must not stop the loop, and the
        previous good value stays in force.
        """
        base = getattr(self.agent, "_base_settings", None)
        if base is None:
            base = self.agent.settings
            self.agent._base_settings = base

        previous = self.agent.settings
        updated = effective_settings(base, self.agent.db, fallback=previous)
        self.agent.settings = updated

        for key in sorted(RUNTIME_EDITABLE):
            current = getattr(previous, key, None)
            value = getattr(updated, key, None)
            if current == value:
                continue
            logger.info("Config override applied: %s %r -> %r", key, current, value)
            self.agent._resync_settings_consumers(key)
            try:
                self.agent.db.log_activity(
                    "CONFIG_CHANGED", f"{key}: {current} -> {value}", status="warning"
                )
            except sqlite3.Error:
                pass  # the setting is already live; the log line is a nicety

    _apply_runtime_overrides = apply_runtime_overrides

    def resync_settings_consumers(self, key: str) -> None:
        """Push a changed setting into the objects that copied it at startup.

        Most runtime-editable settings are read straight off self.settings at
        the point of use, so rebuilding the model is enough. A few were handed
        to a long-lived helper as a plain value in __init__, and that copy does
        not move when self.settings is replaced - the override applies, the
        dashboard reports it applied, and the code doing the enforcing keeps
        using the old number.

        max_per_domain_per_day was the live instance of this: an operator
        lowering the per-domain cap to contain a bouncing domain would have been
        told the change took effect while DomainThrottle went on admitting sends
        at the old limit.
        """
        if key == "max_per_domain_per_day" and self.agent.domain_throttle:
            self.agent.domain_throttle.max_per_domain_per_day = (
                self.agent.settings.max_per_domain_per_day
            )

    _resync_settings_consumers = resync_settings_consumers

    def maybe_check_imap(self) -> None:
        """Run an IMAP reply/bounce check across every account's inbox if the
        configured interval has elapsed."""
        if not self.agent.imap_monitors:
            return
        now = time.time()
        interval = max(60, self.agent.settings.imap_check_interval_minutes * 60)
        if now - self.agent._last_imap_check < interval:
            return
        self.agent._last_imap_check = now
        any_ok = False
        try:
            for mon in self.agent.imap_monitors:
                try:
                    summary = mon.run_check()
                    if not summary or not summary.get("ok"):
                        # A failed check is NOT "no new mail". While IMAP is down
                        # nothing gets suppressed and no hot lead is escalated, so
                        # this must be visible on the dashboard, not just in a log.
                        err = (summary or {}).get("error") or "unknown error"
                        acct = (summary or {}).get("account") or "?"
                        logger.error(f"IMAP check FAILED for {acct}: {err}")
                        self.agent.db.log_activity(
                            "IMAP_ERROR",
                            f"Inbox check failed for {acct}: {err}. Bounces, "
                            f"unsubscribes and hot-lead alerts are NOT being "
                            f"processed while this persists.",
                            status="error",
                        )
                        continue
                    any_ok = True
                    if summary.get("total_processed"):
                        self.agent.db.log_activity(
                            "IMAP_CHECK",
                            f"bounces={summary['bounces']} "
                            f"unsubs={summary['unsubscribes']} replies={summary['replies']}",
                            status="info",
                        )
                    # Hand interested prospects to a human straight away.
                    self.agent._handle_interested_replies(
                        summary.get("reply_entries") or []
                    )
                except Exception as e:
                    logger.exception("IMAP check failed")
                    self.agent.db.log_activity(
                        "IMAP_ERROR", f"Inbox check crashed: {e}", status="error"
                    )
            if any_ok:
                self.agent._last_imap_ok = now
                self.agent._guard_logged.pop("imap_stale", None)  # inbox readable again
            elif self.agent._last_imap_ok:
                stale_h = (now - self.agent._last_imap_ok) / 3600
                if stale_h >= 6:
                    logger.error(
                        f"No successful IMAP check for {stale_h:.1f}h - "
                        "reply/bounce handling is blind."
                    )
                    # Latched to one entry per stale period: the operator needs
                    # to see this on the dashboard, not once per loop.
                    if not self.agent._guard_logged.get("imap_stale"):
                        self.agent.db.log_activity(
                            "IMAP_STALE",
                            f"No successful inbox check for {stale_h:.1f}h. "
                            "Bounces, opt-outs and hot leads are not being "
                            "processed - check the mailbox credentials.",
                            status="error",
                        )
                        self.agent._guard_logged["imap_stale"] = True
            else:
                # Never succeeded: retry sooner rather than burning a full interval.
                self.agent._last_imap_check = now - interval + 300
                # With no success to measure from, the alarm above never
                # fired: a login that never worked logged only IMAP_ERROR.
                if not self.agent._guard_logged.get("imap_stale"):
                    self.agent.db.log_activity(
                        "IMAP_STALE",
                        "No inbox check has succeeded since the agent started. "
                        "Bounces, opt-outs and hot leads are not being "
                        "processed - check the mailbox credentials.",
                        status="error",
                    )
                    self.agent._guard_logged["imap_stale"] = True
        finally:
            # A lead alert sends through the primary mailbox; release its login.
            try:
                self.agent.email_sender.close()
            except OSError as e:
                logger.debug("Could not close the SMTP connection cleanly: %s", e)

    _maybe_check_imap = maybe_check_imap

    def reply_is_interested(self, subject: str, body: str) -> bool:
        """Judge a reply. Deterministic keywords decide; the AI is only asked to
        break a tie when the keywords are inconclusive (and never blocks)."""
        from modules import reply_intent

        verdict = reply_intent.classify(subject, body)
        if verdict == reply_intent.INTERESTED:
            return True
        if verdict == reply_intent.NOT_INTERESTED:
            return False
        # Unclear -> optional AI opinion, failing closed (no alert) on error.
        # Off unless switched on: it sends the reply's text to a provider.
        if (
            self.agent.settings.ai_reply_classification_enabled
            and self.agent.ai_gen
            and getattr(self.agent.ai_gen, "is_configured", False)
        ):
            try:
                return (
                    self.agent.ai_gen.classify_reply_intent(
                        subject, reply_intent.strip_quoted(body)
                    )
                    is True
                )
            except (AttributeError, TypeError) as e:
                # Provider failures already come back as None (no alert); an
                # unusable subject/body is what can still raise.
                logger.debug(f"AI reply-intent check failed: {e}")
        return False

    _reply_is_interested = reply_is_interested

    def propose_reply_answers(self, entries: list) -> None:
        """At AUTONOMY_REPLY=approve, queue each reply for a person: the
        prospect's own words, the agent's reading of them and a suggested
        answer (modules/review_queue.py). Opt-outs and bounces never come
        here; they are applied before, without waiting for anyone."""
        from modules import reply_intent
        from modules.client_profile import PROFILE
        from modules.review_queue import level_for

        if level_for(self.agent.settings, "reply") != "approve":
            return
        own = {
            (a.get(k) or "").strip().lower()
            for a in self.agent._accounts
            for k in ("imap_username", "username")
        }
        for entry in entries:
            addr = (entry.get("email") or "").strip().lower()
            if not addr or addr in own:
                continue
            contact = self.agent.db.get_contact_by_email(addr)
            if not contact or not self.agent.db.has_been_emailed(contact["id"]):
                continue
            subject = (entry.get("subject") or "").strip()
            body = entry.get("body") or ""
            language = "it" if contact.get("language_choice") == "it" else "en"
            try:
                interested = self.agent._reply_is_interested(subject, body)
                self.agent.review_queue.propose(
                    contact["id"],
                    "reply",
                    subject if subject.lower().startswith("re:") else f"Re: {subject}",
                    PROFILE.reply_suggestion(language).replace(
                        "{{name_or_team}}",
                        html.escape(contact.get("name") or "")
                        or ("Cliente" if language == "it" else "Sir or Madam"),
                    ),
                    language=language,
                    evidence={
                        "organization": contact.get("organization"),
                        "their_words": reply_intent.strip_quoted(body)[:1500],
                        "their_subject": subject,
                        "reads_as_interested": bool(interested),
                        "company_country": contact.get("company_country"),
                        "role": contact.get("role"),
                        "language": language,
                    },
                )
            except sqlite3.Error as e:
                logger.error("Could not queue the answer to a reply: %s", e)

    def handle_interested_replies(self, entries: list) -> None:
        """Email the designated person for each newly-interested prospect."""
        from modules import reply_intent

        if not entries:
            return
        # A person answers: each reply from a company we emailed becomes a
        # proposal first, whatever the alert below does.
        self.propose_reply_answers(entries)
        recipients = self.agent.settings.get_lead_alert_recipients()
        if not recipients:
            # Don't fail silently: a prospect may have just said yes and there is
            # nobody configured to receive it.
            logger.error(
                "LEAD_ALERT_EMAIL is not configured - %d reply(ies) will "
                "not be escalated to a human",
                len(entries),
            )
            self.agent.db.log_activity(
                "LEAD_ALERT_UNCONFIGURED",
                f"{len(entries)} prospect reply(ies) received but no alert "
                f"recipient is configured (set LEAD_ALERT_EMAIL)",
                status="error",
            )
            return

        own = {
            (a.get("imap_username") or "").strip().lower() for a in self.agent._accounts
        }
        own |= {(a.get("username") or "").strip().lower() for a in self.agent._accounts}
        for entry in entries:
            addr = (entry.get("email") or "").strip().lower()
            if not addr or self.agent.db.was_lead_alerted(addr):
                continue
            # Only escalate people we actually emailed. Every inbound message
            # that isn't a bounce/unsubscribe/auto-reply lands here - newsletters,
            # suppliers, colleagues - and a phrase like "price list" in a vendor
            # mailshot would otherwise page a human, permanently (dedupe).
            if addr in own:
                continue
            if not self.agent.db.get_contact_by_email(addr):
                logger.debug(
                    f"Ignoring reply from unknown sender {addr} "
                    f"(not a contact we emailed)"
                )
                continue
            if not self.agent._reply_is_interested(
                entry.get("subject", ""), entry.get("body", "")
            ):
                continue
            try:
                if self.agent._send_lead_alert(entry, recipients):
                    self.agent.db.record_lead_alert(
                        addr,
                        notified_to=", ".join(recipients),
                        # Their own words: not our email quoted back.
                        reply_excerpt=reply_intent.strip_quoted(entry.get("body", "")),
                    )
            except (OSError, ValueError, sqlite3.Error) as e:
                # OSError covers the SMTP layer (smtplib.SMTPException is one),
                # ValueError a header create_email refuses (an odd address), and
                # sqlite3.Error the dedupe write. Contained per entry so one
                # unreachable alert cannot stop the remaining replies from
                # being escalated. Only the internal alert is affected - no
                # prospect email is sent or re-sent on this path.
                logger.error(f"Could not send lead alert for {addr}: {e}")

    _handle_interested_replies = handle_interested_replies

    def send_lead_alert(self, entry: dict, recipients: list) -> bool:
        """Send the internal 'interested lead' notification. Returns True on send."""
        import html as _html

        from modules import clock, reply_intent
        from modules.email_sender import name_for_header

        addr = (entry.get("email") or "").strip()
        contact = self.agent.db.get_contact_by_email(addr) or {}
        org = (contact.get("organization") or "").strip() or "Unknown company"
        person = (contact.get("name") or entry.get("from") or addr).strip()
        their_reply = (
            reply_intent.strip_quoted(entry.get("body", ""))[:1500]
            or "(no readable text - see the mailbox)"
        )

        subject = f"Interested lead: {name_for_header(org, 'Unknown company')} <{addr}>"
        rows = [
            ("Company", org),
            ("Contact", person),
            ("Email", addr),
            ("Replied to", entry.get("subject", "") or "-"),
            ("When", entry.get("date", "") or clock.now().strftime("%Y-%m-%d %H:%M")),
        ]
        rows_html = "".join(
            f'<tr><td style="padding:4px 12px 4px 0;color:#666;">{k}</td>'
            f'<td style="padding:4px 0;"><strong>{_html.escape(str(v))}</strong></td></tr>'
            for k, v in rows
        )
        html_body = (
            '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;color:#222;">'
            f"<p><strong>{_html.escape(org)}</strong> replied with interest "
            f"to our outreach.</p>"
            f'<table style="border-collapse:collapse;margin:14px 0;">{rows_html}</table>'
            '<p style="margin:0 0 6px;color:#666;">Their reply:</p>'
            '<blockquote style="margin:0;padding:10px 14px;background:#f5f5f2;'
            'border-left:3px solid #C6B128;white-space:pre-wrap;">'
            f"{_html.escape(their_reply)}</blockquote>"
            f'<p style="margin-top:16px;">Reply directly to '
            f'<a href="mailto:{_html.escape(addr)}">{_html.escape(addr)}</a> to take over.</p>'
            '<p style="font-size:12px;color:#999;">Sent automatically by the outreach agent. '
            "Follow-ups to this contact have stopped.</p></div>"
        )
        plain_body = (
            f"{org} replied with interest to our outreach.\n\n"
            + "\n".join(f"{k}: {v}" for k, v in rows)
            + f"\n\nTheir reply:\n{their_reply}\n\n"
            f"Reply directly to {addr} to take over.\n"
            "Sent automatically by the outreach agent."
        )

        ok_any = False
        for to in recipients:
            msg = self.agent.email_sender.create_email(
                to, subject, html_body, plain_body
            )
            ok, info = self.agent.email_sender.send_email(msg)
            if ok:
                ok_any = True
                logger.info(f"Hot-lead alert sent to {to} for {addr}")
            else:
                logger.error(f"Hot-lead alert to {to} failed: {info}")
        if ok_any:
            self.agent.db.log_activity(
                "LEAD_ALERT",
                f"Interested reply from {addr} ({org}) -> "
                f"notified {', '.join(recipients)}",
                status="info",
            )
        return ok_any

    _send_lead_alert = send_lead_alert

    def discovery_focus_hint(self) -> str:
        """Summarise what's working (and what to avoid) to inform the
        Strategist. Returns '' when there is no useful signal yet."""
        if self.agent.learner:
            try:
                summary = self.agent.learner.focus_summary()
                if summary:
                    return summary
            except (KeyError, TypeError) as e:
                # The learner swallows its own DB read failure and returns empty
                # stats, so only a malformed aggregate can raise out of here.
                logger.debug(f"Learner summary failed: {e}")
        try:
            rows = self.agent.db.get_all_searches(50)
            productive = sorted(
                rows, key=lambda r: r.get("results_count") or 0, reverse=True
            )
            top = [
                r["search_query"]
                for r in productive
                if (r.get("results_count") or 0) > 0
            ][:5]
            if top:
                return "; ".join(top)
        except sqlite3.Error as e:
            logger.debug(f"Could not build discovery focus hint: {e}")
        return ""

    _discovery_focus_hint = discovery_focus_hint

    def queries_for_a_standalone_run(self) -> tuple[list, dict]:
        """What a `python main.py run` should search when nothing was passed.

        Returns (queries, search_ids). ``search_ids`` maps query text to an
        already-queued search_log row so those rows are COMPLETED rather than
        duplicated - without it each queued query would mint a second row and
        leave the original hung at 'in_progress' forever, which is the exact
        trap the continuous loop already documents.
        """
        try:
            pending = self.agent.db.get_pending_searches(limit=5)
        except sqlite3.Error as e:
            logger.warning("Could not read queued searches (%s)", e)
            pending = []

        if pending:
            logger.info("Running %d queued search(es).", len(pending))
            for p in pending:
                self.agent.db.update_search(p["id"], status="in_progress")
            return (
                [p["search_query"] for p in pending],
                {p["search_query"]: p["id"] for p in pending},
            )

        if self.agent.query_generator:
            try:
                existing = {
                    s["search_query"] for s in self.agent.db.get_all_searches(500)
                }
                before = self.agent.db.searched_among(
                    self.agent.query_generator.phrase_queries()
                )
            except sqlite3.Error:
                existing, before = set(), set()
            seed_w, country_w = (
                self.agent.learner.weights() if self.agent.learner else (None, None)
            )
            avoid = self.agent.learner.excluded_terms() if self.agent.learner else None
            generated = self.agent.query_generator.next_batch(
                count=5,
                exclude=existing,
                seed_weights=seed_w,
                country_weights=country_w,
                avoid_terms=avoid,
                searched_before=before,
            )
            if generated:
                logger.info(
                    "No queries configured - generated %d heuristically "
                    "(free, learning-weighted).",
                    len(generated),
                )
                return generated, {}

        return [], {}

    _queries_for_a_standalone_run = queries_for_a_standalone_run

    def print_summary(self) -> None:
        """Print a summary of the session."""
        s = self.agent._stats
        logger.info(f"\n{'=' * 50}")
        logger.info("SESSION SUMMARY")
        logger.info(f"{'=' * 50}")
        logger.info(f"  Searches executed  : {s['searches']}")
        logger.info(f"  Contacts found     : {s['contacts']}")
        logger.info(f"  Leads scored       : {s['scored']}")
        logger.info(f"  Emails verified    : {s['verified']}")
        logger.info(f"  Low score skipped  : {s['low_score_skipped']}")
        logger.info(f"  Off-target skipped : {s['irrelevant_skipped']}")
        logger.info(f"  Domain skipped     : {s['domain_skipped']}")
        logger.info(f"  Warmup skipped     : {s['warmup_skipped']}")
        logger.info(f"  Emails sent        : {s['emails_sent']}")
        logger.info(f"  Emails failed      : {s['emails_failed']}")
        logger.info(f"  Retries queued     : {s['retries_queued']}")
        logger.info(f"{'=' * 50}")

    _print_summary = print_summary

    def run_single(
        self,
        queries: list[str] | None = None,
        dry_run: bool = False,
        _from_continuous: bool = False,
        search_ids: dict | None = None,
    ) -> dict | None:
        """Run a single batch: process all queries, send to all found contacts, then exit.

        `search_ids` maps query text -> an already-queued search_log row id, so
        a queued discovery search completes ITS OWN row instead of minting a
        duplicate and leaving the original hung at 'in_progress' forever."""
        try:
            return self._run_batch(queries, dry_run, _from_continuous, search_ids)
        finally:
            # A one-off run takes its memory with it: the last chance to write
            # what it knows about a refused send whose result met a lock.
            if not _from_continuous and not dry_run:
                self.agent.db.write_kept_send_results()

    def _run_batch(
        self,
        queries: list[str] | None,
        dry_run: bool,
        _from_continuous: bool,
        search_ids: dict | None,
    ) -> dict | None:
        """run_single's batch."""
        queries = queries or self.agent.settings.get_target_queries()
        if not queries and not _from_continuous:
            # Same sources the continuous loop uses, in the same order. A single
            # run used to hard-fail here whenever TARGET_QUERIES was empty -
            # ignoring searches already queued from the dashboard's Add Query
            # box AND the heuristic generator that continuous mode falls back
            # to. So the identical installation worked continuously and refused
            # to run once, which reads as the agent being broken.
            queries, search_ids = self.agent._queries_for_a_standalone_run()
        if not queries:
            logger.error(
                "Nothing to search: no --query, no TARGET_QUERIES, no queued "
                "searches, and heuristic discovery is unavailable."
            )
            return None

        # Reset stats for this batch
        self.agent._stats = {
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

        mode = "DRY RUN" if dry_run else "LIVE"
        self.agent.db.log_activity(
            "AGENT_START", f"Single batch mode ({mode}), {len(queries)} queries"
        )
        logger.info(
            f"=== Agent starting in {mode} mode with {len(queries)} queries ==="
        )

        # Log warmup status
        if self.agent.warmup:
            ws = self.agent.warmup.get_warmup_status()
            logger.info(
                f"Warmup: Day {ws['day_number']}, "
                f"Allowance: {ws['daily_allowance']}, "
                f"Remaining: {ws['remaining']}"
            )

        # Only arm the flag when this is a standalone run. When called from
        # run_continuous, that loop owns _running - re-setting it here silently
        # revived a stop that arrived mid-iteration, so Stop was a no-op unless
        # pressed during the inter-loop sleep.
        if not _from_continuous:
            self.agent._running = True
        elif not self.agent._running:
            # Stop landed after run_continuous checked but before we were called.
            logger.info("Stop requested - skipping batch")
            return self.agent._stats

        if not _from_continuous:
            self.agent.refresh_lists_if_changed()
            self.agent._apply_runtime_overrides()
            # As the continuous loop does at the top of every pass.
            if not dry_run:
                self.agent._reconcile_interrupted_sends()

        # Read the inbox first: without this a single batch run suppresses
        # nothing, honours no opt-out, and leaves the bounce guard with no data.
        # (Self-throttling on _last_imap_check, so this is cheap.)
        self.agent._maybe_check_imap()

        for i, query in enumerate(queries, 1):
            if not self.agent._running:
                break

            logger.info(f"\n-- Query {i}/{len(queries)}: {query} --")

            # Phase 1: Search & Extract (wrapped in try/except to prevent crash)
            try:
                contacts = self.agent.search.search_and_extract(
                    query, search_id=(search_ids or {}).get(query)
                )
            except Exception as e:
                # Deliberately broad: this reaches a third-party search client,
                # the open web and an HTML parser, so the failure surface is not
                # enumerable - and one bad query must never abort the remaining
                # ones. logger.exception keeps the traceback so a real defect in
                # here is still diagnosable instead of being flattened to a line.
                logger.exception(f"Search failed for query '{query}'")
                self.agent.db.log_activity(
                    "SEARCH_ERROR", f"Query '{query}' crashed: {e}", status="error"
                )
                self.agent._stats["searches"] += 1
                continue

            self.agent._stats["searches"] += 1
            self.agent._stats["contacts"] += len(contacts)

            if not contacts:
                logger.info(f"No contacts found for: {query}")
                continue

            logger.info(f"Found {len(contacts)} new contacts")

            # Phase 2: Score & verify contacts
            qualified = self.agent._qualify_contacts(contacts)
            # Phase 3: Email each qualified contact
            self.agent._email_contacts(qualified, dry_run)

        # Phase 3.5: Drain the backlog of previously-discovered 'pending'
        # contacts. Freshly discovered contacts are emailed above, but
        # contacts found on earlier runs sit in the DB unsent; this makes
        # sure real leads actually get contacted. In continuous mode this is
        # driven once per loop instead (see run_continuous).
        if not _from_continuous and self.agent._running:
            self.agent._process_pending_contacts(dry_run)

        # Phase 3.6: Process due follow-up sequence steps.
        if not _from_continuous and self.agent._running:
            self.agent._process_sequences(dry_run)

        # Phase 4: Process retry queue. Like the phases above, left to the
        # continuous loop when it called us: it runs its own retry step, and
        # running both claimed the queue twice per pass.
        if self.agent.retry_queue and not dry_run and not _from_continuous:
            self.agent._process_retries()

        # Phase 5: data retention. The continuous loop runs its own; a single
        # run from the CLI or dashboard must not be the one way it never runs.
        if not _from_continuous:
            self.agent._maybe_apply_retention(dry_run)

        # Only reset _running if not called from continuous mode
        if not _from_continuous:
            self.agent._running = False
        self.agent.db.log_activity(
            "AGENT_COMPLETE", f"Batch complete: {self.agent._stats}", status="info"
        )
        self.agent._print_summary()
        return None

    def run_continuous(
        self,
        queries: list[str] | None = None,
        dry_run: bool = False,
        loop_delay: float = 300.0,
    ) -> None:
        """Run continuously: process pending searches, discover new ones if empty."""
        mode = "DRY RUN" if dry_run else "LIVE"
        self.agent.db.log_activity(
            "AGENT_START", f"Continuous mode ({mode}), loop delay: {loop_delay}s"
        )
        logger.info(f"=== Agent starting in CONTINUOUS {mode} mode ===")

        self.agent._running = True
        loop_count = 0
        base_topics = queries or self.agent.settings.get_target_queries()

        try:
            while self.agent._running:
                loop_count += 1
                logger.info(f"\n{'=' * 50}")
                logger.info(f"Loop #{loop_count}")
                logger.info(f"{'=' * 50}")

                # Wrap each iteration so a transient error never kills the loop.
                try:
                    # Before anything else this iteration: pick up config the
                    # operator changed from the dashboard since the last pass.
                    self.agent._apply_runtime_overrides()
                    # And the client / competitor spreadsheets SUPPLECO edits by hand.
                    self.agent.refresh_lists_if_changed()
                    # And any send a stopped process left 'preparing', before
                    # this pass can reach its contact. A dry run settles nothing.
                    if not dry_run:
                        self.agent._reconcile_interrupted_sends()

                    # Reply / bounce detection via IMAP (throttled), before
                    # anything is sent: after a restart, the opt-outs, requests
                    # and bounces that came in while the agent was off must be
                    # applied first, and the bounces must count before a pause
                    # is reconsidered below. Deliberately NOT gated on the
                    # pause: reading the inbox is how bounces and opt-outs get
                    # processed, which is exactly what a pause needs to keep
                    # doing.
                    if self.agent._running:
                        self.agent._maybe_check_imap()

                    # 0. If sending was paused by a guard, re-check whether the
                    #    condition has cleared (the bounce rate ages out of the
                    #    7-day window, a blocklist delisting takes effect, a
                    #    locked DB frees up). Without this the pause is sticky
                    #    and only a restart would ever resume sending.
                    if self.agent._sending_paused and self.agent._safe_to_resume():
                        self.agent._sending_paused, self.agent._pause_reason = (
                            False,
                            None,
                        )
                        logger.info("Reputation guards clear - resuming sending.")
                        self.agent.db.log_activity(
                            "SENDING_RESUMED",
                            "Reputation guards cleared; sending resumed",
                            status="info",
                        )

                    # 1. Drain the verified-lead backlog FIRST (highest score
                    #    first), so the day's send allowance goes to real queued
                    #    leads before fresh discovery can consume it.
                    if self.agent._running and not self.agent._sending_paused:
                        self.agent._process_pending_contacts(dry_run)

                    # 2. Only hunt for MORE work if any account can still send
                    #    today; once every mailbox is capped, don't waste scraping.
                    #    _running is part of this: a stop during step 1 must not be
                    #    followed by a round of scraping and LLM calls.
                    can_send_more = (
                        self.agent._running
                        and (not self.agent._sending_paused)
                        and (
                            (not self.agent.warmup)
                            or any(
                                self.agent.warmup.can_send(account=k)
                                for k in self.agent._account_keys
                            )
                        )
                    )
                    if not can_send_more:
                        logger.info(
                            "Daily send limit reached - holding discovery "
                            "until the warmup window resets."
                        )
                    else:
                        pending = self.agent.db.get_pending_searches(limit=5)
                        backlog = self.agent.db.count_pending_contacts()
                        # Neither discovery nor a search already queued while
                        # the backlog could not be emailed in time (L1.8).
                        paused = self.collection_paused(backlog)
                        if paused:
                            logger.info(f"Collection paused: {paused}.")
                            if not self.agent._guard_logged.get("collection"):
                                self.agent.db.log_activity(
                                    "COLLECTION_PAUSED", paused, status="warning"
                                )
                                self.agent._guard_logged["collection"] = True
                            pending = []
                        elif (
                            not pending
                            and backlog >= self.agent.settings.max_pending_backlog
                        ):
                            logger.info(
                                f"Pending backlog is {backlog} "
                                f"(>= {self.agent.settings.max_pending_backlog}) - skipping "
                                "discovery to work through the backlog first."
                            )
                        elif not pending:
                            logger.info(f"Queue is empty (backlog: {backlog}).")
                            self.agent._guard_logged.pop("collection", None)

                            # Autonomously discover new targets.
                            new_queries = []
                            source = "discovery"

                            # Strategist (LLM): data-informed European discovery queries.
                            if (
                                self.agent.settings.ai_strategist_enabled
                                and self.agent.ai_gen
                                and self.agent.ai_gen.is_configured
                            ):
                                hint = self.agent._discovery_focus_hint()
                                new_queries = (
                                    self.agent.ai_gen.suggest_discovery_queries(
                                        count=5, focus_hint=hint
                                    )
                                )
                                if new_queries:
                                    source = "strategist"

                            # Legacy AI company discovery, if the strategist yielded nothing.
                            if (
                                not new_queries
                                and self.agent.ai_gen
                                and self.agent.ai_gen.is_configured
                                and base_topics
                            ):
                                topic = random.choice(base_topics)
                                logger.info(
                                    f"AI discovery triggered for topic: '{topic}'"
                                )
                                new_queries = self.agent.ai_gen.generate_companies(
                                    topic=topic, count=5
                                )
                                if new_queries:
                                    source = "ai_generator"
                                else:
                                    logger.info(
                                        "AI returned no companies - falling back to free discovery."
                                    )

                            # Free fallback: heuristic query generation (learning-weighted).
                            if not new_queries and self.agent.query_generator:
                                existing = {
                                    s["search_query"]
                                    for s in self.agent.db.get_all_searches(500)
                                }
                                before = self.agent.db.searched_among(
                                    self.agent.query_generator.phrase_queries()
                                )
                                seed_w, country_w = (
                                    self.agent.learner.weights()
                                    if self.agent.learner
                                    else (None, None)
                                )
                                # Terms proven empty are skipped outright, not
                                # merely down-weighted - with a small re-probe
                                # chance so the skip cannot ratchet shut.
                                avoid = (
                                    self.agent.learner.excluded_terms()
                                    if self.agent.learner
                                    else None
                                )
                                new_queries = self.agent.query_generator.next_batch(
                                    count=5,
                                    exclude=existing,
                                    seed_weights=seed_w,
                                    country_weights=country_w,
                                    avoid_terms=avoid,
                                    searched_before=before,
                                )
                                if new_queries:
                                    logger.info(
                                        f"Heuristic discovery generated {len(new_queries)} "
                                        "queries (free, learning-weighted)."
                                    )

                            if new_queries:
                                for q in new_queries:
                                    self.agent.db.log_search(
                                        q, source=source, status="pending"
                                    )
                                logger.info(
                                    f"Added {len(new_queries)} new targets to the queue."
                                )
                                pending = self.agent.db.get_pending_searches(limit=5)
                            else:
                                logger.info("No new discovery queries generated.")

                        # Process the queue (search + email freshly-found contacts).
                        # Re-check _running: discovery above can take minutes, and
                        # without this a stop mid-discovery still sent a full batch.
                        if pending and self.agent._running:
                            queries_to_run = [p["search_query"] for p in pending]
                            for p in pending:
                                self.agent.db.update_search(
                                    p["id"], status="in_progress"
                                )
                            self.agent.run_single(
                                queries_to_run,
                                dry_run,
                                _from_continuous=True,
                                # Complete the queued rows themselves - without
                                # this each query minted a duplicate row and the
                                # queued one hung at 'in_progress' forever.
                                search_ids={
                                    p["search_query"]: p["id"] for p in pending
                                },
                            )

                    # 4b. Process due follow-up sequence steps.
                    #     Honour _sending_paused here too. These paths used to
                    #     re-derive safety per item via _bounce_guard_tripped(),
                    #     which returns False when recent_sent is below
                    #     bounce_guard_min_sends - and pausing DRAINS that 7-day
                    #     window toward zero. So within a week the guard reported
                    #     "not tripped" for lack of data and follow-ups and
                    #     retries flowed again to the very list that caused the
                    #     bounces, while the dashboard still showed "paused".
                    if self.agent._running and not self.agent._sending_paused:
                        self.agent._process_sequences(dry_run)

                    # 5. Process any due retries
                    if (
                        self.agent.retry_queue
                        and not dry_run
                        and self.agent._running
                        and not self.agent._sending_paused
                    ):
                        self.agent._process_retries()

                    # 6. Data retention, at most once a day - after this
                    # loop's sends, so it can never hold them up.
                    self.agent._maybe_apply_retention(dry_run)
                except Exception as e:
                    # Never let one bad iteration crash the continuous loop -
                    # nor its record: while another writer still holds the
                    # database, the LOOP_ERROR write fails too, and that
                    # second error stopped the agent. The log file has it.
                    logger.exception("Loop #%s failed", loop_count)
                    # First undo what the failed pass wrote on the agent's own
                    # connection and did not commit: it holds the write lock
                    # against every other connection, the LOOP_ERROR below
                    # included, and the next commit would save it half-done.
                    try:
                        self.agent.db.rollback_thread_conn()
                    except sqlite3.Error as undo_err:
                        logger.error(
                            "Could not undo the failed pass's writes (%s)", undo_err
                        )
                    try:
                        self.agent.db.log_activity(
                            "LOOP_ERROR", f"Loop #{loop_count}: {e}", status="error"
                        )
                    except sqlite3.Error as log_err:
                        logger.error("Could not record LOOP_ERROR (%s)", log_err)

                if self.agent._running:
                    delay = self.agent._next_loop_delay(loop_delay)
                    if delay > loop_delay:
                        logger.info(
                            "Outside the send window - sleeping %.0fs "
                            "(next inbox check or window open, whichever is "
                            "sooner) instead of polling every %.0fs...",
                            delay,
                            loop_delay,
                        )
                    else:
                        logger.info(f"Waiting {delay}s before next loop...")
                    self.agent._interruptible_sleep(delay)

        except KeyboardInterrupt:
            logger.info("\nAgent stopped by user (Ctrl+C)")
            self.agent.db.log_activity(
                "AGENT_STOPPED", "Stopped by user (KeyboardInterrupt)"
            )
        finally:
            self.agent._running = False
            # As a one-off run does: only a process that died leaves a refused
            # send to be settled as one that may have gone.
            if not dry_run:
                self.agent.db.write_kept_send_results()
