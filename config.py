"""
Configuration management using pydantic-settings.
Loads all settings from .env file with validation.

V2: Added settings for warmup, domain throttling, IMAP monitoring,
    unsubscribe tokens, lead scoring, sequences, and analytics.
"""

import contextvars
import ipaddress
import logging
import os
import re
import socket
import sqlite3
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from modules.private_files import make_private_dir

_log = logging.getLogger(__name__)


# Resolve project root (directory containing this file)
PROJECT_ROOT = Path(__file__).resolve().parent


_INET_ATON_PART_RE = re.compile(r"0x[0-9a-f]*|0[0-7]*|[1-9][0-9]*", re.IGNORECASE)

# The OpenAI chat interface's reasoning_effort values, and "" for none sent.
LLM_REASONING_EFFORTS = ("none", "low", "medium", "high", "")


def _numeric_ipv4(host: str) -> ipaddress.IPv4Address | bool:
    """The IPv4 address a host written in the resolver's numeric forms means.

    The C resolver reads 134744072, 0x08080808, 010.8.8.8 and 8.526344 all as
    8.8.8.8 without asking DNS, though ipaddress reads none of them as an IP.
    False when the host is not written that way, so it is a name; None when
    it is, but no IPv4 address fits it."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or not all(
        _INET_ATON_PART_RE.fullmatch(p) for p in parts
    ):
        return False
    values = [
        int(p[2:] or "0", 16)
        if p[:2].lower() == "0x"
        else int(p, 8)
        if p.startswith("0")
        else int(p)
        for p in parts
    ]
    *front, last = values
    if any(v > 255 for v in front) or last >= 256 ** (5 - len(parts)):
        return None
    number = last
    for i, v in enumerate(front):
        number |= v << (24 - 8 * i)
    return ipaddress.IPv4Address(number)


# Where the AI may be: this machine and the private network, nothing else.
# Not ipaddress's is_private, which also counts link-local, documentation
# and reserved ranges.
LOCAL_AI_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
    )
)


def _outside_kind(ip) -> str:
    """What kind of address ip is, or "" when it is on this machine or the
    private network."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_unspecified:
        return "an unspecified address (0.0.0.0 or ::)"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if any(ip in net for net in LOCAL_AI_NETWORKS):
        return ""
    return "a public address"


# False only while validate_override rebuilds settings that already loaded.
# Their LLM_BASE_URL was resolved and checked then, cannot be changed at
# runtime, and is resolved again before every AI call. Resolved on every
# rebuild, one DNS hiccup threw away each dashboard override (the warm-up
# cap, the bounce limits) for that pass, and the agent ran on .env's values.
_RESOLVE_AI_HOST: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_RESOLVE_AI_HOST", default=True
)


