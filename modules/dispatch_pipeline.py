"""Central email dispatch pipeline and message assembly.

Holds the one check every send path runs before each contact (send_verdict,
returning a SendVerdict, for the campaign, follow-ups, retries and Direct
Send) with the address/competitor/client screen it applies (screen_contact);
the dispatch chokepoint every outreach email passes through (process_contact);
the campaign and Direct Send paths (email_contacts, send_single_email); lead
score ordering, sender rotation, pacing, the preflight send blockers, the
per-domain bounce backoff; email body assembly (greetings, AI bodies, legal
footers, unsubscribe links, tracking pixels); and post-dispatch recording
(warm-up, domain throttle, tracking, sequences, retries).
"""

from __future__ import annotations

import html
import itertools
import logging
import random
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modules.company_block import blockable_domain
from modules.company_country import settle_company_country
from modules.compliance import (
    MANUAL_CHANNELS,
    RULE_VERSION,
    basis_for,
    country_refusal,
    enabled_countries,
    first_contact_overdue,
    is_public_base_url,
    italian_countries,
    language_for,
    notice_version,
    opt_out_route,
    render_legal_footer,
)
from modules.database import (
    CLOSED_CONTACT_STATUSES,
    UNSETTLED_SEND,
    default_source_channel,
    is_address,
)
from modules.email_sender import (
    CLAIM_LOST,
    PREPARING_STALE_AFTER,
    UNKNOWN_ORGANIZATION,
    ClaimLost,
    EmailSender,
    name_for_header,
)
from modules.imap_monitor import inbox_read
from modules.optout_spool import (
    apply_set_aside,
    is_set_aside,
    pending_addresses,
    pending_company_domains,
    pending_tokens,
    replay_optout_spool,
)
from modules.review_queue import level_for, stage_for
from modules.search import PERSONAL_DOMAINS, SearchModule

if TYPE_CHECKING:
    from agent import OutreachAgent

logger = logging.getLogger(__name__)

# One stamp per dispatch attempt, process-wide. next() on a count is
# atomic under the GIL, so Direct Send on the dashboard's thread and the
# campaign loop can both stamp the same agent safely.
_DISPATCH_STAMPS = itertools.count(1)

# Words that make a stored name a department or a page heading, not a person.
# The scraper stores the first two to four capitalised words beside an address
# as the name, so "Customer Reviews" and "Head Office" came back as names.
_NOT_A_PERSON = frozenset(
    {
        "customer",
        "reviews",
        "team",
        "info",
        "sales",
        "shop",
        "office",
        "support",
        "service",
        "contact",
        "admin",
        "marketing",
        # The pages the crawler reads for an address, in the languages it meets
        # (SearchModule's CONTACT_PATHS_BY_TLD, _DEFAULT and _OWN_PAGE_PATHS).
        "kontakt",
        "contatti",
        "contacto",
        "impressum",
        "imprint",
        "legal",
        "about",
        "us",
        "uns",
        "nous",
        "ons",
        "siamo",
        "somos",
        # What sits beside an address in a footer, next to the cookie banner.
        "privacy",
        "policy",
        "cookie",
        "cookies",
        "datenschutz",
    }
)

# Titles written as a word of their own, which the scraper keeps at the front
# of the name it stores: "Mr John Smith", "Herr Thomas Weber".
_TITLES = frozenset(
    {"dr", "prof", "mr", "mrs", "ms", "herr", "frau", "madame", "monsieur", "mme"}
)


def _is_title(word: str) -> bool:
    """A title or an initial. A word with a full stop in it ("Dr.", "Dott.ssa",
    "J.", an address) is never a first name."""
    return "." in word or word.lower() in _TITLES


# The client's first email says "I am responsible for <title> at the client"; blank,
# it would go out as "responsible for  at the client".
_TITLE_MISSING = (
    "SENDER_TITLE is blank, and the first email says what its sender is "
    "responsible for - set SENDER_TITLE in .env"
)
# The dashboard's activity row for each refusal of a live first email.
_FIRST_EMAIL_REFUSALS = {
    "profile_pdf": "PROFILE_PDF_MISSING",
    "sender_title": "SENDER_TITLE_MISSING",
}


def _signature_lines(text: str) -> list[str]:
    """SENDER_SIGNATURE's lines: split at the two characters \\n, as .env
    holds them, or at a real line break (a quoted value), blank lines and
    surrounding spaces dropped."""
    return [
        line.strip() for line in re.split(r"\\n|\r\n|\r|\n", text or "") if line.strip()
    ]


# The recipient the Template Editor's preview writes to: plainly a sample, so
# nobody at a demo takes it for a real prospect.
_PREVIEW_CONTACT = {
    "name": "Maria Rossi",
    "role": "Purchasing Manager",
    "organization": "Sample Brand S.r.l.",
}


def _preview_block(text: str) -> str:
    return (
        '<p style="margin: 16px 0; padding: 8px; border: 1px dashed #999; '
        f'color: #666; font-size: 12px;">{html.escape(text)}</p>'
    )


def preview_placeholders(settings, language: str = "en") -> dict[str, str]:
    """What the Template Editor's preview puts in each placeholder, as HTML.

    The same keys the send path fills (render_template's and process_contact's
    `values` and `markup`), so a text never previews with a raw {{...}} that
    the email would not have. The sender is this installation's primary
    mailbox, as a first email is sent; the recipient is _PREVIEW_CONTACT, and
    the parts written per contact or per send (the body, the opening line,
    the legal footer) are short sample blocks. A blank sender value shows a
    marker, never a hole. Every value is escaped here."""
    primary = settings.get_sender_accounts()[0]
    sender_name = (primary.get("sender_name") or "").strip()
    title = (primary.get("sender_title") or "").strip()
    company = (settings.company_name or "").strip()
    signature = _signature_lines(primary.get("sender_signature", "")) or [
        sender_name or "[sender name not set]",
        *([title] if title else []),
        company or "[company name not set]",
    ]
    values = {
        "name": _PREVIEW_CONTACT["name"],
        "role": _PREVIEW_CONTACT["role"],
        "organization": _PREVIEW_CONTACT["organization"],
        "company_name": company or "[company name not set]",
        "sender_name": sender_name or "[sender name not set]",
        "sender_title": title or "[sender title not set]",
        "unsubscribe_url": "#unsubscribe",
        "greeting": DispatchPipeline._build_greeting(_PREVIEW_CONTACT, language),
    }
    filled = {key: html.escape(value) for key, value in values.items()}
    filled.update(
        {
            "sender_signature": "<br>".join(html.escape(x) for x in signature),
            "opening_line": html.escape("[Opening line written for each contact]"),
            "body": _preview_block("[Email body written for each contact]"),
            "legal_footer": _preview_block(
                "[Legal footer: the sender's legal identity, why the recipient "
                "is receiving this and how to opt out - added when sent]"
            ),
            "tracking_pixel": "",
        }
    )
    return filled


@dataclass(frozen=True)
class SendVerdict:
    """What the shared send check decided about one contact.

    ``scope`` says how far the answer reaches, which is all a loop needs to
    choose its reaction:

    - ``batch``: nothing more can be sent in this pass - stop the loop.
    - ``drop``: this contact must never get this email - settle it.
    - ``defer``: not this contact, not now - leave it queued.
    - ``cleared``: send it, through ``sender`` / ``account_key``.

    ``kind`` names the check that decided, ``reason`` carries the value a
    caller formats into its own message (a status, a domain), and ``detail``
    any structured data behind it (the backoff record, the matched client or
    competitor). ``contact`` is the row the decision was made on.
    ``outside_window`` marks a manual send cleared outside business hours,
    where the window is advisory.
    """

    scope: str
    kind: str
    reason: str = ""
    contact: dict | None = None
    sender: Any = None
    account_key: str = ""
    detail: Any = None
    outside_window: bool = False


@dataclass(frozen=True)
class AlreadyEmailed:
    """What a Direct Send answers when an email may already have reached the
    address: nothing went out, and the operator is asked before another does.
    Falsy, as every send that did not go out is, so a caller that only reads
    ``ok`` still reports it as not sent. ``timestamp`` is the email log's local
    time, ``language`` "it" or "en"."""

    timestamp: str
    language: str

    def __bool__(self) -> bool:
        return False

    @property
    def on(self) -> str:
        """The day the last email went, as the operator reads a date."""
        day = date.fromisoformat(self.timestamp[:10])
        return f"{day.day} {day:%B %Y}"

    @property
    def language_name(self) -> str:
        return "Italian" if self.language == "it" else "English"


