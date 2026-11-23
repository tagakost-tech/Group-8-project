# Deployment Guide — Omega IFS Outreach Agent

## Quick preflight

Always run the doctor first — it validates SMTP, DNS, SPF/DMARC, template
deliverability, the database, the legal identity the email footer needs, the
first email's sender title and PDF, the opt-out route and the inbox reads,
retention, and the AI model on the company's own server, when one is set
(section 11). It
exits 1 while any `[FAIL]` remains, and live sending is refused until then:

```powershell
py -3.11 main.py doctor          # add --smtp to also test a live SMTP login
```

With Microsoft sign-in (section 12), `--smtp` also gets a token from
Microsoft and signs in to each Microsoft 365 inbox.

Section 10 lists what each check needs, and the steps it cannot see.

## 1. Run locally (no Docker)

On Windows, with Python 3.11 and its `py` launcher:

```powershell
py -3.11 -m pip install -r requirements.txt
copy .env.example .env           # once, for a new install only
```

Before anything opens the database, put `UNSUBSCRIBE_SECRET` and
`ERASURE_KEY` in `.env` (README, Quick start; section 6), and back up the
erasure key. The agent, the dashboard and every command except `doctor`
refuse to start without it. Then add the client list (README, Client
list): while `CLIENT_FILTER_ENABLED` is on, live sending is refused until
it loads and screens at least one company. Set `SENDER_TITLE` (section 7);
the Company Profile PDF at `PDF_PATH` is optional, needed only with
`ATTACH_PROFILE=true` (off for Omega). `docs/SETTINGS.md` lists
every other setting.

```powershell
py -3.11 main.py doctor
py -3.11 main.py dashboard       # http://127.0.0.1:5000
```

Start the agent from the dashboard **Start** button, or from the CLI:

```powershell
py -3.11 main.py run --continuous
```

On Windows the agent does not set file permissions itself: `data\` and
the folders in it take the permissions of the install folder. IT should
allow only the account that runs the agent, and administrators, to read the
install folder, since `data\` holds the database, its backups, the journal
and access answers. (In the Docker image the agent makes these folders
private itself; section 2.)

The agent runs only while that process does: nothing starts it again after
a reboot, a crash or a logoff. Start it again by hand after each, and look
at the dashboard each morning.

**"Refusing to start - ... could not be loaded".** A part that limits, checks
or stops sending is switched on in the settings but cannot be loaded, so the
agent does not start and nothing is sent. This means the install is broken,
for example a package went missing in an update. Install the packages again
(`py -3.11 -m pip install -r requirements.txt`), check that no file of the
agent was deleted, and start again. The dashboard's Start and Send buttons
show the same message. However the start was tried, the refusal is written
to the log file and listed under the dashboard's Recent Errors as
`GUARD_UNAVAILABLE`. A `COMPONENT_MISSING` line there means an extra (the
retry queue, follow-ups, open tracking, the AI that words the emails,
discovery or learning) could not be loaded: the agent runs without it until
the install is repaired.

A send cut off by a stop, a crash or a locked database is never sent again
automatically. Its row stays `preparing`, and its contact is held, for 26
minutes, the longest a send can take; after that it is settled as an email
that may have gone (`sent_unconfirmed`), with an `EMAIL_INTERRUPTED` warning
on the dashboard. A send the server refused is kept as refused, on disk under
`data/spool/refused_sends/` while the database is locked.

## 2. Run with Docker

```bash
docker compose up -d --build   # dashboard on http://localhost:5000
docker compose logs -f
```

- Secrets come from your local `.env` at runtime (via `env_file`) — they are
  **not** baked into the image (see `.dockerignore`).
- `data/`, `logs/`, and `assets/` are mounted as volumes so the SQLite
  database, logs, and your PDF survive container restarts.
- The image carries no client or competitor list (`*.xlsx` is in
  `.dockerignore`): put the list in `data/` and set
  `CLIENT_LIST_PATH=data/<file>.xlsx`. While the client screen is on and its
  list cannot be loaded, or screens no company, live sending is refused and
  `doctor` fails. The same holds for a competitor list set in
  `COMPETITOR_LIST_PATH` while `COMPETITOR_FILTER_ENABLED` is on.
- The image sets `TZ=Europe/Rome`, so what reads the host's clock, the log
  lines among them, is in Italian time. Opt-outs, blocks and the other
  records are stamped in `APP_TIMEZONE` on any host.
- The container runs as uid 10001, not root. On a Linux host, give that uid
  write access to `data/`, `logs/` and `templates/`.
- The dashboard is published on this machine only (`127.0.0.1:5000`). For a
  public unsubscribe page, run the tunnel or reverse proxy of section 3 on
  this machine; never change the port line to `5000:5000`.
- The container is read-only: only `data/`, `logs/`, `assets/`,
  `templates/` (the dashboard's template editor saves there) and `/tmp` can
  be written. It runs with no Linux capabilities and cannot gain any
  (`cap_drop: [ALL]`, `no-new-privileges`).
- Inside the container, `data/` and every folder the agent makes in it
  (`backups/`, `exports/`, `spool/`) can be read only by uid 10001 (0700),
  and the journal and the database file only by it (0600). A `./data`
  folder that already exists on the host keeps its own permissions (Docker
  makes a missing one readable by everyone): run `chmod 700 data` once.
- The image leaves out every copy of `.env` (`.env.bak`, `.env.old`, ...),
  any database file found outside `data/`, and every key or certificate file
  (`*.pem`, `*.key`, `*.pfx`, `*.p12`). The Microsoft sign-in key goes in
  `data/`, owned by uid 10001 and readable by it only (`chmod 600`); the
  agent refuses it otherwise (section 12).
- The image is built from an exact Python version
  (`python:3.11.13-slim-bookworm` in the `Dockerfile`). Move it to a newer
  3.11 release on purpose, for security fixes, and run the tests after. It
  carries no documentation, tests, scripts, spreadsheets, PDFs or Markdown
  files (`.dockerignore`).
- The dashboard refuses to start without a `DASHBOARD_PASSWORD` of at least
  12 characters, in the container and outside it, on this machine too (any
  other user or program here can reach `127.0.0.1`). It never runs Flask's
  debugger, whatever `DEBUG` says. It answers only for localhost and the
  `BASE_URL` host. Section 3a lists what else it checks.
- Start the agent from the dashboard UI (runs in the same process — no SQLite
  contention), or run it standalone. `restart: unless-stopped` brings the
  dashboard back after a restart, but not the agent: start it again.

```bash
docker compose run --rm outreach python main.py run --continuous
```

## 3. Make the unsubscribe link a web page (public BASE_URL)

The unsubscribe link is built from `BASE_URL`. With the default
`BASE_URL=http://127.0.0.1:5000` recipients **cannot** reach your machine, so
the link falls back to a `mailto:` to the sending mailbox, which only works
if `IMAP_ENABLED=true` so the agent reads those emails. With it on, live
sending also stops while an inbox the agent reads, or its junk folder, has
had no complete read within three check intervals (90 minutes at the default
30), as when its login fails, and every pass reads the inbox before its
first send. Without either,
`doctor` fails and live sending is refused: nothing would read an opt-out.

A public `BASE_URL` must be `https://`. Then every email carries a web
unsubscribe link and **one-click unsubscribe** (RFC 8058), which Gmail and
Outlook show next to the sender, and that is an opt-out route on its own. A
public `http://` address still gets a web link, but it is not accepted as an
opt-out route: with `IMAP_ENABLED=false`, live sending stays refused.

Only links the agent recorded when it sent the email work: `/unsubscribe/`
followed by `u` and 32 hex digits. A link in the old form (an encoded
address, a dot and a signature) is refused; no email with one was ever sent.

**What the inbox reader believes.** Omega IFS's mail server (Microsoft 365,
Gmail and most others do this) writes the result of its sender checks into
an `Authentication-Results` header at the top of each message. The agent
reads only that top-most header. When it says `dmarc=fail`, or both
`spf=fail` and `dkim=fail`, or `compauth=fail` (Microsoft 365's own verdict),
the message counts as possibly forged. So does a message from a domain with
no DMARC record (`dmarc=none`, or no DMARC result) where neither SPF nor DKIM
passed for the sender's own domain: a forger can pass both for a domain of
their own. So does a message whose header the agent cannot read (a bracket
or a quote that never closes). For a possibly forged message, an opt-out or data request in it stops
only the sender's own address, never the whole company, and a bounce report
in it marks no address as bounced; the activity log shows a
`MAIL_UNAUTHENTICATED` line. A server that writes no such header changes
nothing. Check once after go-live: a bounce report from a company without
DMARC, sent by a server whose name has no SPF record, now counts as possibly
forged and marks nothing as bounced. If many real bounces show up as
`MAIL_UNAUTHENTICATED`, look at their headers before relying on bounce
marking.

**A bounce report must be about an email the agent sent.** A report that
names the original email's Message-ID (in the attached original, in its
attached headers, in Gmail's `X-Original-Message-ID`, or in its own
`In-Reply-To` and `References`, as Exchange Online and Gmail write them)
marks the address bounced only if the agent sent that very email to that
address. A report that names none counts only if the agent emailed the
address in the last 30 days. Any other report marks nothing, and the
activity log shows a `BOUNCE_UNMATCHED` line. Check once after go-live,
with a test email to an address that does not exist: its bounce must mark
the address bounced, not show `BOUNCE_UNMATCHED`.

A message over 10 MB is not downloaded: the agent reads its headers
and the first 256 KB of its text, where an opt-out or a bounce report is.

**Open tracking is off by default and should stay off for cold outreach.** A
tracking pixel needs the recipient's prior consent (ePrivacy Art. 5(3); the
Garante's provvedimento 284 of April 2026), which a cold prospect has not
given. `doctor` warns if `OPEN_TRACKING_ENABLED=true`. While it is off, the
pixel's address (`/track/...`) answers 404.

Once `BASE_URL` is public, the dashboard is too, behind its password (it
never starts without one).

**Option A — a real host (recommended for production).**
Deploy the container to any server/PaaS with a public domain, put it behind
HTTPS, and set:

```env
BASE_URL=https://outreach.yourdomain.com
```

**Option B — a tunnel (for testing only).**
Expose your local dashboard with a tunnel and use the tunnel URL:

```bash
# using cloudflared
cloudflared tunnel --url http://localhost:5000
# or using ngrok
ngrok http 5000
```

Then set the printed URL in `.env` and restart:

```env
BASE_URL=https://random-name.trycloudflare.com
```

Unsubscribe links in new emails will now point at that address. A quick
tunnel like these gets a new random address each time it starts, and the
links in emails already sent keep the old one: they then lead nowhere, and
only the opt-outs that arrive by email still work, a reply or the mailto in
each email's `List-Unsubscribe` header, while the inbox is read. Do not send
live mail through a quick tunnel; for live sending use a fixed address, such
as Option A or a named tunnel with your own host name.

## 3a. What the dashboard checks on every request

- **A password, always.** `DASHBOARD_PASSWORD` must be 12 characters or more,
  or the dashboard does not start. Pick a long pass phrase.
- **Wrong passwords are limited.** After 10 wrong passwords from one address
  within 15 minutes, that address gets "429 Too Many Requests" for every
  request, the right password included, until the oldest wrong one is 15
  minutes old. A request with no password at all (the browser asking for the
  login box) does not count. Behind a reverse proxy or a tunnel every visitor
  has the proxy's address, so someone guessing there also locks out the
  operator for up to 15 minutes; the dashboard reads no `X-Forwarded-For`,
  since anyone can write that header.
- **Changes come only from the dashboard's own page.** Every request that
  changes something must be JSON and carry an `Origin` header (or, without
  one, a `Referer`) naming the dashboard's own address; otherwise it gets
  403. The browser does this by itself. A script must send the header too,
  for example:
  `curl -u admin -H "Origin: http://127.0.0.1:5000" -H "Content-Type: application/json" -d "{}" http://127.0.0.1:5000/api/agent/stop`.
  The address in `Origin` must match the one the dashboard sees, scheme
  included: behind a proxy that ends HTTPS and forwards plain HTTP, the
  browser says `https://...` while the dashboard sees `http://...`, and every
  change is refused. Use a tunnel or proxy that keeps the scheme, or ask a
  developer to add a trusted-proxy setting.
- **Small bodies only.** A request body over 1 MB gets 413. The largest real
  one is a template save (the shipped templates are about 3 KB).
- **A strict page policy.** The page may run scripts, and load styles and
  fonts, only from the dashboard itself, never from another server. Chart.js
  4.4.1 and Font Awesome Free 6.4.0 are copies in `static/vendor/`, checked
  against the hashes their publishers list; `static/vendor/README.md` gives
  each file's source, sha256 and licence, and how to change a version. A
  test fails if a file there changes. So the page works on an office network
  that blocks the CDNs, and a hacked CDN cannot change it.
- **The template preview is sandboxed**: a script in a template does not run
  and cannot reach the dashboard.
