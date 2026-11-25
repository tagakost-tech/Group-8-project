"""Core agent engine - Coordinating Facade for autonomous outreach loop.

Orchestrates search, contact extraction, email dispatch, and logging.
Preserves 100% backward compatibility for all methods and attributes by
coordinating 4 pipeline stages:
- ExclusionManager (modules/exclusion_manager.py)
- DispatchPipeline (modules/dispatch_pipeline.py)
- BacklogDrainer (modules/backlog_draining.py)
- LoopOrchestrator (modules/loop_orchestration.py)
"""

from __future__ import annotations

import importlib
import logging
import sqlite3

from config import Settings
from modules import clock, microsoft_signin
from modules.backlog_draining import BacklogDrainer
from modules.database import Database, set_erasure_key
from modules.dispatch_pipeline import DispatchPipeline
from modules.email_sender import EmailSender
from modules.exclusion_manager import ExclusionManager
from modules.file_handler import FileHandler
from modules.loop_orchestration import LoopOrchestrator
from modules.optout_spool import replay_optout_spool
from modules.search import SearchModule
from modules.unsubscribe import UnsubscribeHandler

logger = logging.getLogger(__name__)


class GuardUnavailable(RuntimeError):
    """A component that limits, checks or stops sending could not be loaded.

    The settings switch it on, so the agent does not start without it. A
    failed import means a broken install (a package missing after an update,
    a file of the agent lost); the agent used to log one warning and send
    with that guard off.
    """