class DispatchPipeline:
    """Orchestrates email assembly, safety gating, SMTP dispatch, and post-send state."""

    def __init__(self, agent: OutreachAgent):
        self.agent = agent

    @staticmethod
    def by_lead_score(contacts: list[dict]) -> list[dict]:
        """Best leads first.

        The daily warm-up allowance is the scarce resource here: this loop stops
        the moment _pick_sender() runs out of capacity, so the ORDER of the list
        decides which leads get today's sends and which wait for tomorrow (or
        never, if the backlog cap sheds them first).

        get_pending_contacts already sorts the backlog by score, but
        _qualify_contacts returns fresh contacts in SCRAPE order and
        _run_pending concatenates backlog + newly-qualified without re-sorting
        across the join. So a 90-scoring lead found on the fortieth page lost
        its send to a 16-scoring lead found on the first. Sorting here rather
        than at each call site means every path through the sender is ordered,
        including any added later.

        Stable, so equal scores keep discovery order and the sort never
        reshuffles a batch it cannot rank.
        """
        return sorted(contacts, key=lambda c: c.get("lead_score") or 0, reverse=True)

    _by_lead_score = by_lead_score

    def send_delay(self) -> float:
        """Delay between sends, with optional random jitter to look less bot-like.

        emails_per_hour sets a FLOOR on the gap. It is a deliverability control,
        not a volume one - the day's volume is capped by the warm-up allowance
        long before an hourly rate could bind. Its job is the shape of the
        sending: at the default 5s delay a 100-email allowance leaves in an
        eight-minute burst and then silence for the rest of the day, which is
        precisely the pattern spam filters score on. At 30/hour the same 100
        emails spread over 3h20m, inside an 11-hour send window, so nothing is
        lost by pacing them.

        This was previously read by nothing at all: the dashboard offered the
        setting, validated it, and stored it, while the sender kept using
        delay_between_emails. An operator lowering the rate to protect a
        struggling domain would have seen the change accepted and had no effect.
        """
        base = self.agent.settings.delay_between_emails
        per_hour = self.agent.settings.emails_per_hour
        if per_hour and per_hour > 0:
            base = max(base, 3600.0 / per_hour)
        jitter = self.agent.settings.send_jitter_seconds
        if jitter and jitter > 0:
            return max(0.0, base + random.uniform(0, jitter))
        return base

    _send_delay = send_delay

    def send_blocker(self, dry_run: bool) -> str | None:
        """Why no live email may go out at all right now, or None.

        A property of the installation, not of any contact, so the send loops
        check it before claiming work and _process_contact checks it again as
        the guarantee. Dry runs are never blocked: they are how the operator
        previews the result.
        """
        if dry_run:
            return None
        missing = self.agent.settings.missing_legal_identity()
        if missing:
            reason = f"set {', '.join(missing)} in .env"
            logger.error("Live sending refused: %s", reason)
            if not self.agent._guard_logged.get("legal_identity"):
                try:
                    self.agent.db.log_activity(
                        "LEGAL_IDENTITY_MISSING",
                        f"Live sending refused: {reason}. Every commercial "
                        "email must identify the sender (D.Lgs. 70/2003 Art. 8, "
                        "Cod. Civ. 2250, GDPR Art. 14). Dry runs still work.",
                        status="error",
                    )
                    self.agent._guard_logged["legal_identity"] = True
                except sqlite3.Error as e:
                    logger.error("Could not log the legal-identity refusal: %s", e)
            return reason
        # Every email offers an opt-out. If nothing would read one - no public
        # unsubscribe page and no inbox monitor - the objection arrives and is
        # never acted on, so no live email may go out (GDPR Art. 21(3)).
        route_ok, route = opt_out_route(self.agent.settings)
        if not route_ok:
            logger.error("Live sending refused: %s", route)
            if not self.agent._guard_logged.get("opt_out_route"):
                try:
                    self.agent.db.log_activity(
                        "OPT_OUT_ROUTE_MISSING",
                        f"Live sending refused: {route}. Dry runs still work.",
                        status="error",
                    )
                    self.agent._guard_logged["opt_out_route"] = True
                except sqlite3.Error as e:
                    logger.error("Could not log the opt-out-route refusal: %s", e)
            return route
        # IMAP on is not the inbox read: a mailbox whose login fails logs
        # IMAP_ERROR on every check while the objections, data requests and
        # bounces emailed to it go unread. So live sending waits for a
        # complete read of every inbox (GDPR Art. 21(3)).
        if self.agent.settings.imap_enabled:
            try:
                read, detail = inbox_read(
                    self.agent.db,
                    [a["imap_username"] for a in self.agent._accounts],
                    self.agent.settings.imap_check_interval_minutes,
                )
            except sqlite3.Error as e:
                read, detail = False, f"when the inbox was last read is unknown ({e})"
            if not read:
                logger.error("Live sending refused: %s", detail)
                if not self.agent._guard_logged.get("inbox_unread"):
                    try:
                        self.agent.db.log_activity(
                            "INBOX_UNREAD",
                            f"Live sending refused: {detail}. If IMAP_ERROR "
                            "shows the reads failing, check the mailbox login. "
                            "Dry runs still work.",
                            status="error",
                        )
                        self.agent._guard_logged["inbox_unread"] = True
                    except sqlite3.Error as e:
                        logger.error("Could not log the inbox-read refusal: %s", e)
                return detail
            self.agent._guard_logged.pop("inbox_unread", None)  # read again
        # The interim country lock: until counsel enables a country, nothing
        # goes out (decision L1.1).
        if not enabled_countries(self.agent.settings):
            reason = (
                "no country is enabled - set ENABLED_COUNTRIES once counsel has "
                "signed the country table"
            )
            logger.error("Live sending refused: %s", reason)
            if not self.agent._guard_logged.get("country_lock"):
                try:
                    self.agent.db.log_activity(
                        "COUNTRY_LOCK",
                        f"Live sending refused: {reason}. Dry runs still work.",
                        status="error",
                    )
                    self.agent._guard_logged["country_lock"] = True
                except sqlite3.Error as e:
                    logger.error("Could not log the country-lock refusal: %s", e)
            return reason
        # The client screen is on but its list never loaded, or loaded and
        # screens no company (an empty sheet passed this check): existing
        # customers would be cold-emailed with only a log line to say so. A
        # list that failed to RE-load keeps the previous one, which screens.
        register = getattr(self.agent, "client_register", None)
        if self.agent.settings.client_filter_enabled and (
            register is None or not register.matchable
        ):
            reason = (
                "the client list (CLIENT_LIST_PATH) could not be loaded and "
                "CLIENT_FILTER_ENABLED is on"
                if register is None
                else "the client list (CLIENT_LIST_PATH) holds no company the "
                "screen can match and CLIENT_FILTER_ENABLED is on"
            )
            logger.error("Live sending refused: %s", reason)
            if not self.agent._guard_logged.get("client_list"):
                try:
                    self.agent.db.log_activity(
                        "CLIENT_LIST_MISSING",
                        f"Live sending refused: {reason}. Dry runs still work.",
                        status="error",
                    )
                    self.agent._guard_logged["client_list"] = True
                except sqlite3.Error as e:
                    logger.error("Could not log the client-list refusal: %s", e)
            return reason
        # The list screens again. A reload can empty it again later, and that
        # is a new episode, reported again.
        self.agent._guard_logged.pop("client_list", None)
        # The same for the competitor list, once one is configured: the client was
        # told a company on it is never emailed, and an unreadable list leaves
        # only the keyword guess to catch a rival. Blank path = screen off.
        rivals = getattr(self.agent, "competitor_register", None)
        if (
            self.agent.settings.competitor_filter_enabled
            and (self.agent.settings.competitor_list_path or "").strip()
            and (rivals is None or not rivals.matchable)
        ):
            reason = (
                "the competitor list (COMPETITOR_LIST_PATH) could not be loaded "
                "and COMPETITOR_FILTER_ENABLED is on"
                if rivals is None
                else "the competitor list (COMPETITOR_LIST_PATH) holds no company "
                "the screen can match and COMPETITOR_FILTER_ENABLED is on"
            )
            logger.error("Live sending refused: %s", reason)
            if not self.agent._guard_logged.get("competitor_list"):
                try:
                    self.agent.db.log_activity(
                        "COMPETITOR_LIST_MISSING",
                        f"Live sending refused: {reason}. Dry runs still work.",
                        status="error",
                    )
                    self.agent._guard_logged["competitor_list"] = True
                except sqlite3.Error as e:
                    logger.error("Could not log the competitor-list refusal: %s", e)
            return reason
        self.agent._guard_logged.pop("competitor_list", None)
        # SMTP auth is broken. Every send opens a fresh LOGIN, so carrying on
        # means hundreds of failed logins in minutes - enough for the provider
        # to lock the account. Guarded only the campaign loop until follow-ups,
        # retries and Direct Send got it too, through here.
        if any(
            getattr(es, "auth_failed", False) for (_k, _n, es) in self.agent.senders
        ):
            reason = "SMTP authentication is failing - fix the credentials and restart"
            logger.error("Live sending halted: %s", reason)
            if not self.agent._guard_logged.get("smtp_auth"):
                try:
                    self.agent.db.log_activity(
                        "AGENT_HALTED",
                        "SMTP authentication failed - fix the credentials and "
                        "restart. Remaining contacts were left pending.",
                        status="error",
                    )
                    self.agent._guard_logged["smtp_auth"] = True
                except sqlite3.Error as e:
                    logger.error("Could not log the SMTP-auth halt: %s", e)
            return reason
        return None

    _send_blocker = send_blocker

    def profile_problem(self) -> str | None:
        """Why the Company Profile cannot go with a first email, or None.

        the client's first email says "Please find attached our Company Profile",
        so with ATTACH_PROFILE on the PDF is part of the text. Looked for at
        each first email: a file deleted while the agent runs stops the next
        one, and a file put in place is picked up without a restart.
        """
        if not self.agent.settings.attach_profile:
            return None
        path, problem = self.agent.file_handler.pdf_problem(
            self.agent.settings.pdf_path
        )
        if problem is None:
            self.agent._pdf_path = path
            # Found again: the next time it goes missing is reported again.
            self.agent._guard_logged.pop("profile_pdf", None)
            self.agent._guard_logged.pop("profile_pdf_dry_run", None)
            return None
        self.agent._pdf_path = None
        return f"the Company Profile PDF (PDF_PATH) {problem}: {path}"

    def first_email_problems(self, senders: list | None = None) -> list[tuple]:
        """What refuses every live first email, as (key, reason) pairs.

        The title counts only when no mailbox in ``senders`` (default: all)
        has one and every first-email template in use says it; otherwise
        it is a refusal for some contacts, made in process_contact.
        """
        problems = []
        pdf = self.profile_problem()
        if pdf:
            problems.append(("profile_pdf", pdf))
        templates = [self.agent._email_template]
        if self.agent._email_template_it and italian_countries(self.agent.settings):
            templates.append(self.agent._email_template_it)
        mailboxes = (
            senders
            if senders is not None
            else [es for _k, _n, es in self.agent.senders]
        )
        if all("{{sender_title}}" in (t or "") for t in templates) and not any(
            (getattr(m, "sender_title", "") or "").strip() for m in mailboxes
        ):
            problems.append(("sender_title", _TITLE_MISSING))
        return problems

    def _titled(self, mailbox) -> bool:
        """Whether this mailbox has a title for the first email, read as
        process_contact reads it."""
        title = getattr(mailbox, "sender_title", self.agent.settings.sender_title)
        return bool((title or "").strip())

    def first_email_needs_title(self, contact: dict) -> bool:
        """Whether the first email this contact would get says its sender's
        title: the Italian text, or the English one and any A/B variant of it,
        as process_contact chooses between them."""
        if self.agent._email_template_it and (
            language_for(self.agent.settings, self.agent.db, contact)[0] == "it"
        ):
            templates = [self.agent._email_template_it]
        else:
            templates = [self.agent._email_template]
            if self.agent.settings.ab_testing_enabled:
                templates += [t for _n, t in self.agent._template_variants or []]
        return any("{{sender_title}}" in (t or "") for t in templates)

    def refuse_first_email(self, key: str, reason: str) -> None:
        """Log a refused live first email, and record it on the dashboard once:
        one clear line, not one per prospect in the backlog."""
        logger.error("Live first email refused: %s", reason)
        if self.agent._guard_logged.get(key):
            return
        try:
            self.agent.db.log_activity(
                _FIRST_EMAIL_REFUSALS[key],
                f"First emails refused: {reason}. Dry runs still work.",
                status="error",
            )
            self.agent._guard_logged[key] = True
        except sqlite3.Error as e:
            logger.error("Could not log the first-email refusal: %s", e)

    def first_email_blocker(self, dry_run: bool, sender=None) -> str | None:
        """Why no live first email may go out at all right now, or None.

        The first email's own refusals, beside send_blocker's: the send loops
        stop on it instead of walking the backlog into the same refusal, and a
        retry waiting on it keeps its attempts. ``sender`` narrows the title
        check to one mailbox (Direct Send uses the primary).
        """
        if dry_run:
            return None
        problems = self.first_email_problems([sender] if sender else None)
        for key, reason in problems:
            self.refuse_first_email(key, reason)
        return "; ".join(reason for _key, reason in problems) or None

    _first_email_blocker = first_email_blocker

    def refresh_domain_backoff(self) -> None:
        """Recompute which recipient domains are bouncing too hard to keep
        emailing. Called once per send batch, not once per contact - this is a
        GROUP BY over two tables and a 300-contact backlog would otherwise run
        it 300 times.

        Fails OPEN, unlike _bounce_guard_tripped. That guard is the last line
        of defence for the sending domain and must stop everything when it
        cannot see; this one is a refinement on top of it. If the query fails,
        the global guard is still watching, so degrading to "no per-domain
        backoff" loses an optimisation rather than removing a protection - and
        failing closed here would mean one bad query stops all sending.

        The map is built aside and swapped in whole. Direct Send reads it from
        the dashboard's thread while the campaign agent refreshes it, and a map
        emptied before the query and refilled after it reads as "no domain is
        backed off" for the length of that query.
        """
        if not self.agent.settings.domain_bounce_backoff_enabled:
            self.agent._domain_backoff = {}
            return
        try:
            stats = self.agent.db.get_domain_bounce_stats(
                min_sends=self.agent.settings.domain_bounce_min_sends
            )
        except sqlite3.Error as e:
            logger.warning(
                "Could not evaluate per-domain bounce rates (%s); continuing "
                "with the global bounce guard only.",
                e,
            )
            self.agent._domain_backoff = {}
            return

        backoff = {
            domain: s
            for domain, s in stats.items()
            if s["enough_data"]
            and s["bounce_rate"] > self.agent.settings.max_domain_bounce_rate
        }
        self.agent._domain_backoff = backoff

        for domain, s in backoff.items():
            key = f"domain_backoff:{domain}"
            if not self.agent._guard_logged.get(key):
                logger.warning(
                    "Backing off %s: %.0f%% bounce rate (%d/%d in the last 7 "
                    "days) exceeds %.0f%%. Other domains are unaffected.",
                    domain,
                    s["bounce_rate"] * 100,
                    s["bounced"],
                    s["sent"],
                    self.agent.settings.max_domain_bounce_rate * 100,
                )
                try:
                    self.agent.db.log_activity(
                        "DOMAIN_BACKOFF",
                        f"Skipping {domain}: {s['bounce_rate']:.0%} bounce rate "
                        f"({s['bounced']}/{s['sent']} in 7d)",
                        status="warning",
                    )
                    self.agent._guard_logged[key] = True
                except sqlite3.Error as exc:
                    logger.debug("Could not log the domain backoff: %s", exc)

        # Drop the log-once latch for domains that have recovered, so a future
        # relapse is reported again instead of being silently swallowed.
        for key in [
            k
            for k in self.agent._guard_logged
            if k.startswith("domain_backoff:") and k.split(":", 1)[1] not in backoff
        ]:
            self.agent._guard_logged.pop(key, None)

    _refresh_domain_backoff = refresh_domain_backoff

    def domain_backed_off(self, email: str) -> dict | None:
        """The backoff record for this address's domain, or None to proceed."""
        if not self.agent._domain_backoff:
            return None
        addr = (email or "").strip().lower()
        _, _, domain = addr.partition("@")
        return self.agent._domain_backoff.get(domain) if domain else None

    _domain_backed_off = domain_backed_off

    def pick_sender(self) -> tuple[str, str, Any] | None:
        """Round-robin to the next sending account that still has warmup capacity
        today. Returns (account_key, sender_name, EmailSender), or None when every
        account has hit its daily cap."""
        n = len(self.agent.senders)
        for i in range(n):
            key, name, es = self.agent.senders[(self.agent._rr + i) % n]
            if (not self.agent.warmup) or self.agent.warmup.can_send(account=key):
                self.agent._rr = (self.agent._rr + i + 1) % n
                # Capacity is back, so the next time it runs out is a new
                # episode and is reported again.
                self.agent._guard_logged.pop("warmup", None)
                return key, name, es
        return None

    _pick_sender = pick_sender

    def send_possible_now(self) -> bool:
        """Whether anything could be sent right now: not paused, inside the
        send window, and some mailbox with warm-up allowance left today."""
        if self.agent._sending_paused or not self.agent._within_send_window():
            return False
        return (not self.agent.warmup) or any(
            self.agent.warmup.can_send(account=k) for k in self.agent._account_keys
        )

    _send_possible_now = send_possible_now

    def send_verdict(
        self, contact: dict, *, purpose: str, dry_run: bool
    ) -> SendVerdict:
        """The one check every send path runs before each contact.

        ``purpose`` is "first" (the campaign), "followup", "retry" or "manual"
        (Direct Send). Each path carried its own copy of this sequence, and the
        copies drifted apart; each now keeps only its own reaction to the
        answer.

        The order is cheapest-and-widest first: installation-wide refusals
        stop the pass before any contact is read, and the sending mailbox is
        picked last, so a contact that is then skipped never turns the
        rotation or spends a warm-up count query.

        ``contact`` is the loop's row; it is read again here, so a retry can
        pass only ``{"id": ...}``. A manual send passes the operator's row
        (typed details over the stored contact), which is used as given.

        Deliberate differences for a manual send: it runs whether or not the
        agent loop is running; the send window is advisory; a dry run is a
        preview that skips the reputation and pacing guards; it may override
        a filter verdict, though never an opt-out; it never asks whether the
        first email already went out, though it waits for a send that has not
        settled; and it uses the primary mailbox only.
        """
        manual = purpose == "manual"
        if not manual and not self.agent._running:
            return SendVerdict("batch", "stopped")
        # The latched pause is authoritative for a manual send and checked
        # first. Re-deriving from _bounce_guard_tripped() alone is not enough:
        # a pause drains the 7-day recent_sent window, so the guard falls below
        # bounce_guard_min_sends and reports "not tripped" for lack of data
        # while the agent is still deliberately paused. (The loops never run
        # while paused.)
        if manual and not dry_run and self.agent._sending_paused:
            return SendVerdict(
                "batch", "paused", self.agent._pause_reason or "guard tripped"
            )
        # The blocker below wants a recent complete read of the inbox, and
        # the loop reads it only between passes, so a send loop reads it here
        # when a read falls due: the first send after a start, and a batch
        # that outlasts the check interval (100 emails at 30 an hour take
        # over three hours), then go out with the inbox just read. The read
        # is throttled to the interval, so this costs nothing otherwise.
        if not manual and not dry_run and self.agent.settings.imap_enabled:
            self.agent._maybe_check_imap()
        # Installation-wide refusals: blank legal identity, dead SMTP login. A
        # config error, not a per-contact one, so stop the pass instead of
        # burning every remaining contact on it.
        blocker = self.agent._send_blocker(dry_run)
        # And the first email's own: its PDF, and what its sender is
        # responsible for. A follow-up says neither, and still goes.
        if not blocker and purpose in ("first", "retry", "manual"):
            blocker = self.agent._first_email_blocker(
                dry_run, sender=self.agent.email_sender if manual else None
            )
        if blocker:
            return SendVerdict("batch", "blocked", blocker)
        # A manual dry run is a preview of the draft, so none of the
        # reputation or pacing guards below apply to it.
        preview = manual and dry_run
        # Hold sends outside business hours (they resume next window). For a
        # manual send the window is advisory: the operator chose the moment.
        outside_window = not preview and not self.agent._within_send_window()
        if outside_window and not manual:
            return SendVerdict("batch", "window")
        # Circuit breakers. Stop SENDING, but keep the agent alive:
        # _maybe_check_imap is gated on self._running, so clearing it would
        # also switch off bounce suppression, GDPR opt-out processing and
        # hot-lead alerts - exactly when they matter most.
        # A manual send latches the pause only on a running agent: the
        # dashboard's standalone Direct Send agent never runs the loop that
        # releases it, so it would refuse every later send, even after recovery.
        latch = not manual or self.agent._running
        if not preview and self.agent._bounce_guard_tripped():
            if latch:
                self.agent._sending_paused, self.agent._pause_reason = True, "bounce"
            return SendVerdict("batch", "bounce")
        if not preview and self.agent._blacklist_guard_tripped():
            if latch:
                self.agent._sending_paused, self.agent._pause_reason = (
                    True,
                    "blacklist",
                )
            return SendVerdict("batch", "blacklist")

        if manual:
            fresh = contact
        else:
            # Judged on the contact's CURRENT row. A loop works through a list
            # read when its pass began and sleeps the send delay after every
            # contact, so the last rows of a long batch can be hours old: a
            # prospect who replied, was blocked from the dashboard or was
            # erased in the meantime still got the email. The stored row is
            # laid over the loop's copy, so anything held only in memory is
            # kept.
            row = (
                self.agent.db.get_contact_by_id(contact["id"])
                if contact.get("id")
                else None
            )
            if not row:
                return SendVerdict("drop", "missing", "contact not found")
            fresh = {**contact, **row}

        # Someone who replied, became a client or opted out - or a filter
        # verdict recorded since the batch was read. The operator may
        # override a filter verdict by hand, never a closed contact: the
        # dashboard's Send button has refused those all along.
        closed = (
            self.agent._contact_is_closed(fresh)
            if not manual
            else "suppressed"
            if self.agent.db.is_suppressed(fresh["email"])
            else "company blocked"
            if self.agent.db.company_blocked(fresh["email"])
            else fresh.get("status")
            if fresh.get("status") in CLOSED_CONTACT_STATUSES
            else None
        )
        if closed:
            return SendVerdict("drop", "closed", closed, contact=fresh)

        # The address, competitor and client screens, here as well as in the
        # send itself: a contact that is never going to be emailed must not
        # pick a mailbox, run a warm-up query or cost the loop its send delay.
        screen = self.agent._screen_contact(fresh)
        if screen:
            kind, reason, match = screen
            return SendVerdict("drop", kind, reason, contact=fresh, detail=match)

        # A send to this contact that has not settled: going out right now,
        # from this process or another, or cut off after the server may have
        # accepted it. Either may already have reached them, so nothing more
        # goes until it settles. Deferred, not dropped: a send still going
        # out may yet fail, so the contact is neither marked emailed nor its
        # retry cancelled. reconcile_interrupted_sends settles a cut-off one
        # once no live send could still be making it. A preview sends nothing.
        if not preview and self.agent.db.send_in_doubt(fresh.get("id")):
            return SendVerdict("defer", "in_doubt", "send not settled", fresh)

        # A follow-up the interim country lock refuses ends the sequence: its
        # country was switched off after the first email. Refused only in the
        # send, it left the contact open, so the enrolment stayed and was
        # refused on every pass, holding the contact past the no-reply period
        # the footer promises. A first email waits, pending, for its country.
        # Dry runs preview every country, as the send itself does.
        if purpose == "followup" and not dry_run:
            refusal = country_refusal(self.agent.settings, fresh["email"])
            if refusal:
                return SendVerdict("drop", "country", refusal, contact=fresh)

        # The FIRST email goes out once. A retry re-sends it, and the
        # campaign's batch can be hours old: delivered by another route since
        # (a Direct Send, a retry, a pass whose status write failed after the
        # send), sending it again would be a duplicate.
        if purpose in ("first", "retry") and self.agent.db.has_been_emailed(
            fresh["id"]
        ):
            return SendVerdict("drop", "already_emailed", "already emailed", fresh)

        # Per-domain cap (recipient domain; global across accounts). Every
        # successful send records a domain count in _process_contact, so a
        # path that skipped this would blow past the limit. Reputation damage
        # does not care that a human pressed the button either. The cap means
        # "not today", so it comes after every "never" above.
        if not preview and self.agent.domain_throttle:
            domain = self.agent.domain_throttle.extract_domain(fresh["email"])
            if not self.agent.domain_throttle.can_send_to_domain(domain):
                return SendVerdict("defer", "domain_cap", domain, fresh)

        # This domain is rejecting us hard enough that continuing costs
        # reputation for nothing. Deferring it keeps the campaign running for
        # every other domain, instead of waiting for the global guard to trip
        # and stop all of them. Re-evaluated as the window rolls.
        backoff = None if preview else self.agent._domain_backed_off(fresh["email"])
        if backoff:
            return SendVerdict(
                "defer", "backoff", backoff["domain"], fresh, detail=backoff
            )

        # A stage a person runs by hand: the agent sends nothing there itself.
        # Direct Send is that person's own act, so it is never held.
        if not manual:
            stage = stage_for(purpose)
            if stage and level_for(self.agent.settings, stage) == "manual":
                return SendVerdict(
                    "defer", "review", f"{stage} is set to manual", fresh
                )

        if manual:
            # Direct Send goes out through the primary mailbox, so only its
            # warm-up allowance counts - and the send increments it, so
            # skipping this pushed the day's total past the cap.
            if (
                not preview
                and self.agent.warmup
                and not self.agent.warmup.can_send(account="")
            ):
                return SendVerdict("batch", "warmup", contact=fresh)
            return SendVerdict(
                "cleared", "ok", contact=fresh, outside_window=outside_window
            )

        # Last: the next sending account with warm-up capacity (rotates
        # across mailboxes). None = every account hit today's cap.
        picked = self.agent._pick_sender()
        if picked is None:
            return SendVerdict("batch", "warmup", contact=fresh)
        # A first email that says what its sender is responsible for goes
        # through a mailbox that has a title. process_contact refuses the
        # others, and the retry queue counts that refusal as a failed attempt:
        # a retry landing on the untitled mailbox lost one each time.
        if (
            purpose in ("first", "retry")
            and not dry_run
            and self.first_email_needs_title(fresh)
        ):
            for _turn in range(len(self.agent.senders) - 1):
                if self._titled(picked[2]):
                    break
                picked = self.agent._pick_sender() or picked
            if not self._titled(picked[2]):
                # No mailbox with allowance left today can say it. Deferred:
                # the contact waits and a retry keeps its attempts.
                if not any(self._titled(es) for _k, _n, es in self.agent.senders):
                    self.refuse_first_email("sender_title", _TITLE_MISSING)
                return SendVerdict("defer", "title", "SENDER_TITLE", fresh)
        acct_key, _name, sender = picked
        return SendVerdict(
            "cleared", "ok", contact=fresh, sender=sender, account_key=acct_key
        )

    _send_verdict = send_verdict

    def email_contacts(self, qualified: list[dict], dry_run: bool = False) -> None:
        """Send email to each qualified contact, honoring warmup, domain
        throttling, suppression, and the running flag.

        Shared by fresh-discovery (run_single) and the pending-contact
        backlog (_process_pending_contacts)."""
        self.agent._refresh_domain_backoff()
        try:
            for contact in self.agent._by_lead_score(qualified):
                verdict = self.agent._send_verdict(
                    contact, purpose="first", dry_run=dry_run
                )
                if verdict.scope == "batch":
                    if verdict.kind == "window":
                        logger.info(
                            "Outside the send window - deferring remaining "
                            "sends until business hours."
                        )
                    elif verdict.kind == "warmup":
                        logger.info("All sending accounts reached today's warmup cap.")
                        self.agent._stats["warmup_skipped"] += 1
                        # Once per cap episode, like every other guard: the loop
                        # comes back here every few minutes for the rest of the day.
                        if not self.agent._guard_logged.get("warmup"):
                            self.agent.db.log_activity(
                                "WARMUP_LIMIT",
                                "Daily send limit reached (all accounts)",
                                status="warning",
                            )
                            self.agent._guard_logged["warmup"] = True
                    break
                if verdict.kind == "in_doubt":
                    logger.info(
                        "Skipping %s - a send to it has not settled", contact["email"]
                    )
                    continue
                if verdict.scope == "defer":
                    if verdict.kind == "title":
                        # Left pending, not counted as a domain skip.
                        logger.info(
                            "No mailbox with a SENDER_TITLE has allowance left "
                            "today - %s waits",
                            contact["email"],
                        )
                        continue
                    if verdict.kind == "review":
                        # Its stage is set to manual: a person sends it.
                        continue
                    if verdict.kind == "domain_cap":
                        logger.info(
                            f"Domain limit reached for {verdict.reason} - skipping"
                        )
                    else:
                        backoff = verdict.detail
                        logger.info(
                            "Skipping %s - %s is bouncing at %.0f%% (%d/%d in 7d)",
                            contact["email"],
                            backoff["domain"],
                            backoff["bounce_rate"] * 100,
                            backoff["bounced"],
                            backoff["sent"],
                        )
                    self.agent._stats["domain_skipped"] += 1
                    continue
                if verdict.kind == "already_emailed" and not dry_run:
                    self._repair_emailed_status(verdict.contact)
                if verdict.scope == "drop":
                    continue

                stamp = self.agent._last_dispatch
                sent = self.agent._process_contact(
                    verdict.contact,
                    dry_run,
                    sender=verdict.sender,
                    account_key=verdict.account_key,
                )
                # Pacing is for the receiving servers, so only an attempt they
                # saw keeps the gap; a refusal before dispatch costs nothing.
                # Interruptible so a stop during the inter-send delay takes
                # effect promptly instead of waiting out the full delay.
                if sent or self.agent._last_dispatch != stamp:
                    self.agent._interruptible_sleep(self.agent._send_delay())
        finally:
            # Release every account's pooled SMTP login once the batch is done.
            for _k, _n, es in self.agent.senders:
                es.close()

    _email_contacts = email_contacts

    def _repair_emailed_status(self, contact: dict) -> None:
        """Record that a contact the campaign found already emailed is emailed.

        The status write after a real send can fail (a locked database), and
        the backlog selects on status, so without this the contact is offered
        to every pass. Contained like every other verdict write.
        """
        try:
            if contact.get("status") != "emailed":
                self.agent.db.update_contact_status(contact["id"], "emailed")
            self.agent.db.log_activity(
                "ALREADY_EMAILED",
                f"Skipped {contact['email']}: the first email was already delivered",
                status="info",
            )
        except sqlite3.Error as e:
            logger.error(
                "Could not record that %s was already emailed: %s",
                contact.get("email", "?"),
                e,
            )

    def reconcile_interrupted_sends(self) -> list[dict]:
        """Settle the sends a stopped process left 'preparing', and return them.

        Called where a live pass starts - each pass of the continuous loop, a
        one-off run, a Direct Send - before anything is sent; one indexed
        read when nothing is waiting. First come the results of refused sends
        that met a locked database, kept by this process or held on disk by
        any process (Database.record_refused_send), so that only a send whose
        result no process knows - one cut off by a stop, a kill or a reboot -
        leaves a row to settle. A row young enough to be a send still going out is left
        as it is, and the send check holds its contact back meanwhile; an older
        one is recorded as an unconfirmed send
        (Database.settle_interrupted_sends). Contained: a locked database
        leaves the rows for a later pass, and never costs this one.
        """
        self.agent.db.write_kept_send_results()
        try:
            settled = self.agent.db.settle_interrupted_sends(
                PREPARING_STALE_AFTER,
                sequence_id=self.agent.sequence_id if self.agent.sequences else None,
                count_send=self._count_settled_send,
            )
        except sqlite3.Error as e:
            logger.error(
                "Could not settle the sends left 'preparing' (%s) - their "
                "contacts stay held back until a later pass settles them",
                e,
            )
            return []
        for row in settled:
            logger.warning(
                "[INTERRUPTED] %s: the send was cut off after the server may "
                "have accepted it - recorded as unconfirmed, not resending",
                row["recipient_email"],
            )
        return settled

    def _count_settled_send(self, conn, day, mailbox, email) -> None:
        """Count a settled send against the caps, as process_contact counts one
        that went out, inside the transaction that settles it. Its row names
        the mailbox; the warm-up counter goes by the account's key."""
        if self.agent.warmup:
            keys = {es.smtp_username: key for key, _name, es in self.agent.senders}
            self.agent.warmup.record_send(
                keys.get(mailbox, mailbox or ""), day=day, conn=conn
            )
        if self.agent.domain_throttle and email:
            self.agent.domain_throttle.record_domain_send(
                self.agent.domain_throttle.extract_domain(email), day=day, conn=conn
            )

    def is_public_base_url(self) -> bool:
        """True when BASE_URL is a public address a recipient could reach
        (gates open-tracking and the web unsubscribe link). The rule lives in
        modules.compliance, which doctor shares."""
        return is_public_base_url(self.agent.settings.base_url)

    _is_public_base_url = is_public_base_url

    def unsubscribe_target(self, email: str, record: bool = True) -> str:
        """Unsubscribe href to embed. Signed web URL when BASE_URL is public;
        otherwise a working mailto (picked up by the IMAP monitor). A preview
        (record=False) gets the same link without recording it."""
        if self.agent._is_public_base_url() and self.agent.unsubscribe_handler:
            return self.agent.unsubscribe_handler.generate_unsubscribe_url(
                email, self.agent.settings.base_url, record=record
            )
        return f"mailto:{self.agent.settings.smtp_username}?subject=Unsubscribe"

    _unsubscribe_target = unsubscribe_target

    @staticmethod
    def _build_greeting(contact: dict, language: str = "en") -> str:
        """A human first-name greeting, e.g. 'Hi Maria,', for the first email
        and both follow-ups alike.

        Falls back to 'Hi there,' (warmer and more natural than 'Dear Sir/Madam')
        when we don't have a usable personal name, or the name is not a
        person's: more than three words, or a whole word such as 'Customer'
        or 'Office' (_NOT_A_PERSON). A title is skipped to reach the first
        name, and one word alone after a title is a surname: 'Herr Weber'.
        An Italian email opens 'Buongiorno,' with no name, as the client's own
        first email does: a first name before formal Lei text mixes two
        registers."""
        name = (contact.get("name") or "").strip()
        words = name.split()
        given = list(itertools.dropwhile(_is_title, words))
        if (
            given
            and name.lower() not in ("sir/madam", "n/a", "unknown", "none")
            and (len(given) > 1 or given == words)
            and len(given) <= 3
            and _NOT_A_PERSON.isdisjoint(re.findall(r"\w+", name.lower()))
        ):
            first = given[0]
            first = first[:1].upper() + first[1:]
            return "Buongiorno," if language == "it" else f"Hi {first},"
        return "Buongiorno," if language == "it" else "Hi there,"

    @staticmethod
    def _body_to_html(text: str) -> str:
        """Turn plain body text (blank-line separated paragraphs) into simple,
        email-safe <p> blocks."""
        import html as _html

        paras = [
            p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()
        ]
        return "\n".join(
            f'<p style="margin: 0 0 16px 0;">{_html.escape(p).replace(chr(10), "<br>")}</p>'
            for p in paras
        )

    def build_email_body(self, contact: dict) -> str:
        """Return the email body as HTML paragraphs.

        Uses the AI to write a natural, human message per contact when
        ``ai_humanize_emails`` is on and a provider is configured; otherwise a
        concise, hand-written default that still reads like a person."""
        org = (contact.get("organization") or "your company").strip()

        if (
            self.agent.settings.ai_humanize_emails
            and self.agent.ai_gen
            and getattr(self.agent.ai_gen, "is_configured", False)
        ):
            try:
                body = self.agent.ai_gen.generate_email_body(
                    contact,
                    self.agent.settings.sender_name,
                    self.agent.settings.company_name,
                )
                if body and body.strip():
                    self.agent._stats["ai_body"] = (
                        self.agent._stats.get("ai_body", 0) + 1
                    )
                    return self.agent._body_to_html(body)
            except (AttributeError, TypeError) as e:
                # The generator retries and falls back to "" on any provider
                # error, so only an unusable contact field reaches us here.
                logger.warning(
                    f"Email humanization failed for {contact.get('email')}: {e}"
                )

        # AI disabled, unconfigured, rate-limited, or empty -> hand-written body.
        self.agent._stats["ai_body_fallback"] = (
            self.agent._stats.get("ai_body_fallback", 0) + 1
        )
        # The client's own words for what it does, from its profile: a fixed
        # sentence here once described the previous client's business to
        # Omega's buyers.
        from modules.client_profile import PROFILE

        offer = ", ".join(PROFILE.value_proposition_phrases.get("en", [])[:3])
        default = (
            f"I came across {org} and wanted to reach out. We're "
            f"{self.agent.settings.company_name}: {offer}. "
            f"If parts like these are ever on your list, would you be open "
            f"to a short call?"
        )
        return self.agent._body_to_html(default)

    _build_email_body = build_email_body

    def build_opening_line(self, contact: dict) -> str:
        """Return a personalized opening sentence for the email.

        Uses the AI model on the client's own server when AI personalization is enabled and configured;
        otherwise falls back to a safe generic opener referencing the org."""
        org = contact.get("organization") or "your organization"
        default = f"I've been following the work at {org} with interest."

        if not (
            self.agent.settings.ai_personalization_enabled
            and self.agent.ai_gen
            and getattr(self.agent.ai_gen, "is_configured", False)
        ):
            return default

        try:
            line = self.agent.ai_gen.generate_opening_line(contact)
            return line.strip() if line and line.strip() else default
        except (AttributeError, TypeError) as e:
            # As above: provider errors already come back as "", so what is left
            # is an unusable contact field.
            logger.warning(f"Personalization failed for {contact.get('email')}: {e}")
            return default

    _build_opening_line = build_opening_line

    @staticmethod
    def _default_template() -> str:
        """Fallback email template if the HTML file is not found."""
        return """
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
            <p>{{greeting}}</p>
            <p>{{opening_line}}</p>
            <p>
                I am writing to introduce <strong>{{company_name}}</strong> and explore
                a potential partnership with <strong>{{organization}}</strong>.
            </p>
            <p>Please find our company presentation attached for your review.</p>
            <p>
                We would welcome the opportunity to schedule a brief call at your
                convenience to discuss potential collaboration.
            </p>
            <br/>
            <p>Best regards,<br/><strong>{{sender_name}}</strong><br/>{{company_name}}</p>
            <hr style="border: none; border-top: 1px solid #ccc;"/>
            <p style="font-size: 11px; color: #999;">
                If you prefer not to receive further emails,
                <a href="{{unsubscribe_url}}">click here to unsubscribe</a>.
            </p>
            {{legal_footer}}
            {{tracking_pixel}}
        </body>
        </html>
        """

    def screen_contact(self, contact: dict) -> tuple[str, str, str] | None:
        """Why this contact must not be emailed at all, or None.

        Returns ``(kind, reason, match)``: kind is "screened" (not one
        address, or a personal or PEC address), "competitor" or "client";
        reason is the discard reason recorded on the contact; match is what
        matched (the screen, or the register entry). Each refusal is logged
        and written to the contact, so a follow-up or retry that reaches it
        later sees a closed contact.
        """
        # Screens on the ADDRESS, here as well as at harvest: contacts stored
        # before the personal-mailbox list grew still reached the backlog,
        # follow-ups and retries. A private mailbox needs consent (Art. 130
        # Codice Privacy); a PEC address is legal correspondence. A row
        # stored before the one-address rule may hold a list or a display
        # name, which would reach everyone in it.
        email = contact.get("email") or ""
        domain = email.rsplit("@", 1)[-1].lower()
        screened = (
            "not one address the agent can email"
            if not is_address(email.strip().lower())
            else "PEC (certified mail) address"
            if SearchModule._is_certified_mail(domain)
            else "personal mailbox"
            if domain in PERSONAL_DOMAINS
            else None
        )
        if screened:
            logger.info("Skipping %s - %s", email, screened)
            self.agent.db.log_activity(
                "ADDRESS_SCREENED", f"Skipped {email}: {screened}", status="warning"
            )
            if contact.get("id"):
                try:
                    self.agent.db.update_contact_status(
                        contact["id"], "irrelevant", discard_reason=screened
                    )
                except sqlite3.Error as e:
                    logger.error("Could not record the address verdict: %s", e)
            return "screened", screened, screened

        # Known competitor. Checked at the same chokepoint as the client screen
        # below, and for the same reason: the keyword gate runs at
        # qualification, but a contact can reach here from the backlog or a
        # Direct Send without passing it. Pitching contract manufacturing to a
        # rival is the most embarrassing mail this agent can send.
        competitor = self.agent._known_competitor(contact.get("organization"))
        if competitor:
            logger.info(
                "Skipping %s - %r is a known competitor (%s)",
                email,
                contact.get("organization"),
                competitor,
            )
            self.agent.db.log_activity(
                "KNOWN_COMPETITOR",
                f"Skipped {email}: {contact.get('organization')} is a known "
                f"competitor ({competitor})",
                status="warning",
            )
            if contact.get("id"):
                # Contained like every other verdict write: the refusal is
                # already decided, and a locked database must not take the
                # rest of the batch down with it.
                try:
                    self.agent.db.update_contact_status(
                        contact["id"],
                        "irrelevant",
                        discard_reason=f"competitor: {competitor}",
                    )
                except sqlite3.Error as e:
                    logger.error(
                        "Could not record the competitor verdict for %s: %s",
                        email,
                        e,
                    )
            return "competitor", f"competitor: {competitor}", competitor

        # Active client. Checked HERE as well as during qualification, because
        # this is the one choke point EVERY send passes through - manual Direct
        # Send, follow-up sequence steps and retries included, none of which go
        # near the qualification gate. A cold pitch to a current customer is
        # the single mistake this agent can make that costs an existing
        # relationship rather than a hypothetical one.
        client = self.agent._active_client(contact.get("organization"))
        if client:
            logger.info(
                "Skipping %s - %r is an active client (%s)",
                email,
                contact.get("organization"),
                client,
            )
            self.agent.db.log_activity(
                "ACTIVE_CLIENT",
                f"Skipped {email}: {contact.get('organization')} is a client ({client})",
                status="warning",
            )
            # Recorded, as qualification does: a follow-up or retry that
            # reaches here must see the contact is closed and stop.
            if contact.get("id"):
                try:
                    self.agent.db.update_contact_status(
                        contact["id"],
                        "active_client",
                        discard_reason=f"Active client: {client}",
                    )
                except sqlite3.Error as e:
                    logger.error("Could not record the client verdict: %s", e)
            return "client", f"Active client: {client}", client

        return None

    _screen_contact = screen_contact

    def opt_out_held(self, email: str) -> bool:
        """Whether an opt-out for this address, or for its whole company, is
        waiting on disk until the database can take it."""
        db_path = self.agent.db.db_path
        handler = self.agent.unsubscribe_handler
        token = handler.token_for(email) if handler else None
        held_link = token is not None and (
            token in pending_tokens(db_path) or is_set_aside(db_path, token)
        )
        return (
            email.strip().lower() in pending_addresses(self.agent.db.db_path)
            or held_link
            or blockable_domain(email) in pending_company_domains(self.agent.db.db_path)
        )

    def refresh_templates_if_changed(self) -> None:
        """Re-read the first-email template and its A/B variants when one changed.

        Both were read once, when the agent was built, so the editor's "Template
        saved successfully!" reached neither the dashboard's Direct Send agent,
        which lives as long as the process, nor a campaign already running. One
        directory listing per send; never raises.

        A file that cannot be used (caught mid-save, not UTF-8, locked) keeps
        the text it had, never the generic default nobody reviewed, and is read
        again at each send until it can be: unlike a spreadsheet, it costs
        nothing to look. A deleted main template counts as such a file; a
        deleted variant is a retired arm.

        the client's Italian first email is refreshed the same way, and is never an
        arm. Deleted, Italian recipients get the English one, whole, as when
        the agent starts without it.
        """
        main, italian = "email_template", "email_template_it"
        handler = self.agent.file_handler
        try:
            # Noted BEFORE the reads: a save between the two looks new next time.
            stamps = handler.template_stamps()
            if stamps == self.agent._template_stamps:
                return
            read = handler.get_template_variants()
        except OSError as e:
            logger.warning("Could not check the email templates: %s", e)
            return

        texts = {name: html for name, html in read if html.strip()}
        broken = sorted(((stamps.keys() - {italian}) | {main}) - texts.keys())
        previous = dict(self.agent._template_variants)
        texts.update({name: previous[name] for name in broken if previous.get(name)})
        if main in texts:
            self.agent._email_template = texts[main]
        self.agent._template_variants = sorted(texts.items())
        reread = list(texts)
        if italian in stamps:
            text = self._read_template(f"{italian}.html")
            if text:
                self.agent._email_template_it = text
                reread.append(italian)
            else:
                broken.append(italian)
        elif self.agent._email_template_it:
            self.agent._email_template_it = None
            logger.warning(
                "templates/email_template_it.html not found - Italian "
                "recipients will get the English first email."
            )
        self.agent._template_stamps = None if broken else stamps

        new_version = stamps != self.agent._template_failed
        if new_version:
            # The footer guard logs once while it stays tripped. Without a
            # restart to clear that, a later bad save would be refused unseen.
            self.agent._guard_logged.pop("legal_footer", None)
        if broken:
            self.agent._template_failed = stamps
            if not new_version:
                return
            action, status = "TEMPLATE_RELOAD_FAILED", "error"
            names = ", ".join(f"{name}.html" for name in broken)
            details = (
                f"Cannot use {names} (empty, unreadable or deleted): any previous "
                "text stays in use until it is fixed."
            )
            logger.error(details)
        else:
            self.agent._template_failed = None
            action, status = "TEMPLATE_RELOADED", "info"
            details = (
                f"First-email templates re-read after an edit: {', '.join(reread)}"
            )
            logger.info(details)
        try:
            self.agent.db.log_activity(action, details, status=status)
        except sqlite3.Error as e:
            logger.warning("Could not log the template reload: %s", e)

    def _read_template(self, name: str) -> str | None:
        """The template's text, or None when it is blank or cannot be read."""
        try:
            text = self.agent.file_handler.get_email_template(name)
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Could not read template %s: %s", name, e)
            return None
        return text if text and text.strip() else None

    @staticmethod
    def review_evidence(
        contact: dict, recorded: dict, language: str, sender: str
    ) -> dict:
        """What a proposal rests on, shown to the reviewer before the draft."""

        def field(name):
            return contact.get(name) or recorded.get(name)

        return {
            "organization": field("organization"),
            "found_on_their_site": (field("qualification_rationale") or "")[:600],
            "source_url": field("source_url"),
            "lead_score": field("lead_score"),
            "lead_tier": field("lead_tier"),
            "company_country": field("company_country"),
            "role": field("role"),
            "language": language,
            "sender": sender,
        }

    def process_contact(
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
    ) -> bool | ClaimLost:
        """Process a single contact: personalize email, attach PDF, dispatch.

        ``template_override``/``subject_override`` let follow-up sequence steps
        reuse this pipeline with a different template and subject, and
        ``sequence_step_id`` tags the send with the step it is.
        ``language`` is the email's, "it" or "en"; None decides it for the
        contact (compliance.language_for), as every send path does. A
        follow-up whose Italian file is missing passes "en", so the email it
        sends is English throughout.
        Returns True if the email was sent successfully, and CLAIM_LOST if
        another send holds the contact: a refusal, handled as the send check's
        hold is."""
        email = contact.get("email")
        if not email:
            logger.warning("Contact has no email address - skipping")
            return False

        # An unsubscribe that met a locked database waits on disk. Apply it
        # first; while it still cannot be applied, the address is refused.
        handler = self.agent.unsubscribe_handler
        db_path = self.agent.db.db_path
        replay_optout_spool(
            self.agent.db, resolve=handler.verify_token if handler else None
        )
        token = handler.token_for(email) if handler else None
        if token and is_set_aside(db_path, token):
            # A held click no recorded link matched (a restore older than the
            # link): this recipient's own link, so it is theirs to apply.
            try:
                apply_set_aside(self.agent.db, email, token)
            except sqlite3.Error as e:
                logger.warning("Held opt-out not applied yet: %s", e)
        if self.opt_out_held(email):
            logger.info(f"Skipping {email}: its opt-out is held, not yet applied")
            return False

        # Check suppression list before sending
        if self.agent.db.is_suppressed(email):
            logger.info(f"Skipping suppressed email: {email}")
            self.agent.db.log_activity(
                "SUPPRESSED", f"Skipped suppressed email: {email}", status="info"
            )
            return False
        if self.agent.db.company_blocked(email):
            logger.info(f"Skipping {email}: its company is blocked")
            self.agent.db.log_activity(
                "COMPANY_BLOCKED", f"Skipped {email}: company blocked", status="info"
            )
            return False

        # A first email past the limit would carry its notice too late. The
        # contact is refused, not sent late, and erased at the next retention
        # run. A follow-up comes after a sent email, so it is never refused
        # here. Every caller passes a stored contact row, which carries the
        # collection time.
        if first_contact_overdue(
            self.agent.settings, contact.get("timestamp")
        ) and not self.agent.db.has_been_emailed(contact.get("id")):
            logger.info(
                f"Skipping {email}: not emailed within "
                f"{self.agent.settings.first_contact_max_age_days} days of collection"
            )
            return False

        # The interim country lock, per recipient. Logged here only: the
        # contact stays pending and is tried again each pass, and one dashboard
        # row per attempt would bury the audit trail. Dry runs preview all.
        if not dry_run:
            refusal = country_refusal(self.agent.settings, email)
            if refusal:
                logger.info(f"Skipping {email}: {refusal}")
                return False

        # The address, competitor and client screens. Also run by the send
        # check before a mailbox is picked; repeated here as the guarantee
        # for every path, Direct Send included.
        if self.agent._screen_contact(contact):
            return False

        # The basis recorded when the contact was stored - for a Direct Send,
        # the operator's channel's. Without one nobody is emailed live: a
        # contact stored before bases were recorded, or by a path that
        # skipped it (decision L1.4). After the screens, which close a
        # contact for good; this only leaves it waiting. A caller holding only
        # part of the row (an id and an address) gets the stored one's.
        recorded = contact
        if "basis" not in contact and contact.get("id"):
            recorded = self.agent.db.get_contact_by_id(contact["id"]) or {}
        basis = recorded.get("basis")
        if not dry_run and not basis:
            logger.info(f"Skipping {email}: no lawful basis is recorded for it")
            return False

        # Refused, not failed: returning before dispatch leaves the contact
        # pending and queues no retry, so fixing .env loses nothing.
        if self.agent._send_blocker(dry_run):
            return False

        # The review queue (modules/review_queue.py). At "approve" the first
        # pass writes a proposal and sends nothing; a later pass sends exactly
        # the text a person approved, and nothing while it waits or after a
        # rejection. Direct Send is a person's own act and is never queued.
        stage = None if manual else ("followup" if is_followup else "first_email")
        level = level_for(self.agent.settings, stage) if stage else "auto"
        proposing, review_item, approved = False, None, None
        if level == "manual":
            return False
        if level == "approve":
            item = self.agent.review_queue.item_for(
                contact.get("id"), stage, sequence_step_id
            )
            if item is None:
                proposing = True
            elif item["status"] == "approved":
                approved = self.agent.review_queue.approved_text(item)
                if approved is None:
                    return False
                review_item = item
            else:
                return False

        # No name: masking covers the address, but a name written here would
        # outlive the person's erasure for as long as the log is kept.
        logger.info(f"Processing: {email} at {contact.get('organization', 'N/A')}")

        # Generate tracking ID and unsubscribe URL
        tracking_id = None
        unsubscribe_url = "#"
        tracking_pixel = ""

        # Open-tracking pixel: only when BASE_URL is publicly reachable. A pixel
        # pointing at localhost can never load for a real recipient (opens would
        # always read zero) and a 1x1 beacon to an unreachable host is itself a
        # spam signal - so we omit it while running locally. Also skipped on
        # manual one-off sends to keep them personal.
        if (
            self.agent.analytics
            and self.agent.settings.open_tracking_enabled
            and not manual
            and self.agent._is_public_base_url()
        ):
            tracking_id = str(uuid.uuid4())
            src = html.escape(f"{self.agent.settings.base_url}/track/{tracking_id}")
            tracking_pixel = (
                f'<img src="{src}" width="1" height="1" style="display:none" alt="" />'
            )

        # Unsubscribe target: the signed web link when BASE_URL is public,
        # otherwise a mailto: the recipient's client turns into an email, which
        # the IMAP monitor detects and adds to the suppression list. Keeps
        # unsubscribe actually working (and compliant) even when local-only.
        unsubscribe_url = (
            self.unsubscribe_target(email, record=False)
            if dry_run
            else self.agent._unsubscribe_target(email)
        )

        # An edit to a template counts from the next email: a batch at the
        # default pace runs for hours.
        self.agent.refresh_templates_if_changed()

        # The one language of every email to this contact: its first email's
        # once it had one, else Italian for an address or a company in
        # ITALIAN_COUNTRIES, English otherwise. With no Italian first email on
        # disk, the English one goes, whole.
        language = (
            language
            or language_for(self.agent.settings, self.agent.db, contact, email)[0]
        )
        if template_override is None and not self.agent._email_template_it:
            language = "en"
        italian = language == "it"

        # Select the body template + subject (with optional A/B variant). The
        # A/B variants are English: an Italian recipient gets the client's Italian
        # text, under EMAIL_SUBJECT_IT, or under the English subject logic,
        # variants included, while EMAIL_SUBJECT_IT is blank.
        ab_variant = None
        subject_it = self.agent.settings.get_email_subject_it() if italian else ""
        if template_override is not None:
            template = template_override
            subject = (
                subject_override
                if subject_override is not None
                else self.agent.settings.get_email_subject()
            )
        elif self.agent.settings.ab_testing_enabled and (
            self.agent._template_variants or len(self.agent._subject_variants) > 1
        ):
            if italian:
                body_name, template = "email_template_it", self.agent._email_template_it
            else:
                body_name, template = (
                    random.choice(self.agent._template_variants)
                    if self.agent._template_variants
                    else ("email_template", self.agent._email_template)
                )
            if subject_it:
                subject = subject_it
            else:
                subject = (
                    random.choice(self.agent._subject_variants)
                    if self.agent._subject_variants
                    else self.agent.settings.get_email_subject()
                )
                subj_idx = (
                    self.agent._subject_variants.index(subject)
                    if subject in self.agent._subject_variants
                    else 0
                )
                ab_variant = f"{body_name}|s{subj_idx}"
        elif italian:
            template = self.agent._email_template_it
            subject = subject_it or self.agent.settings.get_email_subject()
        else:
            template = self.agent._email_template
            subject = self.agent.settings.get_email_subject()

        if approved is not None:
            subject, template = approved
            ab_variant = None

        # doctor reports a template without the legal footer as a blocking
        # [FAIL] and says live sending is refused - so refuse it here, where
        # the template is actually chosen. An A/B variant or a follow-up file
        # edited on disk never passes the dashboard editor's check.
        if not dry_run and "{{legal_footer}}" not in (template or ""):
            logger.error("Live send to %s refused: template has no legal footer", email)
            if not self.agent._guard_logged.get("legal_footer"):
                try:
                    self.agent.db.log_activity(
                        "LEGAL_FOOTER_MISSING",
                        "Live sending refused: an email template has no "
                        "{{legal_footer}} placeholder, so it would go out without "
                        "the sender's legal identity. Run: python main.py doctor",
                        status="error",
                    )
                    self.agent._guard_logged["legal_footer"] = True
                except sqlite3.Error as e:
                    logger.error("Could not log the missing-footer refusal: %s", e)
            return False

        # The account this send goes through: the send check picks one for the
        # campaign, follow-ups and retries; Direct Send uses the primary mailbox.
        snd = sender or self.agent.email_sender
        sender_name = snd.sender_name or self.agent.settings.sender_name
        title = (
            getattr(snd, "sender_title", self.agent.settings.sender_title) or ""
        ).strip()

        # A text that says what its sender is responsible for is not sent
        # with a hole there. Refused, not failed, like a blank legal identity:
        # the contact stays pending, and a dry run shows where it goes.
        if "{{sender_title}}" in template and not title and not dry_run:
            self.refuse_first_email("sender_title", _TITLE_MISSING)
            return False

        # "Please find attached our Company Profile": a first email is not
        # sent without it. A dry run composes the email and says so, once.
        pdf_path = None
        if attach_pdf and self.agent.settings.attach_profile:
            problem = self.profile_problem()
            if problem and not dry_run:
                self.refuse_first_email("profile_pdf", problem)
                return False
            if problem:
                if not self.agent._guard_logged.get("profile_pdf_dry_run"):
                    said = f"A live first email would be refused: {problem}"
                    logger.warning(said)
                    try:
                        self.agent.db.log_activity(
                            "PROFILE_PDF_MISSING", said, status="warning"
                        )
                        self.agent._guard_logged["profile_pdf_dry_run"] = True
                    except sqlite3.Error as e:
                        logger.error("Could not log the missing PDF: %s", e)
            else:
                pdf_path = self.agent._pdf_path

        # Personalize the subject with the recipient's company. Using their name
        # (not ours) and varying it avoids the identical-subject spam signal.
        # The stand-in for a missing name is in the subject's language: an
        # Italian recipient under the English subject (EMAIL_SUBJECT_IT blank)
        # reads "your brand", not English with "la Sua azienda" in it.
        italian_subject = italian and (
            bool(subject_it)
            if template_override is None
            else subject_override is not None
        )
        subject_org = name_for_header(
            contact.get("organization"),
            UNKNOWN_ORGANIZATION["it"] if italian_subject else "your brand",
        )
        if subject_org.endswith("."):
            subject = subject.replace("{org}.", "{org}")
        subject = subject.replace("{org}", subject_org)

        # Filled by render_template, in one pass, so no inserted value is
        # searched for placeholders again: `values` are escaped there, once,
        # and `markup` is HTML escaped where it was built.
        # The legal footer, built here so every path - first email, follow-up,
        # A/B variant, fallback, Direct Send - carries the same identity, in
        # the email's language.
        markup = {
            "legal_footer": render_legal_footer(
                self.agent.settings,
                recorded,
                unsubscribe_url,
                basis=basis or "legitimate_interest",
                language=language,
            ),
            "tracking_pixel": tracking_pixel,
        }
        values = {
            "unsubscribe_url": unsubscribe_url,
            # Only a dry run gets here with none: it shows where it goes.
            "sender_title": title or "[set SENDER_TITLE]",
        }
        # The signature is lines of typed text, each escaped here, with <br>
        # between them. Blank: the sender's name, title and company; a
        # missing title is left out, not marked twice.
        if "{{sender_signature}}" in template:
            lines = _signature_lines(getattr(snd, "sender_signature", "")) or [
                sender_name,
                *([title] if title else []),
                self.agent.settings.company_name,
            ]
            markup["sender_signature"] = "<br>".join(html.escape(x) for x in lines)

        # A proposal keeps the parts filled at send time as placeholders, so
        # the reviewer can neither remove them nor approve a link that is stale
        # by the time it goes.
        if proposing:
            markup["legal_footer"] = "{{legal_footer}}"
            markup["tracking_pixel"] = "{{tracking_pixel}}"
            values["unsubscribe_url"] = "{{unsubscribe_url}}"

        # Human, per-contact greeting + body - only when the template uses them.
        if "{{greeting}}" in template:
            values["greeting"] = self.agent._build_greeting(contact, language)
        if "{{body}}" in template:
            body_html = self.agent._build_email_body(contact)
            # First-email profile: attach the PDF, OR (deliverability-safe mode)
            # link it in the body instead of attaching.
            if (
                attach_pdf
                and not self.agent.settings.attach_profile
                and self.agent.settings.profile_url
            ):
                url = html.escape(self.agent.settings.profile_url)
                body_html += (
                    f'<p style="margin: 0 0 16px 0;">You can see our company '
                    f'profile here: <a href="{url}">{url}</a></p>'
                )
            markup["body"] = body_html

        # Personalized opening line - only when the template actually uses it
        # (kept for the follow-up templates that still rely on it).
        if "{{opening_line}}" in template:
            values["opening_line"] = self.agent._build_opening_line(contact)

        # Render personalized email (signature uses the chosen account's name)
        try:
            html_body, plain_body = EmailSender.render_template(
                template,
                contact,
                self.agent.settings.company_name,
                sender_name,
                values=values,
                markup=markup,
                language=language,
            )
        except (AttributeError, TypeError, ValueError) as e:
            # Placeholder substitution + HTML parsing over scraped contact
            # fields: a non-string or unusable value is what breaks it. Nothing
            # has been dispatched at this point, so returning False is safe.
            logger.error(f"Template render failed for {email}: {e}")
            self.agent.db.log_activity(
                "TEMPLATE_ERROR", f"Failed for {email}: {e}", status="error"
            )
            self.agent._stats["emails_failed"] += 1
            return False

        if proposing:
            ai_on = bool(
                self.agent.settings.ai_personalization_enabled
                and self.agent.ai_gen
                and getattr(self.agent.ai_gen, "is_configured", False)
            )
            self.agent.review_queue.propose(
                contact["id"],
                stage,
                subject,
                html_body,
                step_id=sequence_step_id,
                language=language,
                evidence=self.review_evidence(contact, recorded, language, sender_name),
                ai_written=ai_on
                and ("{{opening_line}}" in template or "{{body}}" in template),
            )
            return False

        # Dispatch through the chosen account (subject + ab_variant chosen above)
        # From here on the server sees an attempt, so the loops keep their
        # send delay; every return above is a refusal that sent nothing.
        last_stamp = self.agent._last_dispatch
        self.agent._last_dispatch = next(_DISPATCH_STAMPS)
        result = snd.dispatch_to_contact(
            contact=contact,
            subject=subject,
            html_body=html_body,
            plain_body=plain_body,
            # The company profile goes out on the FIRST email only (attach_pdf),
            # and only when attach_profile is on (otherwise it's linked in the
            # body). Follow-up steps stay light and never re-attach it.
            pdf_path=pdf_path,
            dry_run=dry_run,
            tracking_id=tracking_id,
            unsubscribe_url=unsubscribe_url,
            ab_variant=ab_variant,
            notice_version=notice_version(basis, language),
            # Thread a follow-up under the first mail we actually delivered to
            # this contact, so the sequence arrives as one conversation instead
            # of three unrelated emails - which is what it looked like before,
            # since nothing set a Message-ID to reference. None for a first
            # touch, and for anyone contacted before this existed.
            in_reply_to=(
                self.agent.db.get_thread_message_id(contact["id"])
                if (is_followup and contact.get("id"))
                else None
            ),
            # A Direct Send never asks whether the first email already went,
            # and a follow-up comes after it.
            first_email=not (manual or is_followup),
            sequence_step_id=sequence_step_id,
        )

        if result is CLAIM_LOST:
            # Another process passed the same checks and wrote its row first.
            # Nothing went out, so nothing is counted, queued or delayed.
            self.agent._last_dispatch = last_stamp
            return CLAIM_LOST

        if result:
            self.agent._stats["emails_sent"] += 1
            if review_item is not None and not dry_run:
                try:
                    self.agent.review_queue.mark_sent(review_item["id"])
                except sqlite3.Error as e:
                    logger.error("Sent, but the review item was not marked: %s", e)
            # The basis this send relied on, recorded only once it went out:
            # a preview or a failed send contacted nobody.
            if not dry_run:
                try:
                    self.agent.db.log_consent(
                        email,
                        basis,
                        recorded.get("source_url") or "entered by an operator",
                        contact_id=contact.get("id"),
                        channel=recorded.get("send_channel")
                        or recorded.get("source_channel"),
                        rule_version=recorded.get("rule_version") or RULE_VERSION,
                    )
                except sqlite3.Error as e:
                    logger.error(
                        "Sent to %s but could not record the basis: %s", email, e
                    )
            # EVERY post-send write is contained. The mail is already out by
            # this point, so a bookkeeping failure must not propagate: it would
            # reach send_single_email's handler and be reported to the operator
            # as "Send failed" for an email already sitting in the prospect's
            # inbox - who then retries, sending a real duplicate. On the agent
            # thread the same exception aborts the rest of the batch.
            #
            # enroll_contact below has been wrapped for exactly this reason
            # since an earlier fix; its two neighbours here were left bare.
            if self.agent.warmup and not dry_run:
                try:
                    self.agent.warmup.record_send(account=account_key)
                except sqlite3.Error as e:
                    logger.error(
                        "Sent to %s but could not record the warm-up count: %s. "
                        "Today's allowance is now under-counted.",
                        email,
                        e,
                    )
            if self.agent.domain_throttle and not dry_run:
                domain = self.agent.domain_throttle.extract_domain(email)
                try:
                    self.agent.domain_throttle.record_domain_send(domain)
                except sqlite3.Error as e:
                    logger.error(
                        "Sent to %s but could not record the domain count for %s: "
                        "%s. That domain's cap is now under-counted.",
                        email,
                        domain,
                        e,
                    )
            if self.agent.analytics and tracking_id and not dry_run:
                email_log_id = result if isinstance(result, int) else 0
                if email_log_id:
                    self.agent.analytics.generate_tracking_id(email_log_id, tracking_id)
            # Enroll into the follow-up sequence after the FIRST email only
            # (not for the follow-up sends themselves).
            if (
                not is_followup
                and self.agent.sequences
                and self.agent.sequence_id
                and not dry_run
                and contact.get("id")
            ):
                try:
                    self.agent.sequences.enroll_contact(
                        contact["id"], self.agent.sequence_id
                    )
                except sqlite3.Error as e:
                    # The email is already out. Enrollment is a DB write, and a
                    # failed one must stay contained here: reporting it as a
                    # send failure would re-queue a contact we just emailed.
                    logger.warning(f"Sequence enrollment failed for {email}: {e}")
            return True
        else:
            self.agent._stats["emails_failed"] += 1
            # Queue for retry with the actual email_log_id from the DB.
            # A permanent SMTP rejection suppresses the address inside
            # dispatch_to_contact - re-queueing it would ask the same server the
            # same question twice more and inflate our unknown-user rate.
            # Nor after an SMTP-auth failure: the contact stays pending for
            # the backlog, and a retry would be a second route to the same
            # first email once the credentials are fixed.
            if (
                self.agent.retry_queue
                and not dry_run
                and not is_followup
                and not self.agent.db.is_blocked(email)
                and not getattr(snd, "auth_failed", False)
            ):
                contact_id = contact.get("id", 0)
                # The failed send's own row. This read get_all_emails(limit=1),
                # the newest row in the WHOLE table, so a retry could point at
                # another prospect's email.
                # Contained like every other post-send write. A retry must link
                # to its email_log row (email_log_id is NOT NULL and a foreign
                # key), so when the id cannot be read - a locked database - the
                # insert fails too, and an uncaught error here ended the whole
                # batch. The lost retry is logged instead; the contact stays
                # 'failed', where the dashboard's Send can re-queue it.
                try:
                    email_log_id = self.agent.db.get_last_email_log_id(contact_id)
                    self.agent.retry_queue.add_to_retry(
                        email_log_id, contact_id, "Initial send failed"
                    )
                    self.agent._stats["retries_queued"] += 1
                except sqlite3.Error as e:
                    logger.error(
                        "Send to %s failed and its retry could not be queued: %s",
                        email,
                        e,
                    )
            return False

    _process_contact = process_contact

    def _direct_send_refusal(self, verdict: SendVerdict, email: str) -> str:
        """The operator's message for a Direct Send the send check refused."""
        kind, reason = verdict.kind, verdict.reason
        org = (verdict.contact or {}).get("organization") or email
        if kind == "paused":
            return (
                f"Sending is paused ({reason}). "
                "Resolve the underlying issue before sending."
            )
        if kind == "blocked":
            return f"Live sending is blocked: {reason}."
        if kind == "bounce":
            return (
                "Sending is paused: the bounce-rate circuit breaker "
                "has tripped. Review deliverability before sending."
            )
        if kind == "blacklist":
            return (
                f"Sending is paused: {self.agent._sending_domain} is on a spam "
                f"blocklist, so mail would be silently discarded."
            )
        if kind == "closed" and reason == "suppressed":
            return f"{email} has unsubscribed / is suppressed — not sending."
        if kind == "closed":
            why = {
                "responded": "they replied - answer them personally rather "
                "than with the cold pitch",
                "active_client": "they are recorded as an active client",
                "unsubscribed": "they opted out",
                "erased": "their data was erased at their request",
                "bounced": "the address bounced",
                "blocked": "the contact was blocked from the dashboard",
            }.get(reason, f"the contact is closed ({reason})")
            return f"Not sending to {email}: {why}."
        # Screens on the ADDRESS, which the operator's choice of recipient
        # does not change. Applied in dry runs too: they are about who, not
        # about whether anything is sent.
        if kind == "screened" and reason.startswith("PEC"):
            return (
                f"{email} is a PEC (certified mail) address - legal "
                "correspondence, never a marketing channel."
            )
        if kind == "screened":
            return (
                f"{email} is a personal mailbox. B2B legitimate interest does "
                "not cover private addresses (Art. 130 Codice Privacy needs "
                "prior consent) - use a business address."
            )
        if kind == "competitor":
            return (
                f"{org} is on the competitor list (matches {verdict.detail}) - "
                f"not sending to {email}."
            )
        if kind == "client":
            return (
                f"{org} is an active client (matches {verdict.detail}) - a cold "
                f"pitch would go to a customer, so {email} was not emailed."
            )
        if kind == "in_doubt":
            minutes = int(PREPARING_STALE_AFTER.total_seconds() // 60)
            return (
                f"An email to {email} is {UNSETTLED_SEND} - not sending "
                "another. A send that was cut off is recorded as unconfirmed "
                f"within {minutes} minutes: see the activity log."
            )
        if kind == "cut_off":
            return (
                f"An earlier email to {email} was cut off after it may have "
                "reached them, and has just been recorded as unconfirmed: see "
                "the activity log. Nothing was sent now - send again if they "
                "should have it anyway."
            )
        if kind == "domain_cap":
            return (
                f"Today's limit of "
                f"{self.agent.domain_throttle.max_per_domain_per_day} "
                f"email(s) to {reason} has been reached. Sending more "
                f"in one day to one company reads as a blast."
            )
        if kind == "backoff":
            backoff = verdict.detail
            return (
                f"{backoff['domain']} is bouncing at "
                f"{backoff['bounce_rate']:.0%} "
                f"({backoff['bounced']}/{backoff['sent']} in the last 7 "
                "days), so sending is paused for that domain."
            )
        if kind == "warmup":
            return (
                "The sending mailbox has reached today's warm-up "
                "limit. Try again tomorrow."
            )
        return f"Could not send to {email} ({kind})."

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
    ) -> tuple[bool | AlreadyEmailed, str]:
        """Send one email to a manually-entered address (dashboard 'Direct Send').

        Bypasses discovery/scoring/relevance - the user explicitly chose this
        recipient - but still honors the unsubscribe/suppression list. Returns
        ``(ok, message)`` for the UI; ``ok`` is an AlreadyEmailed when an email
        may already have reached the address and ``confirm_repeat`` is not set."""
        email = (email or "").strip().lower()
        # An "@" with a dot after it let a list through: "anna@brand.es,
        # bruno@brand.de" reached both while the opt-out and country checks
        # read the whole string.
        if not is_address(email):
            return False, "Please enter one valid email address."
        # How the contact reached the client decides the basis for emailing them
        # (decision L1.5). A preview contacts nobody and needs none.
        channel = channel or default_source_channel()
        if not dry_run and channel not in MANUAL_CHANNELS:
            return False, (
                "Choose the channel this contact came through - inbound request, "
                "trade fair, existing customer or referral - before sending."
            )

        # The dashboard's Direct Send agent never runs a loop, so this is the
        # only point where it can notice an edited client or competitor list.
        self.agent.refresh_lists_if_changed()
        # Nor one that settles a send a stopped process left 'preparing',
        # which holds its contact back until it is settled. A preview settles
        # nothing. If this click just settled a send to this very address, the
        # operator has not seen that report yet, and the same email must not
        # go out in the call that files it: the next click is theirs to make.
        settled = [] if dry_run else self.reconcile_interrupted_sends()
        if any((row["recipient_email"] or "").lower() == email for row in settled):
            return False, self._direct_send_refusal(
                SendVerdict("defer", "cut_off"), email
            )

        # The operator's recipient: the stored contact if there is one, with
        # the freshly-typed details preferred - for personalization, and for
        # the client and competitor screens.
        stored = self.agent.db.get_contact_by_email(email)
        contact = dict(stored) if stored else {"email": email}
        if name:
            contact["name"] = name
        if organization:
            contact["organization"] = organization
        if role:
            contact["role"] = role

        # A manual click is still a real send from the same mailbox, so it
        # goes through the same check as the send loops. It once bypassed all
        # of the reputation guards AND still incremented the warm-up counter,
        # pushing the day's total past the cap.
        if not dry_run:
            # The dashboard's send agent never runs a batch, so nothing else
            # fills the cache _domain_backed_off reads: without this refresh
            # the backoff could never fire here.
            self.agent._refresh_domain_backoff()
        verdict = self.agent._send_verdict(contact, purpose="manual", dry_run=dry_run)
        if verdict.scope != "cleared":
            return False, self._direct_send_refusal(verdict, email)
        # A second cold email to the same mailbox is the operator's call, not
        # a double click's: it went out with no word from the page. Asked only
        # after every refusal above, so confirming can never get past one.
        if not dry_run and not confirm_repeat:
            earlier = self.agent.db.last_possibly_sent(email)
            if earlier:
                repeat = AlreadyEmailed(earlier["timestamp"], earlier["language"])
                return repeat, (
                    f"{email} was already emailed on {repeat.on}, in "
                    f"{repeat.language_name}. Nothing was sent now: confirm to "
                    "send another email."
                )
        if verdict.outside_window:
            logger.warning(
                f"Manual send to {email} is outside the configured "
                f"send window - sending anyway (operator action)."
            )
            self.agent.db.log_activity(
                "MANUAL_OUT_OF_WINDOW",
                f"Direct Send to {email} outside business hours",
                status="warning",
            )

        # A preview of an address the agent does not hold stores nothing. A
        # new contact is 'pending', which is what the campaign backlog emails,
        # so the previewed address got the cold pitch on the next loop; the
        # operator had looked at a draft and decided not to send. The send
        # logs the simulation without a contact to attach it to.
        preview_only = dry_run and not stored

        # Reuse an existing contact, or create one under the shared manual search.
        if not stored and not preview_only:
            search_id = self.agent.db.get_or_create_manual_search()
            cid = self.agent.db.add_contact(
                search_id,
                email,
                name=name or None,
                role=role or None,
                organization=organization or None,
                qualification_rationale="Added manually via the dashboard.",
                lead_score=50,
                lead_tier="A",
                source_channel=channel,
            )
            stored = (
                self.agent.db.get_contact_by_id(cid)
                if cid
                else self.agent.db.get_contact_by_email(email)
            )
        if not stored and not preview_only:
            return False, "Could not create the contact record."

        # Prefer freshly-typed details for personalization. A preview of a
        # new address keeps the operator's row built above, with no id.
        if stored:
            contact = dict(stored)
            if name:
                contact["name"] = name
            if organization:
                contact["organization"] = organization
            if role:
                contact["role"] = role
        if channel in MANUAL_CHANNELS:
            # This send relies on what the operator knows, not on how the
            # address was first collected (decision L1.5).
            contact["basis"] = basis_for(channel)
            contact["send_channel"] = channel
            contact["rule_version"] = RULE_VERSION

        # Its company's country, before the email is composed, as the
        # campaign's qualify step settles it: a new address typed here is
        # never qualified, so an Italian company on a .com address went in
        # English, and kept English for life. A preview of a new address
        # stores nothing and asks no AI.
        try:
            if contact.get("id"):
                settle_company_country(
                    self.agent.settings, self.agent.db, contact, self.agent.ai_gen
                )
            else:
                country, source = settle_company_country(
                    self.agent.settings,
                    self.agent.db,
                    {**contact, "source_channel": channel},
                    write=False,
                )
                if source:
                    contact["company_country"] = country
                    contact["company_country_source"] = source
        except sqlite3.Error as e:
            logger.warning("Could not record the company country for %s: %s", email, e)

        try:
            # Read before the send, as process_contact reads it: read after,
            # a locked database turned a delivered email into "Send failed",
            # and the operator's second click sent it again.
            language = language_for(self.agent.settings, self.agent.db, contact)[0]
            ok = self.agent._process_contact(
                contact,
                dry_run=dry_run,
                is_followup=not enroll_followup,  # True suppresses sequence enrollment
                manual=True,
            )
        except Exception as e:
            # Deliberately broad: this is the dashboard's boundary. _process_contact
            # spans templating, SMTP and DB writes, and the operator must get a
            # message back instead of a 500 whatever goes wrong in there.
            # logger.exception so the traceback is not lost behind that message.
            logger.exception(f"Manual send to {email} failed")
            return False, f"Send failed: {e}"
        finally:
            # Only close when this is a standalone send. If a campaign is running
            # on this same agent, leave its pooled connection alone.
            if not self.agent._running:
                self.agent.email_sender.close()

        if ok is CLAIM_LOST:
            return False, self._direct_send_refusal(
                SendVerdict("defer", "in_doubt"), email
            )
        if ok and dry_run:
            # A preview is how the operator reads the email before it is
            # live, so it says when the live send would be refused.
            refused = self.first_email_problems([self.agent.email_sender])
            return True, f"Draft looks good — would send to {email}." + (
                self._direct_send_contents(language)
            ) + (
                " A live send would be refused: "
                + "; ".join(reason for _key, reason in refused)
                + "."
                if refused
                else ""
            )
        if ok:
            return True, f"Email sent to {email}." + self._direct_send_contents(
                language
            )
        return False, f"Could not send to {email} (check the logs)."

    def _direct_send_contents(self, language: str) -> str:
        """The language of the Direct Send email and how the Company Profile
        goes with it, in plain words: the language process_contact chose for
        this contact, read before the send. A PDF that cannot be sent is said
        by the refusal, so it adds nothing here."""
        if language != "it":
            said = " Written in English."
        elif self.agent._email_template_it:
            said = " Written in Italian."
        else:
            said = (
                " Italian recipient, but the Italian first email is missing, so "
                "it goes in English."
            )
        if not self.agent.settings.attach_profile:
            said += " The Company Profile is linked, not attached."
        elif self.agent._pdf_path:
            said += (
                f" The Company Profile ({Path(self.agent._pdf_path).name}) is attached."
            )
        return said