- **A production web server.** `main.py dashboard` serves the dashboard with
  waitress, on `127.0.0.1` unless `DASHBOARD_HOST` says otherwise. Waitress
  refuses a body over 1 MB before reading it, and is told of no trusted
  proxy, so the login lockout counts the connection's own address whatever
  `X-Forwarded-For` says. Every check above applies to it. If waitress is missing, the dashboard
  does not start (install `requirements.txt` again).
- **Flask's development server only on request, and only on this
  computer.** `main.py dashboard --dev-server` uses Flask's own server, for
  a developer. It is refused when `DASHBOARD_HOST` lets other computers in,
  or when `BASE_URL` names a public address (a tunnel or proxy brings them
  in). It never runs Flask's debugger, whatever `DEBUG` or `FLASK_DEBUG`
  say.

## 4. Deliverability checklist (protect your sender reputation)

- **SPF / DKIM / DMARC** on your sending domain. `py -3.11 main.py doctor`
  reports whether SPF and DMARC records exist for your sender domain; it
  does not check DKIM. It uses the same lookup as the dashboard's
  Deliverability panel, so both give the same verdict. Doctor says "SPF record
  found" (OK), "no SPF record found" (WARN), or "SPF could not be checked just
  now" (SKIP) when DNS did not answer in time; the panel shows a tick, a cross
  or a question mark, and under a question mark the same "could not be
  checked just now" line. A SKIP says nothing about the domain: run doctor
  again later. Gmail/Workspace manage these for you; a custom domain needs
  them configured.
- Keep `WARMUP_ENABLED=true` and let volume ramp up gradually.
- Keep `MAX_PER_DOMAIN_PER_DAY` low (default 3).
- Set a real `SENDER_NAME` / `COMPANY_NAME` (not the placeholders), and the
  legal identity: `LEGAL_ENTITY_NAME`, `LEGAL_REGISTERED_OFFICE`,
  `LEGAL_COMPANY_REGISTRATION`, `PRIVACY_NOTICE_URL` and
  `PRIVACY_CONTACT_EMAIL`, plus `LEGAL_SHARE_CAPITAL` and
  `LEGAL_SINGLE_MEMBER` for an S.r.l. or S.p.A. (section 5).
- Run the template through the linter: it's part of `doctor`, and flags spam
  words, excess links, and a missing unsubscribe link.
- Consider `SEND_JITTER_SECONDS` > 0 so sends aren't perfectly regular.

## 5. Environment reference

All configuration lives in `.env`. `docs/SETTINGS.md` lists every setting
with its type, default and what it does; `.env.example` is only the
template a new `.env` starts from. Secrets are never written to the logs or
shown by the `/api/config` endpoint.

**Web pages: direct access, public sites only.** The agent fetches company
web pages itself and does not use a proxy named in the environment
(`HTTP_PROXY`, `HTTPS_PROXY`). If this machine can reach the web only
through a proxy, page fetches fail: allow it direct outgoing HTTP and HTTPS.
It refuses any page whose address is on this machine or a private network,
also after a redirect, refuses a host name with unusual characters (such as
a `%` escape), and reads at most 2 MB of a page, also when the page arrives
compressed (`docs/COMPLIANCE.md` section 9). Nothing needs setting for this.

The log files (`logs/<command>.log`, one per command) mask what identifies a
person as they are written: an address keeps only its first letter and its
domain, and unsubscribe tokens and IP addresses (IPv4 and IPv6) are
replaced. Keys and passwords that a library puts in an error message are
replaced too: `sk-...`, `gsk_...` and `AIza...` keys, long values after
`key=`, `token=` and `secret=` (also as `"api_key": "..."` in JSON),
anything after `password=` (all of a quoted one), the value of an
`Authorization`, `api-key` or `x-api-key` header, a signed token on its own
(`eyJ...`, such as a Microsoft access token), and an XOAUTH2 sign-in answer
(section 12). An IPv6 address written
with fewer than three groups, such as `::1` (this machine) or `2001::1`, is
left as it is: it cannot be told apart from a slice in a traceback. Each one
rotates at midnight, but only when its command writes after midnight, and
keeps its newest `LOG_RETENTION_DAYS` (default 30) rotated files
(`setup_logging` in `main.py`). Old files are deleted by count, not by age:
the period holds only for a command that writes every day, and the files of
a command run now and then, such as `gdpr-erase`, stay for months. Delete
those by hand.

Live sending is refused, and `doctor` fails, until the legal identity printed
in every email's footer is complete (Cod. Civ. 2250, decision L6):
`LEGAL_ENTITY_NAME`, `LEGAL_REGISTERED_OFFICE`, `LEGAL_COMPANY_REGISTRATION`,
`PRIVACY_NOTICE_URL` (https only) and `PRIVACY_CONTACT_EMAIL`; for an S.r.l. or
S.p.A. also `LEGAL_SHARE_CAPITAL` and `LEGAL_SINGLE_MEMBER` (`yes` or `no`).
The footer's wording is versioned per lawful basis (`NOTICE_VERSIONS` in
`modules/compliance.py`), and each email records the version it carried.

## 6. The erasure key (`ERASURE_KEY`)

When a person's data is erased, their address is replaced by a token made
with this key. The token stops the address being collected or emailed
again, and nothing else can be learned from it.

The agent, the dashboard and every command except `doctor` refuse to start
without the key. Create it once, before the first run:

```powershell
py -3.11 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the output in `.env` as `ERASURE_KEY=...`. It must be at least 32
characters: a new database refuses a shorter key, because whoever held a
copy of the database could guess it and then test guessed addresses against
every erasure. Then:

- **Back it up apart from the database** (for example in Omega IFS's password
  manager). A backup of the database without the key cannot match tokens to
  guessed addresses; the key without the database holds no personal data.
- **Never change it.** A new key would no longer recognise anyone erased
  under the old one, so their addresses could be collected and emailed
  again. The database records a fingerprint of its key and refuses to open
  with another one; the fix is to restore the original key from its backup.
- **If it is shorter than 32 characters** and a database already keeps it
  (it was made before this rule), `doctor` fails on "Erasure key", and the
  agent and the dashboard refuse to start, saying so. The other commands
  still open the database, so data-subject requests, erasures and backups
  keep working. The key cannot be changed on that database: start again:
  move the old `data/` folder aside, set a long key, and let the agent make a
  new database.
- **Every start of the agent and the dashboard** checks the key again, as
  `doctor` does: one the database was not erased with stops the start with
  the same words, not a program error.
- **If it leaks,** treat it as a breach (`docs/privacy/breach_procedure.md`):
  with the key, someone holding the database could confirm whether a guessed
  address was erased.

## 7. The country lock (`ENABLED_COUNTRIES`)

Whether a company may be emailed without its prior consent depends on its
country, and only Omega IFS's counsel can decide which countries are allowed
(the review's country table, decision L1.1). Until the full country rules
are built, an interim lock applies:

- `ENABLED_COUNTRIES` lists the allowed countries as two-letter codes, for
  example `ENABLED_COUNTRIES=FR,GB` (`GB` covers `.uk`). Blank, the default,
  means **no live email at all**; dry runs still preview everything.
- A recipient is placed by the country code at the end of its domain: an
  address at a `.fr` domain is France. Addresses on `.com`, `.eu` and other generic
  domains show no country, count as needing consent, and are refused unless
  counsel allows them with `UNKNOWN_COUNTRY_ALLOWED=true`.
- `doctor` reports the lock as a blocking check and names the countries
  enabled.

Fill these in only from counsel's signed table.

### The first email's language, title and attachment

The first email is Omega IFS's own text: `templates/email_template.html` in
English and `templates/email_template_it.html` in Italian. Each contact gets
every email - the first, both follow-ups, the legal footer - in one language:

- `ITALIAN_COUNTRIES` (default `IT`): the countries written to in Italian, as
  two-letter codes like `ENABLED_COUNTRIES`, for an address in one of them or
  a company shown to be in one of them. Everyone else, `.com` and `.eu`
  addresses included, gets English.
- `SENDER_TITLE`: what the sender is responsible for ("I am responsible for
  ... at Omega IFS" / "mi occupo di ... in Omega IFS"), so an area such as
  `Business Development`. A live first email through a mailbox without a
  title is refused, and the contact waits, pending, for a mailbox that has
  one; `doctor`'s "Sender title" fails naming the mailboxes without one.
- `SENDER_SIGNATURE`: the lines under "Best regards,", separated by the two
  characters `\n`. Blank: the sender's name, the title and the company name.
- `SENDER_2_TITLE` ... `SENDER_5_TITLE`, `SENDER_2_SIGNATURE` ...
  `SENDER_5_SIGNATURE`, and `sender_title` / `sender_signature` in
  `SENDER_ACCOUNTS`: the same for each extra mailbox.
- `EMAIL_SUBJECT_IT`: the Italian recipients' subject. Blank, they get the
  English subject (`EMAIL_SUBJECT`, or the `EMAIL_SUBJECTS` A/B variants).
- `ATTACH_PROFILE` is optional and `false` by default, as Omega uses it: its
  first email attaches nothing. With `ATTACH_PROFILE=true`, a live first
  email is refused while `PDF_PATH` is missing, a folder, empty or
  unreadable. `doctor` fails; a dry run and the Direct Send
  preview say so. The file is looked for at each first email, so one put in
  place needs no restart. Follow-ups carry no attachment.
- Italian recipients also get the Italian follow-ups
  (`templates/followup_1_it.html`, `followup_2_it.html`), footer and
  notice, and an Italian page when they unsubscribe; the inbox reads opt-out
  replies in Italian too. With `templates/email_template_it.html` missing,
  they get the English first email and English follow-ups, and `doctor`
  warns.

## 8. Retention without the agent

The agent applies the retention periods once a day while it runs live. While
it is not run, nothing would age out, so schedule the command on its own. In
PowerShell, with the project's real folder:

```powershell
$project = "C:\path\to\Omega Outreach Agent"
$py = (Get-Command py).Source
$options = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$retention = New-ScheduledTaskAction -Execute $py -Argument "-3.11 main.py retention" -WorkingDirectory $project
Register-ScheduledTask -TaskName "Omega outreach retention" -Action $retention -Trigger (New-ScheduledTaskTrigger -Daily -At 02:30) -Settings $options
```

- `py -3.11 main.py retention --dry-run` first: it counts what a run would
  erase, mask and delete, and changes nothing.
- A run records itself as the agent's does (`doctor` shows the last run) and
  writes one `RETENTION_APPLIED` line to the activity log, in counts only.
- `RETENTION_ENABLED=false` makes the command refuse, as it stops the agent's
  daily run.
- The periods are the developer's proposals in the decision sheet (L5.1),
  pending counsel; `docs/COMPLIANCE.md` section 6 lists them.

**The task settings matter.** Windows' defaults do not start a task on
battery power, do not run a missed start later, and do not wake the
computer, so a PC that is off, asleep or on battery at 02:30 would skip the
run. `-StartWhenAvailable` runs a missed start as soon as it can,
`-WakeToRun` wakes the computer, and the two battery options let it start
and finish on battery.

**The account.** Registered as above, the task runs under your Windows
account, and only while you are logged on: no password is stored for it.
For it to run while nobody is logged on, IT registers it with `-User` and
`-Password` for an account that has Python 3.11 and the requirements
installed; a Python installed for one user is not there for the others.

**Check it once by hand** after registering it:

```powershell
Start-ScheduledTask -TaskName "Omega outreach retention"
Get-ScheduledTaskInfo -TaskName "Omega outreach retention"
(Get-ScheduledTask -TaskName "Omega outreach retention").Principal.LogonType
py -3.11 main.py doctor
```

`LastTaskResult` 0 means the command succeeded, the logon type says how the
task signs in (`Interactive` is only while you are logged on), and `doctor`'s
"Retention run" line shows today's date.

With Docker, schedule the same command on the host instead, for example
with a cron line such as
`30 2 * * * cd /path/to/project && docker compose run --rm outreach python main.py retention`.

## 9. Backups, the journal, and restoring

Schedule the backup daily, and a restore rehearsal every four weeks, with
the same settings as section 8:

```powershell
$project = "C:\path\to\Omega Outreach Agent"
$py = (Get-Command py).Source
$options = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$backup = New-ScheduledTaskAction -Execute $py -Argument "-3.11 main.py backup" -WorkingDirectory $project
Register-ScheduledTask -TaskName "Omega outreach backup" -Action $backup -Trigger (New-ScheduledTaskTrigger -Daily -At 02:00) -Settings $options
$check = New-ScheduledTaskAction -Execute $py -Argument "-3.11 main.py restore-check" -WorkingDirectory $project
Register-ScheduledTask -TaskName "Omega outreach restore check" -Action $check -Trigger (New-ScheduledTaskTrigger -Weekly -WeeksInterval 4 -DaysOfWeek Sunday -At 03:00) -Settings $options
```

Check them as in section 8: after the backup runs, `doctor`'s "Backup" line
shows today's date; after the restore check, `LastTaskResult` 0 means it
passed. With Docker, use a host cron line such as
`0 2 * * * cd /path/to/project && docker compose run --rm outreach python main.py backup`.

- `main.py backup` copies the database through SQLite's backup API into
  `data/backups/`, checks the copy (`PRAGMA integrity_check`) before it
  counts, and keeps the newest 14 (decision L5.7). The copies are not
  encrypted: keep them, and any off-machine copy, on an encrypted disk.
- Everything that stops or limits sending is also appended to the journal,
  `data/journal.jsonl`, at the moment it is saved: every opt-out (a decline
  by reply too), company block and lift, dashboard Block and Unblock, bounce,
  refusal by the recipient's server, and erasure. So is every data-subject
  request registered or closed. An erasure is written only as its token,
  and rewrites the person's earlier lines as tokens. The journal is trimmed
  to the oldest backup.
- Every email sent is appended to the journal too, when it is recorded as
  sent: who it went to, which message it was (its Message-ID), which
  follow-up step, when, from which mailbox, and which version of the privacy
  notice it carried. An email whose answer from the server was lost is
  written as "unconfirmed". The line holds no subject and no text. A dry run
  and an email the server refused write no line. Why: a backup knows nothing
  of the emails sent after it, so without these lines a restored agent would
  send the same email to the same people a second time.
- **The journal must survive the loss of the database's disk.** It is
  written beside the database (`journal_path` in `modules/journal.py`), so
  whatever destroys `data/`, a disk failure or ransomware, destroys it too.
  A restore from an off-machine copy of a backup then silently undoes every
  opt-out, block, bounce and erasure made since that copy: erased people's
  data come back, people who objected, blocked companies and dead
  addresses can be emailed again, and everyone emailed since that copy can
  get the same email a second time. The code
  keeps no second copy, so IT has to: copy `data\journal.jsonl` to a second
  disk or a share at short intervals, such as every 15 minutes, overwriting
  one copy so that an erasure's rewrite reaches it too. The journal holds
  the addresses of everyone who objected, was blocked or bounced, and of
  everyone emailed since the oldest backup: keep that copy on an encrypted
  disk, like the backups.
- Windows can neither replace nor delete a file another program holds open (a
  sync or backup tool, an editor). An erasure's steps on files, rewriting the
  journal and deleting the person's access answer and held opt-outs, therefore
  come after its database commit and never stop it. A record in
  `data/spool/journal/`, by tokens only, stays until all of them have landed:
  `doctor` reports it, the operator is told, and the next backup, erasure or
  `replay-journal` finishes them. The same goes for a kill at any moment after
  the commit. A backup works while the journal is held; its old lines wait for
  the next one.
- `doctor` also reports an address still held in clear though erased, whatever
  stopped its erasure half-way. Run the erasure again: `main.py dsr erase <id>`
  or `main.py gdpr-erase <address>`. A record it names as unreadable can only be
  looked at and deleted by hand.
- **To restore:** stop the agent and the dashboard, and disable the backup,
  retention and restore drill tasks until the restore is checked: a backup
  deletes the oldest copy and trims the journal to the oldest copy kept,
  which can take the lines a restore of an old copy needs. After a disk loss, first
  put the newest copy of the journal back as `data\journal.jsonl`. Drill the
  backup you mean to restore (`main.py restore-drill --newest`, or
  `--backup <file>`; see the restore drill below) and go on only if it
  passes. Copy that backup over `data/agent.db` (delete `agent.db-wal` and
  `agent.db-shm`), then run `main.py replay-journal` before anything else,
  and then `main.py restore-drill` to check the result. The
  replay applies everything since the backup in the order it happened (a
  Block then an Unblock leaves the address unblocked; an Unblock then a Block
  leaves it blocked; the same for a company block and its lift), marks the
  contact "blocked", "pending" or "bounced" as the dashboard did, keeps each
  bounce with the time it was recorded, and erases again everyone erased
  since. Where a colleague of someone in the backup was erased since, it
  also replaces their company's domain with its code in the activity log
  and deletes its daily send counts, as the erasure did. It puts back every
  email sent since the backup (see "Emails sent since the backup" below).
  Then it applies the
  retention periods, whose erasures are not journalled. A click on an unsubscribe link sent since the backup still
  opts its recipient out: a click the restored database cannot match is
  kept for a year, if the link is one the agent made, and applied before
  any send to its recipient, and the replay records the links of the
  contacts it keeps again. The same `ERASURE_KEY` and `UNSUBSCRIBE_SECRET`
  are required.
- **Nothing starts before the replay.** Every journal line has a number, and
  the database writes the number down just before the line is written. A
  database that does not know a line of its journal was restored from an
  older backup, or lost writes. Until `main.py replay-journal` has run,
  `main.py run`, the dashboard's Start and Direct Send refuse and say to run
  it, the refusal shows in the dashboard's errors panel
  (`JOURNAL_NOT_REPLAYED`), and `doctor` and the "Ready to send" card show
  "Journal replayed" as failed. The replay ends with "The agent may start."
  A replay that stops on an error keeps the refusal: run it again. Blocks or
  opt-outs recorded on the restored database before the replay do not lift
  it. A fresh install, a healthy one, and a journal whose old lines the
  backups trimmed are never refused. A database that was lost while its
  journal was kept is refused too: run the replay on the new, empty one.
  A backup made before the replay (a scheduled task left on) does not trim
  the journal, as that would delete the lines the replay needs; the log
  says "Not trimming the journal". The first backup after the replay trims
  it. A journal that cannot be read (a damaged byte) also refuses the
  start, with "could not be checked": look at the file before going on.
- **Emails sent since the backup.** `main.py replay-journal` puts each one
  back from its journal line, so that no one gets the same email twice:
  - the contact counts as emailed, at the time the email went (a contact
    who has replied, opted out, bounced or was blocked since stays as it is);
  - a follow-up step that went is moved on, and the next step waits from
    the day that one went;
  - the basis the email relied on is recorded again;
  - an email sent today is added to today's counts for its company domain
    and its mailbox, so the daily limits are not passed on the day of the
    restore. Counts of earlier days are not put back.

  In the Emails table such a row has the subject "(not kept: this send was
  restored from the journal)", because the journal holds no subject or text.
  Running the replay a second time changes nothing: an email the database
  already knows is left alone.
- **What a restore still cannot bring back:**
  - Follow-ups for a contact whose first email went after the backup. The
    replay starts no follow-ups for anyone: a reply or a stop since the
    backup is lost with it, and a follow-up must not go out on a guess. Such
    a contact shows as "emailed" and gets nothing more unless an operator
    sends it.
  - A contact found since the backup is not in it at all. If it was emailed,
    the email is kept under a code made from the address, never the address
    itself. When the agent finds that address again, the contact starts as
    "emailed" and gets no first email, and a Direct Send to it asks first.
    If it was blocked or bounced, its address stays on the do-not-contact
    list, and the agent will not email it if it finds it again.
  - Replies and opens recorded since the backup, and the warm-up's count of
    sending days. The daily allowance can be lower for a while, never higher.
  - An email kept only under a code is not counted in its domain's bounce
    rate, so that domain can look worse than it is, never better.
  - One email at most: the one that was going out at the moment the computer
    stopped, if the agent did not run again before the restore. Its line is
    written when the agent next runs and settles it, so a restore made before
    that does not know it, and that one person can get the email twice.
  - Emails sent before the agent was updated to the version that journals
    them. Only emails sent after the update have a journal line. A backup
    made before the update forgets the emails sent between that backup and
    the update, and those people can get the same email again. The restore
    drill of such a backup fails and counts them as missing sends. Once the
    oldest backup kept was made after the update, this no longer applies.
- `main.py restore-check` rehearses this on a scratch copy of the newest
  backup and fails if the copy is damaged or anything erased survives. Run it
  by hand once the schedule is set up; the task above repeats it every four
  weeks.

### The restore drill (IT, every three months)

`main.py restore-drill` is the full rehearsal. It takes a fresh backup with
`main.py backup`'s own code, restores it into a new temporary folder (never
over the live database), replays the journal there and applies the held
opt-outs, as a real restore and the next start would. Then it checks that the
restored copy:

- passes `PRAGMA integrity_check`;
- holds every opt-out, company block, address block (a dashboard Block),
  bounce, erasure and held opt-out that the live database, the journal and
  `data\spool\` know of; the report counts what is missing for each kind
  separately;
- knows every email sent that the live database and the journal know of.
  The report says, for example, "12 send(s) since the backup, all restored".
  One that is missing after the replay is an email the agent would send a
  second time, and the drill fails;
- holds no one erased in clear, and no company of someone erased in clear,
  unless that company was blocked or lifted again since the erasure, which
  names it in clear in the live database too;
- was made under the same `ERASURE_KEY`.

It prints a PASS or FAIL report with counts (never an address), exits 1 on
FAIL, and deletes the temporary folder. It only reads the live database and
its files; the one thing it writes there is a `RESTORE_DRILL` line in the
activity log (and the fresh backup itself, as `main.py backup` does). If
another program holds the database's lock, it says so and stops without
changing anything: stop the agent and the dashboard first.

- `--newest` drills the newest backup already made instead of taking one.
- `--backup <file>` drills any backup file, such as an off-machine copy.
  With the live database gone or damaged, it checks against the journal and
  the held opt-outs only (`docs/INCIDENT_PLAYBOOK.md`, incident 4).

A FAIL that names opt-outs, blocks, bounces or sends missing means a restore
would lose them: the journal did not record them (a full disk, or the journal held
open by another program when they were saved; the agent's log says "Could not
write the journal"). A missing send means someone would get the same email
twice. Tell the developer before relying on the backups. The temporary folder is in the system's temp folder (the user's
`%TEMP%` on Windows, `/tmp` in Docker) and holds a full copy of the database
while the drill runs.

Schedule it every 13 weeks (about every three months), on a Sunday after the
backup:

```powershell
$drill = New-ScheduledTaskAction -Execute $py -Argument "-3.11 main.py restore-drill" -WorkingDirectory $project
$quarterly = New-ScheduledTaskTrigger -Weekly -WeeksInterval 13 -DaysOfWeek Sunday -At 04:00
Register-ScheduledTask -TaskName "Omega outreach restore drill" -Action $drill -Trigger $quarterly -Settings $options
```

Check it after each run: `LastTaskResult` 0 means PASS. Also run it by hand
once a quarter while watching the output, and after any restore (incident 4
of `docs/INCIDENT_PLAYBOOK.md`). With Docker, run
`docker compose run --rm outreach python main.py restore-drill` from the host.

**When something goes wrong** (a mailbox takeover, wrong people emailed, a
data leak or a lost laptop, a restore), follow
[`docs/INCIDENT_PLAYBOOK.md`](INCIDENT_PLAYBOOK.md).

## 10. Go-live checklist

Work through this before the first live email. `doctor` shows most of it: a
`[FAIL]` refuses live sending, a `[WARN]` does not. The last lines are what
`doctor` cannot see.

| Item | `doctor` | Who provides it | What to do |
| --- | --- | --- | --- |
| Unsubscribe secret | every command refuses to start while it is the shipped value or shorter than 32 characters | IT | Generate `UNSUBSCRIBE_SECRET` (README, Quick start). |
| Erasure key | FAIL while blank or shorter than 32 characters; commands refuse to start without it, and a new database refuses a short one | IT | Generate `ERASURE_KEY` once, at least 32 characters, back it up apart from the database, never change it (section 6). "Erasure key matches the database" fails if it is ever changed. |
| Legal identity | FAIL, "Legal identity (email footer)" | Omega IFS | `LEGAL_ENTITY_NAME`, `LEGAL_REGISTERED_OFFICE`, `LEGAL_COMPANY_REGISTRATION`, `PRIVACY_CONTACT_EMAIL`; for an S.r.l. or S.p.A. also `LEGAL_SHARE_CAPITAL` and `LEGAL_SINGLE_MEMBER` (section 5). |
| Privacy notice page | FAIL while `PRIVACY_NOTICE_URL` is blank; whether the page is online is not checked | Counsel approves the text, IT publishes it | The outline is `docs/privacy/privacy_notice_en.md`, a draft for counsel. Publish it over https and set `PRIVACY_NOTICE_URL`. |
| Countries | FAIL, "Country lock" | Counsel (decision L1.1) | `ENABLED_COUNTRIES`, and `UNKNOWN_COUNTRY_ALLOWED`, only from the signed country table (section 7). |
| Opt-out route | FAIL, "Opt-out route" | IT | `IMAP_ENABLED=true` with a working inbox login, or a public https `BASE_URL` (section 3). |
| Client list | FAIL, "Client list file", while it cannot be read or screens no company | Omega sales | The workbook in `data/` and `CLIENT_LIST_PATH` (README, Client list). |
| Lists encrypted | WARN, "Lists encrypted", while `LISTS_KEY` is blank or a plain copy of a list is still beside it; with the key set, "Client list file" fails on a plain list | IT | `LISTS_KEY`, each list encrypted with `main.py lists encrypt`, the plain workbooks deleted, the key backed up apart from the data (section 13). |
| Sender title | FAIL, "Sender title", naming each mailbox without one | Omega sales | `SENDER_TITLE`, and `SENDER_n_TITLE` for a mailbox with its own name (section 7). |
| Company Profile PDF (optional) | FAIL, "PDF attachment present", only while `ATTACH_PROFILE` is on; SKIP while it is off | Omega sales | Nothing while `ATTACH_PROFILE=false`, as for Omega; with it on, the PDF at `PDF_PATH` (section 7). |
| Footer in the templates | FAIL, "Legal footer in templates" | Developer | Every email and follow-up template keeps `{{legal_footer}}` and `{{unsubscribe_url}}`. |
| Dashboard password | FAIL while blank, only spaces or shorter than 12 characters; the dashboard refuses to start | IT | `DASHBOARD_PASSWORD`, 12 characters or more, before the first start (section 3a). |
| Sending mailbox | WARN, "SMTP credentials set"; WARN, "Microsoft sign-in", while a Microsoft 365 mailbox uses a password or the certificate ends within 30 days; `doctor --smtp` tries a live login, and "Microsoft sign-in (live)" a token and an IMAP sign-in | IT | Decide the mailbox. For a Microsoft 365 mailbox, set up Microsoft sign-in, with no password (section 12). F-20 is open (README). |
| Reading the inbox | FAIL, "Inbox read", while an inbox has had no complete read within three check intervals; WARN, "Inbox gap", when an outage ran past the 14-day look-back | IT | `IMAP_ENABLED` and the `IMAP_*` settings; the agent reads the inbox and the Junk Email folder. An "Inbox gap" names the dates to check by hand. |
| SPF, DKIM, DMARC | WARN for SPF and DMARC when a record is missing, SKIP when DNS could not be asked; DKIM is not checked | IT | Publish all three for the sending domain (section 4). |
| Sender name and company | WARN, "Sender identity set (not placeholder)" | Omega sales | `SENDER_NAME` and `COMPANY_NAME`. |
| Hot-lead alerts | WARN, "Hot-lead alerts" | Omega sales | `LEAD_ALERT_EMAIL`. |
| Profile as a link | WARN, "Profile sent as a link, not attached", while the PDF is attached | Omega sales | Omega's first email attaches nothing, so keep `ATTACH_PROFILE=false`; `PROFILE_URL` is linked only in a template that has `{{body}}`, and the shipped first email has none. |
| Warm-up | WARN, "Warm-up ramp" | IT | Keep the defaults: off, a first day above 10 or a daily rise above 5 warns. |
| Open tracking | WARN when on | IT | Keep `OPEN_TRACKING_ENABLED=false` (section 3). |
| AI processing agreement | WARN when an AI sees recipients' data without it | Omega, with counsel | `AI_DPA_REFERENCE`, only if an AI switch is on. |
| Retention schedule | WARN, "Retention run", until a run in the last two days | IT | The scheduled task of section 8. |
| Backups | WARN, "Backup", until a backup in the last two days | IT | The scheduled tasks of section 9. |
| Journal copy | not seen | IT | A second copy of `data\journal.jsonl` on another disk or a share (section 9). |
| Restore drill | not seen | IT | `py -3.11 main.py restore-drill` once by hand, ending in `Restore drill PASSED`, and the scheduled task every three months (section 9). |
| Incident plan | not seen | Omega, with IT | Read `docs/INCIDENT_PLAYBOOK.md`, fill in who is the operator and the privacy contact, and print it. |
| Always-on host | not seen | IT | A machine that stays on, and someone who starts the agent again after a reboot (F-39). |
| Dry run | not seen | Developer, with Omega sales | `py -3.11 main.py run --dry-run`, only once L1.1 is signed and L1.13 answered, as the counsel sheet proposes (L1.9). It emails no prospect, but it searches and stores contacts and, with `IMAP_ENABLED=true`, acts on the inbox and sends lead alerts. |
| Inbox placement test | not seen | Developer, with IT | The dashboard's Spam / inbox test card is disabled until counsel's country table. |

`doctor`'s other lines each name what is missing or what to do: a missing
`.env`, email template, Italian email template or competitor list;
nothing to search; a database it cannot open; no DNS answers; spam signals
in the template; retention switched off; a blank share capital. Once the
agent runs, they also report opt-outs held while the database was locked,
unsubscribe clicks kept that match no recorded link, a database log still to
be truncated, an erasure whose file steps have not all landed ("Erasures
finished", section 9), an overdue data-subject request (`main.py dsr
list`), and a log line that holds an address in clear.

## 11. Running the AI on your own server

The only AI the agent can use is a model on a server of Omega IFS's own: this
machine, or another machine on the company's private network. No outside AI
company receives any data. A `.env` that still holds one of the old cloud
AI settings, `GEMINI_API_KEY`, `GROQ_API_KEY` or `GROQ_MODEL`, stops
start-up with a message naming the line to delete, and
`LLM_PROVIDER=gemini` or `groq` is refused. The same goes for one of these
set in the environment: Docker's `env_file` copies `.env` into the
container's environment, so delete the line from `.env` and start the
container again. Leave `LLM_PROVIDER` blank to run
without AI. The server must speak the OpenAI chat interface, for example
Ollama serving Qwen. Ollama answers that interface at
`http://<host>:11434/v1`. Pull the model on that server first (for example
`ollama pull qwen2.5:14b`), then set in `.env`:

