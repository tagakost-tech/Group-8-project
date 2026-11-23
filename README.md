# SUPPLECO Outreach Agent

An autonomous B2B outreach agent for **SUPPLECO**, an Italian contract manufacturer
of food supplements & nutraceuticals. It discovers European supplement brands,
verifies and scores them, sends SUPPLECO's own first email in Italian or English
with its Company Profile PDF, within reputation-safe limits, runs follow-ups, and learns from what works — all from one
dashboard. Mail is sent as **SUPPLECO** from a company mailbox; which mailbox is
still IT's choice, and a Microsoft 365 mailbox signs in with a certificate, not
a password (see the sending mailbox below).
Its searches start from SUPPLECO's own buyer keywords, and wherever it describes
SUPPLECO (AI prompts, follow-ups) it uses SUPPLECO's own value proposition, kept
verbatim in `modules/suppleco_profile.py`.

**Key capabilities:** autonomous discovery → qualification (European + vertical
filters, MX verification, lead scoring) → SUPPLECO's first email, in Italian
for Italian recipients and English for the rest →
reputation-safe sending (warm-up, business-hours window, per-domain caps,
bounce circuit-breaker, blacklist monitoring) → timed follow-ups → reply/bounce
handling → **hot-lead alerts** to a human → learning from what works. Multiple
sending mailboxes can be rotated to raise daily volume.

## Quick start

The agent runs on Python 3.11, as the Docker image does; the commands use the
Windows `py` launcher. In the project folder:

```powershell
py -3.11 -m pip install -r requirements.txt
copy .env.example .env
py -3.11 -c "import secrets; print(secrets.token_urlsafe(48))"
py -3.11 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the first output in `.env` as `UNSUBSCRIBE_SECRET` and the second as
`ERASURE_KEY`: two different values, each 64 characters long. Both must
be at least 32 characters. Every command refuses to start on the shipped
unsubscribe secret or a shorter one, every command except `doctor` refuses
to start without the erasure key, and a new database refuses a shorter
erasure key. Back the erasure key up apart from the database now, before
anything opens the database, and never change it (`docs/DEPLOY.md` section
6). Copy the template only this once: copying it over an existing `.env`
would wipe the key.

```powershell
py -3.11 main.py doctor      # lists every [FAIL] that blocks live sending
py -3.11 main.py ai-benchmark  # before switching an AI question on: docs/DEPLOY.md section 11
py -3.11 main.py ai-proof      # the whole agent with that AI on invented cases; sends nothing
py -3.11 main.py dashboard   # then open http://127.0.0.1:5000
```

With the two secrets set, the dashboard starts on this machine and dry runs
work. No live email goes out until the list below is complete.

## Before the first live email

Live sending is refused, and `doctor` shows a `[FAIL]`, until each of these
is set in `.env`. `docs/SETTINGS.md` describes every setting.

- **The legal identity** printed in every email: `LEGAL_ENTITY_NAME`,
  `LEGAL_REGISTERED_OFFICE`, `LEGAL_COMPANY_REGISTRATION`,
  `PRIVACY_NOTICE_URL` and `PRIVACY_CONTACT_EMAIL`. For an S.r.l. or an
  S.p.A., also `LEGAL_SHARE_CAPITAL` and `LEGAL_SINGLE_MEMBER` (`yes` or
  `no`), as Cod. Civ. 2250 requires (`docs/DEPLOY.md` section 5).
  `PRIVACY_NOTICE_URL` must start with `https://`, or no command starts,
  and the page it names must be published.
- **`ENABLED_COUNTRIES`**: the countries live email may go to, filled in
  only from counsel's signed country table (decision L1.1). Blank, the
  default, means no live email at all (`docs/DEPLOY.md` section 7).
- **An opt-out route**: `IMAP_ENABLED=true`, so that the agent reads the
  opt-out emails sent to the sending mailbox, or a public `https://`
  `BASE_URL`, which gives a web unsubscribe link (`docs/DEPLOY.md` section
  3). With `IMAP_ENABLED=true`, live sending also stops while an inbox
  the agent reads, or its junk folder, has had no complete read within
  three check intervals, as when its login fails.
- **The client list**, so that no existing customer is cold-emailed (see
  Client list below).
- **`SENDER_TITLE`**: what the sender is responsible for, which SUPPLECO's
  first email states. A live first email through a mailbox without one is
  refused.