class OutreachAgent:
    """Autonomous B2B Outreach Agent.

    A coordinating facade over four stage classes (ExclusionManager,
    DispatchPipeline, BacklogDrainer, LoopOrchestrator). It keeps every method
    and attribute the pre-split agent had, with the same signatures, and the
    stages call each other through it (self.agent._method), so a patch on the
    agent applies. Send behaviour has changed since the split, deliberately;
    the module-level names the old agent.py imported are no longer importable
    from here.
    """

    def __init__(self, settings: Settings):
        self._base_settings = settings
        self.settings = settings
        # Set the app-wide timezone (daily caps + timestamps) before any module
        # that stamps a time is constructed below.
        clock.set_timezone(settings.app_timezone)
        set_erasure_key(settings.erasure_key)

        self._running = False
        self._last_imap_check = 0.0
        self._last_imap_ok = 0.0  # last SUCCESSFUL inbox scan (staleness alarm)
        # Set when a reputation guard trips. Sending stops; the loop keeps
        # running so replies, bounces and opt-outs are still processed.
        self._sending_paused = False
        self._pause_reason = None
        # Latches so a guard that stays tripped logs once, not once per check.
        self._guard_logged = {}
        # Recipient domains bouncing too hard to keep emailing, recomputed once
        # per send batch by _refresh_domain_backoff.
        self._domain_backoff = {}
        # Stamped by _process_contact just before each dispatch, so a send
        # loop can tell a refusal (nothing went out) from an attempt.
        self._last_dispatch = 0
        self._stats = {
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

        # Initialize core components
        self.file_handler = FileHandler()
        self.file_handler.ensure_directories()

        self.db = Database(settings.get_abs_db_path())
        replay_optout_spool(
            self.db,
            resolve=UnsubscribeHandler(
                self.db, settings.unsubscribe_secret
            ).verify_token,
        )

        self.search = SearchModule(
            db=self.db,
            rate_limit=settings.scrape_rate_limit,
            max_results=settings.max_results_per_query,
            blocked_prefixes=settings.get_blocked_prefixes(),
        )

        # Sending accounts (primary + any extras from SENDER_ACCOUNTS), rotated
        # to raise total daily volume; each mailbox keeps its own warmup ramp.
        self._accounts = settings.get_sender_accounts()
        # One app signs every Microsoft 365 mailbox in, each under its own
        # name; None when Microsoft sign-in is not set up.
        self.microsoft_signin = microsoft_signin.from_settings(settings)
        self.senders = []  # list of (account_key, sender_name, EmailSender)
        for i, acct in enumerate(self._accounts):
            es = EmailSender(
                db=self.db,
                smtp_host=acct["smtp_host"],
                smtp_port=acct["smtp_port"],
                smtp_username=acct["username"],
                smtp_password=acct["password"],
                sender_name=acct["sender_name"],
                company_name=settings.company_name,
                sender_title=acct.get("sender_title", ""),
                sender_signature=acct.get("sender_signature", ""),
                signin=self.microsoft_signin if acct.get("microsoft") else None,
            )
            key = "" if i == 0 else acct["username"]  # primary keeps "" (back-compat)
            self.senders.append((key, acct["sender_name"], es))
        self._account_keys = [k for (k, _, _) in self.senders]
        # Sending domain + lazily-built blocklist checker (see _blacklist_guard_tripped)
        self._sending_domain = (
            settings.smtp_username.split("@")[-1].lower()
            if "@" in settings.smtp_username
            else ""
        )
        self._bl_checker = None
        self.email_sender = self.senders[0][2]  # primary: Direct Send, alerts
        self._rr = 0  # round-robin cursor
        if len(self.senders) > 1:
            logger.info(
                f"Sending across {len(self.senders)} accounts "
                f"(rotated to raise daily volume)."
            )

        # Saved-version stamps of the first-email templates, so an edit is
        # noticed. Noted BEFORE the reads: a save between the two looks new at
        # the next send.
        self._template_stamps = self.file_handler.template_stamps()
        self._template_failed = None

        # Load email template
        self._email_template = self.file_handler.get_email_template()
        if not self._email_template:
            logger.warning("Email template not found - using default template")
            self._email_template = self._default_template()

        # The client's Italian first email, for recipients ITALIAN_COUNTRIES lists.
        # Without it they get the English one, whole: said once, here. A file
        # of only white space counts as missing, as a reload and the dashboard
        # count it, or the footer guard would refuse every Italian contact.
        italian = self.file_handler.get_email_template("email_template_it.html")
        self._email_template_it = italian if italian and italian.strip() else None
        if not self._email_template_it:
            logger.warning(
                "templates/email_template_it.html not found - Italian "
                "recipients will get the English first email."
            )

        # A/B variants: body templates + subject lines.
        self._template_variants = self.file_handler.get_template_variants()
        self._subject_variants = settings.get_email_subjects()

        # The Company Profile the first email says is attached. Looked for
        # again at each first email, so a file put in place needs no restart.
        self._pdf_path = self.file_handler.get_pdf_path(settings.pdf_path)
        if not self._pdf_path and settings.attach_profile:
            logger.warning(
                f"PDF presentation not found at: {settings.pdf_path}. First "
                f"emails are refused until it is in place (ATTACH_PROFILE is on)."
            )

        # Instantiate pipeline stage managers
        self.exclusion_manager = ExclusionManager(self)
        self.dispatch_pipeline = DispatchPipeline(self)
        self.backlog_drainer = BacklogDrainer(self)
        self.loop_orchestrator = LoopOrchestrator(self)

        # Proposals waiting for, and decided by, a person (modules/review_queue.py).
        from modules.review_queue import ReviewQueue

        self.review_queue = ReviewQueue(self.db, self.settings.review_daily_cap)

        # The guards and the extras. A guard that cannot be loaded stops here.
        self._init_v2_modules()
        self._apply_runtime_overrides()

    def _init_v2_modules(self):
        """Build the guards and the extras the settings switch on.

        A guard limits, checks or stops sending; if one cannot be imported
        this raises GuardUnavailable and no agent is built. An extra only
        adds something, and the agent runs without it, saying so.
        """
        self.lead_scorer = None
        self.relevance_filter = None
        self.email_verifier = None
        self.warmup = None
        self.domain_throttle = None
        self.retry_queue = None
        self.unsubscribe_handler = None
        self.analytics = None
        self.sequences = None
        self.sequence_id = None
        self.imap_monitor = None
        self.query_generator = None
        self.client_register = None
        self.competitor_register = None
        # Saved-version stamps of the list files, so an edit is noticed.
        self._list_stamps: dict = {}
        self._list_failed: dict = {}
        self.learner = None

        # Lead Scoring
        if self.settings.lead_scoring_enabled:
            try:
                from modules.lead_scoring import LeadScorer

                self.lead_scorer = LeadScorer()
                logger.info("Lead scoring: ENABLED")
            except ImportError as e:
                # A guard, not an extra: it drops every lead that scores
                # under MIN_LEAD_SCORE_TO_EMAIL before any email.
                raise self._guard_unavailable(
                    "Lead scoring (drops leads under the minimum score)", e
                ) from e

        # Relevance Filter (Phase 1: target-vertical + European gate)
        if self.settings.relevance_filter_enabled:
            try:
                from modules.relevance import RelevanceFilter

                self.relevance_filter = RelevanceFilter(
                    verticals=self.settings.get_relevance_verticals(),
                    require_vertical=self.settings.relevance_require_vertical,
                )
                logger.info("Relevance filter: ENABLED")
            except ImportError as e:
                raise self._guard_unavailable(
                    "The relevance filter (drops off-target companies)", e
                ) from e

        # Known competitors (never pitch contract manufacturing to a rival).
        self.load_competitor_register()

        # Active clients (never cold-email an existing customer)
        self.load_client_register()

        # Email Verification
        if self.settings.verify_emails:
            try:
                from modules.verification import EmailVerifier

                self.email_verifier = EmailVerifier()
                logger.info("Email verification: ENABLED")
            except ImportError as e:
                raise self._guard_unavailable(
                    "The email verifier (drops addresses that cannot receive mail)",
                    e,
                ) from e

        # Warmup Scheduler
        if self.settings.warmup_enabled:
            try:
                from modules.warmup import WarmupScheduler

                self.warmup = WarmupScheduler(
                    db=self.db,
                    warmup_enabled=self.settings.warmup_enabled,
                    warmup_start_daily=self.settings.warmup_start_daily,
                    warmup_increment=self.settings.warmup_increment,
                    warmup_max_daily=self.settings.warmup_max_daily,
                )
                logger.info("Warmup scheduler: ENABLED")
            except ImportError as e:
                raise self._guard_unavailable(
                    "The warm-up scheduler (the daily sending limit)", e
                ) from e

        # Domain Throttle
        try:
            from modules.domain_throttle import DomainThrottle

            self.domain_throttle = DomainThrottle(
                db=self.db,
                max_per_domain_per_day=self.settings.max_per_domain_per_day,
            )
            logger.info("Domain throttling: ENABLED")
        except ImportError as e:
            raise self._guard_unavailable(
                "The domain throttle (the daily limit per company)", e
            ) from e

        # Retry Queue
        if self.settings.retry_enabled:
            try:
                from modules.retry_queue import RetryQueue

                self.retry_queue = RetryQueue(
                    db=self.db,
                    max_retries=self.settings.retry_max_attempts,
                    base_delay_minutes=self.settings.retry_base_delay_minutes,
                )
                logger.info("Retry queue: ENABLED")
            except ImportError as e:
                # An extra, not a guard: without it a failed first email is
                # not tried again. Nothing more is sent, and a retry passes
                # the same send checks as a first email once it is back.
                self._extra_unavailable("The retry queue", e)

        # Unsubscribe Handler
        try:
            from modules.unsubscribe import UnsubscribeHandler

            self.unsubscribe_handler = UnsubscribeHandler(
                db=self.db,
                secret_key=self.settings.unsubscribe_secret,
            )
            logger.info("Unsubscribe handler: ENABLED")
        except ImportError as e:
            raise self._guard_unavailable(
                "The unsubscribe handler (makes and reads opt-out links)", e
            ) from e

        # Blocklist guard. Its checker is built at the first check, where a
        # failed import used to read as "not listed".
        if self.settings.blacklist_check_enabled and self._sending_domain:
            try:
                importlib.import_module("modules.blacklist")
                importlib.import_module("dns.resolver")
            except ImportError as e:
                raise self._guard_unavailable(
                    "The blocklist check (pauses sending when the sending "
                    "domain is listed as spam)",
                    e,
                ) from e

        # Analytics
        if self.settings.open_tracking_enabled:
            try:
                from modules.analytics import Analytics

                self.analytics = Analytics(db=self.db)
                logger.info("Open tracking: ENABLED")
            except ImportError as e:
                # An extra: it only records that an email was opened.
                self._extra_unavailable("Open tracking", e)

        # AI Generator
        try:
            from modules.ai_generator import AIGenerator, own_server_options

            self.ai_gen = AIGenerator(
                **own_server_options(self.settings),
                personal_data_allowed=bool(
                    (self.settings.ai_dpa_reference or "").strip()
                ),
            )
            if self.ai_gen.is_configured:
                logger.info("AI Generator: ENABLED")
            else:
                logger.warning("AI Generator: DISABLED (no own AI server set)")
        except ImportError as e:
            if self._ai_relevance_wanted():
                # A guard here: the AI relevance check is switched on, an AI
                # server is set, and the check drops the companies the AI
                # judges off-target before any email.
                raise self._guard_unavailable(
                    "The AI relevance check (drops companies the AI judges off-target)",
                    e,
                ) from e
            # Otherwise an extra: emails keep the template's own wording.
            self._extra_unavailable("The AI generator", e)
            self.ai_gen = None

        # Follow-up Sequences (Phase 1: multi-touch cadence)
        if self.settings.sequences_enabled:
            try:
                from modules.sequences import SequenceEngine

                self.sequences = SequenceEngine(db=self.db)
                # {org} = recipient company (filled in per email), matching the
                # personalized main subject line rather than the old sender-named
                # "partnership" style.
                default_steps = [
                    {
                        "delay_days": 3,
                        "subject": "Following up — {org}",
                        "template_name": "followup_1",
                    },
                    {
                        "delay_days": 4,
                        "subject": "One last note, {org}",
                        "template_name": "followup_2",
                    },
                ]
                self.sequence_id = self.sequences.get_or_create_default(
                    self.settings.default_sequence_name, default_steps
                )
                logger.info(f"Sequences: ENABLED (sequence id={self.sequence_id})")
            except ImportError as e:
                # An extra: without it no follow-up is sent at all.
                self._extra_unavailable("Follow-up sequences", e)

        # IMAP reply/bounce monitoring - one inbox monitor per sending account,
        # so bounces/replies to ANY mailbox get detected and suppressed.
        self.imap_monitors = []
        if self.settings.imap_enabled:
            try:
                # The words that mark a reply as an opt-out or a data request.
                # The monitor imports them at each message, where a failure
                # left only the subject line to be read.
                importlib.import_module("modules.reply_intent")
                from modules.imap_monitor import ImapMonitor

                for acct in self._accounts:
                    self.imap_monitors.append(
                        ImapMonitor(
                            db=self.db,
                            imap_host=acct["imap_host"],
                            imap_port=acct["imap_port"],
                            username=acct["imap_username"],
                            password=acct["imap_password"],
                            signin=self.microsoft_signin
                            if acct.get("microsoft")
                            else None,
                        )
                    )
                self.imap_monitor = (
                    self.imap_monitors[0] if self.imap_monitors else None
                )
                logger.info(
                    f"IMAP monitor: ENABLED ({len(self.imap_monitors)} inbox(es))"
                )
            except ImportError as e:
                raise self._guard_unavailable(
                    "The inbox monitor (reads opt-outs, data requests and bounces)",
                    e,
                ) from e

        # Heuristic query generator (free discovery, no AI). Used as the
        # fallback when the AI model is off or not answering, so autonomous
        # discovery keeps working without it.
        if self.settings.heuristic_discovery_enabled:
            try:
                from modules.query_generator import QueryGenerator

                self.query_generator = QueryGenerator()
                logger.info("Heuristic discovery: ENABLED (free, no API cost)")
            except ImportError as e:
                # An extra: it only proposes new searches.
                self._extra_unavailable("Heuristic discovery", e)

        # Learning loop (free, deterministic): biases discovery toward the
        # verticals/countries that have actually produced results.
        if self.settings.learning_enabled:
            try:
                from modules.learning import PerformanceLearner
                from modules.query_generator import EUROPEAN_COUNTRIES, VERTICAL_SEEDS

                seeds = (
                    self.query_generator.seeds
                    if self.query_generator
                    else VERTICAL_SEEDS
                )
                countries = (
                    self.query_generator.countries
                    if self.query_generator
                    else EUROPEAN_COUNTRIES
                )
                self.learner = PerformanceLearner(self.db, seeds, countries)
                logger.info("Learning loop: ENABLED (free, feedback-driven)")
            except ImportError as e:
                # An extra: it only weights which searches come next.
                self._extra_unavailable("The learning loop", e)

        missing = self.guards_not_loaded()
        if missing:
            raise GuardUnavailable(
                f"{', '.join(missing)}: switched on in the settings but not "
                "loaded. Nothing is sent without it."
            )

    @staticmethod
    def _guard_unavailable(what: str, error: ImportError) -> GuardUnavailable:
        """The refusal for a guard the settings switch on that cannot be
        imported: raised, so the agent is never built without it."""
        return GuardUnavailable(
            f"{what} could not be loaded ({error}). The install is incomplete: "
            "install the packages again (py -3.15 -m pip install -r "
            "requirements.txt), check that no file of the agent is missing, "
            "then start again. Nothing is sent without it."
        )

    def _extra_unavailable(self, what: str, error: ImportError) -> None:
        """Say, where the dashboard shows it, that an extra the settings switch
        on cannot be imported. The agent runs without it: it sends no more
        for that, and to no one else."""
        logger.error("%s could not be loaded (%s) - running without it", what, error)
        try:
            self.db.log_activity(
                "COMPONENT_MISSING",
                f"{what} could not be loaded ({error}). The agent runs without "
                "it. The install is incomplete: install the packages again.",
                status="error",
            )
        except sqlite3.Error as e:
            logger.warning("Could not record the missing component: %s", e)

    def guards_not_loaded(self) -> list[str]:
        """The guards the settings switch on that this agent does not hold.

        Every send path tests a guard with `if self.agent.warmup:` and the
        like, where None means "switched off". This is what keeps None from
        ever meaning "switched on, but missing".
        """
        s = self.settings
        wanted = (
            (s.lead_scoring_enabled, self.lead_scorer, "lead scoring"),
            (s.relevance_filter_enabled, self.relevance_filter, "the relevance filter"),
            (s.verify_emails, self.email_verifier, "the email verifier"),
            (s.warmup_enabled, self.warmup, "the warm-up scheduler"),
            (True, self.domain_throttle, "the domain throttle"),
            (True, self.unsubscribe_handler, "the unsubscribe handler"),
            (s.imap_enabled, self.imap_monitors, "the inbox monitor"),
        )
        return [name for on, held, name in wanted if on and not held]

    def _ai_relevance_wanted(self) -> bool:
        """Whether the AI relevance check is switched on with an AI server set.
        With no server set the AI is off, and the check with it."""
        s = self.settings
        return bool(s.ai_relevance_enabled and (s.llm_provider or "").strip())

    # -----------------------------------------------------------------
    # Facade Delegations: Loop Orchestration
    # -----------------------------------------------------------------

    def run_single(
        self,
        queries: list[str] | None = None,
        dry_run: bool = False,
        _from_continuous: bool = False,
        search_ids: dict | None = None,
    ):
        """Run a single batch: process all queries, send to all found contacts, then exit."""
        return self.loop_orchestrator.run_single(
            queries=queries,
            dry_run=dry_run,
            _from_continuous=_from_continuous,
            search_ids=search_ids,
        )

    def run_continuous(
        self,
        queries: list[str] | None = None,
        dry_run: bool = False,
        loop_delay: float = 300.0,
    ):
        """Run continuously: process pending searches, discover new ones if empty."""
        return self.loop_orchestrator.run_continuous(
            queries=queries,
            dry_run=dry_run,
            loop_delay=loop_delay,
        )

    def stop(self):
        """Signal the continuous loop to stop after the current query/email completes."""
        return self.loop_orchestrator.stop()

    def _interruptible_sleep(self, seconds: float):
        """Sleep in short increments so stop()/SIGINT takes effect promptly."""
        return self.loop_orchestrator.interruptible_sleep(seconds)

    def _within_send_window(self) -> bool:
        """True if the current local time is inside the configured send window."""
        return self.loop_orchestrator.within_send_window()

    def _seconds_until_send_window(self) -> float:
        """Seconds until the send window next opens, or 0.0 if it is open now."""
        return self.loop_orchestrator.seconds_until_send_window()

    def _next_loop_delay(self, loop_delay: float) -> float:
        """How long to sleep before the next loop."""
        return self.loop_orchestrator.next_loop_delay(loop_delay)

    def _safe_to_resume(self) -> bool:
        """Whether a paused agent may start sending again."""
        return self.loop_orchestrator.safe_to_resume()

    def _blacklist_guard_tripped(self) -> bool:
        """True when the sending domain is on a spam blocklist."""
        return self.loop_orchestrator.blacklist_guard_tripped()

    def _bounce_guard_tripped(self) -> bool:
        """Circuit breaker: True when the recent bounce rate threatens reputation."""
        return self.loop_orchestrator.bounce_guard_tripped()

    def _maybe_apply_retention(self, dry_run: bool) -> None:
        """Run the data-retention job at most once per calendar day."""
        return self.loop_orchestrator.maybe_apply_retention(dry_run)

    def _apply_runtime_overrides(self):
        """Pull operator config changes in without a restart."""
        return self.loop_orchestrator.apply_runtime_overrides()

    def _resync_settings_consumers(self, key: str):
        """Push a changed setting into the objects that copied it at startup."""
        return self.loop_orchestrator.resync_settings_consumers(key)

    def _maybe_check_imap(self):
        """Run an IMAP reply/bounce check across every account's inbox."""
        return self.loop_orchestrator.maybe_check_imap()

    def _reply_is_interested(self, subject: str, body: str) -> bool:
        """Judge a reply: deterministic keywords decide; AI breaks ties."""
        return self.loop_orchestrator.reply_is_interested(subject, body)

    def _handle_interested_replies(self, entries: list):
        """Email the designated person for each newly-interested prospect."""
        return self.loop_orchestrator.handle_interested_replies(entries)

    def _send_lead_alert(self, entry: dict, recipients: list) -> bool:
        """Send internal 'interested lead' notification."""
        return self.loop_orchestrator.send_lead_alert(entry, recipients)

    def _discovery_focus_hint(self) -> str:
        """Summarise what's working (and what to avoid) to inform the Strategist."""
        return self.loop_orchestrator.discovery_focus_hint()

    def _queries_for_a_standalone_run(self) -> tuple[list, dict]:
        """What a `python main.py run` should search when nothing was passed."""
        return self.loop_orchestrator.queries_for_a_standalone_run()

    def _print_summary(self):
        """Print a summary of the session."""
        return self.loop_orchestrator.print_summary()

    # -----------------------------------------------------------------
    # Facade Delegations: Exclusion Manager
    # -----------------------------------------------------------------

    @staticmethod
    def _list_stamp(path) -> tuple | None:
        """What identifies one saved version of a list file, or None."""
        return ExclusionManager._list_stamp(path)

    def _load_register(self, kind: str, raw_path: str) -> bool:
        """Read one spreadsheet into a ClientRegister."""
        return self.exclusion_manager._load_register(kind, raw_path)

    def load_client_register(self) -> bool:
        """(Re)load the active-client list from CLIENT_LIST_PATH."""
        return self.exclusion_manager.load_client_register()

    def load_competitor_register(self) -> bool:
        """(Re)load the client's named-competitor list from COMPETITOR_LIST_PATH."""
        return self.exclusion_manager.load_competitor_register()

    def refresh_lists_if_changed(self) -> None:
        """Re-read the client or competitor list when the client has edited it."""
        return self.exclusion_manager.refresh_lists_if_changed()

    def _contact_is_closed(self, contact: dict) -> str | None:
        """Why no automated email may go to this contact, or None."""
        return self.exclusion_manager.contact_is_closed(contact)

    def _known_competitor(self, organization) -> str | None:
        """The competitor this organisation is, or None."""
        return self.exclusion_manager.known_competitor(organization)

    def _active_client(self, organization) -> str | None:
        """The client this organisation is, or None."""
        return self.exclusion_manager.active_client(organization)

    # -----------------------------------------------------------------
    # Facade Delegations: Dispatch Pipeline
    # -----------------------------------------------------------------

    def send_reply_answer(self, item_id: int, dry_run: bool = False) -> tuple[bool, str]:
        """Send the answer to a reply that a person approved, exactly as
        approved, threaded under our first email. It goes as a person's own
        send (Direct Send's path): never past an opt-out, a suppression or a
        company block."""
        item = self.review_queue.get(item_id)
        if not item or item["stage"] != "reply":
            return False, "No such answer to a reply."
        approved = self.review_queue.approved_text(item)
        if approved is None:
            return False, "This answer is not approved, or changed after approval."
        contact = self.db.get_contact_by_id(item["contact_id"])
        if not contact:
            return False, "The contact is no longer held."
        subject, body = approved
        result = self.dispatch_pipeline.process_contact(
            contact,
            dry_run,
            template_override=body,
            subject_override=subject,
            is_followup=True,
            manual=True,
            attach_pdf=False,
            language=item.get("language") or "en",
        )
        if result is not True:
            return False, "Not sent: see the activity log for the reason."
        if not dry_run:
            self.review_queue.mark_sent(item_id)
        return True, "Preview only: dry run." if dry_run else "Answer sent."

    def send_single_email(
        self,
        email: str,
        name: str | None = None,
        organization: str | None = None,
        role: str | None = None,
        enroll_followup: bool = False,
        dry_run: bool = False,
        channel: str | None = None,
        confirm_repeat: bool = False,
    ) -> tuple[bool, str]:
        """Send one email to a manually-entered address (dashboard 'Direct Send')."""
        return self.dispatch_pipeline.send_single_email(
            email=email,
            name=name,
            organization=organization,
            role=role,
            enroll_followup=enroll_followup,
            dry_run=dry_run,
            channel=channel,
            confirm_repeat=confirm_repeat,
        )

    def _send_delay(self) -> float:
        """Delay between sends, with optional random jitter to look less bot-like."""
        return self.dispatch_pipeline.send_delay()

    def _send_blocker(self, dry_run: bool) -> str | None:
        """Why no live email may go out at all right now, or None."""
        return self.dispatch_pipeline.send_blocker(dry_run)

    def _refresh_domain_backoff(self):
        """Recompute which recipient domains are bouncing too hard to keep emailing."""
        return self.dispatch_pipeline.refresh_domain_backoff()

    def _domain_backed_off(self, email: str) -> dict | None:
        """The backoff record for this address's domain, or None to proceed."""
        return self.dispatch_pipeline.domain_backed_off(email)

    @staticmethod
    def _by_lead_score(contacts: list[dict]) -> list[dict]:
        """Best leads first."""
        return DispatchPipeline.by_lead_score(contacts)

    def _email_contacts(self, qualified: list[dict], dry_run: bool = False):
        """Send email to each qualified contact."""
        return self.dispatch_pipeline.email_contacts(qualified, dry_run=dry_run)

    def _pick_sender(self):
        """Round-robin to the next sending account that still has warmup capacity."""
        return self.dispatch_pipeline.pick_sender()

    def _screen_contact(self, contact: dict):
        """Why this contact must not be emailed at all: (kind, reason, match)."""
        return self.dispatch_pipeline.screen_contact(contact)

    def _send_possible_now(self) -> bool:
        """Whether anything could be sent right now (pause, window, warm-up)."""
        return self.dispatch_pipeline.send_possible_now()

    def _reconcile_interrupted_sends(self):
        """Settle the sends a stopped process left 'preparing'."""
        return self.dispatch_pipeline.reconcile_interrupted_sends()

    def _send_verdict(self, contact: dict, *, purpose: str, dry_run: bool):
        """The shared send check: may this contact be emailed now, and how."""
        return self.dispatch_pipeline.send_verdict(
            contact, purpose=purpose, dry_run=dry_run
        )

    def _is_public_base_url(self) -> bool:
        """True when BASE_URL is a public address a recipient could reach."""
        return self.dispatch_pipeline.is_public_base_url()

    def _unsubscribe_target(self, email: str) -> str:
        """Unsubscribe href to embed."""
        return self.dispatch_pipeline.unsubscribe_target(email)

    def _opt_out_held(self, email: str) -> bool:
        return self.dispatch_pipeline.opt_out_held(email)

    def refresh_templates_if_changed(self) -> None:
        """Re-read the first-email template and its variants when one changed."""
        return self.dispatch_pipeline.refresh_templates_if_changed()

    def _process_contact(
        self,
        contact: dict,
        dry_run: bool = False,
        template_override: str | None = None,
        subject_override: str | None = None,
        is_followup: bool = False,
        manual: bool = False,
        attach_pdf: bool = True,
        sender=None,
        account_key: str = "",
        language: str | None = None,
        sequence_step_id: int | None = None,
    ):
        """Process a single contact: personalize email, attach PDF, dispatch."""
        return self.dispatch_pipeline.process_contact(
            contact=contact,
            dry_run=dry_run,
            template_override=template_override,
            subject_override=subject_override,
            is_followup=is_followup,
            manual=manual,
            attach_pdf=attach_pdf,
            sender=sender,
            account_key=account_key,
            language=language,
            sequence_step_id=sequence_step_id,
        )

    def _first_email_blocker(self, dry_run: bool, sender=None) -> str | None:
        """Why no live first email may go out at all right now, or None."""
        return self.dispatch_pipeline.first_email_blocker(dry_run, sender=sender)

    @staticmethod
    def _build_greeting(contact: dict, language: str = "en") -> str:
        """A human first-name greeting, e.g. 'Hi Maria,'; 'Buongiorno,' in Italian."""
        return DispatchPipeline._build_greeting(contact, language)

    @staticmethod
    def _body_to_html(text: str) -> str:
        """Turn plain body text into simple, email-safe <p> blocks."""
        return DispatchPipeline._body_to_html(text)

    def _build_email_body(self, contact: dict) -> str:
        """Return the email body as HTML paragraphs."""
        return self.dispatch_pipeline.build_email_body(contact)

    def _build_opening_line(self, contact: dict) -> str:
        """Return a personalized opening sentence for the email."""
        return self.dispatch_pipeline.build_opening_line(contact)

    @staticmethod
    def _default_template() -> str:
        """Fallback email template if the HTML file is not found."""
        return DispatchPipeline._default_template()

    # -----------------------------------------------------------------
    # Facade Delegations: Backlog Draining
    # -----------------------------------------------------------------

    def _process_pending_contacts(self, dry_run: bool = False):
        """Email contacts already stored as 'pending' in the database."""
        return self.backlog_drainer.process_pending_contacts(dry_run=dry_run)

    def _qualify_contacts(self, contacts: list[dict]) -> list[dict]:
        """Score and verify contacts, filter out low-quality leads."""
        return self.backlog_drainer.qualify_contacts(contacts)

    def _load_followup_template(self, template_name: str) -> str:
        """Load a follow-up template by name."""
        return self.backlog_drainer.load_followup_template(template_name)

    def _process_sequences(self, dry_run: bool = False):
        """Send any due follow-up steps, then advance or cancel enrollments."""
        return self.backlog_drainer.process_sequences(dry_run=dry_run)

    def _process_retries(self):
        """Process any due retries from the retry queue."""
        return self.backlog_drainer.process_retries()

    # -----------------------------------------------------------------
    # Status & Inspection
    # -----------------------------------------------------------------

    def get_status(self) -> dict:
        """Get current agent status and database stats."""
        db_stats = self.db.get_stats()
        status = {
            "running": self._running,
            "sending_paused": self._sending_paused,
            "pause_reason": self._pause_reason,
            "session_stats": dict(self._stats),
            "database_stats": db_stats,
            "config": {
                "dry_run": self.settings.dry_run,
                "smtp_host": self.settings.smtp_host,
                "pdf_available": self._pdf_path is not None,
                "queries": self.settings.get_target_queries(),
                "warmup_enabled": self.settings.warmup_enabled,
                "lead_scoring_enabled": self.settings.lead_scoring_enabled,
                "verify_emails": self.settings.verify_emails,
                "retry_enabled": self.settings.retry_enabled,
            },
        }
        if self.warmup:
            status["warmup"] = self.warmup.get_warmup_status()
        return status