For Ollama on the same machine as the agent:

```
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen2.5:14b
```

For Ollama on another machine in the company network (its private address,
or a name that resolves only to private addresses):

```
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://192.168.1.20:11434/v1
LLM_MODEL=qwen2.5:14b
```

The address must point at this machine or the private network. The agent
looks up the host name and accepts it only when every address it gets back
is loopback (`127.x`, `::1`) or private (`10.x`, `172.16.x` to `172.31.x`,
`192.168.x`, or an IPv6 address in `fc00::/7`). A public address, a
link-local one (such as `169.254.169.254`, a cloud's metadata service), or a
multicast or unspecified one (`0.0.0.0`) stops start-up, over `http://` and
`https://` alike. A name that cannot be looked up at start-up (the AI machine
is off, or the name server is slow after a restart) does not stop the agent,
so opt-outs and bounces are still read; `doctor` reports it, and no AI
question is sent until the name resolves to an allowed address. An IP address
in `LLM_BASE_URL` avoids the lookup altogether. An address written
as one number or in hex counts as the IP address it stands for:
`http://134744072/v1` is 8.8.8.8 and is refused. The message never repeats
the address.

The host is looked up again before every AI question, so a DNS answer that
changes while the agent runs cannot send a prompt outside: the question goes
to an address that check found (the next one only when the first cannot be
reached), never to the name looked up a second time, and the server still
sees its own name. When any address is no longer private, the question is
not sent, the log says `nothing sent:
LLM_BASE_URL must point at this machine or the private network`, and that AI
use takes its path without AI. The agent never follows a redirect from the
server, never goes through a proxy named in the environment (`HTTP_PROXY`
and the like), and every question has a time limit. `LLM_API_KEY` is sent as
a Bearer token when set; Ollama needs none, and the key is never printed. `LLM_TIMEOUT_SECONDS` (default 30, from 5 to 300) is
how long one question may take in all, even from a server that keeps sending
a byte or a header line now and then; raise it if the model is slow to load.

`LLM_REASONING_EFFORT` (default `none`; also `low`, `medium`, `high`, or
blank) goes into each question as `reasoning_effort`. A model that reasons
first, such as Qwen 3.5, otherwise thinks at length before a one-word
answer; keep `none` unless there is a reason not to. Leave it blank (the
line `LLM_REASONING_EFFORT=` with nothing after it) only for a server that
refuses the field: nothing is then sent. Any other value stops start-up.
Only the answer itself is read, never the model's reasoning (Ollama returns
that in a separate field).

`LLM_MODEL` is the name the server knows. Ollama reads a name without a tag
as the `latest` tag (`qwen3.5` is `qwen3.5:latest`), and doctor's list check
reads the two as one name; `qwen3.5` does not stand for `qwen3.5:0.8b`.

On the same machine, write `127.0.0.1` rather than `localhost`. Both work,
but on the Windows laptop tested `localhost` is tried as the IPv6 `::1`
first, where Ollama does not listen, and each question lost about two
seconds there (one took 4.2 s in all, of which the server spent 1.9 s;
doctor's question took 2.5 s against 0.5 s).

The address stops at the API's root: an address ending in `/chat/completions`
stops start-up. `py -3.11 main.py doctor` then shows a line "AI server" with
the whole address and the model (as `[SKIP]`, since it only repeats the
settings; the line "AI generator (optional)" likewise says the server was not
contacted there), a warning there when the address's path does
not end in `/v1` (Ollama's own address, `http://<host>:11434`, lacks it), a
line "AI server stays on this machine or private network", which looks the
host up again and warns when any address it resolves to is outside, and
a line "AI server reachable", which asks the server once for its
list of models (no personal data; three seconds in all, whatever the server
sends or however it falls silent). It reads one of:

- `reachable, qwen2.5:14b is listed` - ready;
- `reachable, but qwen2.5:14b is not in its list (it lists: ...) - pull
  qwen2.5:14b on the server, or set LLM_MODEL to a name it lists`;
- `not reachable or not understood at ...`, followed by what to check: when
  nothing answered, `check that the model server is running and that its
  host, port and firewall let this machine reach it`; when the server
  answered 404, the path of `LLM_BASE_URL` and that for Ollama it ends in
  `/v1`; when something else answered, `check that LLM_BASE_URL is the model
  server's own address`.

Each warning of these two lines ends `until then every AI use falls back to
the path without AI`.

A listed model can still answer nothing in time, so a line "AI server
answers" follows: one fixed question with no personal data (reply with the
single word YES), sent exactly as the agent sends its questions - the same
address, model, key, reasoning setting and timeout, so it can take up to
`LLM_TIMEOUT_SECONDS`. It reads one of:

- `answered as asked in 0.5 s` - the model answers, and in that time;
- `answered in ... s, but not as asked: '...'` (the first 40 characters) -
  the model may not follow instructions well enough for this work;
- `no answer within 30 s (LLM_TIMEOUT_SECONDS) - try LLM_REASONING_EFFORT=none,
  a longer LLM_TIMEOUT_SECONDS, or a smaller or faster model`;
- `refused: HTTP 404 "model 'qwen2.5:14b' not found"` - the status and the
  server's own message, at most 120 characters; when that message mentions
  reasoning, the line adds that `LLM_REASONING_EFFORT` can be left blank, and
  when it is a 404 saying the model is not found, the same `pull ... or set
  LLM_MODEL ...` step as above;
- `not asked - ...` (as `[SKIP]`, never `[OK]`) when network checks are off
  or the list line did not reach the server.

The dashboard never asks this question. Each of these lines is at most a
`[WARN]`, never a refusal of live sending: when the model
cannot answer, every AI use falls back to the path without AI. `doctor` also
warns when `LLM_BASE_URL` is set while `LLM_PROVIDER` is blank, as the
address is then not used.

A server that hangs does not hold the agent. Each contact can cost up to
four AI questions (relevance, country, opening line, body), and each one
that gets no answer costs the whole `LLM_TIMEOUT_SECONDS`, during which the
inbox is not read and requests to stop wait. So after three questions in a
row end in a timeout or a failed connection, the agent asks the server
nothing for ten minutes and every AI use takes its path without AI at once.
The log then shows one warning (here for `http://192.168.1.20:11434/v1`;
the log masks every IP address as `[ip]`, as it masks personal data):

```
The AI server at [ip]:11434 gave no answer 3 times in a row: no AI question is asked for the next 10 minutes, and every AI use takes its path without AI
```

and, when the ten minutes are over and the next question is about to go,
one line:

```
The pause on the AI server at [ip]:11434 is over: asking it again (AI relevance classification)
```

With more than one model server, the port tells them apart in the log, or a
host name in `LLM_BASE_URL` (such as `http://ai-server:11434/v1`), which the
log shows as it is.

If that question gets no answer either, the pause starts again at once with
the same warning, its count going on (`gave no answer 4 times in a row`,
then 5, and so on, while the server stays silent); any answer ends the
count. An error answer (such as 404
or 500) comes back at once and does not count, and so does a question not
sent because the address is no longer private. `doctor`'s question is not
affected by the pause.

`AI_DPA_REFERENCE` must still be filled in: while it is blank, no question
that carries a recipient's data is asked. (With the AI only on Omega's own
server, whether this gate is still needed is the owner's decision; the code
keeps it until then.) For a
model on the company's own server with no outside processor, a plain note
such as `own server, no outside processor` is enough for the program; whether
that is the right record is for the company's legal adviser.

