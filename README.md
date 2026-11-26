# Omega IFS: one entry point, a whole network behind it

[![CI](https://github.com/tagakost-tech/Group-8-project/actions/workflows/ci.yml/badge.svg)](https://github.com/tagakost-tech/Group-8-project/actions/workflows/ci.yml)

**Group 8 · Esade, *Disrupting B-Corp* · client: Omega IFS**

Omega IFS, near Milan, cold-forms special bolts and precision parts to its
customers' drawings. Its buyers (car makers' suppliers, window and furniture
hardware, electrical equipment and household appliances) want one reliable
supplier for the whole order and the finished part. Small Italian
manufacturers often turn big orders away: they lack the capacity, or one
operation they do not do, while machines next door sit idle.

This repository is the digital platform of the ecosystem built around that
need. **Omega is the orchestrator.** It holds the customer relationship,
answers for quality and delivery, and sets the rules, and a network of
certified northern Italian specialists (cold forming, turning, heat treatment,
plating, wire, logistics) does the work behind it. The buyer sees one offer,
one delivery and one invoice, all from Omega.

The platform has two halves:

1. **Demand: the outreach agent.** It finds European manufacturers that buy
   parts like Omega's, explains why each one fits, drafts the first email and
   follow-ups in Omega's own words, and answers replies.
2. **Fulfilment: the partner network.** Omega splits an order between its own
   machines and partners with free capacity, and sends the operations it does
   not do to the members that do them. Each job has its own message thread,
   and every routed job is logged.

Both halves follow one rule: **AI proposes, a named person decides.** No email
and no job leaves until a named Omega person approves it. What is sent is
exactly what was approved, checked by hash, and stored with the approver's name.

```
            buyers                         Omega IFS (orchestrator)                    partners
  ┌───────────────────────┐   email   ┌──────────────────────────────┐   job    ┌─────────────────────┐
  │ car suppliers,        │◀──────────│ outreach agent  ─▶  Review   │─────────▶│ cold forming        │
  │ hardware, electrical, │  replies  │ (drafts)        (a person    │  thread  │ turning, heat       │
  │ appliances            │──────────▶│                  approves)   │◀────────▶│ treatment, plating  │
  └───────────────────────┘           │ Split an order ─▶ Network    │  status  │ wire, logistics     │
                                      └──────────────────────────────┘          └─────────────────────┘
```

## How an order flows

A buyer asks for 1,000 bolts with a turned groove. In **Network → Split an
order**, Omega keeps 200 for its own forming line, gives the other 800 to a
member with free cold-forming capacity, and sends all 1,000 to a turning shop
for the groove. The shares must add up to the order. Each line is a draft
until a named Omega person presses **Approve and send**. The partner then
accepts or declines, starts the job and marks it done. Accepting books the
quantity against its weekly capacity, and declining, cancelling or finishing
releases it. A job names the part, never Omega's customer.

## The app

| Screen | What a person does there |
|---|---|
| **Today** | What waits for them, and the funnel from first email to order: companies found, emailed, replied, interested, meeting booked, order won. |
| **Review** | Reads, edits, approves or rejects every proposed email: first emails, follow-ups and answers to replies. A rejection needs a reason, and the agent learns from it. Each email shows why the company was chosen, in its own words. The outcome with the company is recorded here. |
| **Leads** | Every company with its fit score (market, need, certifications, distance) and outcome. **Import** a CSV (a trade-fair list, a directory, referrals) with a preview before anything is stored; **export** approved leads as a HubSpot-ready CSV. |
| **Markets** | The sectors Omega serves today and new ones it could enter, with the evidence for each. |
| **Network** | Members and their free capacity, **Send a job**, **Split an order**, the jobs log with approver and order, and a message thread per job. |
| **Setup** | "Connect Omega": every step left before the first live email (sending mailbox, SPF/DKIM/DMARC, legal details, opt-out, customer list, countries, inbox, AI writer), each with what to do and a live check. |

The full operator dashboard (sent emails, writing one email, templates,
searches, deliverability, settings, logs, data requests) is at **`/classic`**.

**Readability.** The **Aa** button, top right on every screen, opens four
choices, saved on that computer:

- **Text size** from 90% to 150%. Every text size scales, and no screen
  scrolls sideways, even on a phone at 150%.
- **Higher contrast**: solid text colours, visible edges on every card and
  field, outlined labels, underlined links and a thicker focus ring.
- **Easier reading**: more space between letters, words and lines (WCAG 1.4.12).
- **Reduce motion**, on top of the system setting.

A partner signs in with its own login and sees only the network: its own jobs,
the member list and the matching. Omega's leads, emails and settings answer
403 to it.

### Compared with commercial outreach tools

| Table stakes in Apollo, Lemlist, Instantly, Outreach | Here |
|---|---|
| Lead sourcing and import | Web search for manufacturers by sector and country; CSV import with column matching, de-duplication and do-not-contact screening |
| Personalisation | Drafts from the company's own website text, Italian or English; an optional AI writer on any OpenAI-compatible endpoint, with the hand-written template as a fallback |
| Sequences | First email plus follow-ups, stopped automatically on a reply, bounce or opt-out |
| Deliverability | Warm-up ramp (5 to 100 a day), 3 per domain per day, office hours only, a bounce circuit breaker, SPF/DKIM/DMARC checks, blocklist check |
| Compliance | One-click unsubscribe (`List-Unsubscribe-Post`), suppression lists, a lawful basis recorded per lead, GDPR access and erasure, PII-masked logs |
| Analytics | Funnel to meeting and order, reply rate, A/B tests, per-sector results |
| **Not in other tools** | **Every email and every partner job approved by a named person; the order is fulfilled by a network of SMEs behind one supplier** |

## Product and demo

| | Branch | Data | Where it runs |
|---|---|---|---|
| **Product** | `main` | none: it starts empty | Omega's own server (Docker) |
| **Demo** | `demo` | invented companies and partners | https://group-8-omega.vercel.app |

Fixes land on `main` first and are then cherry-picked to `demo`. Vercel deploys
the `demo` branch only. The demo cannot send email, and its data resets when
the host restarts.

## First run

The app starts empty: no companies, no partners, no invented data. Configure
`.env` (see `.env.example` and `docs/DEPLOY.md`), start it, and sign in as the
administrator. **Setup** lists every step left before the first live email.
In **Network**, set Omega's own weekly capacity on its card (**Set capacity**)
and admit each certified partner (**Add a partner**) with its own login.

Keyboard: `1`–`6` switch screens, `J`/`K` move through a list, `Ctrl K`
searches, `Ctrl Enter` approves the open email.

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
- Three kinds of login:
  - the **administrator** starts and stops the agent, sends, imports, changes
    settings and admits partners;
  - **reviewers** (named logins) decide on emails and jobs and record outcomes;
  - **partners** see only the network.

`docs/DEPLOY.md` covers installing, the public address for opt-out links,
Microsoft 365 sign-in, backups and security updates.

## Where things are

| Path | What |
|---|---|
| `client/omega.json` | Omega's own words: value proposition, buyer keywords, sectors, best customers. |
| `modules/review_queue.py` | The human-in-the-loop gate for every email. |
| `modules/network.py` | The partner network: members, jobs, split orders, messages, partner logins. |
| `modules/dispatch_pipeline.py`, `modules/sequences.py` | Drafting, sending and follow-ups. |
| `modules/fit_score.py`, `modules/lookalike.py` | Fit score and likeness to Omega's best customers. |
| `modules/outcomes.py` | Outcomes per company and the funnel. |
| `modules/lead_import.py` | CSV lead import. |
| `modules/compliance.py`, `modules/export.py` | Lawful basis, personal-data register, access and erasure. |
| `templates/app.html`, `static/app/` | The app (`/`). |
| `templates/dashboard.html`, `static/js/` | The older operator dashboard (`/classic`). |
| `main.py` | The web app and the command line (`py -3.15 main.py --help`). |
| `.github/workflows/ci.yml` | Lint, tests on Python 3.15, browser tests. |

## Tests

```powershell
py -3.15 -m pytest -n 6 -o addopts="" -m "not ui"   # about 5 minutes
py -3.15 -m pytest -o addopts="" -m ui tests/ui      # browser tests (Playwright, Chromium)
ruff check .
py -3.15 scripts/ui_contrast.py                      # every colour pair against WCAG 2.2 AA
py -3.15 scripts/ui_check.py --url http://127.0.0.1:5000 --out shots
```

GitHub Actions runs the first three on every push. The older tests run on a
fixed test client in `tests/fixtures`, so they check the machinery and not
Omega's wording. `ui_contrast.py` reads the colours from `static/app/app.css`,
in the normal and higher-contrast sets. `ui_check.py` signs in (with
`UI_CHECK_USER` / `UI_CHECK_PASSWORD`) and takes a screenshot of every screen
at 1440 and 390 px, at 100% and 150% text, with the Readability switches off
and on. It fails on console errors, requests to other sites, sideways
scrolling, controls under 28 px, a text size or contrast that does not
change, or a Readability choice that does not survive a reload.

## Limits

- **This is a prototype for a course**, handed over to Omega IFS.
- **The lawful-basis rules** in `modules/compliance.py` are the developers'
  proposals and need a lawyer's review before live use.
- **The network's fee, ranking and audits** described in the ecosystem design
  are not in the software yet: it routes, logs and rates jobs.
- **The older tests** run against a fixed stand-in client ("Suppleco") so they
  check the machinery, not Omega's wording; Omega's own words live only in
  `client/omega.json` and `templates/`.
