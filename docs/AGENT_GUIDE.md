# SUPPLECO Outreach Agent — Study Guide

A complete, plain-English reference to everything the agent does, how each part
works, and prepared answers for the hard questions. Read the **Q&A appendix**
(§10) last: it gives short answers to the questions IT, sales and counsel are
likely to ask.

Companion docs: `DEPLOY.md` (installing and running) · `SETTINGS.md` (every
setting) · `DELIVERABILITY.md` (sending strategy) · `COMPLIANCE.md` (the GDPR
controls) · `ARCHITECTURE.md` (the org-chart view) ·
`docs/privacy/decisions_for_counsel.md` (the legal questions open with counsel).

---

## 1. What it is, in one paragraph

An autonomous B2B cold-outreach system. It **discovers** European companies that
sell food supplements / nutraceuticals, **extracts and qualifies** their contact
addresses, **sends SUPPLECO's own first email** to each, in Italian or English
by the recipient's country and with the Company Profile PDF attached, **sends**
within reputation-safe limits, runs **timed follow-ups**, watches the inbox for
**replies / bounces / unsubscribes**, and **learns** which searches produce real
leads. It runs as a single Python service with a web dashboard. The end customer
is **SUPPLECO** (an Italian contract manufacturer), and outreach goes out **as
SUPPLECO** from a company mailbox; which mailbox, and how the agent signs in to
it, is still open (§6).

**Not pharma.** SUPPLECO makes food supplements & nutraceuticals (vitamins,
probiotics, omega/collagen, sports nutrition, herbal, beauty-from-within). The
agent targets **buyers** of contract manufacturing — supplement _brands,
distributors, retailers, pharmacies_ — never other manufacturers (competitors).

---

## 2. End-to-end flow

```
        ┌──── continuous loop (every LOOP_DELAY_SECONDS) ──────┐
        │                                                       │
 1. Read the INBOX and junk folder when a check is due (IMAP    │
        on), before any send: replies / bounces / opt-outs /    │
        data requests                                           │
 2. Drain backlog ──► email already-qualified leads first       │
 3. Can we still send today? (warmup budget left?)              │
        │  yes                                                  │
 4. Get pending searches ── empty? ──► DISCOVER new queries,    │
        │   if the backlog is under MAX_PENDING_BACKLOG         │
        │   (Strategist AI → AI company list → free generator)  │
        │   none at all while the backlog could not be emailed  │
        │   within FIRST_CONTACT_MAX_AGE_DAYS (warm-up on)      │
 5. For each query: SEARCH the web ──► scrape pages ──►         │
        extract emails ──► QUALIFY (score, relevance, verify)   │
 6. EMAIL qualified contacts (warmup + window + domain caps)    │
 7. Process due FOLLOW-UPS (sequence steps)                     │
 8. Process RETRY queue (failed sends, backoff)                 │
 9. Apply RETENTION once a day (never on a dry run)             │
        │                                                       │
        └───────────────── sleep, repeat ──────────────────────┘
```

The loop runs every `LOOP_DELAY_SECONDS` (about 5 minutes) in business hours;
outside the send window it sleeps until the window opens, or until the next
inbox check when IMAP is on.

Every send - first email, follow-up, retry, Direct Send - goes through one shared
check, `DispatchPipeline.send_verdict`: **stop/pause → install blockers (legal
identity, opt-out route, an inbox unread with IMAP on, no country enabled,
client list not loaded or screening no company, SMTP auth)
→ business-hours window → bounce circuit-breaker → blocklist guard → closed
contact (suppressed, company blocked, replied, client, opted out, bounced,
erased, filtered) → address, competitor and client screens → already emailed,
or a send still `preparing` →
per-domain cap → per-domain bounce backoff → warm-up allowance (mailbox pick) →
send → record**. For Direct Send the window is advisory. The send itself
(`_process_contact`) repeats the suppression and company-block checks, the
screens and the blockers, and adds refusals of its own: a first email more
than `FIRST_CONTACT_MAX_AGE_DAYS` (30) after collection, and, for a live
send, a recipient outside the enabled countries, a contact with no recorded
lawful basis, a first email through a mailbox without `SENDER_TITLE`, or
one without the Company Profile PDF while `ATTACH_PROFILE` is on. It also
refuses a template without the legal footer, and checks the suppression list
again just before it writes anything about the person and just before the
email goes, so an erasure, opt-out or block that lands meanwhile stops it. A
send cut off after the server may have accepted it (a stop, a crash, a locked
database) is never repeated: its row stays `preparing` and holds the contact
for up to 26 minutes, then is settled as possibly sent
(`sent_unconfirmed`). The
dashboard's contact panel explains why a contact was filtered, closed or
emailed; a contact the country lock refuses stays waiting, and only the log
file says why.

---

## 3. Architecture & technology

**Pattern:** _Orchestrator–Workers._ `agent.py` is a facade over four stage
classes (the loop, the send pipeline, the backlog, the exclusion lists); each
other concern is an independent module ("worker"). The LLM handles the three
main cognitive jobs (see §5) plus three smaller ones: opening lines, a
tie-break on unclear reply intent, and a legacy company-name discovery step.
Everything deterministic is plain, testable code.

