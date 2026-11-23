# Architectural & Code Quality Audit Report
## Omega IFS Autonomous B2B Outreach Agent

**Document Reference**: OMEGA-AUDIT-2026-M1  
**Author**: Implementation Worker (`worker_m1`)  
**Target Repository**: Omega B2B Outreach Engine: this repository's code as of 2026-10-09  
**Git Branch**: `refactor/modular-agent`  
**Date**: 2026-10-06  
**Status**: Historical. Written as the baseline for the stage split (2c9d87e).
Corrected on 2026-10-09: every claim was re-checked against the code on branch
`refactor/modular-agent` and corrected to describe it as of that date; the
figures below, and every "today" in the text, are from that tree (1,355 tests,
for example). Sections 6 and 8.2 describe work that is now done. The original
self-certification has been removed. The GDPR work of 27 to 29 October 2026
came later, so the compliance statements are out of date: section 5.4, for
instance, gives legitimate interest as the basis and calls a blank share
capital no bar to sending, while the lawful basis is now recorded per source
channel and is for counsel to decide country by country (decision L1.1 in
`docs/privacy/decisions_for_counsel.md`), and an S.r.l. or S.p.A. cannot send
live without its share capital. For the code as it is, see
`docs/COMPLIANCE.md`, `docs/DEPLOY.md` and `docs/ARCHITECTURE.md`.  

---