What was tested on 12 November 2026: Ollama 0.40.16 serving `qwen3.5:0.8b`
on a laptop without a graphics card, with the agent's own code asking every
kind of question it asks, on invented companies and replies in Italian and
English.

- With reasoning left on (no `reasoning_effort` sent), a one-word question
  got no answer in three minutes, three times in a row. With
  `LLM_REASONING_EFFORT=none` the same question took about a second, and
  every question of the run was answered: the yes/no and country questions
  in 1.5 to 4.8 s, the discovery queries in 13.8 s, the company list in
  3.9 s, the opening line in 7.2 s and the email body in 8.5 s.
- The agent asked at temperature 0.7, so the same question could get another
  answer each time, and this small model's verdicts flipped. Since 13
  November the three classifier questions (relevance, company country and
  reply interest) are asked at temperature 0, so a case asked again gets
  the same verdict; the writing
  questions keep the temperature they had. How this model answers the
  three classifier questions is now measured by the benchmark, not by the
  ad-hoc probe of 17 cases that stood here: see "A worked example" under
  "Choosing a model: the benchmark" below. Its company list
  held country names and pharmaceutical groups; three of its five discovery
  queries were dropped as supplier searches; its opening line was discarded
  by the length check; and its email body passed every check with the
  sender and the recipient's roles reversed, so it would have been sent.
  A larger model has to be chosen, and its answers checked on real examples
  in a dry run, before any AI switch is turned on.
- Ollama unloads a model after five idle minutes by default
  (`OLLAMA_KEEP_ALIVE`), so the first question after a pause is slower while
  the model loads again; allow for it in `LLM_TIMEOUT_SECONDS`.
- `doctor` showed, in turn: `answered as asked in 0.5 s`; with reasoning on
  and a 5-second timeout, `no answer within 5 s (LLM_TIMEOUT_SECONDS) - ...`;
  for a model the server does not have, `reachable, but no-such-model:1b is
  not in its list (...) - pull ...` and `refused: HTTP 404 "model
  'no-such-model:1b' not found" - pull ...` (each line names the step to
  take); with nothing listening, `not reachable or not understood at ...`
  and `not asked - the server did not answer the list row`.

Nothing in this first test says that any model is good enough for the
agent's work; the benchmark below is what judges one.

### Before the first benchmark or proof run

On a fresh server, in this order; each command below refuses to start
until the steps before it are done:

1. `py -3.11 -m pip install -r requirements.txt` (section 1).
2. A `.env` with `ERASURE_KEY` and `UNSUBSCRIBE_SECRET` filled in (section 1
   and section 6): `ai-benchmark` and `ai-proof` both stop with an
   `[ERROR]` line without them, even `ai-benchmark --check`.
3. Ollama installed on the model server, and the model pulled there
   (`ollama pull qwen3.5:0.8b`, or the model to be judged).
4. `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL` and `LLM_MODEL` in
   `.env`, as above.
5. `py -3.11 main.py doctor`: the lines "AI server reachable" and "AI server
   answers" should not warn.
6. `py -3.11 main.py ai-benchmark --check`, then `--quick`, then the full
   exam; and `py -3.11 main.py ai-proof` to see the whole agent use the
   model.

How long the full exam takes is the cases times the askings times the
seconds per answer. For `qwen3.5:4b-q4_K_M` on the laptop of the worked
example below (about 14 s a relevance answer with its second wording and
13 s with the third, 7 s a country answer, 8 s a
reply answer and 18 s for discovery queries), today's 148 relevance, 125
country and 124 reply cases asked twice took 2 hours 14 minutes, and the 15
query cases asked twice another 10 minutes; `qwen3.5:0.8b` took 42 minutes
for the same three questions. The default of three askings takes half as
long again, and a larger or slower model takes longer in proportion.

### Choosing a model: the benchmark

`py -3.11 main.py ai-benchmark` says whether the configured model answers the
agent's three classifier questions (relevance, company country, reply
interest) well enough on the shipped cases; a pass is needed before a
switch is turned on, but it is not enough by itself (see "A worked
example" below). Its discovery queries are checked for their form only. It asks the agent's own
questions, with the agent's own prompts, reasoning setting, timeout and
reading of the answer, about a fixed set of cases whose right answers are
known (`benchmark/cases`, one file per question), each case several times,
and scores the answers by what a wrong one costs here. It asks the model on
the company's own server, with the settings in `.env`. It writes nothing to the contacts database and sends no email.

The shipped cases are invented companies, people and replies (every address
ends in `.example`), so the exam runs even while `AI_DPA_REFERENCE` is blank;
that exception is the benchmark's alone and changes no setting. The
company's own cases (`--cases <folder>`, files named like the shipped ones)
may be real, and are asked only when `AI_DPA_REFERENCE` is set: their
domains and texts need not end in `.example` (until 13 November they had
to, which refused every real company). A `--cases` folder that does not
exist, or holds no file for the questions asked, is refused. `--quick`
takes its first cases of each kind from the shipped cases and from the
company's own alike.

```powershell
py -3.11 main.py ai-benchmark --check            # validate the case files; asks no AI
py -3.11 main.py ai-benchmark --quick            # a fixed small subset, as a first look
py -3.11 main.py ai-benchmark                    # the full exam, each case asked 3 times
py -3.11 main.py ai-benchmark --out bench-qwen   # also writes report.md and results.json
py -3.11 main.py ai-benchmark --models qwen2.5:14b,llama3.1:8b   # own server: compare models
py -3.11 main.py ai-benchmark --blind reply      # the cases without their answers, to check labels
py -3.11 main.py ai-benchmark --blind reply --out labels   # the same in a file, and its key apart
```

`--blind` shows each case as a neutral number (`reply-017`), its language and
its inputs only, in an order that does not follow the file: the case ids name
each case's kind (`rep-interested-01`), which gives the answer away, so they
are not shown, nor is the difficulty. With `--out` it writes
`blind-<question>.json` for the reader and `key-<question>.json`, which turns
each number back into its case id; keep the key from the reader. The same
files always give the same numbers.

`--questions relevance,reply` asks only those questions (relevance,
country, reply, queries); `--repeat N` asks each case N times (1 to 10;
consistency needs 2 or more). `--models` asks the same exam of each named
model on the configured server, one after another, then prints one
comparison table. A run takes about (cases x repeat x seconds per answer):
with a model answering in 2 s, 100 cases asked three times take ten minutes.

How to read it: one line per question, `PASS`, `FAIL`, `not enough cases`
(fewer than ten clear cases behind a measure: never a pass), `asked once:
use --repeat 2` (enough cases, but with `--repeat 1` consistency cannot be
measured: never a pass), `INCOMPLETE`, or for the discovery queries
`FORMAT OK` in place of `PASS` (see below);
then, per question, each measure as a count and a percentage ("3 of 75,
4%") beside its pass mark and the reason for that mark, with a plausible
range ("plausible 1% to 20%"): where the true share probably lies, counted
over the cases rather than the askings, since a case asked three times is
still one case. A verdict reads the share itself; a wide range says the
exam is too small to be sure. Beside each pass mark, "cases behind it" gives
the clear cases the measure rests on and the fewest wrong cases (and wrong
askings) that would fail the mark: "52 clear cases; 3 wrong cases fail it"
is a firmer PASS than "10 clear cases; 1 wrong case fails it". Then the seconds per answer (median and
slowest) and answers that came too late; the hard cases on a line of their
own (cases a careful person could read either way; they never count toward
the verdict); the share of clear askings answered right in each language
("it 40 of 45, 89%; pl 2 of 6, 33%"), since one share over all languages
hides a model that fails the agent's Polish or Greek companies; the five
cases answered worst; and every case answered
differently across its askings, which is where the prompt leaves the model
unsure. The header gives the temperatures asked and a fingerprint of the
cases: two reports with the same fingerprint examined the same cases. What
each question measures:

- **relevance**: the AI can only remove a company (a NO marks it off-target;
  a YES or no answer keeps it). So it counts real clients wrongly removed,
  non-clients caught (unrelated industries and non-European companies), and
  contract manufacturers (competitors) removed, on a line of their own.
- **country**: the answer chooses the email's language and is stored on the
  contact. So it counts the language decision (Italian or not, by
  `ITALIAN_COUNTRIES`), a country named where nothing shows one, and the
  exact country.
- **reply**: asked only about replies the wording rules could not decide; a
  YES raises an interest alert. So it counts interest missed and false
  alerts.
- **queries**: how many of the five proposals survive the agent's own
  filters. That is a check of form only: five lines of nonsense with no
  supplier word survive them, and nothing checks that a proposal names a
  sector, a kind of buyer and a European country. So this question never
  reads `PASS`; at its mark it reads `FORMAT OK`.
- every question: answered (a usable answer), consistency (cases given the
  same answer every time) and speed.

The pass marks, in `PASS_MARKS` at the top of `modules/ai_benchmark.py`, are
a proposal for Omega IFS to agree, and are changed there:

| Question | Measure | Pass mark |
| --- | --- | --- |
| relevance | real clients wrongly removed | at most 5% |
| relevance | non-clients caught | at least 60% |
| country | language decision right | at least 95% |
| country | named a country where nothing shows one | at most 10% |
| reply | interest missed | at most 10% |
| reply | false alerts | at most 20% |
| queries | proposals kept by the filters | at least 80% |
| every question | answered | at least 98% |
| relevance, country, reply | consistency | at least 90% |

A silent server is never scored as wrong answers. On the own server, an
address that does not answer or a model it does not list is named before
anything is asked; the run stops when the agent's own ten-minute pause
starts (three questions in a row without an answer) or when the first three
questions get no answer at all; the report then says `Run incomplete` and
why, and a question that timed out is left out of every measure but
"answered".

The command exits 1 when the case files are refused, the AI is not
configured, or any model's run stopped before its end (the server down,
the model not listed, the pause), and 0 when every model was examined,
whatever its verdicts: a script can tell a model that failed from one that
was never asked.

A result holds for one model, with one reasoning setting and one timeout:
run the benchmark again after changing any of them, or the server. `--check`
also counts the cases the agent settles before the AI is asked (a reply its
inbox files away as an auto-reply, a bounce or an opt-out, or its wording
rules decide; a company its relevance gate removes; a country its own page
shows), since in the agent those never reach the AI. Until 13 November it
missed the inbox's step, and 7 reply cases the agent never shows the AI (5
out-of-office replies whose subject says so, 2 delivery failures) counted
toward false alerts; the delivery failures are gone, and the out-of-office
replies now come back as `Re:` with the original subject, as those that
reach the AI do. A case's
domain is read as the agent would find it, without its `.example` ending:
until 13 November, 14 non-European companies with domains such as
`vidacerrado.com.br.example` passed that check although the agent removes
`vidacerrado.com.br` before the AI is asked; their domains now carry no
country code, and the page text shows where they are. That left a domain
with no code NO in 36 of 39 cases, so the domain alone gave a
non-European company away; 37 European companies' domains have since lost
their code too (49 of 119 now carry none), each showing in its page text
where it is. The shipped
files hold 148 relevance, 125 country, 124 reply and 15 discovery-query
cases, all invented, none settled before the AI is asked. On 13 November
2026 every relevance, country and reply case was answered by two readers
besides its writer, each from `--blind` sheets that showed a neutral number,
the language and the case's inputs, with no id, kind, difficulty or answer;
each said it opened nothing but its sheet. Both
readers gave the labelled answer for 390 of the 397 cases: 143 of 148
relevance, 123 of 125 country and all 124 reply cases. From what they
answered and noted, judged on each case's text alone, 3 labels were
corrected (an Istanbul brand and a European brand that makes everything
itself are now YES; a page with Austrian words and no place is now
UNKNOWN), 4 cases were rewritten so that the text decides (a pharmacy that
named no place, two Italian companies placed only by where a product is
made or its ingredients grow, and one in "Florence" with no country), 1
reply that hands the email to a colleague was moved to hard, and none was
dropped. Behind each pass mark of the three classifier questions stand at least 30
clear cases: 56 clear
clients and 55 clear non-clients, 36 clear companies with no evidence of a
country, 48 clear interested replies and 63 clear ones that are not; the
eleven clear query cases are one with no hint and ten that carry the
agent's own two kinds of hint. No two cases send the same question.

On 13 November 2026 the relevance and country questions were reworded
(the relevance question's second wording, replaced on 14 November; see
below), after the benchmark showed the first wording removing real clients and
naming countries the page does not show: asked of Qwen 3.5 4b at
temperature 0, on cases the new wording had never seen, real clients
wrongly removed went from 8 of 29 to 2 of 29, competitors caught from 4 of
10 to 7 of 10 and non-clients caught from 29 of 32 to 27 of 32, the
email-language decision was right for 65 of 68 instead of 59 of 68, and a
country was named where nothing shows one for 3 of 20 instead of 16 of 20.
These figures are from one model and say nothing sure about another; the
reply question was not changed.