| Layer              | Tech                                                                                                               |
| ------------------ | ------------------------------------------------------------------------------------------------------------------ |
| Language / runtime | Python (single process; agent runs on a background thread)                                                         |
| Dashboard          | Flask + a JSON API served by waitress, HTTP Basic Auth, vanilla JS + Chart.js frontend; Chart.js and Font Awesome are served from `static/vendor/` (without Chart.js the charts say so and the rest works) |
| Data               | SQLite in **WAL** mode, `busy_timeout=5000`; every write is a transaction                                          |
| AI                 | Only an OpenAI-compatible server on SUPPLECO's own machine or private network, at `LLM_BASE_URL` (`LLM_PROVIDER=openai_compatible`), over `httpx`; no cloud AI provider |
| Discovery          | DuckDuckGo (`ddgs`) search + `curl_cffi` fetch (browser TLS fingerprint; `httpx` fallback) + `BeautifulSoup` parse |
| Email              | `smtplib` (SMTP send, pooled) · `imaplib` (inbox) · `dnspython` (MX)                                               |
| Timezone           | `zoneinfo` Europe/Rome via `modules/clock.py`                                                                      |
| Tests              | `pytest` — more than 1,700 tests in the default run, plus more than 40 browser tests (`-m ui`)                     |

**Why SQLite (not Postgres)?** The workload is the agent plus a low-traffic
dashboard, thousands of rows — not millions. SQLite in WAL mode with a busy
timeout handles this with zero operational overhead. It's a deliberate
right-sizing, not a limitation. Swapping to Postgres would mean changing the
`Database` class plus the SQLite-specific SQL that several modules (warm-up,
domain throttle, retry queue, sequences, analytics, retention, export, the IMAP
monitor, the contact explainer) run directly on its connection.

---

## 4. Module-by-module (the `modules/` folder)

