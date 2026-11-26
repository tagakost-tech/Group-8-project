# Omega Outreach Agent

[![CI](https://github.com/tagakost-tech/Group-8-project/actions/workflows/ci.yml/badge.svg)](https://github.com/tagakost-tech/Group-8-project/actions/workflows/ci.yml)

**Group 8 · Esade, *Disrupting B-Corp* · client: Omega IFS**

Omega IFS is a family-owned SME in Cesano Maderno (Italy). It cold-forms special
fasteners and precision parts to customer drawings, for car makers' suppliers,
window and furniture hardware, electrical equipment and household appliances.
It sells through a few people and personal relationships, so finding and
opening new accounts is slow.

The Omega Outreach Agent finds European manufacturers that buy parts like
Omega's, explains why each one fits, drafts the first email in Omega's own
words, and follows up. It is built on one rule: **AI proposes, a named person
decides.** Nothing is sent until a person approves it, and what is sent is
exactly the text that was approved, stored with the reviewer's name.

## What it does

| Screen | What a person does there |
|---|---|
| **Today** | Sees what waits for them, and the funnel from first email to order: companies found, emailed, replied, interested, meeting booked, order won. |
| **Review** | Reads, edits, approves or rejects every proposed email: first emails, follow-ups and answers to replies. A rejection needs a reason, and the agent learns from it. Each email shows why the company was chosen, in its own words. The outcome with the company is recorded here. |
| **Leads** | Every company with its fit score (market, need, certifications, distance) and outcome. **Import** a CSV (a trade-fair list, a directory, referrals) with a preview before anything is stored; **export** approved leads in a HubSpot-ready CSV. |
| **Markets** | The sectors Omega serves today and new ones it could enter, with the evidence for each. |
| **Setup** | "Connect Omega": every step left before the first live email (sending mailbox, SPF/DKIM/DMARC, legal details, opt-out, customer list, countries, inbox, AI writer), each with what to do and a live check. |

People sign in on a sign-in page (an eight-hour session, sign-out, lock-out
after repeated wrong passwords); changing a password signs that account out
everywhere. The full operator dashboard (settings, templates, deliverability, logs, data
requests) is at **`/classic`**.

### Compared with commercial outreach tools

| Table stakes in Apollo, Lemlist, Instantly, Outreach | Here |
|---|---|
| Lead sourcing and import | Web search for manufacturers by sector and country; CSV import with column matching, de-duplication and do-not-contact screening |
| Personalisation | Drafts from the company's own website text, Italian or English; an optional AI writer on any OpenAI-compatible endpoint, with the hand-written template as a fallback |
| Sequences | First email plus follow-ups, stopped automatically on a reply, bounce or opt-out |
| Deliverability | Warm-up ramp (5 to 100 a day), 3 per domain per day, office hours only, a bounce circuit breaker, SPF/DKIM/DMARC checks, blocklist check |
| Compliance | One-click unsubscribe (`List-Unsubscribe-Post`), suppression lists, a lawful basis recorded per lead, GDPR access and erasure, PII-masked logs |
| Analytics | Funnel to meeting and order, reply rate, A/B tests, per-sector results |
| CRM | HubSpot-ready CSV export |
| **Not in other tools** | **Every email approved by a named person, checked by hash before sending; rejections teach the agent which companies to skip** |

## First run

The app starts empty: no companies, no partners, no invented data. Configure
`.env` (see `.env.example` and `docs/DEPLOY.md`), start it, and sign in as the
administrator. **Setup** lists every step left before the first live email.
In **Network**, set Omega's own weekly capacity on its card (**Set capacity**)
and admit each certified partner (**Add a partner**) with its own login.

Keyboard: `1`–`6` switch screens, `J`/`K` move through a list, `Ctrl K`
searches, `Ctrl Enter` approves the open email.

## Network

Omega orchestrates a network of specialist SMEs. Omega's staff send a job to a
member, either spare capacity ("form 800 of these 1,000 bolts") or an operation
Omega does not do (turning, heat treatment, plating). **Split an order** does it
in one go: Omega keeps part of its own operation, the rest goes to partners
with free capacity, and other operations go to the members that do them; the
shares must add up to the order.

Like an email, a job is sent only when a named person at Omega approves it.
Every job starts as a draft the recipient cannot see; **Approve and send**
records who approved it and when, **Reject** drops it unsent. The member then
accepts or declines it, starts it and marks it done; accepting books the
quantity against the member's weekly capacity, and declining, cancelling or
finishing releases it. Each job has its own message thread between the two
companies. The customer deals only with Omega: a job names the part, never the
customer. The Jobs tab is the log of every routed job, its order and approver.

A member signs in with its own login and sees only the network: its own jobs,
the member list and the matching. Omega's leads, contacts, reviews and settings
answer 403. The administrator admits a partner (**Add a partner**, or
`POST /api/network/members`) and sets each card's weekly capacity; a partner
can change only its own capacity.

## Run it in production

Production is a single small server (a VM or an on-premise box) running the
Docker image, with the database on a persistent volume:

```powershell
copy .env.example .env      # then fill it in; see docs/SETTINGS.md
docker compose up -d
docker compose exec outreach python main.py doctor
```

- `doctor` lists every `[FAIL]` that blocks live sending: legal identity,
  sending mailbox, opt-out route, client list, enabled countries. No live email
  goes out until there are none.
- `GET /healthz` answers without a login, for the host's health check.
- The container runs as a non-root user. `main.py backup` makes a checked copy
  of the database. HTTPS responses carry HSTS.
- Two roles: the **administrator** starts and stops the agent, sends, imports
  and changes settings. **Reviewers** (named logins) decide on proposals and
  record outcomes.

`docs/DEPLOY.md` covers installing, the public address for opt-out links,
Microsoft 365 sign-in, backups and security updates.

## Where things are

| Path | What |
|---|---|
| `client/omega.json` | Omega's own words: value proposition, buyer keywords, sectors, best customers. |
| `modules/review_queue.py` | The human-in-the-loop gate for every email. |
| `modules/dispatch_pipeline.py`, `modules/sequences.py` | Drafting, sending and follow-ups. |
| `modules/fit_score.py`, `modules/lookalike.py` | Fit score and likeness to Omega's best customers. |
| `modules/outcomes.py` | Outcomes per company and the funnel. |
| `modules/lead_import.py` | CSV lead import. |
| `modules/compliance.py`, `modules/export.py` | Lawful basis, personal-data register, access and erasure. |
| `templates/app.html`, `static/app/` | The app (`/`). |
| `templates/dashboard.html`, `static/js/` | The operator dashboard (`/classic`). |
| `main.py` | The web app and the command line (`py -3.15 main.py --help`). |
| `.github/workflows/ci.yml` | Lint, tests on Python 3.15, browser tests. |

## Tests

```powershell
py -3.15 -m pytest -n 6 -o addopts="" -m "not ui"   # about 5 minutes
py -3.15 -m pytest -o addopts="" -m ui tests/ui      # browser tests (Playwright, Chromium)
ruff check .
```

GitHub Actions runs all three on every push. The older tests run on a fixed
test client in `tests/fixtures`, so they check the machinery and not Omega's
wording. The tests for this app's screens are `tests/test_outreach_shell.py`
and `tests/ui/test_app_shell.py`.

## Limits

- **This is a prototype for a course**, handed over to Omega IFS.
- **The lawful-basis rules** in `modules/compliance.py` are the developers'
  proposals and need a lawyer's review before live use.
- **The older tests** run against a fixed stand-in client ("Suppleco") so they
  check the machinery, not Omega's wording; Omega's own words live only in
  `client/omega.json` and `templates/`.