- **The Company Profile PDF** at `PDF_PATH` (`assets/presentation.pdf`):
  the first email says it is attached, and while `ATTACH_PROFILE` is on, as
  it is by default, a live first email is refused without it.

The dashboard refuses to start, and `doctor` fails, without a
`DASHBOARD_PASSWORD` of at least 12 characters, wherever it listens (this
machine too). See docs/DEPLOY.md section 3a for what else it checks.

The agent fetches web pages directly, never through a proxy, and only from
public sites (never this machine or the office network), reading at most
2 MB of each, in at most 15 seconds per request; the machine needs direct outgoing web
access. When a search finds nothing, no other service is asked. An email that
SUPPLECO's mail server found possibly forged can stop only its own sender's
address, never a whole company, and cannot mark an address as bounced.
`docs/COMPLIANCE.md` section 9 lists every security measure.

Also set these:

- **The sending mailbox**: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME` and
  `SMTP_PASSWORD`; the defaults are Gmail's hosts. For Microsoft 365 they
  are `SMTP_HOST=smtp.office365.com` and `SMTP_PORT=587`, with
  `IMAP_HOST=outlook.office365.com`. A Microsoft 365 mailbox should sign in
  without a password: Exchange Online refuses password logins over IMAP,
  password SMTP AUTH is switched off by default at the end of December
  2026, and a stored
  password is what would let a thief send mail as SUPPLECO. Set
  `MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID` and
  `MICROSOFT_CERTIFICATE_PATH` and leave every password for those mailboxes
  blank; `docs/DEPLOY.md` section 12 has IT's steps. A mailbox on another
  provider still signs in with its password. Still open: when the review
  checked, suppleco.com's mail was delivered to internetservizi.it, not to
  Exchange Online (F-20). IT decides which mailbox the agent uses.
- **`SENDER_NAME` and `COMPANY_NAME`**: the defaults are placeholders
  (`Pharmaceutical Outreach`, `PharmaCorp`); the first email names the
  sender, and the follow-ups and the default signature name the company.
  `SENDER_SIGNATURE` sets the first email's signature.
- **`LEAD_ALERT_EMAIL`**: who is told when a prospect replies with interest.
- Optionally the AI: a model on SUPPLECO's own server, such as Ollama
  (`LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL`, `LLM_MODEL`;
  `docs/DEPLOY.md` section 11). It is the only AI the agent can use: no
  outside AI company receives data, `LLM_BASE_URL` must point at this
  machine or the private network, and a `.env` that still holds
  `GEMINI_API_KEY`, `GROQ_API_KEY` or `GROQ_MODEL` stops start-up until
  that line is deleted. The AI on its own switches on AI company discovery in
  continuous mode, with `TARGET_QUERIES` as its topics; it is skipped while
  `TARGET_QUERIES` is blank, the default. Each AI use that sees a
  recipient's data has its own switch as well: `AI_HUMANIZE_EMAILS` (the
  body) and `AI_PERSONALIZATION_ENABLED` (the opening line), which the
  shipped templates, SUPPLECO's fixed text, do not use;
  `AI_RELEVANCE_ENABLED` (judging a company) and
  `AI_REPLY_CLASSIFICATION_ENABLED` (unclear replies). Those calls are also
  skipped while `AI_DPA_REFERENCE`, where the provider's processing agreement
  is filed, is blank.

`docs/DEPLOY.md` section 10 is the go-live checklist: what `doctor` checks,
the steps it cannot see, and who provides each.

## Client list

While `CLIENT_FILTER_ENABLED` is on, as it is by default, live sending is
refused and `doctor` fails until SUPPLECO's list of existing customers loads
and holds at least one company it can screen (a sheet of nothing but legal
forms such as `S.r.l.` screens none), so that none of them is cold-emailed. The list holds client data, so it is
not in the repository: `*.xlsx` is git-ignored and left out of the Docker
image.

- Get the current list from SUPPLECO sales and save it as an `.xlsx`
  workbook, with one company name per row in the first column of the first
  sheet.
- Put it in `data/` and set `CLIENT_LIST_PATH=data/<file>.xlsx`. A relative
  path starts at the project folder.
- The agent re-reads the list when it changes. The dashboard's Mark as
  client adds companies to it, which fails while the file is open in Excel.
  It first copies the workbook to a dated backup beside it (the newest five
  are kept) and replaces the file only once the new one is written.
- To keep the list encrypted on disk, set `LISTS_KEY` and encrypt it with
  `py -3.11 main.py lists encrypt <file>`; then a copy of the folder without
  the key shows neither list (`docs/DEPLOY.md` section 13).

## Commands

Run each as `py -3.11 main.py <command>`; `--help` after any command shows
its options.

| Command | What it does |
| --- | --- |
| `run` | One pass: reads the inbox, searches, qualifies and emails, then sends due follow-ups and retries; retention runs once a day. `--continuous` keeps the agent running in a loop until it is stopped, `--query "..."` searches only that query, and `--loop-delay` sets the seconds between loops. `--dry-run` emails no prospect, but it still searches and stores contacts and, with `IMAP_ENABLED=true`, acts on the inbox and sends lead alerts. |
| `dashboard` | The web dashboard, at http://127.0.0.1:5000 by default, served by waitress. Its Start button runs the agent inside the dashboard's process. `--dev-server` uses Flask's development server instead, and is refused unless the dashboard is reachable from this computer only. |
| `status` | Database counts, and whether the folders, the email template, the PDF and `.env` are in place. |
| `doctor` | The preflight checks. Exits 1 while any `[FAIL]` remains; `--smtp` also tries a live SMTP login. |
| `add-query "..."` | Queues a search for the next run. |
| `gdpr-export <email>` | Writes the access answer for one address, the records the database holds about it, to a folder under `data/exports/` (`docs/COMPLIANCE.md` section 7 says what it leaves out). |
| `gdpr-erase <email>` | Erases one address from the database, keeps it suppressed as a token, and lists the places it cannot reach. |
| `dsr list`, `show`, `add`, `export`, `close`, `erase` | The register of data-subject requests (`docs/privacy/dsr_procedure.md`). |
| `suppress-domain list`, `lift` | Company-wide blocks: list them, or lift one with `--reason`. |
| `retention` | Applies the retention periods now; `--dry-run` only counts (`docs/DEPLOY.md` section 8). |
| `company-country` | Works out the company country of stored contacts not yet emailed, from their website, organisation and page description, so an Italian company on a `.com` address gets the Italian email; `--ai` also asks the AI (`COMPANY_COUNTRY_AI`), `--dry-run` only counts. |
| `backup` | Writes a checked backup of the database (`docs/DEPLOY.md` section 9). |
| `lists encrypt`, `check`, `decrypt` | The client and competitor lists encrypted with `LISTS_KEY`: encrypt a plain workbook into `<file>.enc` and check it (`--delete-plain` also overwrites and deletes the plain file), say whether each configured list is encrypted and readable, or write a plain copy to the file `--out` names (`docs/DEPLOY.md` section 13). |
| `replay-journal` | After a restore: re-applies what the journal recorded since the backup, and puts back every email sent since, so that none is sent twice (`docs/DEPLOY.md` section 9). |
| `restore-check` | Rehearses a restore of the newest backup on a scratch copy (`docs/DEPLOY.md` section 9). |
| `security-check` | Asks pip-audit about the packages installed for the agent's Python and records the answer in the activity log: an error, on the dashboard, when a package the agent uses has a known vulnerability, with the version that fixes it. Installs and upgrades nothing; IT runs it weekly (`docs/DEPLOY.md` section 14). |
| `ai-benchmark` | Scores the AI model on SUPPLECO's own server on a fixed exam of the agent's AI questions, before an AI question is switched on (`docs/DEPLOY.md` section 11). Sends no email. |
| `ai-proof` | Runs the whole agent with the AI on invented cases, in a database of its own that is removed afterwards; sends nothing (`docs/DEPLOY.md` section 11). |
| `handover-restore <archive>` | On SUPPLECO's side: checks the owner's encrypted handover archive (asks its passphrase), unpacks it into a new folder, puts `ERASURE_KEY` and `LISTS_KEY` in a file only this account can read, then runs `doctor` there. Refuses a folder that already holds a `.env` or a database (`docs/DEPLOY.md` section 15). |
| `restore-drill` | Takes a fresh backup (or `--newest`, or `--backup <file>`), restores it in a temporary folder, replays the journal, and fails unless every opt-out, company block, address block, bounce, erasure, held opt-out and email sent is kept (counted by kind) and no one erased is back. Changes nothing live but one activity-log line; IT runs it every three months (`docs/DEPLOY.md` section 9). |

Tests: `py -3.11 -m pip install -r requirements-dev.txt`, then
`py -3.11 -m pytest`. The browser tests run with `py -3.11 -m pytest -m ui`,
after `py -3.11 -m playwright install chromium` has downloaded the browser.

## Project layout

| Path                     | What it is                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `agent.py`               | `OutreachAgent`: a facade that builds the workers and delegates to four stage classes in `modules/` - the loop (`loop_orchestration`), every send path through one send check (`dispatch_pipeline`), the backlog, follow-ups and retries (`backlog_draining`), and the client/competitor lists (`exclusion_manager`)                                                                               |
| `main.py`                | CLI entry point + Flask dashboard and JSON API                                                                                                                                                                                                                                                                                                                                                     |
| `config.py`              | Settings (pydantic), loaded from `.env`                                                                                                                                                                                                                                                                                                                                                            |
| `modules/`               | The four pipeline stages, and the workers: search, query generator, relevance, verification, lead scoring, client/competitor registers, AI generator, email sender, legal footer, unsubscribe, sequences, IMAP monitor, reply intent, warm-up, domain throttle, blocklist, deliverability linter, retry queue, retention, learning, analytics, GDPR export, the contact explainer, clock, database |
| `templates/` · `static/` | Dashboard UI and the email templates                                                                                                                                                                                                                                                                                                                                                               |
| `assets/`                | The company-profile PDF. It is git-ignored: add it yourself as `presentation.pdf`, or point `PDF_PATH` at it. The logos are in `static/img/`.                                                                                                                                                                                                                                                      |
| `tests/`                 | Automated test suite (pytest)                                                                                                                                                                                                                                                                                                                                                                      |
| `docs/`                  | Architecture, deliverability, compliance, deployment, and the full study guide                                                                                                                                                                                                                                                                                                                     |

## Documentation

- **[`docs/USER_MANUAL.md`](docs/USER_MANUAL.md)** — for the people who use
  the agent day to day: the dashboard, the daily and weekly routine, the
  lists, replies and opt-outs, and what each warning means
- **`docs/SETTINGS.md`** — every setting, with its default and what it does
- **`docs/DEPLOY.md`** — installing and running, scheduled retention and
  backups, restoring, the restore drill, the go-live checklist, and handing
  the agent and its data to SUPPLECO (section 15)
- **[`docs/INCIDENT_PLAYBOOK.md`](docs/INCIDENT_PLAYBOOK.md)** — what to do,
  and who does it, when a mailbox is taken over, the wrong people are
  emailed, data leaks or a laptop is lost, or a restore is needed
- **`docs/AGENT_GUIDE.md`** — full study guide: how everything works, module by module, plus a Q&A appendix
- **`docs/ARCHITECTURE.md`** — design and data flow
- **`docs/DELIVERABILITY.md`** — sending strategy and reputation safeguards
- **`docs/COMPLIANCE.md`** — what every email carries and the GDPR controls
  the code enforces, with the legal questions left to counsel
- **`docs/privacy/`** — the GDPR working documents, drafts for SUPPLECO's counsel
- **`docs/review/`** — the developer's review notes; in the owner's copy
  only, never in the copy handed to SUPPLECO
- **`docs/AUDIT_REPORT.md`** — architecture audit written for the split of
  `agent.py` into stages, corrected against the code on 2026-10-09

## Configuration

Behavior is configured in `.env`, and `docs/SETTINGS.md` lists every setting
with its default; a setting `.env` does not mention takes that default.
`.env` holds live secrets, `ERASURE_KEY` among them, and is git-ignored:
never commit or share it, and never overwrite it with the template.

The dashboard's settings panel can change the operational dials, those with
_yes_ in the Dashboard column of `docs/SETTINGS.md` (pacing, thresholds, the
send window), while the agent runs. Its values are saved in the database
and override `.env` for every run, a single run included, until they are
reset; a reset reaches a running agent. `DRY_RUN=true` makes every run a
simulation, the dashboard's Start Agent and Direct Send included.

The agent runs on Italy time (`APP_TIMEZONE=Europe/Rome`). Discovery is
biased toward Europe, and the relevance filter, off by default
(`RELEVANCE_FILTER_ENABLED`), drops non-European contacts once they are
found. Whatever the filter does, live email goes only to the countries in
`ENABLED_COUNTRIES` (`docs/DEPLOY.md` section 7).