| Module                  | What it does                                                                                                                                                                                                                                                                                                                                                                                                |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent.py` _(root)_     | **Facade.** `OutreachAgent` builds every component and keeps the old method names, delegating to the four stage modules below.                                                                                                                                                                                                                                                                              |
| `loop_orchestration.py` | The continuous loop and one-off runs, reputation guards (bounce, blocklist) and resuming after them, IMAP checks, hot-lead alerts, the retention trigger.                                                                                                                                                                                                                                                   |
| `dispatch_pipeline.py`  | The shared send check (`send_verdict`) every send path runs, the screens, the send itself (`_process_contact`), the campaign batch and Direct Send.                                                                                                                                                                                                                                                         |
| `backlog_draining.py`   | The backlog (qualification only when a send can follow), follow-up and retry passes.                                                                                                                                                                                                                                                                                                                        |
| `exclusion_manager.py`  | Loads the client and competitor spreadsheets (and reloads them when edited); the closed-contact and register screens.                                                                                                                                                                                                                                                                                       |
| `config.py` _(root)_    | All settings via `pydantic-settings`, loaded from `.env`; operational dials (`RUNTIME_EDITABLE`: pacing, thresholds, send window) can be changed from the dashboard's settings panel, which shows and validates the values in force; they apply to every run, and a reset reaches a running agent. Helper methods: subjects, send-window days, sender accounts, legal-identity check.                       |
| `search.py`             | Web search + page scrape + **email extraction**. Ignores `<script>`/`<style>` so config blobs (Sentry DSNs) can't become fake contacts; reads `mailto:` links + visible text; decodes HTML entities; de-obfuscates `[at]`/`[dot]`; drops asset filenames and generic mailboxes.                                                                                                                             |
| `relevance.py`          | Deterministic **relevance gate**: European geo (TLD + local-part region tokens + non-EU domain blocklist), placeholder/junk rejection, negative-industry keywords, and an optional vertical requirement. Free, no API.                                                                                                                                                                                      |
| `lead_scoring.py`       | Scores each contact (role seniority, org signals, domain quality) into a tier; below `MIN_LEAD_SCORE_TO_EMAIL` it's skipped.                                                                                                                                                                                                                                                                                |
| `verification.py`       | Email verification: format (`email-validator`), **MX record** lookup (`dnspython`), optional SMTP probe (off by default).                                                                                                                                                                                                                                                                                   |
| `ai_generator.py`       | The **LLM interface**: `generate_email_body` (Copywriter), `classify_relevance` (Analyst), `suggest_discovery_queries` (Strategist), plus `generate_opening_line`, `classify_reply_intent` (reply tie-break) and `generate_companies` (legacy discovery). Retries with `Retry-After` handling; on failure each returns an empty result so callers fall back. The three classifiers (relevance, company country, reply intent) are asked at temperature 0 on every provider, so the same case gets the same verdict. |
| `ai_benchmark.py`       | `main.py ai-benchmark`: asks `ai_generator`'s relevance, country, reply and query questions about the invented cases in `benchmark/cases`, several times each, and scores them by what a wrong answer costs against the pass marks in `PASS_MARKS`; writes nothing to the database (DEPLOY.md §11). |
| `ai_proof.py`           | `main.py ai-proof`: builds the agent as `main.py` does and puts a few of the benchmark's invented companies through its own qualification and dry-run send, and a few invented replies through the inbox's filing and its judgement of a reply, with the three AI questions on. It sends nothing, works in a database of its own that it removes afterwards, and fails if any step but the AI reaches the network (DEPLOY.md §11). |
| `query_generator.py`    | **Free heuristic discovery** — supplement verticals × buyer types × European countries, weighted by the learning loop. Runs when the search queue is empty, the backlog is under `MAX_PENDING_BACKLOG`, and the AI steps produced no queries. Terms proven empty are skipped.                                                                                                                               |
| `learning.py`           | **Learning loop.** Aggregates per-search outcomes; up-weights verticals/countries that produced verified contacts (and especially replies); feeds the query generator and the Strategist.                                                                                                                                                                                                                   |
| `email_sender.py`       | Builds the MIME message (HTML + plain text + `List-Unsubscribe`; the Company Profile PDF on first emails only), sends over a **pooled, lock-guarded SMTP** connection, logs every attempt, writing its row `preparing` before the server sees it.                                                                                                                                                                                                                              |
| `sequences.py`          | **Follow-up engine.** Enrollments, timed steps (delay-days), advance/complete/cancel. Auto-cancels on reply/unsubscribe. UTC-safe date math.                                                                                                                                                                                                                                                                |
| `imap_monitor.py`       | Watches the inbox and the Junk Email folder, and holds live sends while one goes unread; classifies **hard bounce / soft bounce / unsubscribe / data request / auto-reply / read receipt / reply**. Parses the failed recipient from a DSN (`Final-Recipient`) and suppresses hard bounces; treats opt-outs, data requests and (from people emailed) declines as objections; marks replies "responded"; ignores auto-replies and read receipts.         |
| `warmup.py`             | **Warm-up scheduler.** Daily allowance = start + (day−1)×increment, capped at max; resets on the Italy calendar day; `can_send()` gates the loop.                                                                                                                                                                                                                                                           |
| `domain_throttle.py`    | Per-recipient-**domain** daily cap (default 3), so you never hammer one company.                                                                                                                                                                                                                                                                                                                            |
| `retry_queue.py`        | Failed sends re-queued with **exponential backoff** (5 min → 30 min → 3 h → exhausted).                                                                                                                                                                                                                                                                                                                     |
| `analytics.py`          | Open-tracking pixel storage + chart data (sends/day, funnel, status, success trend), counting only mail that went out.                                                                                                                                                                                                                                                                                                                        |
| `deliverability.py`     | **Spam linter** — flags trigger words, too many links, missing unsubscribe, image-heavy bodies.                                                                                                                                                                                                                                                                                                             |
| `unsubscribe.py`        | Unsubscribe tokens that verify themselves under `UNSUBSCRIBE_SECRET` + the confirmation page, in Italian for an Italian recipient (used when hosted publicly). A click on a token the database has no record of, as after a restore, is kept for a year if the token is ours.                                                                                                                                                                                                                                                                                                                              |
| `company_block.py`      | **Company-wide blocks.** Blocks a company by its email domain after an opt-out by email or a data request from someone the agent emailed, a one-click unsubscribe or an erasure, so no colleague is emailed or collected; never a free-mail or PEC domain. `main.py suppress-domain` lists and lifts blocks.                                                                                                |
| `optout_spool.py`       | Opt-outs that met a locked database, held on disk one file each and applied at start-up and before every send; the address is refused meanwhile.                                                                                                                                                                                                                                                            |
| `database.py`           | The `_transaction()` helper, the core schema + migrations and most queries (incl. `get_deliverability_metrics` and the shared opt-out routine). It defines every table; several modules run their own SQL on its connection.                                                                                                                                                                             |
| `journal.py`            | The journal beside the database (`journal.jsonl`): opt-outs, company blocks and lifts, dashboard Blocks and Unblocks, bounces and server refusals, erasures, data-subject requests registered and closed, and every email sent (who, which message and follow-up step, when, from which mailbox; no subject or text), one line each, re-applied in order by `main.py replay-journal` after a restore, which also puts back each email the backup does not know, so none is sent twice. An erasure is kept only as its token.                                                                                                                                                                                           |
| `backup.py`             | `main.py backup`: a checked copy of the database (SQLite's backup API, then `PRAGMA integrity_check`) in `backups/` beside it, keeping the newest 14.                                                                                                                                                                                                                                                       |
| `security_check.py`     | `main.py security-check`: asks pip-audit about the packages installed for the agent's Python and records the answer in the activity log: an error when a package the agent uses (requirements.txt and what those need) has a known vulnerability. Installs nothing (DEPLOY.md §14).                                                                                                                         |
| `clients.py`            | The client and competitor registers: organisation-name matching against the Excel lists.                                                                                                                                                                                                                                                                                                                    |
| `blacklist.py`          | DNSBL check of the sending domain (Spamhaus DBL, SURBL).                                                                                                                                                                                                                                                                                                                                                    |
| `compliance.py`         | The legal footer and its version per lawful basis, in English or Italian (`language_for`: the first email's language once one was sent, else the operator's choice, else Italian for an address or a company in `ITALIAN_COUNTRIES`), the required template placeholders, the public-`BASE_URL` and opt-out-route checks, the country lock, the source channels and the basis each gives, the first-email deadline, the masking of log lines and AI prompts, and what an erasure does with each column.                                                                                        |
| `company_country.py`    | The country of a contact's company: its website's domain, an Italian VAT number or legal form on its own page, or the AI (`COMPANY_COUNTRY_AI`); written at collection, before the first email, and by `main.py company-country`. |
| `reply_intent.py`       | Deterministic multilingual reply-interest classifier.                                                                                                                                                                                                                                                                                                                                                       |
| `retention.py`          | Daily GDPR retention erasure (also run without the agent by `main.py retention`), committed per address so the dashboard is never locked out.                                                                                                                                                                                                                                                               |
| `export.py`             | The GDPR access answer (`records_held_about`; `write_access_answer` writes `records.json` and a cover letter) and erasure.                                                                                                                                                                                                                                                                                  |
| `contact_explainer.py`  | Plain-language "why was or wasn't this contact emailed", served at `GET /api/contacts/<id>/why`.                                                                                                                                                                                                                                                                                                            |
| `clock.py`              | The app clock — one timezone (Europe/Rome) for daily caps and timestamps.                                                                                                                                                                                                                                                                                                                                   |
| `file_handler.py`       | Loads templates, the PDF, ensures directories.                                                                                                                                                                                                                                                                                                                                                              |

---

## 5. The AI layer (the three cognitive roles)

- **Strategist** — `suggest_discovery_queries`: proposes the next European search
  queries, informed by the learning loop's "what's working" hint.
- **Copywriter** — `generate_email_body`: writes a short (≤~3 sentence), natural
  email body per contact; **guardrailed** (no invented facts, no greeting/sign-off,
  strips stray ones). Used only in a template that asks for `{{body}}`: the
  shipped first email is SUPPLECO's fixed text, so it is not called for it.
- **Analyst** — `classify_relevance`: YES/NO on whether a scraped company is a
  plausible _client_. It answers NO only when the page clearly shows a
  company based outside Europe (Europe being the whole continent, the UK,
  Switzerland and Norway included), a contract or private-label manufacturer
  (a competitor: it offers to make FOR other brands), or media, job boards,
  directories, schools, hospitals and industries with nothing to do with
  supplements, health foods or wellness products;
  otherwise YES, a small brand that makes its own products in its own lab,
  kitchen, farm or workshop, a shop or pharmacy, and a company whose page
  does not say where it is based included. It is judged by what the company
  does, not by "Lab" or "Pharma" in its name, and making its own products
  does not make a company a contract manufacturer. It leans to YES because a
  NO removes the company for good, while a YES leaves it where it would be
  without the AI. `classify_company_country` answers an ISO code, or
  UNKNOWN unless the page shows where the company itself is based; the
  page's language, the countries it sells to and where its ingredients come
  from do not count.

**Local-only & fail-safe.** The only AI is an OpenAI-compatible server on
SUPPLECO's own machine or private network (`LLM_PROVIDER=openai_compatible`,
`LLM_BASE_URL`, `LLM_MODEL`; for example Ollama). No cloud AI service is
supported, and every address the server's host resolves to must be loopback
or private, checked at start-up and again before each call. A leading
`<think>` block from a reasoning model is dropped before the answer is read
(before, the reasoning could become the opening line), and an answer whose
content is not text counts as no answer. A generated opening line
or body that still holds think markup after that is discarded. Rate-limit (429) → honors `Retry-After`, else backs
off, else **falls back**: the email uses a hand-written template body, discovery
uses the free heuristic generator, relevance falls back to the deterministic
filter. **The AI never blocks or crashes the pipeline.**

**Off until switched on.** Each AI use that sees a recipient's data has its own
switch, off by default: the opening line (`AI_PERSONALIZATION_ENABLED`), the
body (`AI_HUMANIZE_EMAILS`), the relevance check (`AI_RELEVANCE_ENABLED`) and
the reply tie-break (`AI_REPLY_CLASSIFICATION_ENABLED`). Those calls are also
skipped while `AI_DPA_REFERENCE`, where the provider's processing agreement is
filed, is blank (L2.4), and no prompt carries an address, a phone number or a
URL's query string. The Strategist (`AI_STRATEGIST_ENABLED`) and AI company
discovery choose search words and see no recipient's data.

---

## 6. Deliverability — how the inbox is protected

(Full detail + the open infrastructure items in `DELIVERABILITY.md`.)

Enforced in code: **warm-up ramp**, **business-hours send window** (Italy),
**per-domain daily cap**, **bounce/spam circuit-breaker** (auto-pause), **MX
verification**, **pooled SMTP**, a **`List-Unsubscribe` header on every email**
(plus RFC 8058 one-click when `BASE_URL` is public HTTPS), and a live
**Deliverability panel** (bounce rate vs the 2% limit, SPF/DMARC, breaker
state). The **`mailto:` opt-out works only with `IMAP_ENABLED=true` while the
agent runs**, since only the IMAP monitor reads it; while neither route is live
(no public `https://` `BASE_URL` and IMAP off), `doctor` fails and live sending
is refused. Also in place: an **advisory spam-word linter** (`doctor` runs
it; it warns, never blocks), and subjects carrying the recipient's company
wherever the subject contains `{org}` (rotated only with
`AB_TESTING_ENABLED=true` and several `EMAIL_SUBJECTS`).

