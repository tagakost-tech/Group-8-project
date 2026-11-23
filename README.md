# Omega Carbon Advantage

**Group 8 · Esade, *Disrupting B-Corp* · client: Omega IFS**

Omega IFS is a family-owned SME in Cesano Maderno (Italy). It cold-forms special
fasteners and precision parts to customer drawings, for car makers' suppliers,
window and furniture hardware, electrical equipment and household appliances.

This is a course prototype of an AI assistant for Omega. It is built on one
rule: **AI proposes, a named person decides.** Nothing leaves Omega without a
person's approval: an email, a quote or a carbon passport. Each decision is
stored with the reviewer's name and the exact text that was approved.

## The idea

EU carbon rules are changing the price of imported fasteners:

- **CBAM**, the EU's carbon border charge, covers screws and bolts (CN 7318)
  from 2026. Without verified supplier data, an EU importer pays on the EU's
  high default emissions for China.
- **Scope 3 reporting** (CSRD) means large buyers must report their
  suppliers' emissions.

Omega forms Italian electric-arc steel and wastes little material. Its parts
carry roughly **9–10 times less CO₂** than the EU default for the same part made
in China. The prototype turns that into proof, lot by lot, and into sales.

It also keeps the honest finding visible. On a small pin, CBAM adds only
€0.17 (2026) to about €8 (2034) per 1,000 parts. That is not enough to close a
€27 price gap. So the main argument is the footprint for the buyer's own
reporting, not the tax. CBAM closes the gap only on heavier parts, and the
comparison screen shows from which year.

## What it does

| Screen | What a person does there |
|---|---|
| **Today** | Sees what waits for them, and the carbon advantage of the lots measured so far. |
| **Review** | Reads, edits, approves or rejects every proposed email (first emails, follow-ups, answers to replies). A rejection needs a reason, and the agent learns from it. An optional paragraph quotes approved carbon figures. |
| **Quotes** | Drops a drawing (PDF). The agent reads the sizes and checks the part can be cold-formed. It estimates the forming stations, the price per 1,000 parts, the tooling cost and the CO₂ against machining. It shows the parts Omega **made before** that are most alike, with the setters' notes. A person approves the quote. |
| **Carbon** | Records a lot from the steel certificate, the press counter and the energy meter. That gives a **carbon passport** (CO₂ per part, PDF plus Catena-X-style data file), released only after approval. The **CBAM** view compares an import's landed price with Omega's, year by year, from 2026 to 2034, with every rate sourced. |
| **Leads** | Companies the agent found, with a fit score (market, need, certifications, distance) and a "carbon pressure" signal that explains why a buyer may care about low-carbon parts. |
| **Markets** | The sectors Omega serves today and new ones it could enter, with the evidence for each. |

The earlier full dashboard, with settings, deliverability, templates and logs,
stays available at **`/classic`**.

Behind the screens:

- **Outreach:** finds European manufacturers, checks and scores them, and
  drafts Omega's first email (Italian or English), follow-ups and answers. It
  sends only the approved text, within safe daily limits.
- **Review queue:** each stage (first email, follow-up, reply) can be set to
  manual, approve or auto. The default is approve. A daily cap applies, and
  the approved text is checked by hash before sending.
- **Security and privacy:**
  - Named reviewer logins.
  - Content-Security-Policy, and no third-party scripts.
  - Personal data registered for GDPR access and erasure.
  - Encrypted client lists.
  - A scan that keeps secrets out of any handover archive.

## Try the demo

Python 3.11 on Windows (the `py` launcher):

```powershell
py -3.11 -m pip install -r requirements.txt
py -3.11 scripts/run_demo.py
```

Open <http://127.0.0.1:5078> and sign in as `admin` / `demo-admin-password`,
or as the reviewer `maria` / `demo-reviewer-password`.

The demo builds its own settings and never reads `.env`. It seeds invented
companies (`.example` addresses, which cannot receive mail), invented lots and
an invented job history. It runs in dry-run mode, so nothing is sent.

Keyboard: `1`–`6` switch screens, `J`/`K` move through a list, `Ctrl K`
searches, `Ctrl Enter` approves the open email.

Screenshots of every screen at desktop and phone width are in
[`docs/screens/`](docs/screens/).

## Run it for real

```powershell
copy .env.example .env
py -3.11 main.py doctor      # lists every [FAIL] that blocks live sending
py -3.11 main.py dashboard   # then open http://127.0.0.1:5000
```

`.env` needs its own `UNSUBSCRIBE_SECRET` and `ERASURE_KEY`. Make each with
`py -3.11 -c "import secrets; print(secrets.token_urlsafe(48))"`. No live email
goes out until `doctor` shows no `[FAIL]`. That needs the legal identity, a
sending mailbox, an opt-out route, the client list and the enabled countries.
`docs/SETTINGS.md` describes every setting, and `docs/DEPLOY.md` covers
installing and running.

## Where things are

| Path | What |
|---|---|
| `client/omega.json` | Omega's own words: value proposition, buyer keywords, sectors, best customers. |
| `client/omega_costing.json` | Indicative rates for quotes: wire, machine time, tooling, margin, CO₂ factors. |
| `client/omega_carbon.json` | CBAM rates, steel-route emissions and grid factor, each with its source. |
| `client/omega_jobs.json` | An **invented** job history with setters' notes, standing in for Omega's records. |
| `modules/cbam.py` | The CBAM landed-price comparison. |
| `modules/carbon_passport.py` | Lot footprints, passports (PDF and PCF data) and their approval. |
| `modules/job_memory.py` | "Made before": nearest past jobs by shape and description. |
| `modules/quoting.py` | Reads drawings, checks formability, estimates cost and carbon. |
| `modules/review_queue.py` | The human-in-the-loop gate for every email. |
| `modules/fit_score.py`, `modules/lookalike.py` | Fit score, carbon pressure, likeness to Omega's best customers. |
| `templates/app.html`, `static/app/` | The new app (`/`). |
| `templates/dashboard.html`, `static/js/` | The classic dashboard (`/classic`). |
| `main.py` | The web app and the command line (`py -3.11 main.py --help`). |

## Tests

```powershell
py -3.11 -m pytest -n 6 -o addopts="" -m "not ui"   # about 5 minutes
py -3.11 -m pytest -o addopts="" -m ui tests/ui      # browser tests (Playwright, Chromium)
ruff check .
```

Last run (23 Nov 2026): 5,285 passed, 141 browser tests passed, ruff clean.
The older tests run on a fixed test client in `tests/fixtures`, so they check
the machinery and not Omega's wording. The Omega-specific tests are separate:
`tests/test_carbon_advantage.py`, `tests/test_drawing_to_quote.py` and
`tests/ui/test_app_shell.py`.

## Limits — read before using any figure

- **This is a prototype for a course.** Companies, lots, mills, past jobs and
  demo logins are invented. Costing rates are indicative, not Omega's.
- **Check the CBAM figures against the Official Journal before any real use.**
  The China default values came from secondary copies of the regulation.
  CBAM rules are still changing: a 2026 proposal would stretch the phase-in to
  2038.
- **Passports cover steel parts only.** Their scope is the steel and the
  forming electricity; transport, heat treatment and coating are not
  included. They are not a verified declaration.
- **The docs in `docs/`** still describe the agent with the earlier
  placeholder client ("SUPPLECO") in many places. The mechanics they describe
  are the same.