On 14 November 2026 each classifier question was then asked about cases it
had not been written with, each case judged by two readers who did not see
the model's answers (Qwen 3.5 4b, each case once; only cases on which the
readers agreed and were sure are counted). The country question held: of
the relevance file's companies, 76 are ones the agent would ask about, the
readers agreed on 57, and the language decision was right for all 57, with
no country named for the 7 that show none (only 5 of the 57 are Italian).
The reply question held: of 79 new replies by another writer, that writer
and both readers agreed on 63, and no interest was missed of 23 and no
false alert raised of 40. The relevance question did not hold. Of the
country file's 125 companies the readers call 79 clients in Europe and 37
more clients whose page shows no place, and the model removed 29 of the 79
and 8 of the 37: mostly small brands that make their own products, in
their own lab, kitchen, farm or workshop, which it read as manufacturers,
and pages that say little. The shipped relevance cases hold only one such
company, and it is marked hard, so it does not count toward the verdict.
So the exam could pass while the model removed these clients.

The relevance question was therefore reworded a third time: NO only when
the page clearly shows a company outside Europe, a contract manufacturer
or an unrelated business, and YES otherwise. On the half of those 125
companies that had been kept unread, the model then removed 3 of 31
clients in Europe and 1 of 16 whose page shows no place, where the wording
before lost 13 of 31 and 3 of 16; but it filters less, as the worked
example below shows. A step-by-step wording was tried as well and removed
real clients as before (18 of 48 on the other half), so it was dropped.
Neither new wording made this model both keep these clients and filter
as much as the second wording did. With a model of this size, leave
`AI_RELEVANCE_ENABLED` off until the question has been tested on the
company's own cases: the wording errs toward YES because a company
wrongly kept gets the email it would have got without AI, while a company
wrongly removed is lost. The readers' relevance answers for the country
file's 125 companies, and the 79 new replies, are not yet part of the
shipped case files.

#### A worked example: two models on a laptop, 14 November 2026

What the benchmark said about two models of one family, one of 3.3 GB and
one of 1.3 GB, on today's files (cases fingerprint `9d7435081439` for the
three classifier questions, `eb029b1548a0` for the discovery queries):
Ollama 0.40.16 on a laptop without a graphics card (Intel Core Ultra 7
155U, 31 GB of memory) that did nothing else, reasoning `none`, classifier
questions at temperature 0, every case asked twice. `LLM_TIMEOUT_SECONDS`
was 60, and 180 for the larger model's discovery queries, so that one slow
answer could not stop an unattended run; the slowest answer took 20.7 s
(in the first run of the relevance question, with its second wording;
15.5 s with the third) and 23.3 s for a discovery query, so the default
30 would have held. The relevance question was asked again by itself
after its third wording (cases fingerprint `76bb5f4eddfb` for that
question alone), and the relevance rows below are from that run.

```powershell
$env:LLM_PROVIDER="openai_compatible"; $env:LLM_BASE_URL="http://127.0.0.1:11434/v1"
$env:LLM_MODEL="qwen3.5:4b-q4_K_M"; $env:LLM_TIMEOUT_SECONDS="60"
py -3.11 main.py ai-benchmark --questions relevance,country,reply --repeat 2 --out bench-qwen-4b
py -3.11 main.py ai-benchmark --questions relevance --repeat 2 --out bench-qwen-4b-relevance
$env:LLM_TIMEOUT_SECONDS="180"
py -3.11 main.py ai-benchmark --questions queries --repeat 2 --out bench-qwen-4b-queries
```

`qwen3.5:4b-q4_K_M`, 2 hours 14 minutes for the three classifier questions,
10 minutes for the queries and 63 minutes for the relevance question
alone:

| Question | Verdict | Measures | Median s | Slowest s |
| --- | --- | --- | --- | --- |
| relevance | PASS | real clients wrongly removed 0 of 112 askings, 0% (plausible 0% to 6%); non-clients caught 78 of 110, 71% (plausible 58% to 81%); competitors removed 26 of 48, 54% | 12.6 | 15.5 |
| country | PASS | language decision right 222 of 228, 97% (plausible 93% to 99%); named a country where nothing shows one 6 of 72, 8% (plausible 3% to 22%); exact country right 210 of 228, 92% | 6.6 | 8.3 |
| reply | PASS | interest missed 0 of 96, 0% (plausible 0% to 7%); false alerts 0 of 126, 0% (plausible 0% to 6%) | 8.3 | 9.8 |
| queries | FORMAT OK | proposals kept 109 of 110, 99% (plausible 73% to 100%) | 18.1 | 23.3 |

`qwen3.5:0.8b`, 42 minutes, 5 minutes and 22 minutes:

| Question | Verdict | Measures | Median s | Slowest s |
| --- | --- | --- | --- | --- |
| relevance | FAIL | real clients wrongly removed 89 of 112 askings, 79% (plausible 67% to 88%); non-clients caught 96 of 110, 87%; competitors removed 26 of 48, 54% | 4.3 | 5.5 |
| country | FAIL | language decision right 100 of 228, 44% (plausible 35% to 53%); named a country where nothing shows one 72 of 72, 100% | 2.3 | 2.8 |
| reply | FAIL | false alerts 98 of 126, 78% (plausible 66% to 86%); interest missed 0 of 96, 0% | 2.8 | 3.9 |
| queries | FAIL | proposals kept 73 of 110, 66% (plausible 38% to 87%); answered 20 of 22, 91% | 8.9 | 17.0 |

Both models answered every classifier question every time. The larger
model passes the three classifier questions, the country question with
little to spare: it named a country its page does not show on 6 askings
where 8 fail the mark. With its second wording the relevance question had
passed as well (real clients wrongly removed 3 of 112, non-clients caught
96 of 110, competitors removed 34 of 48); it was the outside check
described above, not the exam, that showed that wording removing real
clients. With the third wording the model removes no clear client of the
exam, catches seven in ten non-clients and removes about half of the
contract manufacturers, which is reported only. The agent's own lists
and keyword screen do not catch every competitor either: in the rehearsal
below, two contract manufacturers passed the agent's rules, the AI kept
them, and they would have been emailed. The smaller model fails every
question: it removes four in five real clients, names a country for every
company whose page shows none, and raises an alert for four in five
replies that show no interest. These are single runs on one laptop, on
invented cases, and the pass marks are a proposal: run the benchmark on
each model being considered, and on the company's own cases, before a
switch is turned on; and a pass here is not yet a pass on real companies,
as the outside check showed.

### Seeing the whole agent use the model

The benchmark asks the AI's questions one at a time. `py -3.11 main.py
ai-proof` shows the agent itself working with the configured model, end to
end, and sends nothing:

```powershell
py -3.11 main.py ai-proof                                 # 12 companies, 8 replies
py -3.11 main.py ai-proof --companies 4 --replies 2       # a smaller batch
py -3.11 main.py ai-proof --out proof-qwen                # also writes proof.txt and agent.log
```

It builds the agent as `main.py run` does, from `.env`, and takes a few of
the benchmark's invented companies and replies (clear cases only, every
kind in turn, the same ones every time). It stores each company as the web
search stores what it finds, then runs them through the send loop's own
step: the rules (client and competitor lists, lead score, relevance gate),
the AI relevance question, the company-country question, and a dry-run
first email whose language the agent chooses. Each reply goes first
through the inbox's filing, as in the agent: one it files away as an
auto-reply or a bounce is never judged and alerts nobody, and its line says
`filed by the inbox as ..., the AI is not asked`. The others go through the
agent's own judgement of a reply: its wording rules first, the AI when they
cannot tell. The three AI switches are on for this run only.

It prints one line per company (what the rules decided, what the AI
answered, the outcome: emailed in Italian or English, or removed and why,
the seconds, and whether that is the case's right answer) and one per reply
(the AI's answer, and whether a salesperson would be alerted), then the
totals: companies kept and removed, alerts, AI questions asked and
answered, the median and slowest answer, how many outcomes agree with the
cases' right answers, and that no email was sent and nothing but the AI
server was contacted.

What it does not touch, and what it changes for itself:

- It works in a new folder with a database of its own, removed at the end
  (`--work-dir` names a folder that must not exist yet, in a folder that
  does), even when the
  log cannot be copied to `--out`; an `--out` that cannot be made a folder
  (an existing file, say) is refused before the run. `DB_PATH` is never
  read, and the agent's real database is never opened. The agent's log for
  the run stays in that folder, or goes to `--out`; nothing else is written.
- Dry run; no pause between emails and no send window, since nothing is
  sent; no MX lookup of the addresses and no blocklist check of the sending
  domain, since both ask DNS and the addresses are invented; addresses whose
  domain shows no country count as allowed, so the country question is
  asked of the invented `.example` addresses. With `AI_DPA_REFERENCE` blank
  the AI is asked anyway, as the benchmark is, since every case is invented.
  The output lists each of these. No stored setting changes.
- The web search, the inbox (IMAP) and the lead alert's email (SMTP) are the
  agent's other network steps; they are not part of the run. While it runs,
  every outgoing connection but the AI server's is refused, and one attempt,
  even one the code swallows, fails the run with what was attempted.
- It stops, saying why and asking nothing, when the AI is not configured or
  the own server does not answer or list the model; and it stops when the
  agent's own ten-minute pause starts (three questions in a row without an
  answer). It exits 1 then, and 0 when every case went through.

It proves the plumbing and shows the model's answers on a few invented
cases. A dozen cases say little about how often a model is wrong: the
benchmark is what judges a model.

On 14 November 2026, with `qwen3.5:4b-q4_K_M` on the laptop of the worked
example above (`--companies 30 --replies 20`, `LLM_TIMEOUT_SECONDS=60`),
the agent asked 74 AI questions and got 74 answers (median 8.2 s, slowest
13.2 s) in under twelve minutes. Of the 30 companies it would have emailed
24, seven in Italian and 17 in English, and the AI removed 6; of the 20
replies, a salesperson would have been alerted for the four that show
interest. 46 of the 50 outcomes agreed with the cases' right answers: two
companies outside Europe and two contract manufacturers were kept, as the
third wording of the relevance question lets some through. No email was
sent and nothing but the AI server was contacted. A watcher
outside the program, which looked at the run's processes about four
times a second (3,086 looks), saw connections to `127.0.0.1:11434` and nowhere else; no file in
the agent's folder was added, removed or changed, and the work folder was
gone at the end. The plumbing works with that model, and its answers were
the benchmark's.

## 12. Connect to Microsoft 365 without a password

**Why.** Exchange Online already refuses a password for IMAP (F-17), and
password sign-in for SMTP AUTH is switched off by default at the end of
December 2026 (F-18; Microsoft revised its timeline on 27 January 2026,
and a refused password login answers `550 5.7.30`). More important: a mailbox password kept on this
machine is the main way a thief could send spam or phishing from Omega IFS's
domain. With Microsoft sign-in, no mailbox password exists on this machine.
The agent signs in as an app registered in Omega's Microsoft Entra, with a
certificate whose private key never leaves this machine (OAuth 2.0 "client
credentials", then SASL XOAUTH2 for IMAP and SMTP).

**What the agent does.** It signs a short request (valid 5 minutes, never
reused) with the private key, sends it only to
`login.microsoftonline.com` (certificate checked, no redirect, no proxy,
30-second limit), and gets an access token for Exchange Online, valid about
an hour. It keeps the token in memory, never on disk and never in a log, and
asks for a new one 5 minutes before it runs out. Every Microsoft 365 sending
mailbox (the main one, `SENDER_2_EMAIL` to `SENDER_5_EMAIL`, and the
`SENDER_ACCOUNTS` entries on `smtp.office365.com`) uses the same app, each
under its own address. A mailbox on another provider keeps its password.

**What it refuses.** While Microsoft sign-in is set up, the agent does not
start if any of these is true, and says which settings to fix (all of them
at once, never their values):

- a password is still set for a Microsoft 365 mailbox (`SMTP_PASSWORD`,
  `IMAP_PASSWORD`, a `SENDER_n_PASSWORD`, or a password in a
  `SENDER_ACCOUNTS` entry on any Microsoft mail server, such as
  `smtp.office365.com`, `outlook.office365.com` or `smtp-mail.outlook.com`);
- a `SENDER_ACCOUNTS` entry on a Microsoft mail server does not send through
  `smtp.office365.com` port 587 or (with `IMAP_ENABLED=true`) read from
  `outlook.office365.com` port 993;
- the tenant id is not a GUID or a domain, or the client id is not a GUID;
- `SMTP_HOST` is not `smtp.office365.com` on port 587, or (with
  `IMAP_ENABLED=true`) `IMAP_HOST` is not `outlook.office365.com` on port
  993;
- the certificate file is missing, cannot be read, holds no certificate or a
  certificate that is not the key's, holds a key that is not RSA of 2048 bits
  or more, needs a passphrase that is missing or wrong, or (on Linux and in
  Docker) can be read by other accounts.

If Microsoft refuses the sign-in, or a mailbox refuses the token, sending
stops exactly as for a wrong password: the batch halts, the contact stays
pending, and nothing more is tried until the agent is restarted. If Microsoft
cannot be reached, the send counts as an ordinary failed send and is retried
later.

### Step 1. Make the certificate (on the agent's machine)

Make it on the machine that runs the agent, so the private key is never
copied anywhere. Git for Windows includes `openssl`. In the agent's folder:

```powershell
openssl req -x509 -newkey rsa:3072 -sha256 -days 730 -noenc `
  -subj "/CN=Omega outreach agent" `
  -keyout agent-key.pem -out agent-cert.pem