Also enforced: **blacklist monitoring** (Spamhaus DBL / SURBL on the sending
domain). The dashboard's **Spam / inbox test** card, meant to send the real
email to a mail-tester.com address, is disabled until counsel's country table:
the country lock refuses that address, whose domain shows no country, and a
live Direct Send needs a channel. Meanwhile, a test message sent from the
sending mailbox to mail-tester.com scores the domain's SPF, DKIM, DMARC and
blocklist status, though not the agent's own headers and footer.

These apply to first emails, follow-ups, retries and live Direct Sends alike:
the bounce and blocklist breakers, warm-up, the per-domain cap and bounce
backoff, suppression, company blocks and the closed-contact check, the
address/client/competitor screens, the country lock, and the legal-identity,
opt-out-route and client-list blocks. Direct Send differs on purpose: the send
window is only advisory, it skips lead scoring, relevance and MX verification,
it uses the primary mailbox, it takes exactly one address, and a live send
needs the channel the contact came through. Its preview (dry run) skips the
reputation and pacing guards and adds no contact for an address not yet held;
with `DRY_RUN=true` every Direct Send is a preview.

**Sending identity (open):** mail goes out as SUPPLECO from a company mailbox;
which mailbox is IT's decision. A Microsoft 365 mailbox signs in with a
certificate, not a password (`MICROSOFT_*` settings, DEPLOY section 12):
Exchange Online refuses password logins over IMAP, and switches password
SMTP AUTH off by default at the end of December 2026. A mailbox at another
provider signs in with its password. When the review checked, suppleco.com's
mail was delivered to internetservizi.it, not to Exchange Online (F-20), so
IT must confirm where replies arrive. With `IMAP_ENABLED=true`,
live sending stops while an inbox the agent reads, or its junk folder, has had
no complete read within three check intervals, as when its login fails. suppleco.com also had no DKIM
and no DMARC record when the review checked (F-21).