def resolve_host(host: str) -> list:
    """Every address the system resolver gives for host. A test replaces it."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos]


def own_server_host_problem(
    host: str, *, unresolved_ok: bool = False, checked: list | None = None
) -> str:
    """Why the AI server's host is not on this machine or the private
    network, or "" when every address it resolves to is.

    Asked when the settings load, by doctor, and again before every AI call,
    so a DNS answer that changes after start-up cannot send a prompt
    outside. The message never names the host, which is part of a setting.

    unresolved_ok: a name that cannot be looked up right now is let through.
    Only the settings load passes it: the AI is optional, and a name server
    that is slow after a restart, or an AI machine that is switched off,
    must not keep the agent from starting and reading opt-outs. Doctor and
    every AI call still refuse the name until it resolves.

    checked: when given, the addresses found are added to it, for the AI call
    to connect to when this returns "": never the name looked up again, whose
    answer may have changed since (modules.ai_generator)."""
    if not host:
        return "names no host"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = _numeric_ipv4(host)
    if ip is None:
        return "has a host that no address fits"
    if ip is False:
        if not _RESOLVE_AI_HOST.get():
            return ""
        try:
            addresses = resolve_host(host)
        except (OSError, UnicodeError, ValueError):
            addresses = []
        if not addresses:
            if unresolved_ok:
                return ""
            return (
                "has a host that does not resolve, so it cannot be shown to "
                "be on this machine or the private network"
            )
    else:
        addresses = [ip]
    if checked is not None:
        checked.extend(addresses)
    for address in addresses:
        kind = _outside_kind(address)
        if kind:
            return (
                f"must point at this machine or the private network, but its "
                f"host resolves to {kind}. The AI runs only on the client's own "
                "server: use 127.0.0.1, a 10.x, 172.16-31.x or 192.168.x "
                "address, ::1, an fc00::/7 address, or a name that resolves "
                "only to those"
            )
    return ""


def own_server_address_problem(address: str, *, unresolved_ok: bool = False) -> str:
    """What is wrong with an LLM_BASE_URL, or "" when nothing is.

    Every prompt that carries a recipient's data goes to this address, so it
    must stay on this machine or the private network: every address its host
    resolves to (or the IP literal it is, in any form the resolver reads as
    one, such as 2130706433) has to be loopback or private. A user name or
    password in the address would be written to every log that names it.
    No message quotes the address.
    """
    if not address:
        return (
            "is required when LLM_PROVIDER is openai_compatible - the server's "
            "address up to and including /v1, for example http://127.0.0.1:11434/v1"
        )
    parts = urlsplit(address)
    if parts.scheme not in ("http", "https"):
        return "must start with http:// or https://"
    if "@" in parts.netloc:
        return "must not carry a user name or password - set LLM_API_KEY instead"
    if "?" in address or "#" in address:
        return "must not carry a query (?) or a fragment (#)"
    if parts.path.rstrip("/").lower().endswith("/chat/completions"):
        return (
            "must stop at the API's root - cut /chat/completions off, so that "
            "for Ollama it ends in /v1"
        )
    host = parts.hostname or ""
    if not host:
        return "names no host"
    try:
        _ = parts.port
    except ValueError:
        return "has a port that is not a number"
    return own_server_host_problem(host, unresolved_ok=unresolved_ok)


class Settings(BaseSettings):
    """Application settings - loaded from environment variables / .env file."""

    # hide_input_in_errors: pydantic quotes a refused value in its error,
    # and start-up prints that error to the console and the Docker log. A
    # misspelt key holding a password was refused as an extra input, and the
    # refusal printed the password.
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    # The cloud AI providers' settings. The agent asks only the model on
    # The client's own server, so a .env that still holds one is refused rather
    # than leaving a cloud key on the machine.
    RETIRED_SETTINGS: ClassVar[tuple[str, ...]] = (
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "GROQ_MODEL",
    )

    # -- SMTP Configuration -----------------------------------------------
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = Field(default="", repr=False)

    # -- Microsoft 365 sign-in without a password ---------------------------
    # The app registered in the client's Microsoft Entra, and the PEM file holding
    # its certificate and private key (docs/DEPLOY.md, "Connect to Microsoft
    # 365 without a password"). Set, every mailbox on smtp.office365.com
    # signs in with a token (OAuth 2.0, XOAUTH2), and no password may be kept
    # for it. All blank: mailboxes sign in with their passwords.
    microsoft_tenant_id: str = ""
    microsoft_client_id: str = ""
    microsoft_certificate_path: str = ""
    microsoft_certificate_passphrase: str = Field(default="", repr=False)

    # Extra sending accounts to rotate through, so the daily total = the sum of
    # each account's warmup cap (each mailbox keeps its own ramp + inbox
    # monitoring). They inherit the primary SMTP/IMAP host+port.
    #
    # Simple way - just fill in the numbered slots (leave unused ones blank):
    sender_2_email: str = ""
    sender_2_password: str = Field(default="", repr=False)
    sender_2_name: str = ""
    sender_2_title: str = ""
    sender_2_signature: str = ""
    sender_3_email: str = ""
    sender_3_password: str = Field(default="", repr=False)
    sender_3_name: str = ""
    sender_3_title: str = ""
    sender_3_signature: str = ""
    sender_4_email: str = ""
    sender_4_password: str = Field(default="", repr=False)
    sender_4_name: str = ""
    sender_4_title: str = ""
    sender_4_signature: str = ""
    sender_5_email: str = ""
    sender_5_password: str = Field(default="", repr=False)
    sender_5_name: str = ""
    sender_5_title: str = ""
    sender_5_signature: str = ""
    # Advanced alternative: a JSON list, for per-account hosts or >4 extras.
    #   [{"username":"a@x.com","password":"pw","sender_name":"...",
    #     "sender_title":"...","sender_signature":"..."}]
    sender_accounts: str = Field(default="", repr=False)

    # -- Human review (modules/review_queue.py) ----------------------------
    # Who decides at each stage: "manual" (the agent does nothing there, a
    # person writes and sends), "approve" (the agent drafts, a named person
    # approves, edits or rejects each one), or "auto" (the agent acts alone).
    # Any other value reads as "approve", never "auto". Opt-outs, bounces and
    # erasures are never queued.
    autonomy_first_email: str = "approve"
    autonomy_followup: str = "approve"
    autonomy_reply: str = "approve"
    # The most proposals one reviewer may approve in a day; past it, approvals
    # stop being reviews.
    review_daily_cap: int = 60

    # -- Hot-lead alerts ---------------------------------------------------
    # When a prospect replies with genuine interest ("yes, let's talk"), the
    # agent emails this person so a human can take over. Blank = feature off,
    # so it can be plugged in later without touching any code. Multiple
    # addresses may be comma-separated.
    lead_alert_email: str = ""

    # -- Sender Identity --------------------------------------------------
    sender_name: str = "Omega IFS"
    company_name: str = "Omega IFS"
    # What the sender is responsible for. The first email says "I am
    # responsible for <this> at Omega IFS" / "mi occupo di <this> in Omega IFS", so
    # it reads best as an area ("Business Development"). Blank is never sent:
    # a live first email whose template uses {{sender_title}} is refused.
    sender_title: str = ""
    # The signature under the first email, its lines separated by the two
    # characters \n. Blank means the sender's name, the title above and the
    # company name, one per line.
    sender_signature: str = ""

    # -- Legal identity (EU commercial-email footer) ----------------------
    # Printed in every email. D.Lgs. 70/2003 Art. 8 (a commercial email must
    # say so, and who sends it), Cod. Civ. 2250 (registered office and
    # Registro Imprese in all correspondence, email included), GDPR Art. 14
    # (controller identity at first contact). Blank means UNKNOWN and is never
    # guessed: doctor fails and live sends are refused until these are set.
    legal_entity_name: str = ""  # e.g. "Omega IFS S.r.l."
    legal_registered_office: str = ""  # sede legale, full postal address
    legal_company_registration: str = ""  # "Reg. Imprese di ... n. ... - P.IVA ..."
    legal_share_capital: str = ""  # Cod. Civ. 2250 c.2 (S.p.A. / S.r.l.)
    # "yes" or "no": whether an S.p.A. or S.r.l. has a single member, which
    # Cod. Civ. 2250 c.4 requires it to state (decision L6).
    legal_single_member: str = ""
    privacy_notice_url: str = ""  # the page holding the full Art. 13/14 notice
    # Where a person writes about their data; printed in every email (L3.2).
    privacy_contact_email: str = ""

    # -- Interim country lock (decision L1.1, until the country table W2) ---
    # Live email goes only to recipients whose country-code domain is listed
    # here: ISO codes, comma-separated, GB for .uk. Blank, the default, means
    # no live email at all - set it only once counsel has signed the table.
    enabled_countries: str = Field(default="", alias="ENABLED_COUNTRIES")
    # .com, .eu and other generic domains show no country. Such a recipient
    # counts as needing consent (PS-10) and is refused unless counsel says so.
    unknown_country_allowed: bool = Field(
        default=False, alias="UNKNOWN_COUNTRY_ALLOWED"
    )
    # -- The language of every email to a contact --------------------------
    # Recipients whose country-code domain is listed here are written to in
    # Italian, everyone else in English, a .com or .eu address included: ISO
    # codes, comma-separated, as above. Blank means English for everyone.
    italian_countries: str = Field(default="IT", alias="ITALIAN_COUNTRIES")
    REQUIRED_LEGAL_FIELDS: ClassVar[tuple[str, ...]] = (
        "legal_entity_name",
        "legal_registered_office",
        "legal_company_registration",
        "privacy_notice_url",
        "privacy_contact_email",
    )
    # An S.p.A. or S.r.l., which must also state its share capital and
    # whether it has a single member (Cod. Civ. 2250 c.2, c.4).
    _CAPITAL_COMPANY: ClassVar[re.Pattern] = re.compile(
        r"\bs\.?\s?(?:r\.?\s?l|p\.?\s?a)\b", re.IGNORECASE
    )

    # -- Data retention (GDPR Art. 5(1)(e)) ------------------------------
    # Once a day: contacts not emailed within first_contact_max_age_days of
    # collection are erased; contacts who never replied, retention_days_
    # contacted after the last email; contacts who replied or became leads,
    # retention_days_engaged after the last exchange; an objection, at the
    # next run. The suppression list is never touched. The footer states the
    # contacted period (Art. 14(2)(a)).
    retention_enabled: bool = True
    # Superseded by first_contact_max_age_days, and no longer read; still
    # accepted so a settings file that sets it keeps loading.
    retention_days_discarded: int = 90
    # The first email carries the notice, due within a month of collection
    # (Art. 14(3)(a)): past this a first email is refused, and the contact
    # is erased at the next run (decision L1.8).
    first_contact_max_age_days: int = Field(
        default=30, alias="FIRST_CONTACT_MAX_AGE_DAYS", ge=1, le=30
    )
    # Someone who replied or became a lead: days after the last exchange -
    # the last email, reply or lead alert (decision L5.1).
    retention_days_engaged: int = Field(
        default=730, alias="RETENTION_DAYS_ENGAGED", ge=30
    )
    # Days of log files kept: they rotate at midnight and older ones are
    # deleted. Addresses in them are masked when written (PrivacyFormatter).
    log_retention_days: int = Field(default=30, alias="LOG_RETENTION_DAYS", ge=1)
    retention_days_contacted: int = 365

    # -- Scraping Configuration -------------------------------------------
    scrape_rate_limit: float = 2.0
    max_results_per_query: int = 20

    # -- File Paths -------------------------------------------------------
    db_path: str = "data/agent.db"
    pdf_path: str = "assets/presentation.pdf"

    # -- Agent Behavior ---------------------------------------------------
    emails_per_hour: int = 30
    delay_between_emails: float = 5.0
    loop_delay_seconds: float = 300.0
    dry_run: bool = False
    debug: bool = False
    # Timezone the agent operates in - daily send caps reset on this zone's
    # calendar day and stored timestamps use it. The customer is Italy-based.
    app_timezone: str = "Europe/Rome"

    # -- Dashboard --------------------------------------------------------
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 5000

    # -- Target Criteria --------------------------------------------------
    # Blank, so a run without --query searches what is queued from the
    # dashboard, then the queries modules/query_generator.py builds: the client's
    # own words for its buyers first, then the supplement categories. The
    # default searched for pharmaceutical distributors and healthcare
    # providers, which are not the client's buyers.
    target_queries: str = ""

    # -- Email Filtering --------------------------------------------------
    blocked_email_prefixes: str = (
        "noreply,no-reply,support,admin,webmaster,abuse,postmaster,mailer-daemon"
    )

    # -- Email Subject & Content ------------------------------------------
    # The first is the English subject; the second goes to recipients written
    # to in Italian, and blank falls back to the English one.
    email_subject: str = "Special cold-formed parts to your drawing | Omega IFS"
    email_subject_it: str = (
        "Particolari speciali stampati a freddo su disegno | Omega IFS"
    )

    # -- V2: Warmup Scheduling --------------------------------------------
    warmup_enabled: bool = True
    warmup_start_daily: int = 5
    warmup_increment: int = 3
    warmup_max_daily: int = 100

    # -- V2: Domain Throttling --------------------------------------------
    max_per_domain_per_day: int = 3

    # -- V2: Retry Queue --------------------------------------------------
    retry_enabled: bool = True
    retry_max_attempts: int = 3
    retry_base_delay_minutes: float = 5.0

    # -- V2: Lead Scoring -------------------------------------------------
    lead_scoring_enabled: bool = True
    min_lead_score_to_email: int = 15

    # -- Active clients: never cold-email an existing customer -------------
    # A cold pitch to a company that already buys from the client is not a lost
    # prospect, it is a live commercial relationship being offered an
    # introduction to its own supplier. Matched on organisation NAME because
    # the client list holds no email addresses, so the suppression list (keyed
    # on address) cannot cover this.
    client_filter_enabled: bool = True
    client_list_path: str = "data/clients.xlsx"

    # -- Known competitors: never pitch contract manufacturing to a rival -----
    # COMPETITOR_KEYWORDS in modules/relevance.py guesses from how a company
    # describes itself, and a guess always misses some: it missed "CDMO", the
    # industry's own term for what the client is, until a live discovery run found
    # a company using it. A list of NAMES is deterministic, and catches the
    # rival whose website says nothing self-incriminating at all.
    #
    # Same file format and matching as the client list. Blank path = off, so
    # the feature costs nothing until the client supplies the names.
    competitor_filter_enabled: bool = True
    competitor_list_path: str = ""

    # Encrypts both lists at rest (modules/list_crypto.py). Set, the agent
    # reads only an encrypted list, in memory only, and refuses a plain one;
    # blank, the lists are plain files and doctor warns. At least
    # SECRET_MIN_LENGTH characters. Back it up apart from the data: without it
    # the encrypted lists cannot be read (docs/DEPLOY.md).
    lists_key: str = Field(default="", alias="LISTS_KEY", repr=False)

    # -- Phase 1: Relevance Filter (target-vertical + European gate) ------
    relevance_filter_enabled: bool = False
    relevance_require_vertical: bool = True
    # The client makes FOOD SUPPLEMENTS / NUTRACEUTICALS (incl. sports nutrition
    # and beauty-from-within), not pharma / topical cosmetics / medical devices.
    # Blank, the gate reads the built-in vocabulary in modules/relevance.py
    # (DEFAULT_VERTICALS, in the five languages the agent meets). A list here
    # replaces it, so the operator can narrow the gate; the client's own buyer
    # phrases are always read. The default used to be an English-only list,
    # which rejected every Italian prospect.
    relevance_verticals: str = ""

    # -- Phase 1: AI Personalization (AI-written opening lines) -----------
    ai_personalization_enabled: bool = False

    # When on (and an AI provider is configured), the AI writes the whole email
    # body in a natural, human voice per contact instead of using the fixed
    # template copy. Falls back to a concise hand-written body if the AI fails.
    ai_humanize_emails: bool = False

    # -- Free heuristic discovery (no API cost; fallback when AI unavailable) --
    heuristic_discovery_enabled: bool = True

    # Stop discovering new targets while this many contacts are still pending,
    # so the agent works through its backlog instead of accumulating thousands.
    max_pending_backlog: int = 300

    # -- Learning loop: bias discovery toward what has produced results --------
    learning_enabled: bool = True

    # -- Phase 2: AI relevance classification (target/no-target) ---------
    ai_relevance_enabled: bool = False

    # When neither the address, the company website nor its page shows a
    # contact's country, the AI is asked from what the fit check sends, so an
    # Italian company on a .com address gets the Italian email. Still nothing
    # is sent without a key and AI_DPA_REFERENCE.
    company_country_ai: bool = True

    # An unclear reply judged by the AI: its own text only, 600 characters.
    ai_reply_classification_enabled: bool = False

    # -- Cognitive layer: AI Strategist (LLM-chosen discovery queries) ----
    # Dormant until a working model key exists; falls back to free discovery.
    ai_strategist_enabled: bool = False

    # -- Phase 2: Deliverability -----------------------------------------
    # Random extra delay (0..N seconds) added between sends to look less bot-like.
    send_jitter_seconds: float = 0.0

    # -- V3: Send window (business hours, in the app timezone) ------------
    # Only send during working hours/days - night-time blasts read as automated
    # and reduce reply rates. Hours are 24h local time; days are Mon=0..Sun=6.
    send_window_enabled: bool = True
    send_window_start_hour: int = 8
    send_window_end_hour: int = 19
    send_window_days: str = "0,1,2,3,4"  # Mon-Fri

    # -- V3: Blacklist (DNSBL) monitoring --------------------------------
    # Check the sending domain against public spam blocklists (Spamhaus DBL,
    # SURBL). Being listed silently kills delivery, so it's worth watching.
    blacklist_check_enabled: bool = True

    # -- V3: Bounce/spam circuit breaker ---------------------------------
    # Auto-pause sending if the recent bounce rate gets dangerously high, to
    # protect sender reputation (mailbox providers want bounces < 2%).
    bounce_guard_enabled: bool = True
    max_bounce_rate: float = 0.05  # pause above 5%
    bounce_guard_min_sends: int = 20  # ignore until enough signal

    # Per-recipient-domain backoff. The guard above blends every domain into a
    # single rate, so one provider rejecting us hard stays invisible until it
    # drags the global number over the line - and then sending stops to
    # everyone, healthy domains included. This backs the offender off on its
    # own. The threshold is deliberately higher than the global one: one
    # provider running warm is normal, and the point is to catch an outlier,
    # not to quit at the first dead address.
    domain_bounce_backoff_enabled: bool = True
    max_domain_bounce_rate: float = 0.30  # skip a domain above 30%
    domain_bounce_min_sends: int = 5  # ignore a domain until enough signal

    # -- V3: Company profile attachment ----------------------------------
    # Attach the profile PDF on first emails, OR (attach_profile=false) link it.
    # A big attachment on cold first-touch hurts deliverability; a link is safer.
    attach_profile: bool = False
    profile_url: str = "https://www.omegaifs.it"  # used as the link when not attaching

    # -- Phase 2: A/B testing --------------------------------------------
    ab_testing_enabled: bool = False
    # Optional comma-separated subject-line variants; blank falls back to EMAIL_SUBJECT.
    email_subjects: str = ""

    # -- V2: Email Verification -------------------------------------------
    verify_emails: bool = True
    verify_mx: bool = True
    verify_smtp_probe: bool = False

    # -- V2: IMAP Monitoring (bounce/reply detection) ---------------------
    imap_enabled: bool = False
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = Field(default="", repr=False)
    imap_check_interval_minutes: int = 30

    # -- V2: Unsubscribe --------------------------------------------------
    # The HMAC key behind every unsubscribe link. Left at this shipped default
    # it is not a secret at all - the string is in the public source, so anyone
    # can compute a valid token for ANY address and POST it to
    # /unsubscribe/<token> to suppress that prospect and cancel their sequence.
    # A competitor could pre-emptively suppress an entire target list. See the
    # validator below, which refuses to start on the default.
    UNSUBSCRIBE_SECRET_DEFAULT: ClassVar[str] = "change-this-to-a-random-secret-key"
    # A shorter key can be guessed offline from one link: the token is
    # HMAC(key, address), and a recipient knows their own address.
    SECRET_MIN_LENGTH: ClassVar[int] = 32
    unsubscribe_secret: str = Field(default=UNSUBSCRIBE_SECRET_DEFAULT, repr=False)
    # Keys every erasure token (modules/database.py, set_erasure_key). Blank
    # means none: the agent and dashboard refuse to start and doctor fails.
    # Back it up apart from the database and never change it - see DEPLOY.md.
    erasure_key: str = Field(default="", alias="ERASURE_KEY", repr=False)
    base_url: str = "http://127.0.0.1:5000"
    # Requests one address may make to the public unsubscribe page in any
    # minute before it is answered 429. A one-click opt-out for a real link
    # is applied whatever the count: mailbox providers send them in bursts.
    unsubscribe_requests_per_minute: int = Field(
        default=30, alias="UNSUBSCRIBE_REQUESTS_PER_MINUTE", ge=1
    )

    # -- V2: Sequences (follow-up emails) ---------------------------------
    sequences_enabled: bool = False
    default_sequence_name: str = "Standard Outreach"

    # V2: Analytics & Dashboard
    # Off by default. A tracking pixel reads information from the recipient's
    # device, which ePrivacy Art. 5(3) / Codice Privacy Art. 122 allow only
    # with prior consent - applied to email pixels by the Garante (provv. 284,
    # 17 Apr 2026) following EDPB Guidelines 2/2023. A cold prospect has given
    # none. Turn it on only for recipients who have.
    open_tracking_enabled: bool = Field(default=False, alias="OPEN_TRACKING_ENABLED")
    dashboard_username: str = Field(default="admin", alias="DASHBOARD_USERNAME")
    dashboard_password: str = Field(default="", alias="DASHBOARD_PASSWORD", repr=False)
    # Named reviewers besides the administrator: "name:password" pairs,
    # comma-separated. The login name is recorded as who approved or rejected
    # each email (modules/review_queue.py). A password shorter than 12
    # characters is ignored. Only the administrator may change who decides.
    dashboard_reviewers: str = Field(
        default="", alias="DASHBOARD_REVIEWERS", repr=False
    )

    # V2: AI Generator
    # The provider's data processing agreement, as a reference to where it
    # is filed. doctor warns while an AI sees recipients' data without one.
    ai_dpa_reference: str = Field(default="", alias="AI_DPA_REFERENCE")

    # LLM provider: "openai_compatible", a server speaking the OpenAI chat
    # interface at LLM_BASE_URL on this machine or the private network, such
    # as Ollama on the client's own server; blank for no AI. There is no other.
    llm_provider: str = ""

    # Read only when LLM_PROVIDER is openai_compatible; blank, they are
    # neither checked nor used. The address runs up to and including /v1.
    # The model has no default, because no name fits every server.
    llm_base_url: str = Field(default="", alias="LLM_BASE_URL", validate_default=True)
    llm_model: str = Field(default="", alias="LLM_MODEL", validate_default=True)
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY", repr=False)
    # A model on the company's own machine can take a while to load; 30 is
    # what every chat call waited before this setting existed.
    llm_timeout_seconds: int = Field(
        default=30, alias="LLM_TIMEOUT_SECONDS", validate_default=True
    )
    # Sent as the chat call's reasoning_effort. A model that reasons first
    # (Qwen 3.5 on Ollama) spent minutes on a one-word question without it,
    # and a second with none; blank sends no such field, for a server that
    # refuses it.
    llm_reasoning_effort: str = Field(
        default="none", alias="LLM_REASONING_EFFORT", validate_default=True
    )

    # ---------------------------------------------------------------------
    # Validators
    # ---------------------------------------------------------------------

    @field_validator("lists_key")
    @classmethod
    def lists_key_stripped(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("scrape_rate_limit")
    @classmethod
    def rate_limit_positive(cls, v: float) -> float:
        if v < 0.5:
            raise ValueError("Rate limit must be >= 0.5 seconds to be respectful")
        return v

    @field_validator("emails_per_hour")
    @classmethod
    def emails_per_hour_reasonable(cls, v: int) -> int:
        if v > 100:
            raise ValueError("emails_per_hour should not exceed 100 for deliverability")
        return v

    @field_validator("loop_delay_seconds")
    @classmethod
    def loop_delay_reasonable(cls, v: float) -> float:
        if v < 5:
            raise ValueError("loop_delay_seconds must be >= 5 to avoid a busy loop")
        return v

    @field_validator("delay_between_emails", "send_jitter_seconds")
    @classmethod
    def send_gap_not_negative(cls, v: float, info) -> float:
        # 0 is legitimate: it switches the gap (or the jitter) off. A negative
        # value was accepted by the dashboard and meant nothing at all.
        if v < 0:
            raise ValueError(f"{info.field_name} cannot be negative - use 0 for no gap")
        return v

    @field_validator("send_window_start_hour", "send_window_end_hour")
    @classmethod
    def send_window_hours_valid(cls, v: int) -> int:
        if not 0 <= v <= 24:
            raise ValueError("send window hours must be between 0 and 24")
        return v

    @field_validator("send_window_end_hour")
    @classmethod
    def send_window_end_after_start(cls, v: int, info) -> int:
        # The window is evaluated as `start <= hour < end`, so an end at or
        # before the start can never be satisfied: the agent would run forever,
        # log "deferring" each loop, and silently send nothing at all.
        start = (info.data or {}).get("send_window_start_hour")
        if start is not None and v <= start:
            raise ValueError(
                "send_window_end_hour must be greater than "
                "send_window_start_hour - otherwise the send window "
                "never opens and no email is ever sent. Overnight windows are "
                "not supported; keep both within one day."
            )
        return v

    @field_validator("privacy_notice_url")
    @classmethod
    def privacy_notice_url_absolute(cls, v: str) -> str:
        # "www.example.it/privacy" renders as a RELATIVE link inside an email,
        # which opens nothing - the recipient is told a notice exists and
        # cannot reach it. Blank is allowed here; doctor reports it.
        v = (v or "").strip()
        if v and not v.lower().startswith("https://"):
            raise ValueError(
                "privacy_notice_url must start with https:// - "
                "without a scheme it is a dead link in every email, and over "
                "plain http the notice can be altered on its way."
            )
        return v

    @field_validator("privacy_contact_email")
    @classmethod
    def privacy_contact_is_an_address(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("privacy_contact_email is not an email address")
        return v

    @field_validator("legal_single_member")
    @classmethod
    def single_member_is_yes_or_no(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in ("", "yes", "no"):
            raise ValueError("legal_single_member must be yes or no")
        return v

    @field_validator("enabled_countries", "italian_countries")
    @classmethod
    def country_codes(cls, v: str, info) -> str:
        bad = [
            c
            for c in (v or "").split(",")
            if c.strip() and not (len(c.strip()) == 2 and c.strip().isalpha())
        ]
        if bad:
            raise ValueError(
                f"{info.field_name.upper()} takes two-letter country codes such "
                f"as IT,FR - {len(bad)} of its entries are not"
            )
        return v

    @field_validator("llm_provider")
    @classmethod
    def llm_provider_known(cls, v: str) -> str:
        # Read whatever its case: compared as written, "OpenAI_Compatible"
        # meant no AI without a word.
        provider = (v or "").strip().lower()
        if provider not in ("", "openai_compatible"):
            raise ValueError(
                "LLM_PROVIDER must be openai_compatible, for the AI model on "
                "the client's own server, or blank for no AI. Gemini and Groq "
                "were removed: no outside AI company may receive data"
            )
        return provider

    @model_validator(mode="before")
    @classmethod
    def no_cloud_ai_settings(cls, data):
        if not isinstance(data, dict):
            return data
        # The process environment too: pydantic-settings passes on only the
        # names it knows from there, and Docker's env_file turns .env into
        # environment variables, so in the container a cloud key in .env
        # reached neither the data nor this check.
        in_environment = {
            key.upper() for key in os.environ if key.upper() in cls.RETIRED_SETTINGS
        }
        found = sorted(
            {
                key.upper()
                for key in data
                if isinstance(key, str) and key.upper() in cls.RETIRED_SETTINGS
            }
            | in_environment
        )
        if found:
            lines = ", ".join(f"the line that starts {key}=" for key in found)
            exported = (
                f" {', '.join(sorted(in_environment))} is also set in the "
                "environment (Docker's env_file reads it from .env): remove it "
                "there too."
                if in_environment
                else ""
            )
            raise ValueError(
                f"{', '.join(found)} belongs to a cloud AI provider the agent no "
                "longer has: it asks only the AI model on the client's own server. "
                f"Delete {lines} from .env, then start again.{exported}"
            )
        return data

    @model_validator(mode="before")
    @classmethod
    def llm_base_url_without_password(cls, data):
        # pydantic quotes a refused value as it came in, and start-up prints
        # the refusal to the Docker log or the task's output. An address with
        # a password in it is refused anyway, so the password is masked here,
        # before the field's own check can quote it.
        if not isinstance(data, dict):
            return data
        masked = {}
        for key, value in data.items():
            if key.lower() == "llm_base_url" and isinstance(value, str):
                parts = urlsplit(value.strip())
                if "@" in parts.netloc:
                    host = parts.netloc.rpartition("@")[2]
                    value = parts._replace(netloc=f"***@{host}").geturl()
            masked[key] = value
        return masked

    @field_validator("llm_base_url")
    @classmethod
    def llm_base_url_usable(cls, v: str, info) -> str:
        v = (v or "").strip()
        if (info.data or {}).get("llm_provider") != "openai_compatible":
            return v
        problem = own_server_address_problem(v, unresolved_ok=True)
        if problem:
            raise ValueError(f"LLM_BASE_URL {problem}")
        # Every trailing slash, as AIGenerator drops them, so that doctor
        # asks the same address the agent calls.
        return v.rstrip("/")

    @field_validator("llm_model")
    @classmethod
    def llm_model_named(cls, v: str, info) -> str:
        v = (v or "").strip()
        if (info.data or {}).get("llm_provider") == "openai_compatible" and not v:
            raise ValueError(
                "LLM_MODEL is required when LLM_PROVIDER is openai_compatible - "
                "the model's name as the server knows it, for example qwen2.5:14b"
            )
        return v

    @field_validator("llm_timeout_seconds")
    @classmethod
    def llm_timeout_in_range(cls, v: int, info) -> int:
        # Under 5 a local model still loading fails every call; over 300 one
        # hung server holds a send pass for five minutes per contact.
        if (info.data or {}).get("llm_provider") == "openai_compatible" and not (
            5 <= v <= 300
        ):
            raise ValueError("LLM_TIMEOUT_SECONDS must be between 5 and 300 seconds")
        return v

    @field_validator("llm_reasoning_effort")
    @classmethod
    def llm_reasoning_effort_known(cls, v: str, info) -> str:
        # A misspelt value would reach the server as it is, and a server that
        # refuses it answers nothing for the whole run.
        effort = (v or "").strip().lower()
        if (info.data or {}).get(
            "llm_provider"
        ) == "openai_compatible" and effort not in LLM_REASONING_EFFORTS:
            raise ValueError(
                "LLM_REASONING_EFFORT must be none, low, medium, high or blank"
            )
        return effort

    @model_validator(mode="after")
    def microsoft_signin_usable(self):
        problems = self.microsoft_signin_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @field_validator("retention_days_discarded", "retention_days_contacted")
    @classmethod
    def retention_long_enough(cls, v: int) -> int:
        # Same failure shape as the validators below: at a few days the daily
        # job erases the backlog before it can ever be emailed, and the agent
        # runs forever sending nothing.
        if v < 30:
            raise ValueError(
                "the retention period must be at least 30 days - shorter "
                "erases contacts before the agent can reach them."
            )
        return v

    @field_validator("warmup_start_daily")
    @classmethod
    def warmup_start_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("warmup_start_daily must be >= 1")
        return v

    @field_validator("max_per_domain_per_day")
    @classmethod
    def domain_limit_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_per_domain_per_day must be >= 1")
        return v

    # The validators below all guard the same failure shape as
    # send_window_end_after_start: a value the app accepts happily and which
    # then makes it run forever while sending nothing. Only that one field had
    # the treatment; these are the rest.

    @field_validator("warmup_max_daily")
    @classmethod
    def warmup_max_positive(cls, v: int) -> int:
        # 0 or negative yields allowance=0, so can_send() is always False: the
        # agent loops indefinitely and never sends a single email.
        if v < 1:
            raise ValueError(
                "warmup_max_daily must be >= 1 - at 0 the daily allowance "
                "is zero and the agent runs forever without sending anything."
            )
        return v

    @field_validator("warmup_increment")
    @classmethod
    def warmup_increment_not_negative(cls, v: int) -> int:
        # 0 is legitimate: it pins the allowance at warmup_start_daily, i.e. a
        # constant send rate with no ramp. Only a negative value is broken -
        # it ramps DOWNWARD until nothing can be sent at all.
        if v < 0:
            raise ValueError(
                "warmup_increment cannot be negative - the daily "
                "allowance would shrink each day until the agent sends nothing. "
                "Use 0 for a constant rate with no ramp."
            )
        return v

    @field_validator("max_bounce_rate")
    @classmethod
    def bounce_rate_in_range(cls, v: float) -> float:
        # A fraction, not a percentage. 0.0 pauses sending permanently on the
        # first bounce. The breaker trips only above this rate, so at 1 - what
        # an operator types who means 1% - it waits for more bounces than
        # sends, and above 0.5 it waits until most of the mail is bouncing,
        # long after the sending domain's reputation is gone.
        if not 0 < v <= 0.5:
            raise ValueError(
                "max_bounce_rate must be a fraction in (0, 0.5], where "
                "0.05 means 5%. 0 pauses sending forever; at 1 the breaker "
                "trips only on more bounces than sends, which in practice is "
                "never."
            )
        return v

    @field_validator("bounce_guard_min_sends")
    @classmethod
    def bounce_min_sends_in_range(cls, v: int) -> int:
        # The breaker judges only once the last 7 days hold this many sends, so
        # a typo of 2000 for 20 keeps it silent for weeks at warm-up volumes
        # while the bounces pile up. 500 is a full week, Monday to Friday, at
        # the default warm-up ceiling of 100 a day.
        if not 1 <= v <= 500:
            raise ValueError(
                "bounce_guard_min_sends must be between 1 and 500. The "
                "breaker judges the last 7 days of sends, and with a higher "
                "minimum it can stay silent for weeks."
            )
        return v

    @field_validator("max_domain_bounce_rate")
    @classmethod
    def domain_bounce_rate_in_range(cls, v: float) -> float:
        # Same failure shape as max_bounce_rate: 0 backs off every domain that
        # ever bounced once, and at 1 a domain is backed off only on more
        # bounces than sends, so in practice nothing is ever skipped.
        if not 0 < v <= 0.5:
            raise ValueError(
                "max_domain_bounce_rate must be a fraction in (0, 0.5], "
                "where 0.3 means 30%. 0 backs off any domain with a single "
                "bounce; at 1 a domain is backed off only on more bounces "
                "than sends, which in practice is never."
            )
        return v

    @field_validator("domain_bounce_min_sends")
    @classmethod
    def domain_min_sends_in_range(cls, v: int) -> int:
        # 0 would judge a domain on zero sends, where one bounce reads as an
        # infinite rate and blacklists a domain we have never even emailed.
        if v < 1:
            raise ValueError(
                "domain_bounce_min_sends must be at least 1; below that "
                "a domain is judged before it has any sends to judge."
            )
        # A domain is judged on its last 7 days of sends, and the per-domain
        # cap allows 3 a day by default, so a minimum in the hundreds is never
        # reached.
        if v > 100:
            raise ValueError(
                "domain_bounce_min_sends must be at most 100; a domain "
                "gets a few sends a day and is judged on the last 7 days, so "
                "above that it is in effect never backed off."
            )
        return v

    @field_validator("send_window_days")
    @classmethod
    def send_window_days_valid(cls, v: str) -> str:
        # Python's weekday() is 0-6. "7" parsed cleanly, matched nothing, and
        # the agent logged "deferring" every loop while never sending. Text like
        # "Mon,Tue" contained no digits and silently fell back to Mon-Fri,
        # quietly ignoring what the operator actually wrote.
        raw = [p.strip() for p in (v or "").split(",") if p.strip()]
        if not raw:
            raise ValueError(
                "send_window_days must list at least one day (0=Mon..6=Sun)"
            )
        bad = [p for p in raw if not p.isdigit() or not 0 <= int(p) <= 6]
        if bad:
            raise ValueError(
                "send_window_days has an invalid entry: use digits 0-6 "
                "(0=Monday .. 6=Sunday). Anything outside that range matches no "
                "day, so the agent defers forever and sends nothing."
            )
        return v

    # ---------------------------------------------------------------------
    # Helper methods
    # ---------------------------------------------------------------------

    def get_target_queries(self) -> list[str]:
        """Return target search queries as a list."""
        return [q.strip() for q in self.target_queries.split(",") if q.strip()]

    def get_blocked_prefixes(self) -> list[str]:
        """Return blocked email prefixes as a list."""
        return [p.strip() for p in self.blocked_email_prefixes.split(",") if p.strip()]

    def get_relevance_verticals(self) -> list[str]:
        """Return the client target-vertical keywords as a list."""
        return [v.strip() for v in self.relevance_verticals.split(",") if v.strip()]

    def get_abs_db_path(self) -> str:
        """Return absolute path to the database file."""
        p = Path(self.db_path)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        make_private_dir(p.parent)
        return str(p)

    def missing_legal_identity(self) -> list[str]:
        """The .env names of required legal fields that are still blank."""
        required = list(self.REQUIRED_LEGAL_FIELDS)
        if self._CAPITAL_COMPANY.search(self.legal_entity_name or ""):
            required += ["legal_share_capital", "legal_single_member"]
        return [f.upper() for f in required if not (getattr(self, f, "") or "").strip()]

    def get_lead_alert_recipients(self) -> list:
        """Addresses to notify when a prospect replies with interest."""
        return [
            a.strip()
            for a in (self.lead_alert_email or "").split(",")
            if a.strip() and "@" in a
        ]

    def microsoft_signin_on(self) -> bool:
        """True when Microsoft sign-in is set up (any of its settings set)."""
        return any(
            (value or "").strip()
            for value in (
                self.microsoft_tenant_id,
                self.microsoft_client_id,
                self.microsoft_certificate_path,
            )
        )

    def get_abs_microsoft_certificate_path(self) -> str:
        """The certificate file's absolute path, relative to the project."""
        p = Path((self.microsoft_certificate_path or "").strip())
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return str(p)

    def _extra_account_entries(self) -> list:
        """SENDER_ACCOUNTS's objects, or none when it does not parse
        (get_sender_accounts reports that)."""
        import json as _json

        try:
            entries = _json.loads((self.sender_accounts or "").strip() or "[]")
        except ValueError:
            return []
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)]

    def microsoft_signin_problems(self) -> list[str]:
        """Why Microsoft sign-in cannot be used as set up, each in words that
        name settings, never their values. None when it is not set up.

        With it, no password may be kept for a Microsoft 365 mailbox: a stored
        password is what lets whoever copies it send mail as the client."""
        from modules import microsoft_signin as ms

        if not self.microsoft_signin_on():
            return []
        missing = [
            name
            for name, value in (
                ("MICROSOFT_TENANT_ID", self.microsoft_tenant_id),
                ("MICROSOFT_CLIENT_ID", self.microsoft_client_id),
                ("MICROSOFT_CERTIFICATE_PATH", self.microsoft_certificate_path),
            )
            if not (value or "").strip()
        ]
        if missing:
            return [
                (
                    f"{', '.join(missing)} must be set too: Microsoft sign-in "
                    "needs the tenant id, the app's client id and the "
                    "certificate file"
                )
            ]
        problems = [
            p
            for p in (
                ms.tenant_problem(self.microsoft_tenant_id),
                ms.client_id_problem(self.microsoft_client_id),
            )
            if p
        ]
        if not ms.is_microsoft_host(self.smtp_host) or self.smtp_port != ms.SMTP_PORT:
            problems.append(
                f"Microsoft sign-in sends through {ms.SMTP_HOST} port "
                f"{ms.SMTP_PORT}: set SMTP_HOST={ms.SMTP_HOST} and "
                f"SMTP_PORT={ms.SMTP_PORT}"
            )
        if self.imap_enabled and (
            (self.imap_host or "").strip().lower() != ms.IMAP_HOST
            or self.imap_port != ms.IMAP_PORT
        ):
            problems.append(
                f"Microsoft sign-in reads mail from {ms.IMAP_HOST} port "
                f"{ms.IMAP_PORT}: set IMAP_HOST={ms.IMAP_HOST} and "
                f"IMAP_PORT={ms.IMAP_PORT}"
            )
        passwords = [
            name
            for name, value in (
                ("SMTP_PASSWORD", self.smtp_password),
                ("IMAP_PASSWORD", self.imap_password),
                *(
                    (f"SENDER_{n}_PASSWORD", getattr(self, f"sender_{n}_password"))
                    for n in (2, 3, 4, 5)
                ),
            )
            if (value or "").strip()
        ]
        for i, entry in enumerate(self._extra_account_entries(), 1):
            smtp_host = entry.get("smtp_host") or self.smtp_host
            imap_host = str(entry.get("imap_host") or "").strip().lower()
            if not (
                ms.is_microsoft_mail_host(smtp_host)
                or ms.is_microsoft_mail_host(imap_host)
            ):
                continue
            if entry.get("password") or entry.get("imap_password"):
                passwords.append(f"the password of SENDER_ACCOUNTS entry {i}")
            smtp_port = str(entry.get("smtp_port") or self.smtp_port).strip()
            if not ms.is_microsoft_host(smtp_host) or smtp_port != str(ms.SMTP_PORT):
                problems.append(
                    f"SENDER_ACCOUNTS entry {i} is a Microsoft mailbox, so it "
                    f"must send through {ms.SMTP_HOST} port {ms.SMTP_PORT} with "
                    "the token: remove its smtp_host and smtp_port"
                )
            imap_port = str(entry.get("imap_port") or "").strip()
            if self.imap_enabled and (
                (imap_host and imap_host != ms.IMAP_HOST)
                or (imap_port and imap_port != str(ms.IMAP_PORT))
            ):
                problems.append(
                    f"SENDER_ACCOUNTS entry {i} sends through {ms.SMTP_HOST}, so "
                    f"it must read mail from {ms.IMAP_HOST} port {ms.IMAP_PORT}: "
                    "remove its imap_host and imap_port"
                )
        if passwords:
            problems.append(
                "Microsoft sign-in is set up, so the Microsoft 365 mailboxes "
                "sign in without a password, and none may be kept on this "
                f"machine: blank {', '.join(passwords)} in .env (and change "
                "that password in Microsoft 365 if it was ever used)"
            )
        try:
            ms.load_credential(
                self.get_abs_microsoft_certificate_path(),
                self.microsoft_certificate_passphrase,
            )
        except ms.SignInRefused as e:
            problems.append(str(e))
        return problems

    def reviewer_accounts(self) -> dict:
        """The named reviewers, as {name: password}. An entry without a name,
        with a password under 12 characters, or reusing the administrator's
        name is left out."""
        accounts = {}
        for entry in (self.dashboard_reviewers or "").split(","):
            name, _, password = entry.strip().partition(":")
            name = name.strip()
            if name and len(password) >= 12 and name != self.dashboard_username:
                accounts[name] = password
        return accounts

    def get_sender_accounts(self) -> list:
        """Every sending account, PRIMARY FIRST. Each dict has smtp_host/port,
        username, password, sender_name, sender_title, sender_signature,
        imap_host/port, imap_username/password, and microsoft: True when it
        signs in with a Microsoft token instead of a password.
        Extra accounts (from SENDER_ACCOUNTS json) inherit the primary host/port
        and default their IMAP creds to their own SMTP creds."""
        import json as _json

        from modules.microsoft_signin import is_microsoft_host

        def _microsoft(host) -> bool:
            return self.microsoft_signin_on() and is_microsoft_host(host)

        def _identity(name, title, signature) -> dict:
            # An extra mailbox with no name of its own is the same sender's,
            # so it takes the primary's name, title and signature. One with
            # its own name still takes the shared title, but never the
            # primary's signature, which names somebody else: left blank, its
            # signature is built from its own name.
            name, signature = (name or "").strip(), (signature or "").strip()
            return {
                "sender_name": name or self.sender_name,
                "sender_title": (title or "").strip() or self.sender_title.strip(),
                "sender_signature": signature
                or ("" if name else self.sender_signature.strip()),
            }

        primary = {
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "username": self.smtp_username,
            "password": self.smtp_password,
            **_identity("", "", ""),
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "imap_username": self.imap_username or self.smtp_username,
            "imap_password": self.imap_password or self.smtp_password,
            "microsoft": _microsoft(self.smtp_host),
        }
        accounts = [primary]

        def _add(
            username,
            password,
            name,
            title="",
            signature="",
            host=None,
            port=None,
            imap_host=None,
            imap_port=None,
            imap_user=None,
            imap_pw=None,
        ):
            accounts.append(
                {
                    "smtp_host": host or self.smtp_host,
                    "smtp_port": int(port or self.smtp_port),
                    "username": username,
                    "password": password or "",
                    **_identity(name, title, signature),
                    "imap_host": imap_host or self.imap_host,
                    "imap_port": int(imap_port or self.imap_port),
                    "imap_username": imap_user or username,
                    "imap_password": imap_pw or password or "",
                    "microsoft": _microsoft(host or self.smtp_host),
                }
            )

        # Simple numbered slots (SENDER_2_EMAIL ... SENDER_5_EMAIL)
        for n in (2, 3, 4, 5):
            email = (getattr(self, f"sender_{n}_email", "") or "").strip()
            if not email:
                continue
            pw = (getattr(self, f"sender_{n}_password", "") or "").strip()
            if not pw and not _microsoft(self.smtp_host):
                # A half-filled slot would be rotated into like any other
                # mailbox and fail SMTP AUTH on ~1/N of the campaign. Skip it
                # loudly instead of poisoning the rotation.
                _log.error(
                    "SENDER_%d_EMAIL is set but SENDER_%d_PASSWORD is "
                    "empty - skipping this mailbox.",
                    n,
                    n,
                )
                continue
            _add(
                email,
                pw,
                getattr(self, f"sender_{n}_name", ""),
                getattr(self, f"sender_{n}_title", ""),
                getattr(self, f"sender_{n}_signature", ""),
            )

        raw = (self.sender_accounts or "").strip()
        if raw:
            try:
                for a in _json.loads(raw):
                    if not a.get("username"):
                        continue
                    accounts.append(
                        {
                            "smtp_host": a.get("smtp_host") or self.smtp_host,
                            "smtp_port": int(a.get("smtp_port") or self.smtp_port),
                            "username": a["username"],
                            "password": a.get("password", ""),
                            **_identity(
                                a.get("sender_name"),
                                a.get("sender_title"),
                                a.get("sender_signature"),
                            ),
                            "imap_host": a.get("imap_host") or self.imap_host,
                            "imap_port": int(a.get("imap_port") or self.imap_port),
                            "imap_username": a.get("imap_username") or a["username"],
                            "imap_password": a.get("imap_password")
                            or a.get("password", ""),
                            "microsoft": _microsoft(
                                a.get("smtp_host") or self.smtp_host
                            ),
                        }
                    )
            except (ValueError, TypeError, AttributeError) as e:
                # ValueError covers json.JSONDecodeError and a non-numeric port;
                # TypeError/AttributeError cover JSON that parses but is not a
                # list of objects (a bare string or number in the .env).
                #
                # Silence here would halve (or worse) the sending capacity while
                # the operator believed all mailboxes were live.
                # The error's type only: int()'s message quotes the value,
                # and a password typed in the wrong field would be printed.
                _log.error(
                    "SENDER_ACCOUNTS could not be parsed (%s) - only the "
                    "primary mailbox will be used. Check the JSON in .env.",
                    type(e).__name__,
                )
        # Dedupe by username: the same mailbox listed twice (easy to do with the
        # numbered slots) would get two independent warm-up counters and quietly
        # send double its real daily allowance.
        seen, unique = set(), []
        for a in accounts:
            key = (a["username"] or "").strip().lower()
            if key in seen:
                _log.warning("Duplicate sending mailbox %s ignored.", key)
                continue
            seen.add(key)
            unique.append(a)
        return unique

    def get_send_window_days(self) -> set:
        """Weekdays (Mon=0..Sun=6) on which sending is allowed."""
        out = {
            int(t)
            for t in (self.send_window_days or "").split(",")
            if t.strip().isdigit()
        }
        return out or {0, 1, 2, 3, 4}

    def get_email_subject(self) -> str:
        """Return the subject with {company_name} (sender) filled in. A
        {org} placeholder is left intact for per-recipient substitution."""
        return self.email_subject.replace("{company_name}", self.company_name)

    def get_email_subject_it(self) -> str:
        """The subject for a recipient written to in Italian, or "" when
        EMAIL_SUBJECT_IT is blank and the English subject logic applies."""
        return self.email_subject_it.strip().replace(
            "{company_name}", self.company_name
        )

    def get_email_subjects(self) -> list[str]:
        """Return A/B subject-line variants with {company_name} filled in
        ({org} is left for per-recipient substitution at send time).

        Falls back to a single-element list containing EMAIL_SUBJECT when no
        variants are configured."""
        raw = [s.strip() for s in self.email_subjects.split(",") if s.strip()]
        subjects = raw or [self.email_subject]
        return [s.replace("{company_name}", self.company_name) for s in subjects]