Get-Content agent-key.pem, agent-cert.pem | Set-Content -Encoding ascii data\microsoft-signin.pem
Remove-Item agent-key.pem
```

- `agent-cert.pem` is the public certificate: it is what you upload in
  step 3. It is not secret.
- `data\microsoft-signin.pem` holds the private key and the certificate. It
  is the secret. It is valid for 2 years (`-days 730`).
- To protect the key with a passphrase, leave out `-noenc` (openssl asks
  for one) and put it in `MICROSOFT_CERTIFICATE_PASSPHRASE`. This helps only
  where the key file could be copied without `.env`, for example in a
  backup.
- `New-SelfSignedCertificate` (PowerShell) also makes a certificate, but it
  keeps the key in the Windows certificate store, and the agent reads only a
  PEM file. Use the `openssl` commands above.

Let only the account that runs the agent read the key file:

- Windows: `icacls data\microsoft-signin.pem /inheritance:r /grant:r "$($env:USERNAME):(R)" "*S-1-5-32-544:(F)"`
  (run as the account that runs the agent; `*S-1-5-32-544` is the
  Administrators group in any Windows language).
- Linux or Docker: `chmod 600 data/microsoft-signin.pem`, and in Docker also
  `sudo chown 10001 data/microsoft-signin.pem` (the container's user). The
  agent refuses a key file that other accounts can read.

`.gitignore` and `.dockerignore` leave out every `*.pem`, `*.key`, `*.pfx`
and `*.p12` file, so the key is never committed or built into an image. In
Docker it reaches the container through the `data/` folder.

### Step 2. Register the app in Microsoft Entra

1. In the Microsoft Entra admin center, open **App registrations** and choose
   **New registration**.
2. Name it, for example "Omega outreach agent". Choose **Accounts in this
   organizational directory only**. Leave the redirect URI blank.
3. On its **Overview** page, note the **Application (client) ID** and the
   **Directory (tenant) ID**.

### Step 3. Upload the certificate

In the app registration, open **Certificates & secrets**, then
**Certificates**, then **Upload certificate**, and choose `agent-cert.pem`.
Do not add a client secret: the agent does not use one, and a secret is a
password again.

### Step 4. Give the app the two mail permissions

1. In the app registration, open **API permissions**, then **Add a
   permission**, then the **APIs my organization uses** tab, and search for
   **Office 365 Exchange Online**.
2. Choose **Application permissions**, tick **IMAP.AccessAsApp** and
   **SMTP.SendAsApp**, and choose **Add permissions**.
3. Choose **Grant admin consent for** the organisation.

Do not add Microsoft Graph mail permissions (`Mail.Read`, `Mail.Send`, ...)
or `EWS.AccessAsApp`: the agent does not need them, and they would reach
every mailbox.

### Step 5. Register the app in Exchange Online

In PowerShell, as an Exchange administrator:

```powershell
Install-Module -Name ExchangeOnlineManagement
Import-Module ExchangeOnlineManagement
Connect-ExchangeOnline -Organization <Directory (tenant) ID>
New-ServicePrincipal -AppId <Application (client) ID> -ObjectId <Object ID> -DisplayName "Omega outreach agent"
```

The `<Object ID>` is the one on the **Enterprise applications** page for the
app (Microsoft Entra, Enterprise applications, the app, Overview). It is
**not** the Object ID on the App registrations page. With the wrong one,
every sign-in fails.

### Step 6. Give the app the outreach mailbox, and nothing else

For each mailbox the agent sends from (and only those):

```powershell
$sp = Get-ServicePrincipal -Identity "Omega outreach agent"
Add-MailboxPermission -Identity "outreach@example.com" -User $sp.Identity -AccessRights FullAccess
```

How the app is kept to these mailboxes: `IMAP.AccessAsApp` and
`SMTP.SendAsApp` open no mailbox by themselves. Over IMAP and SMTP the app
reaches only the mailboxes given to it with `Add-MailboxPermission`, so give
it the outreach mailboxes and no other. Microsoft's mailbox scoping tool,
RBAC for Applications (which replaces Application Access Policies), is not
used here: its page lists Microsoft Graph and EWS as its supported
protocols, and although its role list has an `Application SMTP.SendAsApp`
role, it has none for IMAP, so it cannot replace the mailbox grant for this
agent. Do not give the app any RBAC for Applications role either: such a
role is added on top of the grants above, and one with a wide scope would
open more mailboxes. Check that none exists with
`Test-ServicePrincipalAuthorization -Identity "Omega outreach agent"` (it
should list no role). Check which mailboxes the app can open with
`Get-MailboxPermission -Identity "outreach@example.com"` on each mailbox,
and keep the list in IT's records.

Each mailbox sends as itself, so no `SendAs` right is needed. Only if a
mailbox ever had to send as another address would that address need
`Add-RecipientPermission -Identity "<that address>" -Trustee $sp.Identity -AccessRights SendAs`.

### Step 7. Allow SMTP AUTH on the outreach mailbox only

Microsoft needs SMTP AUTH switched on for the mailbox even when it signs in
with a token. Microsoft recommends keeping it off for the organisation and
on only for the mailboxes that need it:

```powershell
Set-CASMailbox -Identity "outreach@example.com" -SmtpClientAuthenticationDisabled $false
Get-CASMailbox -Identity "outreach@example.com" | Format-List SmtpClientAuthenticationDisabled
```

The second line must show `False`. Microsoft's page notes that while the
tenant has **security defaults** on, SMTP AUTH is disabled. With
`IMAP_ENABLED=true`, IMAP must also be on for the mailbox (it is, unless
someone switched it off: `Set-CASMailbox -Identity "outreach@example.com" -ImapEnabled $true`).

### Step 8. Set the agent up

In `.env` (the names are listed in `docs/SETTINGS.md`):

```
SMTP_HOST=smtp.office365.com
SMTP_PORT=587
SMTP_USERNAME=outreach@example.com
SMTP_PASSWORD=
IMAP_HOST=outlook.office365.com
IMAP_PORT=993
IMAP_PASSWORD=
MICROSOFT_TENANT_ID=<Directory (tenant) ID>
MICROSOFT_CLIENT_ID=<Application (client) ID>
MICROSOFT_CERTIFICATE_PATH=data/microsoft-signin.pem
```

Delete every mailbox password for these mailboxes from `.env`: the agent
does not start while one is there. If that password was ever used by the
agent, change it in Microsoft 365, since copies of `.env` may exist.

### Step 9. Check it

```powershell
py -3.11 main.py doctor --smtp
```

- **Microsoft sign-in**: the settings are usable, and the certificate's end
  date. It warns 30 days before the certificate ends.
- **Microsoft sign-in (live)** (only with `--smtp`): gets a token, then signs
  in to each Microsoft 365 inbox over IMAP and sends a NOOP.
- **SMTP login (live)** (only with `--smtp`): signs in to SMTP with the token.

What the common failures mean:

| `doctor` says | Cause | Fix |
| --- | --- | --- |
| Microsoft does not know this app in this tenant (AADSTS700016) | Wrong client id or tenant id | Copy both again from the app's Overview page. |
| Microsoft refused the app's credential (AADSTS7000215) | The certificate is not uploaded, or a secret was expected | Step 3. |
| Microsoft could not check the signature (AADSTS700027) | The uploaded certificate is not the one in the key file, or it expired | Upload `agent-cert.pem` again (step 3), or renew (below). |
| The signed request is outside its valid time (AADSTS700024) | This computer's clock is wrong | Set the clock to automatic time. |
| The mailbox refused it over IMAP / SMTP | The app has no access to the mailbox, consent is missing, or the Object ID was the wrong one | Steps 4, 5 and 6. A new permission can take a while to reach Exchange. |
| SMTP AUTH is switched off (5.7.139) | SMTP AUTH is off for the mailbox or the tenant | Step 7. |

### Renewing the certificate (every 2 years)

Put a reminder in IT's calendar for 30 days before the end date `doctor`
shows. Then: make a new certificate (step 1, to a new file), upload its
`agent-cert.pem` next to the old one (step 3), replace
`data\microsoft-signin.pem` with the new file, and run
`py -3.11 main.py doctor --smtp`. The agent reads the file each time it asks
for a token, so it uses the new one within the hour; restart it to be sure.
When `doctor` passes, delete the old certificate in Entra.

**If the key file may have been copied**, delete its certificate in Entra
(Certificates & secrets) at once: that stops every new sign-in with it. A
token already given out keeps working until it runs out, about an hour, so
also close the mailbox to the app (`docs/INCIDENT_PLAYBOOK.md`, incident 1).
Then make and upload a new one.

### Microsoft's documents used

- Authenticate an IMAP, POP or SMTP connection using OAuth:
  https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth
- Certificate credentials (the signed request):
  https://learn.microsoft.com/en-us/entra/identity-platform/certificate-credentials
- OAuth 2.0 client credentials flow:
  https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
- Enable or disable SMTP AUTH in Exchange Online:
  https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission
- Role Based Access Control for Applications in Exchange Online:
  https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac
- Exchange Online to retire Basic auth for Client Submission (SMTP AUTH),
  with its 27 January 2026 update:
  https://techcommunity.microsoft.com/blog/exchange/exchange-online-to-retire-basic-auth-for-client-submission-smtp-auth/4114750
- Updated Exchange Online SMTP AUTH Basic Authentication Deprecation
  Timeline (password SMTP AUTH off by default for existing tenants at the end
  of December 2026; the final removal date to be announced in the second
  half of 2027):
  https://techcommunity.microsoft.com/blog/exchange/updated-exchange-online-smtp-auth-basic-authentication-deprecation-timeline/4489835

## 13. Encrypting the client and competitor lists (`LISTS_KEY`)

The client list and the competitor list name Omega IFS's customers and rivals.
As plain workbooks, anyone who gets a copy of the install folder can open
them: a backup, a zip sent to a colleague, a folder synced to the cloud.
With `LISTS_KEY` set, each list is kept on disk only encrypted.

**What it protects:** a copy of the folder, or of a list file, without the
key. Whoever has it sees neither list.

**What it does not protect:** someone who has the whole disk, or the folder
together with `.env`, which holds the key. That is the job of disk
encryption (BitLocker on the agent's computer, set up by Omega's IT). It
does not protect the list while the agent runs either: the agent holds it
in memory, as it must to screen with it. And it covers the two list files
only: when the agent skips a contact because its company is on a list, the
database, its backups and the log record which client or competitor it
matched, so those name the companies that were matched (they are protected
by the folder's permissions and by disk encryption, as before).

### How it works

- The agent reads an encrypted list into memory only, and never writes a
  plain copy to disk. The dashboard's **Mark as client** adds the company
  in memory and writes the list back encrypted; the dated backup it keeps
  beside the list is the encrypted file as it was.
- With `LISTS_KEY` set, a plain list is refused. A list that cannot be
  opened, because the key is wrong or the file was changed or damaged, is
  refused too. Each is treated like a missing list: live sending stops
  (`CLIENT_LIST_MISSING`, `COMPETITOR_LIST_MISSING` in the activity log),
  `doctor` fails, and the reason is written out. It is never read as an
  empty list, which would let the agent email Omega IFS's customers. Dry runs
  still work.
- With `LISTS_KEY` blank, the lists are plain files, as before, and `doctor`
  warns ("Lists encrypted").
- The encryption is Fernet from the `cryptography` package (AES with an
  HMAC-SHA256 check). The check is why a wrong key or one changed byte is
  refused instead of being read as something else.

### Set it up

1. Make the key and put it in `.env`:

   ```powershell
   py -3.11 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   `LISTS_KEY=...`, at least 32 characters, or every command refuses to
   start.
2. **Back the key up apart from the data** (for example in Omega IFS's
   password manager, next to `ERASURE_KEY`), not in the same backup as the
   folder. A backup that holds the key and the lists together protects
   nothing.
3. Encrypt each list:

   ```powershell
   py -3.11 main.py lists encrypt "data\clients.xlsx"
   ```

   It writes `data\clients.xlsx.enc`, checks that it decrypts
   to exactly the same file, and says which line of `.env` to change. Set
   `CLIENT_LIST_PATH` (and `COMPETITOR_LIST_PATH` for the competitor list)
   to the `.enc` file.