## Table of Contents
1. [Executive Summary & System Architecture Overview](#1-executive-summary--system-architecture-overview)
2. [Monolithic Hotspot Analysis of `agent.py`](#2-monolithic-hotspot-analysis-of-agentpy)
3. [Complete Module Inventory & Dependency Analysis](#3-complete-module-inventory--dependency-analysis)
4. [Data Persistence & Concurrency Review](#4-data-persistence--concurrency-review)
5. [Deliverability & Compliance Guardrails](#5-deliverability--compliance-guardrails)
6. [Refactoring Blueprint for the 4 Pipeline Stages](#6-refactoring-blueprint-for-the-4-pipeline-stages)
7. [Residual Operational Risks & Mitigation Plan](#7-residual-operational-risks--mitigation-plan)
8. [Quality Assurance & Verification Strategy](#8-quality-assurance--verification-strategy)

---

## 1. Executive Summary & System Architecture Overview

### 1.1 Executive Summary
The Omega IFS Outreach Agent is an enterprise autonomous system designed for targeted B2B customer discovery, qualification, and automated multi-touch cold email outreach across Italy and the broader European market. Operating under stringent European legal standards—specifically the General Data Protection Regulation (GDPR, EU 2016/679) and Article 2250 of the Italian Civil Code—the platform combines web scraping, heuristic (rule-based) lead scoring with optional AI relevance classification, MX/SMTP mailbox verification, rate-limited email dispatch, IMAP inbox monitoring, and autonomous feedback loops.

When this audit was written, the core orchestration engine resided in a single 3,293-line `agent.py`, and the suite had 1,099 tests. Today the engine is split across four stage modules (`modules/exclusion_manager.py`, `dispatch_pipeline.py`, `backlog_draining.py`, `loop_orchestration.py`); `OutreachAgent` in `agent.py` (696 lines) is a facade that keeps every old method name and delegates to them. The default `python -m pytest` run has 1,355 tests, and another 42 Playwright UI tests run with `pytest -m ui`.

This audit profiles the architecture, inventories the supporting modules (`modules/` now holds 30 modules plus `__init__.py`; the five added since the audit are the four stage modules and `contact_explainer.py`), analyzes the database persistence and deliverability guardrails, and describes the refactoring blueprint that was then carried out in e83dadd: `agent.py` is now a facade over four stage modules.

### 1.2 System Architecture Overview
The Omega IFS Outreach Agent operates as an autonomous pipeline spanning seven distinct operational stages:

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   OMEGA IFS PIPELINE ARCHITECTURE                                  │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘

   [ 1. DISCOVERY ]
        │  • Heuristic Query Generator (modules/query_generator.py)
        │  • Autonomous Search Strategist & Company Generator (modules/ai_generator.py)
        │  • Manual Query Injection (CLI / Dashboard / search_log)
        ▼
   [ 2. EXTRACTION & CRAWLING ]
        │  • SearchModule (modules/search.py)
        │  • Web Scraping via BeautifulSoup & DuckDuckGo / Bing SERP
        │  • Email & Entity Regex Extraction + Deduplication
        ▼
   [ 3. SCREENING AT HARVEST ]
        │  • Address screens: blocked prefixes, personal webmail (PERSONAL_DOMAINS),
        │    Italian PEC (_is_certified_mail)
        │  • Suppression list match when a contact is stored (case-insensitive)
        ▼
   [ 4. QUALIFICATION, in this order ]
        │  • Heuristic Lead Scoring (modules/lead_scoring.py)
        │  • Client & Competitor Registers (modules/clients.py)
        │  • Domain & Geographic Relevance Gates (modules/relevance.py)
        │  • AI Relevance Classification (modules/ai_generator.py, optional)
        │  • MX lookup and optional SMTP probe (modules/verification.py)
        │  • GDPR Art. 6 Legitimate Interest Logging (consent_log table)
        │  (Suppression and the address/client/competitor screens run again at
        │   send time, in send_verdict and process_contact.)
        ▼
   [ 5. DISPATCH & PACING ]
        │  • Multi-Account Warmup Scheduling (modules/warmup.py)
        │  • Recipient Domain Throttling (modules/domain_throttle.py) & Domain Backoff
        │  • Business Hours & Weekday Send Window Enforcement (_within_send_window)
        │  • Rate Limiting with Jitter Floor (_send_delay)
        │  • Legal Identity Preflight Check (_send_blocker)
        │  • Multipart Email Rendering with Mandatory Legal Footers & Unsubscribe Tokens
        │  • SMTP Dispatch (modules/email_sender.py)
        ▼
   [ 6. CADENCE & RETRIES ]
        │  • Multi-Touch Follow-up Sequences (modules/sequences.py)
        │  • Email Threading Headers (In-Reply-To, References)
        │  • Exponential Backoff Retry Queue with In-Flight Leasing (modules/retry_queue.py)
        ▼
   [ 7. MONITORING & FEEDBACK ]
           • Multi-Inbox IMAP Scanner: Bounces & Unsubscribes (modules/imap_monitor.py)
           • NLP Reply Intent Classification (modules/reply_intent.py)
           • Operator Hot-Lead Escalation & Email Alerts (lead_alerts table)
           • Daily GDPR Data Retention Erasure (modules/retention.py)
           • Search Performance Autonomous Learner (modules/learning.py)
```

---

## 2. Monolithic Hotspot Analysis of `agent.py`

### 2.1 Quantitative Code Metrics
- **File Location**: `c:\Users\tagak\Downloads\AR AGENT\agent.py`
- **Total Physical Lines**: 696 lines (3,293 before the split)
- **Total Bytes**: 28,849 bytes (155,752 before the split)
- **Classes**: 1 (`OutreachAgent`, spanning lines 30 to 696)
- **Method Count**: 55 total methods
  - Static Methods: 5 (`_list_stamp`, `_by_lead_score`, `_build_greeting`, `_body_to_html`, `_default_template`)
  - Public Instance Methods: 8 (`run_single`, `run_continuous`, `stop`, `load_client_register`, `load_competitor_register`, `refresh_lists_if_changed`, `send_single_email`, `get_status`), plus `__init__`
  - Internal/Private Instance Methods: 41
- **Instance Attributes & State Variables**: 47 distinct attributes initialized across `__init__` and `_init_v2_modules` (the table lists them all).

### 2.2 Comprehensive Attribute Inventory of `OutreachAgent`
The state of `OutreachAgent` is maintained through 47 attributes that are read and mutated across all pipeline phases. Line numbers are left out: they move with every edit.

| # | Attribute Name | Type | Initialized In | Role & State Transitions |
|---|---|---|---|---|
| 1 | `settings` | `config.Settings` | `__init__` | System configuration; hot-reloaded dynamically by `_apply_runtime_overrides`. |
| 2 | `_running` | `bool` | `__init__` | Main loop execution flag; set `True` on start, `False` on `stop()` or shutdown. |
| 3 | `_last_imap_check` | `float` | `__init__` | Epoch timestamp of the last attempted IMAP poll. |
| 4 | `_last_imap_ok` | `float` | `__init__` | Epoch timestamp of the last successful IMAP inbox scan. |
| 5 | `_sending_paused` | `bool` | `__init__` | Reputation circuit breaker latch; `True` if bounce or DNSBL blacklist trips. |
| 6 | `_pause_reason` | `Optional[str]` | `__init__` | Descriptive pause trigger reason (`"bounce"` or `"blacklist"`). |
| 7 | `_guard_logged` | `dict[str, bool]` | `__init__` | Idempotent logging latch preventing log flooding on persistent pause conditions. |
| 8 | `_domain_backoff` | `dict[str, dict]` | `__init__` | In-memory cache of recipient domains experiencing high bounce rates. |
| 9 | `_stats` | `dict[str, int]` | `__init__` | Real-time session metrics (`searches`, `contacts`, `scored`, `verified`, `emails_sent`, `emails_failed`, `retries_queued`, `domain_skipped`, `warmup_skipped`, `low_score_skipped`, `irrelevant_skipped`, plus `client_skipped`, `ai_body`, `ai_body_fallback` added at run time). |
| 10 | `file_handler` | `FileHandler` | `__init__` | File I/O subsystem managing template and PDF attachments. |
| 11 | `db` | `Database` | `__init__` | Thread-local SQLite persistence connection manager. |
| 12 | `search` | `SearchModule` | `__init__` | Search engine crawler and prospect extractor. |
| 13 | `_accounts` | `list[dict]` | `__init__` | Configured SMTP sender account credential definitions. |
| 14 | `senders` | `list[tuple]` | `__init__` | Active sender tuples: `(account_key, sender_name, EmailSender)`; `account_key` is `""` for the primary mailbox. |
| 15 | `_account_keys` | `list[str]` | `__init__` | Identifier keys for configured sender accounts. |
| 16 | `_sending_domain` | `str` | `__init__` | Primary domain extracted from SMTP username for DNSBL blacklist monitoring. |
| 17 | `_bl_checker` | `Optional[Any]` | `__init__` | Lazily instantiated `BlacklistChecker` instance. |
| 18 | `email_sender` | `EmailSender` | `__init__` | Primary fallback `EmailSender` instance (points to `senders[0][2]`). |
| 19 | `_rr` | `int` | `__init__` | Round-robin cursor for cycling sender accounts. |
| 20 | `_email_template` | `str` | `__init__` | Loaded default HTML email template content. |
| 21 | `_template_variants` | `list[tuple]` | `__init__` | Discovered A/B testing template variants `(name, content)`. |
| 22 | `_subject_variants` | `list[str]` | `__init__` | Configured A/B email subject lines. |
| 23 | `_pdf_path` | `Optional[str]` | `__init__` | Absolute path to the company profile PDF, attached only while `ATTACH_PROFILE` is on (off for Omega). |
| 24 | `lead_scorer` | `Optional[LeadScorer]` | `_init_v2` | Heuristic rule-based lead qualification scorer. |
| 25 | `relevance_filter` | `Optional[RelevanceFilter]` | `_init_v2` | Heuristic domain, keyword, and country relevance classifier. |
| 26 | `email_verifier` | `Optional[EmailVerifier]` | `_init_v2` | DNS MX probe and SMTP socket ping validator. |
| 27 | `warmup` | `Optional[WarmupScheduler]` | `_init_v2` | Daily sender mailbox ramp governor. |
| 28 | `domain_throttle` | `Optional[DomainThrottle]` | `_init_v2` | Recipient domain daily send volume limiter. |
| 29 | `retry_queue` | `Optional[RetryQueue]` | `_init_v2` | Exponential backoff retry engine for failed dispatches. |
| 30 | `unsubscribe_handler`| `Optional[UnsubscribeHandler]` | `_init_v2` | Cryptographic HMAC token generator for opt-out links. |
| 31 | `analytics` | `Optional[Analytics]` | `_init_v2` | 1x1 tracking pixel manager with privacy blind mode. |
| 32 | `sequences` | `Optional[SequenceEngine]` | `_init_v2` | Multi-step follow-up cadence state machine. |
| 33 | `sequence_id` | `Optional[int]` | `_init_v2` | ID of the active default follow-up sequence. |
| 34 | `imap_monitors` | `list[ImapMonitor]` | `_init_v2` | List of configured IMAP inbox monitor instances. |
| 35 | `imap_monitor` | `Optional[ImapMonitor]` | `_init_v2` | Primary IMAP inbox monitor (`imap_monitors[0]`). |
| 36 | `query_generator` | `Optional[QueryGenerator]` | `_init_v2` | Heuristic European search query builder. |
| 37 | `learner` | `Optional[PerformanceLearner]`| `_init_v2` | Autonomous query performance feedback optimizer. |
| 38 | `client_register` | `Optional[ClientRegister]` | `_init_v2` (filled by `ExclusionManager._load_register`) | Loaded in-memory register of active customers to exclude. |
| 39 | `competitor_register`| `Optional[ClientRegister]`| `_init_v2` | Loaded in-memory register of known competitors to exclude. |
| 40 | `_list_stamps` | `dict[str, tuple]` | `_init_v2` | Cached `(mtime_ns, size)` stamps for client/competitor files. |
| 41 | `_list_failed` | `dict[str, tuple]` | `_init_v2` | Stamps of corrupted/unparseable spreadsheets to avoid retry loops. |
| 42 | `_last_dispatch` | `int` | `__init__` | Stamped just before each dispatch, so a send loop can tell a refusal (nothing sent, no send delay) from an attempt. |
| 43 | `ai_gen` | `Optional[AIGenerator]` | `__init__` | The LLM interface, when a provider key is configured. |
| 44 | `exclusion_manager` | `ExclusionManager` | `__init__` | Stage: client/competitor lists and the closed-contact and register screens. |
| 45 | `dispatch_pipeline` | `DispatchPipeline` | `__init__` | Stage: the shared send check (`send_verdict`), the send, Direct Send. |
| 46 | `backlog_drainer` | `BacklogDrainer` | `__init__` | Stage: backlog qualification, follow-ups, retries. |
| 47 | `loop_orchestrator` | `LoopOrchestrator` | `__init__` | Stage: the loop, reputation guards, IMAP, alerts, retention. |

*(Note: Attributes 1–32 are core base instances, 33–41 support specialized subsystem modules, and 42–47 were added with the stage split.)*

### 2.3 Catalog of 55 Methods in `OutreachAgent`
The 55 methods can be organized into 6 functional clusters. Apart from `__init__`, `_init_v2_modules` and `get_status`, each is a single delegating call into the stage module that implements it.

1. **Lifecycle & Control (9 methods)**:
   - `__init__(settings)`
   - `_init_v2_modules()`
   - `run_single(queries, dry_run, _from_continuous, search_ids)`
   - `run_continuous(queries, dry_run, loop_delay)`
   - `stop()`
   - `get_status() -> dict`
   - `_print_summary()`
   - `_interruptible_sleep(seconds)`
   - `_next_loop_delay(loop_delay)`
2. **Exclusion & Spreadsheet Management (8 methods)**:
   - `_list_stamp(path) -> tuple | None` [@staticmethod]
   - `_load_register(kind, raw_path) -> bool`
   - `load_client_register() -> bool`
   - `load_competitor_register() -> bool`
   - `refresh_lists_if_changed()`
   - `_contact_is_closed(contact) -> str | None`
   - `_known_competitor(organization) -> str | None`
   - `_active_client(organization) -> str | None`
3. **Dispatch Pipeline & Assembly (16 methods)**:
   - `send_single_email(...) -> tuple[bool, str]`
   - `_send_delay() -> float`
   - `_by_lead_score(contacts) -> list[dict]` [@staticmethod]
   - `_email_contacts(qualified, dry_run)`
   - `_pick_sender() -> tuple | None`
   - `_is_public_base_url() -> bool`
   - `_unsubscribe_target(email) -> str`
   - `_process_contact(contact, dry_run, ...) -> bool`
   - `_build_greeting(contact) -> str` [@staticmethod]
   - `_body_to_html(text) -> str` [@staticmethod]
   - `_build_email_body(contact) -> str`
   - `_build_opening_line(contact) -> str`
   - `_default_template() -> str` [@staticmethod]
   - `_screen_contact(contact) -> tuple | None`
   - `_send_possible_now() -> bool`
   - `_send_verdict(contact, *, purpose, dry_run) -> SendVerdict`
4. **Safety Guards & Compliance (8 methods)**:
   - `_send_blocker(dry_run) -> str | None`
   - `_within_send_window() -> bool`
   - `_seconds_until_send_window() -> float`
   - `_safe_to_resume() -> bool`
   - `_blacklist_guard_tripped() -> bool`
   - `_bounce_guard_tripped() -> bool`
   - `_refresh_domain_backoff()`
   - `_domain_backed_off(email) -> dict | None`
5. **Backlog & Cadence Execution (5 methods)**:
   - `_process_pending_contacts(dry_run)`
   - `_qualify_contacts(contacts) -> list[dict]`
   - `_load_followup_template(template_name) -> str`
   - `_process_sequences(dry_run)`
   - `_process_retries()`
6. **Background Tasks & Autonomous Learning (9 methods)**:
   - `_maybe_apply_retention(dry_run)`
   - `_apply_runtime_overrides()`
   - `_resync_settings_consumers(key)`
   - `_maybe_check_imap()`
   - `_reply_is_interested(subject, body) -> bool`
   - `_handle_interested_replies(entries)`
   - `_send_lead_alert(entry, recipients) -> bool`
   - `_queries_for_a_standalone_run() -> tuple`
   - `_discovery_focus_hint() -> str`

### 2.4 Code Smells and Architectural Deficiencies (status as of 2026-10-09)
1. **The God Class Anti-Pattern**:
   *Resolved.* `OutreachAgent` is now a facade. The responsibilities are split across `ExclusionManager` (list loading and screening), `DispatchPipeline` (send check, assembly, SMTP dispatch), `BacklogDrainer` (qualification, follow-ups, retries) and `LoopOrchestrator` (loop, pacing, guards, IMAP). (The agent never signed emails; only unsubscribe tokens are HMAC-signed.)
2. **Excessive Cyclomatic Complexity**:
   - `DispatchPipeline.process_contact` (`modules/dispatch_pipeline.py` lines 818–1136, 319 lines) is still the most complex function: about 20 `if`/`elif` tests, a render-error handler, and five separately contained post-send writes. The screens it used to own moved into `screen_contact`.
   - `LoopOrchestrator.run_continuous` (`modules/loop_orchestration.py` lines 874–1127, 254 lines) interleaves 10 distinct phases within a `try...except Exception` loop.
3. **Duplicated Delivery Guard Sequences**:
   *Resolved.* When this audit was written, the guards ran in four independent copies (`_email_contacts`, `_process_sequences`, `_process_retries`, `send_single_email`), and the copies had drifted apart. They now live once in `DispatchPipeline.send_verdict`, which all four paths call with a purpose of `first`, `followup`, `retry` or `manual`; each path keeps only its own reaction. `process_contact` repeats only suppression, the address/competitor/client screens and the send blocker as a final guarantee. See 5.5 for the order.
4. **Tight Test Coupling**:
   About 450 lines across 64 test files name an `OutreachAgent` private method, and about 90 of them replace one on the instance. `tests/test_facade_routes_every_call.py` requires every stage-to-stage call to go through the agent, so the facade's method names and signatures must stay stable.

---

## 3. Complete Module Inventory & Dependency Analysis

The `modules/` package contains 31 files: `__init__.py` and 30 modules. Besides the 25 below, it holds the four orchestration modules split out of `agent.py` and `contact_explainer.py` (rows 27–31). Line counts are as of 2026-10-09.

| # | Module | LOC | Primary Class / Functions | Description & Responsibilities | Key Dependencies |
|---|---|---|---|---|---|
| 1 | `modules/clock.py` | 108 | `now`, `today_str`, `now_iso`, `db_now`, `set_timezone`, `get_timezone` | Centralized timezone-aware datetime. Default `Europe/Rome`; falls back to UTC with a logged error if the zone cannot be resolved. | `zoneinfo`, `datetime` |
| 2 | `modules/compliance.py` | 104 | `render_legal_footer`, `is_public_base_url`, `missing_placeholders` | Renders the mandatory legal footer (Cod. Civ. 2250 / GDPR Art. 14) with the opt-out link. | `urllib.parse`, `html`, `ipaddress` |
| 3 | `modules/database.py` | 1,595 | `Database`, `CLOSED_CONTACT_STATUSES`, `DISCARDED_CONTACT_STATUSES` | Core SQLite persistence layer. WAL, foreign keys and a 5s busy timeout on every connection. Each `Database` method opens and closes its own connection; a thread-local `conn` is exposed for modules that write directly. Holds the shared opt-out routine `record_opt_out`. | `sqlite3`, `threading`, `contextlib`, `modules.clock` |
| 4 | `modules/email_sender.py` | 679 | `EmailSender` (`dispatch_to_contact`, `render_template`) | SMTP client: implicit TLS or STARTTLS, EmailMessage assembly with optional PDF attachment, 4xx (transient) / 5xx (permanent) rejection classification. | `smtplib`, `email.message`, `ssl` |
| 5 | `modules/file_handler.py` | 154 | `FileHandler` (`ensure_directories`, `get_pdf_path`) | Template loading, PDF resolution, setup validation. | `pathlib` |
| 6 | `modules/search.py` | 1,421 | `SearchModule`, `PERSONAL_DOMAINS`, `_is_certified_mail` | DuckDuckGo search plus robots-aware site scraper, PEC detection, personal-domain screening. | `httpx`, `ddgs`, `bs4`, `curl_cffi` (optional), `urllib`, `re` |
| 7 | `modules/lead_scoring.py` | 323 | `LeadScorer` | Rule-based 0–100 score: role (0–35), email quality (0–25), organization keywords (0–25), source page (0–15); tiers A–D. | `re`, `logging` |
| 8 | `modules/relevance.py` | 736 | `RelevanceFilter` | Deterministic gate: placeholder/junk addresses, non-European domains and mailboxes, competitor self-descriptions, negative keywords, optional Omega IFS-vertical match. | `urllib.parse`, `re` |
| 9 | `modules/verification.py` | 343 | `EmailVerifier` | Syntax check (email_validator), MX lookup, optional SMTP RCPT TO probe (no VRFY). | `dns.resolver`, `smtplib`, `email_validator` |
| 10 | `modules/warmup.py` | 189 | `WarmupScheduler` | Per-mailbox warm-up ramp (start + (day−1) × increment, capped). | `modules.database`, `modules.clock` |
| 11 | `modules/domain_throttle.py` | 187 | `DomainThrottle` | Caps sends to any one recipient domain per local calendar day (resets at midnight, Europe/Rome). | `modules.database`, `modules.clock` |
| 12 | `modules/retry_queue.py` | 387 | `RetryQueue` | Exponential-backoff retries (5 min × 6^attempt, 3 attempts) with in_flight claiming and stale-claim recovery. | `modules.database`, `datetime` |
| 13 | `modules/unsubscribe.py` | 225 | `UnsubscribeHandler` | HMAC-SHA256 signed opt-out tokens; opt-outs recorded via `Database.record_opt_out`. | `hmac`, `hashlib`, `base64` |
| 14 | `modules/analytics.py` | 330 | `Analytics` | 1x1 open-tracking pixel (records only the open time; off unless `OPEN_TRACKING_ENABLED`) and dashboard chart/funnel data. | `modules.database`, `modules.clock`, `uuid` |
| 15 | `modules/ai_generator.py` | 548 | `AIGenerator` | LLM interface (only an OpenAI-compatible server on Omega's own machine or private network, at `LLM_BASE_URL`; Gemini and Groq removed 16 Nov 2026). Company/discovery queries, opening lines, email bodies, relevance and reply-intent classification. | `httpx`, `re` |
| 16 | `modules/sequences.py` | 364 | `SequenceEngine` | Follow-up sequences: enroll, due-step detection, advance, cancel (recording why). A reply ends the sequence at its next due step, when the follow-up pass finds the contact closed. | `modules.database`, `modules.clock` |
| 17 | `modules/imap_monitor.py` | 1,119 | `ImapMonitor` | IMAP poller: DSN bounces, opt-outs, replies, auto-replies; durable scan watermark. | `imaplib`, `email`, `modules.database`, `modules.reply_intent` |
| 18 | `modules/query_generator.py` | 158 | `QueryGenerator`, `EUROPEAN_COUNTRIES` | Heuristic discovery queries: vertical seeds × modifiers × European countries. | `random` |
| 19 | `modules/learning.py` | 315 | `PerformanceLearner` | Bandit weighting of verticals and countries by verified contacts and replies per search (prior shrinkage, 0.25–4.0 clamp, exploration bonus). | `math`, `random`, `sqlite3` (db passed in) |
| 20 | `modules/blacklist.py` | 186 | `BlacklistChecker`, `CLEAN`, `LISTED` | DNSBL monitor for the sending domain (Spamhaus DBL, SURBL), 1h result cache. IP lists (Spamhaus ZEN, SpamCop, Barracuda) are defined for `check_ip`, which nothing calls. | `dns.resolver`, `time` |
| 21 | `modules/retention.py` | 118 | `apply_retention` | GDPR storage limitation: pseudonymises never-emailed and no-reply contacts after their retention periods (via `anonymise_contact_rows`), committing per address; clears tracking device data after 30 days. | `modules.database`, `modules.export`, `modules.clock` |
| 22 | `modules/clients.py` | 373 | `ClientRegister`, `load_from_xlsx`, `append_to_xlsx` | Organisation-name matcher for the client and competitor spreadsheets (exact normalised name or identical distinctive tokens). | `openpyxl`, `re`, `unicodedata` |
| 23 | `modules/reply_intent.py` | 221 | `classify`, `is_optout`, `INTERESTED`, `NOT_INTERESTED`, `UNCLEAR` | Multilingual regex classification of replies as interested / not interested / unclear, plus opt-out detection. | `re`, `logging` |
| 24 | `modules/deliverability.py` | 110 | `DeliverabilityLinter` | Advisory template lint: spam words, link count, image-heavy/low-text, ALL-CAPS, missing unsubscribe, exclamation marks. | `re` |
| 25 | `modules/export.py` | 545 | `DataExporter`, `anonymise_contact_rows`, `records_held_about` | GDPR Art. 15/17: data-subject export and erasure (shared with retention). | `modules.database`, `modules.clock` |
| 26 | `modules/__init__.py` | 1 | Package root | Marks `modules` as a package (one comment line). | None |
| 27 | `modules/exclusion_manager.py` | 219 | `ExclusionManager` | Stage: loads and reloads the client/competitor spreadsheets; closed-contact and register screens. | `modules.clients`, `modules.database` |
| 28 | `modules/dispatch_pipeline.py` | 1,352 | `DispatchPipeline`, `SendVerdict` | Stage: the shared send check (`send_verdict`), the screens, the send (`process_contact`), the campaign batch and Direct Send. | `modules.email_sender`, `modules.compliance`, `modules.search` |
| 29 | `modules/backlog_draining.py` | 678 | `BacklogDrainer` | Stage: backlog qualification (only when a send can follow), follow-up and retry passes. | `modules.database` |
| 30 | `modules/loop_orchestration.py` | 1,127 | `LoopOrchestrator` | Stage: the continuous loop and one-off runs, reputation guards, IMAP checks, hot-lead alerts, retention, runtime overrides. | `config`, `modules.clock` |
| 31 | `modules/contact_explainer.py` | 206 | `explain_contact`, `HEADLINES` | Plain-language "why was or wasn't this contact emailed", served at `GET /api/contacts/<id>/why`. | `modules.database`, `modules.export` |

### 3.1 Layered Dependency Hierarchy
The modules form a four-tier architecture:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        TIER 4: ORCHESTRATION                           │
│     agent.py (Facade) ──► Loop / Dispatch / Backlog / Exclusion       │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │
┌────────────────────────────────────▼───────────────────────────────────┐
│                   TIER 3: WORKFLOW ENGINES & FEEDBACK                  │
│  sequences.py, retry_queue.py, imap_monitor.py, learning.py,          │
│  retention.py, query_generator.py, ai_generator.py, reply_intent.py   │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │
┌────────────────────────────────────▼───────────────────────────────────┐
│                 TIER 2: DOMAIN PROCESSING & SAFETY                     │
│  search.py, lead_scoring.py, relevance.py, verification.py,            │
│  warmup.py, domain_throttle.py, blacklist.py, clients.py,             │
│  email_sender.py, unsubscribe.py, analytics.py, deliverability.py      │
└────────────────────────────────────┬───────────────────────────────────┘
                                     │
┌────────────────────────────────────▼───────────────────────────────────┐
│                     TIER 1: FOUNDATION & PERSISTENCE                   │
│  database.py, clock.py, compliance.py, file_handler.py, export.py     │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Data Persistence & Concurrency Review

### 4.1 Database Engine Configuration
The persistence layer in `modules/database.py` manages an embedded SQLite database using explicit concurrency configurations applied to every connection:
- **Write-Ahead Logging (WAL)**: `PRAGMA journal_mode=WAL;`  
  Permits concurrent multi-threaded read transactions while a single write transaction executes, preventing database readers from blocking writers.
- **Foreign Key Enforcement**: `PRAGMA foreign_keys=ON;`  
  Guarantees relational integrity across enrollments, retry records, and email logs.
- **Spin-Lock Busy Timeout**: `PRAGMA busy_timeout=5000;`  
  Configures the SQLite engine to wait up to 5,000 milliseconds for lock clearance before raising `sqlite3.OperationalError: database is locked`.

### 4.2 Threading and Connections
Every `Database` method opens a fresh connection through `_transaction()` / `_get_conn()` and closes it afterwards; there is no pool. A thread-local `conn` (`threading.local`) exists for modules that write directly (sequences, retention, export, the IMAP monitor, the contact explainer) and commit by hand.
- **Agent Thread**: The agent owns its own `Database` instance, on a dashboard daemon thread or on the CLI main thread.
- **Web Dashboard Threads**: Flask request threads use the dashboard's `Database` instance.
- **Teardown**: `@app.teardown_request` calls `close_thread_conn()` for the dashboard's `Database`. An agent used for Direct Send on a request thread keeps its thread-local connection open.

### 4.3 Transaction Atomicity
ACID transactions are managed via the `Database._transaction()` context manager:
```python
@contextmanager
def _transaction(self):
    conn = self._get_conn()   # a new connection per call
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```
Each `Database` method is its own transaction. A send is recorded in several steps (email_log row 'preparing' → Message-ID → delivery status → contact status → warm-up/domain/enrollment counters), and post-send failures are contained rather than rolled back: a delivered message must never look unsent because a bookkeeping row failed. Multi-table atomicity exists in specific methods such as `record_opt_out` (suppression + contact + enrollments); `get_due_retries` reads and claims retry rows `in_flight` in one transaction. The retention job commits per erased address and steps aside every second so dashboard writes are not locked out.

### 4.4 Schema Inventory (15 Relational Tables)
1. **`search_log`**: Every search query, with source (`google`, `cli`, `dashboard`, `discovery`, `strategist`, `ai_generator`, `manual`), result counts and status (`pending`, `in_progress`, `completed`, `no_results`).
2. **`contacts`**: Core entity registry. Unique index `idx_contacts_email_unique` on `contacts(email)`; addresses are stored trimmed and lower-cased by `add_contact` and the startup migration, which makes the index effectively case-insensitive (an expression index on `LOWER(email)` serves the case-insensitive lookups). Tracks lead score, company, role, qualification timestamp, status and the reason for it.
3. **`email_log`**: One row per dispatch attempt, updated as it progresses: `contact_id`, `recipient_email`, `sender_account`, `subject`, `body_preview`, `ab_variant`, `message_id`, `tracking_id`, `opened_at`, `delivery_status` (`preparing` → `sent`, `sent_unconfirmed`, `failed`, `rejected`, `dry_run`), `smtp_response`, `error_message`. Pseudonymised on erasure/retention.
4. **`suppression_list`**: Do-not-contact registry, stored lower-case with a UNIQUE email. Reasons: `unsubscribe`, `bounce`, `smtp_rejected`, `gdpr_erasure` (permanent) and `blocked_via_dashboard` (the one reason the dashboard's Unblock may remove; an opt-out from a blocked address replaces it).
5. **`activity_log`**: Operational event ledger (e.g. `SEARCH_START`, `EMAIL_SENT`, `BOUNCE_GUARD`, `BLACKLIST_GUARD`, `IMAP_CHECK`, `REPLY_RECEIVED`, `CONTACT_BLOCKED`).
6. **`daily_send_counts`**: Tracks daily dispatches per sender account for warmup schedule compliance.
7. **`domain_send_counts`**: Tracks daily dispatches per recipient domain for domain throttle enforcement.
8. **`retry_queue`**: Retries with exponential backoff (5 min × 6^attempt, 3 attempts). Status values used by the code (no CHECK constraint): `pending`, `in_flight`, `succeeded`, `exhausted`, `cancelled`.
9. **`sequences`**: Cadence templates defining multi-touch follow-up workflows.
10. **`sequence_steps`**: Individual steps within a sequence defining step numbers, delay days, template variants, and subject lines.
11. **`sequence_enrollments`**: Tracks contact progression through sequences, and why a cancelled one was cancelled (`cancel_reason`). Enforces partial unique index `idx_enrollment_active_unique` on `(contact_id, sequence_id)` WHERE `status = 'active'`.
12. **`open_tracking`**: Pixel open events (tracking id, email_log id, open time). The IP and User-Agent columns are legacy and never filled; retention clears any old values after 30 days. Tracking is off unless `OPEN_TRACKING_ENABLED`.
13. **`lead_alerts`**: Deduplicated record of operator notifications for positive prospect replies.
14. **`agent_state`**: Durable key/value runtime state (e.g. `'retention:last_run_date'`, `'cfg:<setting>'` runtime overrides, `'imap_last_scan::<mailbox>'`).
15. **`consent_log`**: GDPR Art. 6 record: email, legal basis (`legitimate_interest`), free-text details (e.g. the page the address was found on) and timestamp. One row per basis, written when a contact is qualified or sent a live Direct Send.

### 4.5 Status State Machine Invariants
The system enforces strict contact status partitioning:
- **`CLOSED_CONTACT_STATUSES`**: `frozenset({"responded", "active_client", "unsubscribed", "erased", "bounced", "blocked"})`  
  Every send path refuses these - first, follow-up, retry or manual - through `DispatchPipeline.send_verdict`, and the dashboard will not re-queue them. All are final except `blocked`, which the dashboard's Unblock can return to `pending` when the block came from the dashboard.
- **`DISCARDED_CONTACT_STATUSES`**: `frozenset({"irrelevant", "ai_irrelevant", "low_score", "unverified"})`  
  Contacts that failed qualification screening and are suppressed from cold outreach.

---

## 5. Deliverability & Compliance Guardrails

### 5.1 Multi-Account Warmup Ramps
Sender reputation is protected by `WarmupScheduler` (`modules/warmup.py`):
- New sender accounts begin at `WARMUP_START_DAILY` emails on Day 1 (default 5/day; `doctor` flags anything above 10).
- The daily cap grows linearly by `WARMUP_INCREMENT` (default +3/day) for each calendar day on which that mailbox has sent, so allowance = start + (day−1) × increment, capped at `WARMUP_MAX_DAILY` (default 100). Days need not be consecutive.
- Hard ceiling cap enforces maximum daily throughput per mailbox.
- Round-robin cursor (`_rr`) rotates outbound traffic across available senders. The mailbox is picked last in the send check, so a contact that is then skipped never turns the rotation.

### 5.2 Recipient Domain Throttling & Domain Backoff
- **Domain Throttle (`DomainThrottle`)**: Restricts outbound messages to any single recipient corporate domain to a maximum threshold (default: 3 emails/day). Prevents triggering corporate email spam defenses.
- **Domain Backoff (`_domain_backoff`)**: Analyzes 7-day rolling bounce statistics per recipient domain. If a domain exhibits repeated bounces exceeding threshold ratios, the agent places the domain in backoff status, deferring subsequent emails without wasting retry attempts.

### 5.3 Reputation Protection Circuit Breakers
1. **Global Bounce Guard (`_bounce_guard_tripped`)**:
   Monitors the aggregate 7-day bounce rate across all dispatches. If `recent_sent >= min_sends` and `bounce_rate > max_bounce_rate` (default: 5.0%), the circuit breaker trips, setting `_sending_paused = True`. Halts all outbound dispatches while keeping monitoring tasks active.
2. **DNSBL Spam Blacklist Monitor (`_blacklist_guard_tripped`)**:
   Checks the sending domain against the domain blocklists Spamhaus DBL and SURBL (results cached 1h). A LISTED verdict latches `_sending_paused = True` with reason "blacklist" (for a Direct Send, only on a running campaign agent). The IP lists (ZEN, SpamCop, Barracuda) exist in `blacklist.py` but the guard does not use them.
3. **SMTP Authentication Circuit Breaker**:
   If an SMTP server returns a `535 Authentication Failed` error, the agent trips the circuit breaker immediately to prevent continuous authentication failures that could trigger a permanent mailbox lockout by Microsoft 365 or Google Workspace.

### 5.4 Legal Compliance & Address Screening
1. **GDPR Article 14 & Legitimate Interest**:
   Cold B2B communication is grounded in GDPR Article 6(1)(f) (Legitimate Interest). The agent records legitimate interest justifications in `consent_log`, generates signed HMAC opt-out tokens, and processes opt-outs automatically.
2. **Codice Civile Art. 2250 (Italian Legal Disclosure)**:
   Mandates the inclusion of corporate identity details in all business communications: registered corporate name, registered office address, Chamber of Commerce registration (REA / Partita IVA), and share capital (`capitale sociale`) for an S.p.A./S.r.l. `_send_blocker` refuses live sends (dry runs still work) while `LEGAL_ENTITY_NAME`, `LEGAL_REGISTERED_OFFICE`, `LEGAL_COMPANY_REGISTRATION` or `PRIVACY_NOTICE_URL` is blank. Share capital is printed when set and flagged by `doctor` when blank, but it does not block sending.
3. **Certified Email (PEC) Screening**:
   Under Italian law, Posta Elettronica Certificata (PEC) is reserved for formal legal and administrative notices. `_is_certified_mail` matches whole domain labels (pec, arubapec, legalmail, postacert, cert, postecert, ...), so `pec.aruba.it` is refused and `specialita.it` is not. PEC addresses are dropped at harvest; one that reaches a send is refused by `screen_contact` and marked `irrelevant` (reason: `PEC (certified mail) address`).
4. **Consumer Webmail Screening**:
   To prevent contacting private consumer mailboxes without prior consent, `PERSONAL_DOMAINS` screens out personal email providers across Europe (`gmail.com`, `libero.it`, `tiscali.it`, `virgilio.it`, `gmx.de`, `web.de`, `orange.fr`, `yahoo.com`, `hotmail.com`).

### 5.5 The Shared Send Check (`DispatchPipeline.send_verdict`)
Added after this audit. Every send path runs the same check before each contact, with a purpose of `first` (the campaign), `followup`, `retry` or `manual` (Direct Send), and gets back a `SendVerdict` whose scope says how far the answer reaches: `batch` (stop the pass), `drop` (settle this contact), `defer` (not now) or `cleared` (send it, through this mailbox). The order, cheapest and widest first:

1. The agent is running (not for a manual send).
2. The latched pause (a manual send only; the loops never run while paused).
3. Installation-wide blockers: a blank legal identity, a failed SMTP login.
4. The business-hours window (advisory for a manual send).
5. The bounce breaker, then the blocklist guard (both skipped by a manual preview).
6. The contact's current row, re-read (the loop's copy can be hours old).
7. Closed contacts: suppressed, replied, client, opted out, bounced, erased, blocked; the automated paths also refuse the filter verdicts, which the operator may override by hand.
8. The address, competitor and client screens (`screen_contact`).
9. Already emailed (the campaign and retries; never a second first email).
10. The per-domain daily cap, then the per-domain bounce backoff (both "not today").
11. Last, the sending mailbox with warm-up allowance left (the primary mailbox for a manual send).

`process_contact` repeats suppression, the screens and the blockers as the final guarantee, and refuses a template without the legal footer. A refusal before dispatch costs no send delay.

---

## 6. Refactoring Blueprint for the 4 Pipeline Stages

**Status: done.** Implemented in e83dadd, then repaired in follow-up commits: the six stage-to-stage calls that skipped the agent were routed through it (7e685b2), the explanatory comments the extraction deleted were restored (b385050), the stop request was logged again (181943f), and the delivery guards were merged into one check (e88c39e and after; see 5.5).

### 6.1 Architectural Pattern: Coordinating Facade
`OutreachAgent` in `agent.py` is a **Coordinating Facade** (built in e83dadd). It keeps every pre-refactor method name and signature for `main.py` (CLI and Flask dashboard) and for the test suite, which has grown from 1,099 tests to 1,355 in the default run plus 42 UI tests. Send behaviour has changed since, deliberately.

```
                                ┌────────────────────────┐
                                │     OutreachAgent      │  (agent.py - Facade)
                                │  (same method names)   │
                                └───────────┬────────────┘
          ┌───────────────────┬─────────────┴─────────────┬───────────────────┐
          ▼                   ▼                           ▼                   ▼
 ┌──────────────────┐┌──────────────────┐┌──────────────────┐┌──────────────────┐
 │ ExclusionManager ││ DispatchPipeline ││  BacklogDrainer  ││ LoopOrchestrator │
 │ (modules/        ││ (modules/        ││ (modules/        ││ (modules/        │
 │ exclusion_       ││ dispatch_        ││ backlog_         ││ loop_            │
 │ manager.py)      ││ pipeline.py)     ││ draining.py)     ││ orchestration.py)│
 └──────────────────┘└──────────────────┘└──────────────────┘└──────────────────┘
```

#### The Inverted Callback / Bound Agent Protocol
Extracted stage modules take a reference to the parent agent at construction (`__init__(self, agent: OutreachAgent)`, with the type imported under `TYPE_CHECKING`) and store it as `self.agent`. When a stage needs a sibling's method (for example, `BacklogDrainer` invoking email dispatch), it calls `self.agent._process_contact(...)` rather than the sibling directly; `tests/test_facade_routes_every_call.py` pins this.

**Rationale**: Existing test suites monkeypatch internal methods on `OutreachAgent` instances (e.g. `monkeypatch.setattr(agent, "_process_contact", mock_fn)`). Calling through `self.agent.<method>` makes such a patch apply wherever the method is used.

### 6.2 Detailed Stage Module Specifications

#### Stage 1: `modules/exclusion_manager.py` (`ExclusionManager`)
- **Primary Responsibility**: Encapsulates file-based client and competitor spreadsheet synchronization, in-memory register caching, fingerprinting, and contact exclusion screening.
- **Key Methods**:
  - `_list_stamp(path) -> tuple[int, int] | None`: returns the `(st_mtime_ns, st_size)` fingerprint of a `pathlib.Path`, or None when `stat()` raises OSError.
  - `_load_register(kind: str, raw_path: str) -> bool`: Safe loader preserving prior register if file read fails.
  - `load_client_register() -> bool`: Loads active client list from Excel.
  - `load_competitor_register() -> bool`: Loads competitor list from Excel.
  - `refresh_lists_if_changed() -> None`: Inspects disk timestamps and hot-reloads modified registers.
  - `known_competitor(organization: str | None) -> str | None`: matches the company against the competitor register by exact normalised name or an identical set of distinctive tokens (legal forms, generic words, accents and case ignored; no similarity matching). Returns None (fails open) on error.
  - `active_client(organization: str | None) -> str | None`: the same matching against the client register. Returns None (fails open, logged) on error.
  - `contact_is_closed(contact: dict) -> str | None`: returns "suppressed" for an address on the suppression list; otherwise the contact's status when it is closed (responded, active_client, unsubscribed, erased, bounced, blocked) or discarded (irrelevant, ai_irrelevant, low_score, unverified); else None. It does not consult the client register.

#### Stage 2: `modules/dispatch_pipeline.py` (`DispatchPipeline`)
- **Primary Responsibility**: Encapsulates the centralized email send chokepoint, preflight safety guards, rate limiting, sender rotation, email body rendering, legal footer insertion, and post-dispatch state recording.
- **Key Methods**:
  - `by_lead_score(contacts: list[dict]) -> list[dict]`: Stable sort descending by `lead_score`.
  - `send_delay() -> float`: Calculates rate-limited delay with jitter and hourly floor.
  - `pick_sender() -> tuple[str, str, Any] | None`: Round-robin sender selection checking daily warmup caps.
  - `email_contacts(qualified: list[dict], dry_run: bool = False) -> None`: Batches qualified contact dispatch.
  - `send_verdict(contact: dict, *, purpose: str, dry_run: bool) -> SendVerdict`: the one check every send path runs before each contact (see 5.5).
  - `screen_contact(contact: dict) -> tuple[str, str, str] | None`: the address, competitor and client screens.
  - `send_possible_now() -> bool`: not paused, inside the window, and some mailbox with allowance left.
  - `process_contact(contact: dict, dry_run: bool = False, ...) -> bool`: Central send chokepoint executing guards, template rendering, SMTP dispatch, and database logging.
  - `send_single_email(email: str, ...) -> tuple[bool, str]`: Manual / ad-hoc email dispatch handler.

#### Stage 3: `modules/backlog_draining.py` (`BacklogDrainer`)
- **Primary Responsibility**: Encapsulates queued lead retrieval, fast-path vs. deep qualification partitioning, sequence cadence processing, and exponential backoff retry draining.
- **Key Methods**:
  - `process_pending_contacts(dry_run: bool = False) -> None`: reads pending contacts and passes already-qualified ones through a fast path that still applies the client and heuristic relevance gates. It qualifies the rest only when a send could follow now (otherwise they stay pending for a later pass), then hands everything to `_email_contacts`, which sorts by lead score and runs the shared send check before each send.
  - `qualify_contacts(contacts: list[dict]) -> list[dict]`: the full qualification pipeline: lead scoring, client and competitor screening, the heuristic relevance gate, an optional AI relevance gate (`AI_RELEVANCE_ENABLED`, off by default) and email verification (a format check plus a DNS MX lookup; the SMTP RCPT probe runs only when `VERIFY_SMTP_PROBE` is on).
  - `process_sequences(dry_run: bool = False) -> None`: Identifies and dispatches due multi-touch follow-up cadence steps.
  - `process_retries() -> None`: Recovers stale in-flight retry claims and dispatches due retry attempts.

#### Stage 4: `modules/loop_orchestration.py` (`LoopOrchestrator`)
- **Primary Responsibility**: Encapsulates the top-level autonomous continuous loop lifecycle, single batch runs, graceful shutdown, pacing sleep, IMAP inbox monitoring, hot-lead alerts, data retention, and dynamic config overrides.
- **Key Methods**:
  - `run_continuous(queries: list[str] | None = None, dry_run: bool = False, loop_delay: float = 300.0) -> None`: Continuous daemon loop execution.
  - `run_single(queries: list[str] | None = None, dry_run: bool = False, ...) -> dict | None`: Standalone batch run execution.
  - `stop() -> None`: Graceful shutdown signal handler.
  - `interruptible_sleep(seconds: float) -> None`: 1-second sliced sleep loop monitoring `_running`.
  - `next_loop_delay(loop_delay: float) -> float`: Calculates cycle sleep respecting business hours and IMAP schedules.
  - `safe_to_resume() -> bool`: Evaluates reputation pause conditions and validates recovery.
  - `maybe_check_imap() -> None`: Schedules and executes periodic IMAP mailbox scans.
  - `handle_interested_replies(entries: list) -> None`: Coordinates operator notification for warm leads.
  - `send_lead_alert(entry: dict, recipients: list) -> bool`: Dispatches hot-lead alert notifications.
  - `maybe_apply_retention(dry_run: bool) -> None`: Daily GDPR data minimization cleanup pass.
  - `apply_runtime_overrides() -> None`: Hot-reloads configuration overrides from the database.

---

## 7. Residual Operational Risks & Mitigation Plan

### 7.1 Risk Analysis & Mitigations

1. **SMTP Account Lockout via Cascading Failures**:
   - *Risk*: A transient SMTP server error or credential expiration could trigger repeated login attempts during backlog draining, resulting in tenant-wide account lockout by Microsoft 365 or Google Workspace.
   - *Mitigation*: On `SMTPAuthenticationError` (e.g. 535) that mailbox's `EmailSender.auth_failed` latches, and `_send_blocker` then refuses every live send (campaign, follow-ups, retries and Direct Send on the same agent) until the credentials are fixed and the agent is restarted. The contact is left `pending`, and the server's own text is kept so a disabled-tenant setting can be told from a wrong password.
2. **Database Lock Contention under Concurrent Load**:
   - *Risk*: Heavy concurrent read/write operations between the background agent thread and Flask dashboard worker threads could cause `sqlite3.OperationalError: database is locked`.
   - *Mitigation*: Every connection sets WAL mode and `busy_timeout=5000`. `Database`'s own methods go through `_transaction()`, which opens a short-lived connection per transaction, commits, and rolls back on error. A thread-local connection (`Database.conn`) is used by sequences, retention, export, the IMAP monitor and the contact explainer, which commit on it directly. The retention job commits per address and steps aside every second.
3. **DNS Lookup Timeouts in Constrained Environments**:
   - *Risk*: Synchronous DNS lookups (MX resolution, DNSBL checks) using `dnspython` can raise `LifetimeTimeout` in network-restricted sandboxes or corporate firewalls.
   - *Mitigation*: DNSBL lookups use a 4s timeout and a 1-hour in-memory cache. MX lookups use dnspython's default resolver lifetime (5s) and are not cached. Both catch DNS exceptions: an unavailable DNSBL verdict does not trip the guard, and a failed MX lookup leaves the contact queued for another attempt.
4. **Corrupt or File-Locked Spreadsheets During Live Reload**:
   - *Risk*: Operators editing the client list (`CLIENT_LIST_PATH`) or the competitor list (`COMPETITOR_LIST_PATH`, blank = off) in Microsoft Excel or syncing via OneDrive may temporarily lock the file or leave half-written binary bytes.
   - *Mitigation*: `ExclusionManager` catches `openpyxl` read exceptions, records the corrupted file stamp in `_list_failed` to prevent log flooding, and retains the prior valid in-memory register.
5. **Stale In-Flight Retries Due to Process Termination**:
   - *Risk*: Sudden process termination (SIGKILL, container restart) while processing retries can strand rows in `in_flight` status indefinitely.
   - *Mitigation*: At the start of each retry pass that `_send_blocker` lets through, `BacklogDrainer.process_retries` calls `retry_queue.reset_stale_in_flight()`, which returns to `pending` any `in_flight` row whose `next_retry_at` is more than 30 minutes past (a claim records no timestamp of its own). A failed attempt with retries left goes straight back to `pending`.

### 7.2 Operational Risk Matrix

| Risk Event | Severity | Likelihood | Impact Area | Automated Detection | Operational Mitigation |
|---|---|---|---|---|---|
| SMTP Credential Rejection | HIGH | LOW | Dispatch Pipeline | Catches `SMTPAuthenticationError` / code 535 | Trips circuit breaker, halts sending, alerts operator |
| SQLite Lock Timeout | MEDIUM | LOW | Data Persistence | Catches `sqlite3.OperationalError` | 5s busy timeout, WAL mode, transaction rollback |
| DNSBL / MX DNS Timeout | LOW | MEDIUM | Verification / Guards | Catches `dns.resolver.LifetimeTimeout` | DNSBL: 4s timeout, 1h cache; an unavailable lookup does not trip the guard, but a blacklist pause lifts only on a full CLEAN result. MX: 5s default lifetime, no cache; an indeterminate result leaves the contact pending |
| Locked / Broken Excel File | MEDIUM | MEDIUM | Exclusion Reload | Catches `openpyxl` & `zipfile` errors | Preserves existing register, caches failed stamp |
| Sudden Process Termination | MEDIUM | LOW | Retry Engine | `in_flight` rows whose `next_retry_at` is more than 30 minutes past | Stale in-flight recovery resets them to `pending` |
| Rate Limit / IP Greylisting | MEDIUM | MEDIUM | Deliverability | 4xx SMTP answers and 4.x.x soft-bounce DSNs are treated as transient | The retry queue re-attempts with exponential backoff (~5 min, 30 min, 3 h). Domain backoff counts only hard bounces and 5xx recipient rejections, so it does not react to greylisting |

---

## 8. Quality Assurance & Verification Strategy

### 8.1 Baseline Verification Results
Prior to initiating refactoring, the complete test suite was verified on branch `refactor/modular-agent`:
- **Headless Test Suite**: `python -m pytest -q`
  - **Result**: `1099 passed, 36 deselected in 140s` (Exit Code 0).
  - Status: 100% passing at that commit. (Today: 1,355 tests in the default run, all passing, plus 42 UI tests under `pytest -m ui`; a mutation audit of 230 deliberate defects reports every one caught.)
- **Preflight Doctor Check**: `python main.py doctor`
  - **Result**: not recorded. The command prints about 24 rows (more when SPF/DMARC or `--smtp` apply), catches each check's exception into its own row, and exits 1 only when a blocking check (legal identity, legal footer in templates) fails.

### 8.2 Refactoring Test Expansion Plan (Milestone 3)
**Status: done.** bc83428 added the five per-stage test files below, a5903b6 rewrote the ones that could not fail, and `tests/test_facade_routes_every_call.py` checks that stages call one another through the agent. What each file covers today:
1. `tests/test_exclusion_manager.py`: timestamp fingerprinting, missing, corrupt and empty spreadsheets, unmatchable rows, change-triggered reload, register preservation, matcher fail-open and `contact_is_closed`. (No case-normalisation test.)
2. `tests/test_dispatch_pipeline.py`: lead-score ordering, pacing and jitter, sender rotation and warm-up caps, PEC, personal and suppression screening, the missing-footer refusal, unsubscribe and tracking links, and Direct Send. (Footer rendering is tested in `tests/test_legal_footer.py`.)
3. `tests/test_backlog_draining.py`: the fast-path client and relevance gates, qualification (scoring, low score, competitor, stop), sequence advance, cancel and window deferral, and retry stale-claim reset, already-emailed cancel and release.
4. `tests/test_loop_orchestration.py`: lifecycle (stop, run_single, run_continuous), interruptible sleep, the send window, the bounce guard, blacklist resume, reply intent, hot-lead escalation, retention scheduling and runtime overrides. (No IMAP scan-trigger test.)
5. `tests/test_stage_error_recovery.py`: transient vs permanent SMTP error classification (including disconnect and MX-resolution error texts), DSN soft/hard bounce classification, stale in-flight retry recovery, concurrent status writes across threads, and missing or corrupt register files. (A dropped SMTP connection is now tested in `tests/test_a_dropped_connection_is_not_a_send.py`.)

---

**Correction note**:  
The original closed with a self-certification ("verified against the production codebase and approved for immediate execution"). There was no recorded review or approval, the Milestone 2 refactor needed follow-up fixes before it met the protocol in 6.1, and many of the report's figures and mechanisms were wrong; it has been removed. The claims above were re-checked against the code on 2026-10-09.