def config_error_text(error: Exception, *, names: bool = True) -> str:
    """A configuration error in words that never quote a setting's value.

    For pydantic's ValidationError, each problem's message, after the name
    of the setting it is about unless names is False; anything else as it
    words itself. The validators' messages name settings, never values."""
    try:
        details = error.errors()
    except AttributeError:
        return str(error)
    problems = []
    for detail in details:
        message = str(detail.get("msg", "")).removeprefix("Value error, ")
        where = ".".join(str(part) for part in detail.get("loc") or ()).upper()
        problems.append(f"{where}: {message}" if names and where else message)
    return "; ".join(problems)


def load_settings() -> Settings:
    """Load and validate settings from .env file.

    Refuses to start on the shipped unsubscribe secret, or on one shorter than
    SECRET_MIN_LENGTH, and on a LISTS_KEY that is set and shorter. The check
    lives HERE
    rather than on the model so that constructing Settings directly (as every
    test does) still works with defaults, while every real entry point -
    `main.py run`, `dashboard`, `doctor` - goes through this function and fails
    loudly. Deliberately fatal rather than a warning: with the default key in
    place anyone who has read the public source can forge an unsubscribe token
    for any address and suppress that prospect.
    """
    settings = Settings()
    if settings.unsubscribe_secret.strip() == Settings.UNSUBSCRIBE_SECRET_DEFAULT:
        raise ValueError(
            "UNSUBSCRIBE_SECRET is still the value shipped in .env.example, so "
            "it is public knowledge and anyone can forge an unsubscribe link "
            "for any address. Set UNSUBSCRIBE_SECRET in .env to a random "
            "string, e.g. the output of: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if len(settings.unsubscribe_secret.strip()) < Settings.SECRET_MIN_LENGTH:
        raise ValueError(
            f"UNSUBSCRIBE_SECRET is shorter than {Settings.SECRET_MIN_LENGTH} "
            "characters, so it could be guessed and unsubscribe links forged. "
            "Set UNSUBSCRIBE_SECRET in .env to a long random string, e.g. the "
            'output of: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if settings.lists_key and len(settings.lists_key) < Settings.SECRET_MIN_LENGTH:
        raise ValueError(
            f"LISTS_KEY is shorter than {Settings.SECRET_MIN_LENGTH} characters, "
            "so a copy of the encrypted lists could be opened by guessing it. "
            "Set LISTS_KEY in .env to a long random string, e.g. the output of: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    return settings


# Settings an operator may change from the dashboard while the agent is running.
#
# Everything here is an OPERATIONAL dial: pacing, thresholds, business hours.
# The list is an allow-list rather than a deny-list on purpose - a new field
# added to Settings is NOT editable until someone puts it here deliberately.
#
# Deliberately absent, and not to be added:
#   * credentials of any kind (SMTP/IMAP passwords, API keys). They live in
#     .env, and a dashboard write path to them turns one XSS into account
#     takeover. Changing them is a deploy action, not a runtime dial.
#   * db_path, base_url - changing either mid-run points the agent at a
#     different database or puts a wrong URL in mail already being sent.
#   * feature enable/disable flags. The agent builds its modules once at
#     construction; flipping a flag afterwards changes the setting without
#     creating the object it is supposed to control, so the dashboard would
#     report a feature as on while nothing implements it.
RUNTIME_EDITABLE: frozenset[str] = frozenset(
    {
        "min_lead_score_to_email",
        "max_per_domain_per_day",
        "max_pending_backlog",
        "delay_between_emails",
        "send_jitter_seconds",
        "emails_per_hour",
        "max_bounce_rate",
        "bounce_guard_min_sends",
        "max_domain_bounce_rate",
        "domain_bounce_min_sends",
        "send_window_start_hour",
        "send_window_end_hour",
        "send_window_days",
        "imap_check_interval_minutes",
    }
)


# Who decides at each stage. Stored like the runtime overrides, but written
# only by the dashboard's /api/autonomy route - administrator, reason, journal
# entry - never by the general settings panel, which RUNTIME_EDITABLE alone
# governs.
AUTONOMY_SETTINGS: frozenset[str] = frozenset(
    {"autonomy_first_email", "autonomy_followup", "autonomy_reply"}
)


def validate_override(settings: Settings, key: str, value) -> Settings:
    """Return a fully-validated Settings with ``key`` set to ``value``.

    Assignment alone is not enough: this model does not set
    validate_assignment, so `settings.max_bounce_rate = 99` succeeds silently
    and every validator in this file becomes decorative. Rebuilding the whole
    model re-runs the field validators AND the cross-field ones - which is the
    only way to catch a send window whose end no longer follows its start.

    Raises ValueError for a key that is not runtime-editable, and whatever
    pydantic raises for a value that fails validation. Never mutates the
    Settings it was given.
    """
    if key not in RUNTIME_EDITABLE | AUTONOMY_SETTINGS:
        raise ValueError(f"{key!r} is not editable at runtime")
    # by_alias, because a handful of fields are declared with an uppercase
    # env-var alias (OPEN_TRACKING_ENABLED, DASHBOARD_PASSWORD, ...). Dumping
    # by field name produces keys the constructor rejects as extra inputs, so
    # a plain round-trip fails on five unrelated fields.
    data = settings.model_dump(by_alias=True)
    field = Settings.model_fields[key]
    data[field.alias or key] = value
    # _env_file=None so validation judges exactly these values; re-reading .env
    # here would let a stale file override what the operator just set.
    resolving = _RESOLVE_AI_HOST.set(False)
    try:
        return Settings(_env_file=None, **data)
    finally:
        _RESOLVE_AI_HOST.reset(resolving)


def _ordered_override_items(
    base: Settings, overrides: dict
) -> list[tuple[str, object]]:
    """Order override items so a coupled send-window shift never trips on the
    old bound mid-way through validation."""
    items = list(overrides.items())
    if "send_window_start_hour" in overrides and "send_window_end_hour" in overrides:
        try:
            end_first = (
                int(overrides["send_window_end_hour"]) > base.send_window_start_hour
            )
        except (ValueError, TypeError):
            end_first = False
        pair = (
            ["send_window_end_hour", "send_window_start_hour"]
            if end_first
            else ["send_window_start_hour", "send_window_end_hour"]
        )
        rest = [
            (k, v)
            for k, v in items
            if k not in ("send_window_start_hour", "send_window_end_hour")
        ]
        return [(k, overrides[k]) for k in pair] + rest
    return items


def effective_settings(
    base: Settings, db, *, fallback: Settings | None = None
) -> Settings:
    """Return ``base`` with every valid runtime override from ``db`` applied.

    Orders coupled send-window bounds so moving both ends of the window at once
    is judged as a unit, while still validating key by key so one bad row in
    the database is logged and skipped rather than discarding the rest.
    """
    try:
        overrides = db.get_runtime_overrides()
    except sqlite3.Error as e:
        _log.warning("Could not read config overrides (%s); using .env", e)
        return base if fallback is None else fallback

    if not overrides:
        return base

    current_settings = base
    for key, value in _ordered_override_items(current_settings, overrides):
        current = getattr(current_settings, key, None)
        if key in RUNTIME_EDITABLE and current == value:
            continue
        try:
            current_settings = validate_override(current_settings, key, value)
        except (ValueError, TypeError) as e:
            _log.error(
                "Rejecting config override %s: %s. Keeping the value it had.",
                key,
                config_error_text(e, names=False),
            )
    return current_settings