4. Delete the plain workbook. The command does not delete it unless you add
   `--delete-plain`, which overwrites the file with zeros first and then
   deletes it. On an SSD, in a synced folder or with Windows' file history,
   an old copy can survive the overwrite; BitLocker covers that. The command
   also names any dated backups of the plain workbook beside it
   (`<name>.backup-<date>.xlsx`): delete them too. Copies elsewhere (e-mail
   attachments, Downloads, a cloud folder) are not seen; delete them by hand.
5. Run `py -3.11 main.py lists check`. It shows each list as encrypted, with
   the number of companies it screens, and names any plain copy still beside
   it. It exits 1 while the agent would refuse a list. Then run
   `py -3.11 main.py doctor`: "Client list file", "Competitor list file" and
   "Lists encrypted" must be `[OK]`.

### Changing a list by hand

The agent never writes a plain copy, but a person editing the list needs
one, briefly:

```powershell
py -3.11 main.py lists decrypt "data\clients.xlsx.enc" --out "data\clients.xlsx"
# edit it in Excel, save, close Excel
move "data\clients.xlsx.enc" "data\clients.xlsx.enc.old"
py -3.11 main.py lists encrypt "data\clients.xlsx" --delete-plain
del "data\clients.xlsx.enc.old"
```

`decrypt` writes only to the file `--out` names, refuses to overwrite one,
and warns that the copy is plain. `encrypt` refuses to overwrite an `.enc`
file, because it may hold clients marked from the dashboard since: move it
aside first, as above. A running agent keeps screening with the list it
read last until the new `.enc` file is in place, then reads that. Excel may
keep its own AutoRecover copy of an open workbook; close it normally.

### If the key is lost or must change

- **Lost:** the encrypted lists cannot be read by anyone, the agent
  included. Live sending stops until they are back. Make a new key, get the
  original workbooks again from Omega IFS sales, and encrypt them with the new
  key (step 3). Companies marked as clients from the dashboard since the
  workbook was last exported are in the encrypted file only: ask sales to
  check the list, and mark them again. Their contacts already marked stay
  `active_client` in the database and are not emailed.
- **Leaked with a copy of the lists:** treat it as a data leak (incident 3
  in `docs/INCIDENT_PLAYBOOK.md`). Make a new key: decrypt each list with
  the old key (`lists decrypt ... --out`), put the new key in `.env`, and
  encrypt each list again.
- The key never appears in the log, the screen or `doctor`'s output.

## 14. Security updates of the Python packages

The agent runs on about 35 Python packages (`requirements.txt` and what
they need in turn). Now and then a security fix is published for one of
them. `requirements.txt` sets the lowest version of each that has no known
vulnerability on the day it was last checked; that protects a new install,
but not one that is already running.

### The weekly check (`main.py security-check`)

```powershell
py -3.11 main.py security-check
```

It asks pip-audit which packages installed for the Python that runs it have
a known vulnerability. Run it with the Python that runs the agent: `py
-3.11` on an install set up as in section 1, and
`.venv\Scripts\python.exe` once an update below has given the install its
own virtual environment. It needs pip-audit installed for that Python
(`requirements-dev.txt`, which an update below installs) and access to
`pypi.org`. It **never installs or upgrades anything**. It prints each
finding with the version that fixes it, and writes to the activity log:

- **error** (shown under Recent Errors on the dashboard): a package the
  agent uses has a known vulnerability. Update it, as below.
- **warning**: only a tool has one (pip, setuptools, the test packages).
  The agent does not use these while it runs; update them at the next
  update anyway.
- **error** too when the check could not run (pip-audit missing, no
  network, no package looked up), and when pip-audit could not look up a
  package the agent uses: a check that fails quietly would look like a
  clean one.
- **success**: nothing known.

It exits 1 on an error, so a scheduled task shows that it failed.

**Scheduling it (IT, once).** In Task Scheduler, create a task:

1. *General*: run as the account that runs the agent, "Run whether user is
   logged on or not".
2. *Triggers*: weekly, for example Monday 07:00.
3. *Actions*: Start a program. Program: `py`. Arguments:
   `-3.11 main.py security-check`. Start in: the install folder. With the
   install's own virtual environment, Program is
   `C:\Omega\agent\.venv\Scripts\python.exe` (the install folder's) and
   Arguments `main.py security-check`.
4. *Settings*: "Stop the task if it runs longer than" 1 hour.

Or from an administrator command prompt, with the install folder in place
of `C:\Omega\agent` and the agent's account in place of `<account>`
(schtasks asks for its password; with the install's own virtual
environment, put `.venv\Scripts\python.exe` in place of `py -3.11`):

```powershell
schtasks /Create /TN "Omega security check" /SC WEEKLY /D MON /ST 07:00 /TR "cmd /c cd /d C:\Omega\agent && py -3.11 main.py security-check" /RU <account>
```

Look at the dashboard's Recent Errors on Monday, as for any other error.

### Updating: on a copy, never in place

Never run `pip install --upgrade` in the live install. A package that
breaks would stop the agent reading opt-outs and bounces until someone
notices. Update a copy, test it, then swap:

1. Stop the agent and the dashboard. Take a backup
   (`py -3.11 main.py backup`).
2. Copy the whole install folder, for example `C:\Omega\agent` to
   `C:\Omega\agent-new`, with `.env` and `data\`, but without `.venv` if
   there is one. In the copy, make a new virtual environment:
   `py -3.11 -m venv .venv`. The copy must have its own packages: an
   install set up as in section 1 has its packages in the one Python 3.11
   of this computer, so installing with `py -3.11` "in the copy" would
   upgrade the live install in place.
3. In the copy, install and update with the copy's own Python:
   `.venv\Scripts\python.exe -m pip install --upgrade pip setuptools`, then
   everything to the newest versions `requirements.txt` allows:
   `.venv\Scripts\python.exe -m pip install --upgrade -r requirements.txt -r requirements-dev.txt`.
   If the fixed version the check printed is above a
   package's upper limit in `requirements.txt` (for example
   `cryptography<51.0`), stop and ask a developer: the limit is raised only
   after the tests pass on the new version.
4. In the copy, run the tests and the checks with the same Python:
   `.venv\Scripts\python.exe -m pytest -p no:cacheprovider`, then
   `.venv\Scripts\python.exe main.py doctor` and
   `.venv\Scripts\python.exe main.py security-check`.
   All must pass, the security check with no error.
5. Swap: rename the live folder (for example to `agent-old`) and the copy
   to the live name. From now on start everything with the install's own
   Python, `C:\Omega\agent\.venv\Scripts\python.exe main.py ...`, in place
   of `py -3.11 main.py ...`: the dashboard, the agent and every scheduled
   task, this check included. (It still works after the rename; only
   `.venv\Scripts\activate` and `pip.exe` remember the old folder name, so
   always use `python.exe -m pip`.) Start the dashboard and the agent. Keep
   `agent-old` until the agent has run a day without a new error; to go
   back, stop the agent, swap the names again and start it as before.
6. A developer raises the floor in `requirements.txt` to the fixed version,
   so a new install gets it too.

The agent must not run in both folders at once: the copy's tests use their
own temporary data and never send, but `main.py run` or the dashboard in
the copy would use the live mailbox.

Docker: rebuild the image with `docker compose build --pull` instead (it
installs `requirements.txt` fresh), run the tests in a copy as above, then
`docker compose up -d`. The image carries no pip-audit (it installs
`requirements.txt` only), so `security-check` inside the container records
the error "pip-audit is not installed" every time: with Docker, schedule the
rebuild instead, monthly at least.

## 15. Handing the agent and its data to Omega IFS

Omega gets a full copy of the agent: the program, the documents (this
guide and `docs/USER_MANUAL.md` among them) and its own data. The data is
the database (the contacts found, their status, every opt-out, bounce and
block, the erasure records, the data-subject requests and the history of
the emails sent), the journal beside it, the opt-outs held in
`data/spool/`, and the client and competitor lists, encrypted as they are.
All of it goes in **one encrypted file**, the handover archive.

Four things never go in:

1. every folder whose name contains "internal", and the whole
   `docs/review` folder (the owner's prices, deal and meeting notes, and
   internal security notes);
2. `.env` and every copy or variant of it (the owner's own passwords and
   keys; Omega makes its own `.env` from `.env.example`, which does go in);
3. `.git` (its history may hold files deleted since);
4. working files: `logs/`, `__pycache__` and the other caches, virtual
   environments, old backups and every other database file, and every key
   or certificate file (`*.pem`, `*.key`, `*.pfx`, `*.p12`, and the file
   `MICROSOFT_CERTIFICATE_PATH` names). Omega makes its own Microsoft
   certificate (section 12).

`ERASURE_KEY` and `LISTS_KEY` travel inside the archive, and nowhere else.
The database recognises the people who were erased or said no only with
the erasure key it was made with, and the lists open only with their key:
without them, people who said no could be emailed again. Neither key is
ever printed or written to a log.

### The owner: making the archive

1. Stop the agent and the dashboard, and switch off the scheduled tasks
   (backup, retention, security check) until the archive is made. All of
   them write to the database.
2. Run `py -3.11 main.py doctor`. "Journal replayed" and "Erasures
   finished" must pass, and "Held opt-outs" should read "none".
3. Run, from the install folder, with the archive's name outside the
   install folder:

   ```powershell
   py -3.11 scripts/make_handover.py D:\handover\omega-agent.handover
   ```

   It asks for a passphrase twice: at least 16 characters, for example five
   unrelated words. The passphrase is never stored or shown. Write it on
   paper until Omega IFS has opened the archive.
4. It checks that the agent and the dashboard are stopped and that the
   database is not behind its journal, takes a fresh checked backup of the
   database (the live file is never copied), and copies everything else but
   the four groups above. Before it encrypts anything it reads every file
   it packs, and **refuses** (and writes nothing) when it finds:
   - the value of one of this install's secret settings, anywhere;
   - a key, token, private key, or a password written into a document, a
     settings file or the code;
   - an address the database holds, or a name from the client or
     competitor list, in the code or the documents (client data belongs in
     the data folder only);
   - a spreadsheet outside the data folder.
   Each problem is shown as the file and the kind of problem, never the
   value. Move the file out of the install folder or remove the value, then
   run it again.
5. It writes the archive and, beside it, `open_handover.py`: the program
   that opens the archive on a computer that has no copy of the agent yet.
   It holds no data and no key. It also prints how many files, contacts
   and suppressed addresses went in, and how many files of each excluded
   group stayed out.
6. Held clicks on an unsubscribe link the database has no record of (after
   a restore from an older backup) are matched to their recipient only
   under this install's `UNSUBSCRIBE_SECRET`, which does not travel. So the
   archive carries each one that matches an address the database holds as
   that address's held opt-out, applied at Omega's first start, and says
   how many. If it prints a note about held clicks that match no address,
   ask the developer before handing over.

**Sending it.** Give Omega's IT the archive and `open_handover.py`, for
example on an encrypted USB stick or through a file share. **Give the
passphrase by phone**, never in the same email or message as the archive.
Once Omega has confirmed that `doctor` passes in its copy, delete every
copy of the archive: it holds every contact.

**After the handover, your copy stays stopped.** Two copies would not know
of each other's emails and opt-outs: an opt-out that reaches Omega's copy
is unknown to yours.

### Omega IFS's IT: opening the archive

1. On the agent's computer, with Python 3.11, install the one package the
   opener needs: `py -3.11 -m pip install "cryptography>=50.0,<51.0"`.
2. Open the archive into a new folder:

   ```powershell
   py -3.11 open_handover.py omega-agent.handover --into C:\Omega\agent
   ```

   With a copy of the agent already installed, `py -3.11 main.py
   handover-restore omega-agent.handover --into C:\Omega\agent` does the
   same.
3. It refuses a folder that already holds a `.env` or a database, or
   anything at all, before it asks for the passphrase: it never replaces an
   install. It then asks for the passphrase, checks the whole archive
   (a wrong passphrase, or one changed byte, and nothing is written: check
   the passphrase with the owner, then ask for the archive again), checks
   every file against the archive's own list, and unpacks.
4. It puts `ERASURE_KEY` and `LISTS_KEY` in the file `.env.handover-keys`
   in the new folder, readable only by the account that ran it, and never
   shows them. It checks that the erasure key is the one the database was
   made with.
5. It runs `doctor` in the new folder. Until the packages are installed and
   the new `.env` is complete, `doctor` lists what is missing; that is
   expected.
6. Then follow `README_FOR_SUPPLECO.md` in the new folder: give the folder the
   permissions of section 1, make the agent's own Python environment and
   install the packages, make `.env` from `.env.example`, copy the two
   lines from `.env.handover-keys` into it, **keep a copy of both keys
   apart from this computer, never change `ERASURE_KEY`**, delete
   `.env.handover-keys`, fill in the rest of `.env` with Omega's own
   values (a new `DASHBOARD_PASSWORD` and `UNSUBSCRIBE_SECRET`: links
   already sent keep working, as the database records them), and run
   `doctor` until it shows no `[FAIL]`. Then `main.py lists check`, a
   backup, the scheduled tasks (sections 8, 9 and 14), and a dry run before
   the first live email.