**Scaling volume:** extra mailboxes can be added (`SENDER_2_EMAIL`…). The agent
rotates through them and **each warms up independently**, so the daily total is
the sum of the per-mailbox caps, up to the pacing ceiling: one loop sends from
all mailboxes at most `EMAILS_PER_HOUR` an hour (default 30, about 330 a day in
the default 8-19 window).

---

## 7. Compliance (GDPR / ePrivacy)

(The controls in full: `COMPLIANCE.md`. The legal questions, with the
developer's proposed answers, are in `docs/privacy/decisions_for_counsel.md`.)

Whether SUPPLECO may email a company without its prior consent depends on the
recipient's country, and that is counsel's decision (L1.1). The code assumes no
answer: the **country lock** sends live email only to the countries in
`ENABLED_COUNTRIES`, blank by default, so nothing goes out until counsel signs
the country table, and addresses on `.com` and other generic domains count as
needing consent. Controls: business addresses only, the full legal identity and
a notice worded for the contact's recorded lawful basis in every email, easy
one-step unsubscribe, **suppression on objection** - immediate through the web
link (needs a public `BASE_URL`), or at the next inbox check (every 30 minutes
by default, with `IMAP_ENABLED=true` and a running agent) for an emailed
opt-out; objectors are removed from sends _and_ running sequences, an opt-out
survives the dashboard's Unblock, and an opt-out by the one-click button, or
by email from someone the agent emailed, blocks the whole company - bounce
suppression, a register of data-subject requests with a one-month deadline
(`main.py dsr`), and `gdpr-export` / `gdpr-erase` for right of access /
erasure, an erasure keeping only a keyed token. Until counsel answers, the
counsel sheet proposes that the agent is not run at all (§9).

---

## 8. The dashboard

Bound to `127.0.0.1:5000` by default (`DASHBOARD_HOST` / `DASHBOARD_PORT`; the
Docker image binds `0.0.0.0`). Basic Auth always applies: the dashboard
refuses to start without a `DASHBOARD_PASSWORD` of at least 12 characters,
wherever it listens, and never runs Flask's debugger. Wrong passwords, write
origins, body size and field types are checked on every request, and the page
runs under a strict Content-Security-Policy (DEPLOY section 3a). SUPPLECO-branded, responsive (desktop → phone). Tabs: **Overview** (stats +
charts), **Insights** (incl. the **Deliverability & Reputation** panel, A/B,
sequences, per-domain sends, the errors of the last 14 days), **Contacts** (each row expands to explain
why the contact was or wasn't emailed, with its history), **Campaigns**,
**Direct Send** (email one chosen business address on demand, with a dry-run
preview; a live send needs the channel the contact came through; personal and
PEC addresses, opted-out or otherwise closed contacts, clients and listed
competitors are refused, as is any send while a guard has paused sending or a
cap is reached), **Template Editor** (live preview of the English first
email; a save is sent from the next email, without a restart), **Search
Logs**. Start runs the agent's continuous loop inside the dashboard's process,
as a dry run while `DRY_RUN=true`, and Stop ends it; the query box seeds
targeted searches. The settings panel's values, and their resets, reach the
running agent, single runs and Direct Send. The funnel and the email charts
count only mail that went out, never a dry run. The page refreshes its
figures every 5 seconds while the agent runs (every 15 when idle); the charts,
the warm-up figure and the Ready to send card are asked for when the page
opens and then once a minute. **Send** on a contact a filter
discarded (low score, off-target, unverified) only re-queues it, and says so:
the filters run again before anything is sent. The Spam / inbox test card is
disabled (§6). Without a good `DASHBOARD_PASSWORD`, `doctor` fails.

---

## 9. Running & operating it

```powershell
py -3.11 main.py doctor                 # preflight checks: each [FAIL] blocks live sending
py -3.11 main.py ai-benchmark           # score the AI model on a fixed exam (DEPLOY.md §11)
py -3.11 main.py ai-proof               # the whole agent with the AI on invented cases; sends nothing
py -3.11 main.py dashboard              # the web dashboard; its Start runs the agent
py -3.11 main.py run                    # one discovery/send pass from the CLI
py -3.11 main.py run --continuous       # the continuous loop from the CLI
py -3.11 main.py run --dry-run          # a pass that emails no prospect (see Not yet, below)
py -3.11 main.py status                 # database counts, and the files in place
py -3.11 main.py add-query "..."        # queue a search for the next run
py -3.11 main.py gdpr-export <email>    # write the access answer for an address
py -3.11 main.py gdpr-erase <email>     # erase an address from the database
py -3.11 main.py dsr list               # requests: list, show, add, export, close, erase
py -3.11 main.py suppress-domain list   # company blocks: list, or lift <domain> --reason
py -3.11 main.py retention              # apply the retention periods now (--dry-run counts)
py -3.11 main.py company-country        # company countries of stored contacts (--ai, --dry-run)
py -3.11 main.py backup                 # a checked backup of the database
py -3.11 main.py replay-journal         # after a restore: re-apply the journal (nothing starts until it has run)
py -3.11 main.py restore-check          # rehearse a restore on a scratch copy
py -3.11 main.py restore-drill          # prove a backup restores with nothing lost
py -3.11 main.py lists check            # the lists: encrypted and readable? (encrypt, decrypt)
py -3.11 main.py security-check         # known vulnerabilities in the installed packages; installs nothing
py -3.11 -m pytest                      # the test suite (needs requirements-dev.txt)
```

`--help` after any command shows its options; `DEPLOY.md` covers installing,
scheduling and restoring.

**Not yet.** Until counsel signs the country table (L1.1) and answers L1.13, the
counsel sheet proposes, as an operating rule, that the agent is not run (L1.9):
not `run`, not `run --dry-run`, and not the dashboard's Start. A dry run is not
a harmless preview: it searches the web and stores every contact it finds,
whatever its country, and with `IMAP_ENABLED=true` it reads the mailbox, acts
on the opt-outs and requests in it, and sends lead alerts; only sending and
retention are skipped. No other command searches the web, and the dashboard
does not either until Start is pressed.

Key `.env` knobs (grouped; `SETTINGS.md` lists every setting): **AI**
(`LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `AI_*_ENABLED`, `AI_HUMANIZE_EMAILS`,
`AI_DPA_REFERENCE`) · **Sending** (`SMTP_*`, `SENDER_*` with `SENDER_TITLE` and
`SENDER_SIGNATURE`, `DELAY_BETWEEN_EMAILS`,
`SEND_JITTER_SECONDS`) · **Warm-up** (`WARMUP_*`) · **Guards** (`SEND_WINDOW_*`,
`BOUNCE_GUARD_*`, `MAX_BOUNCE_RATE`, `MAX_PER_DOMAIN_PER_DAY`) · **Quality**
(`RELEVANCE_*`, `MIN_LEAD_SCORE_TO_EMAIL`, `VERIFY_*`) · **Profile**
(`ATTACH_PROFILE`, `PROFILE_URL`, `PDF_PATH`; the PDF goes with every first
email) · **Language** (`ITALIAN_COUNTRIES`, `EMAIL_SUBJECT_IT`) ·
**Timezone** (`APP_TIMEZONE`) ·
**Dashboard** (`DASHBOARD_USERNAME/PASSWORD`) · **Legal** (`LEGAL_ENTITY_NAME`,
`LEGAL_REGISTERED_OFFICE`, `LEGAL_COMPANY_REGISTRATION`, `PRIVACY_NOTICE_URL`,
`PRIVACY_CONTACT_EMAIL`, and for an S.r.l. or S.p.A. `LEGAL_SHARE_CAPITAL` and
`LEGAL_SINGLE_MEMBER`; live sends are refused while any is blank) ·
**Countries** (`ENABLED_COUNTRIES`, `UNKNOWN_COUNTRY_ALLOWED`; no live email
while the first is blank) · **Erasure** (`ERASURE_KEY`: every command but
`doctor` refuses to start without it; back it up and never change it) ·
**Unsubscribe** (`UNSUBSCRIBE_SECRET`, which must be changed or the app will
not start; `BASE_URL`) · **Inbox** (`IMAP_*`). Sending also includes
`EMAILS_PER_HOUR`. Secrets live only in `.env` (which is git-ignored).

---

## 10. Q&A appendix — likely questions, strong answers

**"What are you sending from?"**
A company mailbox, as SUPPLECO; which one, and how the agent signs in to it, is
IT's open decision (§6). A Microsoft 365 mailbox signs in with a certificate,
never a password (`MICROSOFT_*` settings, DEPLOY section 12): Exchange Online
refuses password logins for IMAP (F-17) and switches them off by default for
SMTP at the end of December 2026 (F-18). A mailbox at another provider signs
in with its password. When the review checked, suppleco.com's mail was
delivered to internetservizi.it, not Exchange Online (F-20). The dashboard
checks and displays SPF/DMARC live, and monitors the domain against Spamhaus
DBL / SURBL.

**"How do you know your email isn't going to spam?"**
You can't see inside someone else's spam folder, so we test rather than guess.
The dashboard's **Spam / inbox test** card is disabled until counsel's country
table, so for now a test message goes from the sending mailbox to a
mail-tester.com address, which scores the domain's SPF, DKIM, DMARC and
blocklist status, though not the agent's own headers and footer.
Continuously, the agent tracks its own bounce rate against the 2% provider limit
and watches for blocklist listings. For the recipient-side spam-complaint rate
(only Gmail can measure it), enrol the domain in Google Postmaster Tools.

**"Can it send more than ~100 emails a day?"**
Yes — add mailboxes (`SENDER_2_EMAIL`…) and the agent rotates across them, each
with its own warm-up ramp, so capacity is the sum of the per-mailbox caps, up
to the pacing ceiling (`EMAILS_PER_HOUR` times the send-window hours; about 330
a day at the defaults). The deliberate constraint is that a _new_ mailbox
still ramps from 5/day, adding 3 per sending day, and reaches 100/day on its
33rd sending day; that gradual build is what keeps you out of spam.

**"What happens when someone actually replies interested?"**
The agent classifies the reply's intent (multilingual, and it ignores our own
quoted email so it can't be fooled by its own words) and, if the prospect is
interested, emails whoever is set in `LEAD_ALERT_EMAIL` with the company, the
contact and their exact reply — so a human takes over. Follow-ups to that
contact stop automatically, and each lead is only ever escalated once.

**"Cold email in the EU — is this even legal?"**
It depends on the recipient's country, and on whether the address is generic or
names a person, and that is counsel's decision (L1.1 in
`docs/privacy/decisions_for_counsel.md`). The review's research, for counsel to
confirm, reads Italy, Germany, Austria and Spain, among others, as needing prior
consent for both kinds of address, and only France and the United Kingdom as
opt-out for both. The product does not assume an answer: until counsel signs the
table, `ENABLED_COUNTRIES` stays blank and no live email goes out, and addresses
on `.com` and other generic domains count as needing consent. Every email
carries the full legal identity, a notice worded for its lawful basis and an
opt-out, and objections are suppressed as soon as they are received: at once
through the web link, or at the next inbox check (every 30 minutes by default)
for emailed opt-outs. Consumer mailboxes and PEC addresses are never emailed.

**"What happens when the AI/API is down or rate-limited?"**
Nothing breaks. The shipped emails use no AI at all: the first email is
SUPPLECO's fixed text. Every AI call has a deterministic fallback — hand-written
email body, free heuristic discovery, rule-based relevance. It honors `Retry-After` and
degrades gracefully. AI is a quality booster, never a dependency.

**"How do you keep from spamming / getting blocklisted?"**
Layered: warm-up ramp, business-hours-only sends, per-domain daily cap, paced
sends (`EMAILS_PER_HOUR`; random jitter only if `SEND_JITTER_SECONDS` > 0), MX
verification, an advisory spam-word check in `doctor`, and two breakers that
pause all sending: one for the bounce rate, one for a blocklist listing. Live bounce rate is on the
dashboard against the 2% provider limit.

**"How do you avoid emailing garbage / wrong companies?"**
Gates before a campaign send, in order: lead score ≥ `MIN_LEAD_SCORE_TO_EMAIL`;
not an active client; not a named competitor (when a competitor list is
configured); European, on-vertical and not a self-described contract
manufacturer (with `RELEVANCE_FILTER_ENABLED=true`); an AI target check (with
`AI_RELEVANCE_ENABLED=true`, fails open); MX-verified. Each rejection is stored
on the contact as its reason, and the dashboard shows it. Direct Send skips
scoring, relevance and MX. The scraper also ignores scripts/styles and drops
generic (no-reply, support…), personal and PEC addresses; placeholder addresses
are rejected by the relevance filter when it is enabled. At the send check, the
country lock then refuses anyone outside the enabled countries.

**"Why SQLite? Won't it fall over?"**
Low traffic, thousands of rows, a few concurrent writers (the agent thread plus
dashboard requests): SQLite in WAL with a 5 s busy timeout is the right tool,
with zero ops overhead. Moving to Postgres would mean porting the
SQLite-specific SQL that several modules run directly on the connection.

**"Is it multi-threaded / safe?"**
The agent runs on a background thread; the dashboard serves requests on others.
Each `Database` method opens its own connection per transaction (WAL, foreign
keys, 5 s busy timeout); the sequence, retention, export, IMAP and
contact-explainer code shares one thread-local connection per thread, which the
dashboard closes after every request. The retention job commits per erased
address so it never locks the dashboard out. The SMTP connection is
lock-guarded so a campaign and a manual send never collide. Stop is responsive
(checked between every contact). A send is claimed by a `preparing` row
before it goes, so two processes cannot send the same email, and a send cut
off after the server may have accepted it is never repeated.

**"How does it handle bounces and replies?"**
An IMAP monitor reads the inbox and the Junk Email folder, before the first
send of every pass, and classifies each message. The sender is read from the
From header in any form; a message whose sender is not one address is still
recorded and flagged for a person to review (`SENDER_UNPARSED`). A read looks
back at most 14 days, and an outage longer than that is reported with the
dates to check by hand (`IMAP_GAP`). Opt-outs are read in Italian too
("disiscrivimi", "toglietemi dalla lista", a reply that is only "stop"). Bounces: it parses the true failed recipient from the DSN
(`Final-Recipient`) — not the mailer-daemon or our own address — and suppresses
it. Replies mark the contact "responded" and stop their follow-up sequence. A
data request ("delete my data", "where did you get my address?") is registered
with its one-month deadline and stops all email to the person, and to their
company if the agent had emailed them; a plain "not interested" from someone
the agent emailed is recorded as an objection. Out-of-office auto-replies are
detected and ignored, so a vacation responder doesn't look like real
engagement.

**"What personal data do you store, and can you delete it?"**
Business contact data (name/role/org/email, the page it was found on and how),
the emails sent and the lawful basis each relied on, the text of an interested
reply, the register of data-subject requests, and activity-log lines that name
the address. Retention erases a contact never emailed 30 days after collection,
one emailed who never replied 365 days after the last email, one who replied or
became a lead 730 days after the last exchange, and one who objected at the next
daily run. `gdpr-erase <email>` (or `dsr erase <id>`) anonymises what the
database holds on an individual, keeps the address on the suppression list only
as a keyed token, so they are never contacted again, and blocks their company;
`gdpr-export <email>` (or `dsr export <id>`) writes the records the database
holds about them, with a cover letter (Art. 15 / 17; `COMPLIANCE.md` section 7
says what each leaves out).

**"How is it tested?"**
More than 1,700 automated tests (`pytest`), plus more than 40 browser tests of
the dashboard (`pytest -m ui`), across extraction, relevance, scoring, the
send pipeline, sequences, bounce parsing, timezone, the deliverability guards and
the legal footer, erasure and retention. `scripts/verify_regression_tests.py`
checks, for the fixes it lists, that each fix's tests fail on the code before
it, and `scripts/mutation_audit.py` breaks the key guards on purpose and checks
that a test fails each time; a test that already fails on the unbroken code is
reported as BASELINE-FAIL, not as a catch. Name what to run: a group, a
mutation id or a file (`py -3.11 scripts/mutation_audit.py main.py SEC30`;
with no name it runs every mutation, for hours). `--workers 4` runs them in
four copies of the project side by side, with the same report. A name that
matches nothing, or an option it does not know, stops the run; `--help`
prints the usage. A fast test in the normal suite
(`tests/test_every_mutation_points_at_real_code.py`) checks that every
mutation's search text is found exactly once and that a mutation naming a
browser test also runs the browser tests.

**"What would you do next?"**
IT's choice of mailbox and, for Microsoft 365, the certificate sign-in set
up in SUPPLECO's Entra (W1; DEPLOY section 12); DKIM and DMARC on the sending domain (F-21); Google Postmaster
Tools for reputation; public HTTPS hosting for one-click unsubscribe; and, once
counsel answers, the full country table (W2) in place of the interim country
lock.
