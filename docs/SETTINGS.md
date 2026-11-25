# Settings reference — Omega IFS Outreach Agent

Every setting the agent reads, from `Settings` in `config.py`, with its
default and what it does. README and `docs/DEPLOY.md` point here as the
full list.

## How settings are read

- Settings come from `.env` in the project folder, one `NAME=value` per
  line. A variable set in the environment wins over `.env`; that is how
  Docker passes them (`env_file`). Names are not case-sensitive.
- A setting missing from `.env` takes the default below. `.env.example` is
  the template a new `.env` starts from: a setting it does not mention still
  takes its default.
- A value that fails a check below stops every command at start-up with a
  configuration error. So does a blank line for a number or a true/false
  setting, such as `FIRST_CONTACT_MAX_AGE_DAYS=`: delete the line instead to
  get the default.
- true/false settings also accept `yes` and `no`, `on` and `off`, `1` and `0`.
- A configuration error names the setting and what is wrong with it, never
  the value it holds, so a password typed under a misspelt name is not
  printed to the screen or the Docker log. A line whose name the agent does
  not know stops start-up too.

The columns:

- **Type**: text, whole number, number (decimals allowed) or true/false.
- **Default**: what the code uses when the setting is missing. _blank_ is
  empty.
- **Required**:
  - **Start**: commands refuse to start without it: every command while
    `UNSUBSCRIBE_SECRET` is the shipped value, and every command but
    `doctor` while `ERASURE_KEY` is blank.
  - **Live**: live sending is refused, and `doctor` fails, until it is set.
    Dry runs still work.
  - **Live (S.r.l., S.p.A.)**: the same, when `LEGAL_ENTITY_NAME` shows an
    S.r.l. or an S.p.A.
  - **Live (one of two)**: the opt-out route. Live sending needs
    `IMAP_ENABLED=true` or a public `https://` `BASE_URL`.
  - **Dashboard start**: the dashboard refuses to start without it,
    wherever it listens (this machine too), and `doctor` fails.
  - **Mailbox**: part of the sending mailbox, so nothing can be sent until
    it is right; `doctor` warns while the login is missing or a placeholder.
  - Empty: optional.
- **Dashboard**: _yes_ when the dashboard's settings panel can change it
  while the agent runs. The panel's value is saved in the database and
  overrides `.env` for every run, a single run included, until it is reset;
  a reset reaches a running agent.

`tests/test_settings_reference_is_complete.py` checks this list against
`config.py`. It fails for a setting with no row here, for a row naming a
setting the code does not have, for a row whose type, default or Dashboard
mark differs from the code, and for a legal field the code requires that is
not marked Live.

## Start-up secrets

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `UNSUBSCRIBE_SECRET` | text | `change-this-to-a-random-secret-key` | Start | | The key that signs every unsubscribe link. Every command refuses to start on the shipped value, which is public, so anyone could forge a link for any address, and on a value shorter than 32 characters, which could be guessed from one link. Make one with `py -3.15 -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `ERASURE_KEY` | text | _blank_ | Start | | Keys every erasure token. The database records the key it is first opened with and refuses any other. At least 32 characters: a new database refuses a shorter key, and on a database that already keeps one `doctor` fails until the data is started again with a long key (DEPLOY section 6). Make it once with `py -3.15 -c "import secrets; print(secrets.token_urlsafe(48))"`, back it up apart from the database and never change it (DEPLOY section 6). |

## Sending mailbox

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `SMTP_HOST` | text | `smtp.gmail.com` | Mailbox | | The outgoing mail server. Microsoft 365 is `smtp.office365.com`. |
| `SMTP_PORT` | whole number | `465` | Mailbox | | `465` connects over TLS; any other port, such as `587`, connects in plain and upgrades with STARTTLS. |
| `SMTP_USERNAME` | text | _blank_ | Mailbox | | The sending mailbox and its login. Mail goes out From and Reply-To this address, and the opt-out mailto is addressed to it. `doctor` warns while it is blank or a template placeholder. |
| `SMTP_PASSWORD` | text | _blank_ | Mailbox | | Its password, for a mailbox that signs in with one (SMTP AUTH). Must be blank for a Microsoft 365 mailbox while Microsoft sign-in is set up (below): the agent does not start otherwise. |
| `SENDER_NAME` | text | `Omega IFS` | | | The From name and `{{sender_name}}` in the templates: the first email's "My name is ...", and the follow-ups' signature. Set it to the person who sends. |
| `SENDER_TITLE` | text | _blank_ | Live | | What the sender is responsible for, as `{{sender_title}}`: the first email says "I am responsible for ... at Omega IFS" and "mi occupo di ... in Omega IFS", so an area such as `Business Development` reads best. While the first-email templates use it, a live first email through a mailbox without a title is refused (the contact stays pending, and a retry waits for a titled mailbox), and `doctor` fails naming the mailboxes without one. A dry run shows `[set SENDER_TITLE]`. Extra mailboxes without a title of their own take this one. |
| `SENDER_SIGNATURE` | text | _blank_ | | | The signature under the first email (`{{sender_signature}}`), its lines separated by the two characters `\n`. Blank, it is the sender's name, the title and `COMPANY_NAME`, one per line. |
| `COMPANY_NAME` | text | `Omega IFS` | | | `{{company_name}}` in the templates and `{company_name}` in `EMAIL_SUBJECT`, and the last line of a blank `SENDER_SIGNATURE`. |

## Microsoft 365 sign-in without a password

Set all three of the first settings and every mailbox on
`smtp.office365.com` signs in to SMTP and IMAP with a token from Microsoft
(OAuth 2.0 with a certificate, then XOAUTH2), and no password may be kept
for it: `SMTP_PASSWORD`, `IMAP_PASSWORD`, the `SENDER_n_PASSWORD` slots and
the passwords in such `SENDER_ACCOUNTS` entries must be blank, or the agent
does not start, naming each one. `SMTP_HOST` must then be
`smtp.office365.com` with `SMTP_PORT=587`, and, with `IMAP_ENABLED=true`,
`IMAP_HOST` `outlook.office365.com` with `IMAP_PORT=993`. A
`SENDER_ACCOUNTS` entry on any Microsoft mail server (a name ending in
`office365.com`, `outlook.com` or `office.com`, for sending or for reading)
counts as a Microsoft mailbox too: it may keep no password, and must use
`smtp.office365.com` port 587 (and `outlook.office365.com` port 993 for
IMAP), so leave out its own hosts and ports. A mailbox on another provider
keeps its password. All blank: Microsoft sign-in is off.
How Omega IFS's IT sets up the app: DEPLOY section 12.

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `MICROSOFT_TENANT_ID` | text | _blank_ | | | The Directory (tenant) ID from the app registration's Overview page (a GUID), or Omega's verified domain in Entra (its `.onmicrosoft.com` name or a domain it has verified). |
| `MICROSOFT_CLIENT_ID` | text | _blank_ | | | The app registration's Application (client) ID (a GUID). |
| `MICROSOFT_CERTIFICATE_PATH` | text | _blank_ | | | The PEM file holding the app's private key and its certificate, for example `data/microsoft-signin.pem`; relative to the project folder. RSA of 2048 bits or more. On Linux and in Docker it must be readable by the agent's account only (`chmod 600`), or the agent does not start. Read again each time a token is asked for, so a renewed certificate is used without a restart. `doctor` warns 30 days before it ends. |
| `MICROSOFT_CERTIFICATE_PASSPHRASE` | text | _blank_ | | | The passphrase of an encrypted private key; blank for a key without one. Left out of every printed copy of the settings. |

## More sending mailboxes

Mail is rotated across every mailbox, and each has its own warm-up
allowance. An extra mailbox uses the main one's SMTP and IMAP hosts and
ports, and reads its own inbox with its own address and password.

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `SENDER_2_EMAIL` | text | _blank_ | | | The address and login of extra mailbox 2. |
| `SENDER_2_PASSWORD` | text | _blank_ | | | Its password. A slot with an address and no password is skipped, with an error in the log, unless Microsoft sign-in is set up: then the mailbox signs in with the token, and this must be blank. |
| `SENDER_2_NAME` | text | _blank_ | | | Its From name. Blank, the mailbox is the main sender's: it takes `SENDER_NAME`, `SENDER_TITLE` and `SENDER_SIGNATURE`. |
| `SENDER_2_TITLE` | text | _blank_ | | | Its `SENDER_TITLE`; blank uses `SENDER_TITLE`. |
| `SENDER_2_SIGNATURE` | text | _blank_ | | | Its `SENDER_SIGNATURE`. Blank, a mailbox with its own name signs with its own name, title and `COMPANY_NAME`, never with the main mailbox's signature; one without a name takes `SENDER_SIGNATURE`. |
| `SENDER_3_EMAIL` | text | _blank_ | | | As `SENDER_2_EMAIL`, for mailbox 3. |
| `SENDER_3_PASSWORD` | text | _blank_ | | | As `SENDER_2_PASSWORD`, for mailbox 3. |
| `SENDER_3_NAME` | text | _blank_ | | | As `SENDER_2_NAME`, for mailbox 3. |
| `SENDER_3_TITLE` | text | _blank_ | | | As `SENDER_2_TITLE`, for mailbox 3. |
| `SENDER_3_SIGNATURE` | text | _blank_ | | | As `SENDER_2_SIGNATURE`, for mailbox 3. |
| `SENDER_4_EMAIL` | text | _blank_ | | | As `SENDER_2_EMAIL`, for mailbox 4. |
| `SENDER_4_PASSWORD` | text | _blank_ | | | As `SENDER_2_PASSWORD`, for mailbox 4. |
| `SENDER_4_NAME` | text | _blank_ | | | As `SENDER_2_NAME`, for mailbox 4. |
| `SENDER_4_TITLE` | text | _blank_ | | | As `SENDER_2_TITLE`, for mailbox 4. |
| `SENDER_4_SIGNATURE` | text | _blank_ | | | As `SENDER_2_SIGNATURE`, for mailbox 4. |
| `SENDER_5_EMAIL` | text | _blank_ | | | As `SENDER_2_EMAIL`, for mailbox 5. |
| `SENDER_5_PASSWORD` | text | _blank_ | | | As `SENDER_2_PASSWORD`, for mailbox 5. |
| `SENDER_5_NAME` | text | _blank_ | | | As `SENDER_2_NAME`, for mailbox 5. |
| `SENDER_5_TITLE` | text | _blank_ | | | As `SENDER_2_TITLE`, for mailbox 5. |
| `SENDER_5_SIGNATURE` | text | _blank_ | | | As `SENDER_2_SIGNATURE`, for mailbox 5. |
| `SENDER_ACCOUNTS` | text | _blank_ | | | More mailboxes as a JSON list, for more than four extras or for other hosts: `[{"username": "...", "password": "...", "sender_name": "..."}]`, each optionally with `sender_title`, `sender_signature` (read as the numbered slots read theirs), `smtp_host`, `smtp_port`, `imap_host`, `imap_port`, `imap_username` and `imap_password`. JSON that cannot be read is reported in the log and skipped. It holds passwords, so, like every password and key, it is left out of every printed copy of the settings. |

## Reading the inbox

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `IMAP_ENABLED` | true/false | `false` | Live (one of two) | | Reads the inbox and the Junk Email folder of every sending mailbox for bounces, opt-outs, replies and data-subject requests. On, the opt-out mailto counts as an opt-out route, and an inbox the agent cannot log in to stops live sending. A message over 10 MB is read only in part (headers and the first 256 KB of text). A message the mail server marked `dmarc=fail`, both `spf=fail` and `dkim=fail`, or `compauth=fail` in its top-most `Authentication-Results` header, or one from a domain with no DMARC record where neither SPF nor DKIM passed for the sender's own domain, or one whose header cannot be read, stops only its own address, never the company, and marks no bounce (DEPLOY section 3). |
| `IMAP_HOST` | text | `imap.gmail.com` | | | The inbox server, reached over TLS. Microsoft 365 is `outlook.office365.com`, which refuses passwords: use Microsoft sign-in (above). |
| `IMAP_PORT` | whole number | `993` | | | Its port. |
| `IMAP_USERNAME` | text | _blank_ | | | The main mailbox's inbox login; blank uses `SMTP_USERNAME`. |
| `IMAP_PASSWORD` | text | _blank_ | | | Its password; blank uses `SMTP_PASSWORD`. Must be blank while Microsoft sign-in is set up. |
| `IMAP_CHECK_INTERVAL_MINUTES` | whole number | `30` | | yes | Minutes between inbox checks; under 1 counts as 1. |
| `AUTONOMY_FIRST_EMAIL` | text | `approve` | | | Who decides first emails: `manual` (the agent sends none; a person uses Direct Send), `approve` (the agent drafts each one with its evidence in the Review tab, and only the text a named person approved is sent) or `auto` (the agent sends alone). Any other value reads as `approve`. |
| `AUTONOMY_FOLLOWUP` | text | `approve` | | | The same three levels for follow-ups. |
| `AUTONOMY_REPLY` | text | `approve` | | | The same three levels for answers to replies: at `approve`, a suggested answer waits for a person. Opt-outs and bounces are never queued. |
| `REVIEW_DAILY_CAP` | whole number | `60` | | | The most proposals one reviewer may approve in a day. Past it, approvals are refused until the next day: a cap, so that approving stays reviewing. |
| `LEAD_ALERT_EMAIL` | text | _blank_ | | | Who is emailed, through the main mailbox, when a prospect the agent emailed replies with interest. Comma-separated; an entry without an @ is ignored. Blank, interested replies alert nobody, and `doctor` warns. |

## Legal identity (the email footer)

Printed in every email (D.Lgs. 70/2003 Art. 8, Cod. Civ. 2250, GDPR
Art. 14; DEPLOY section 5).

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `LEGAL_ENTITY_NAME` | text | _blank_ | Live | | The company's legal name, such as `Example S.r.l.`, in the footer and as the controller in access answers. A name that shows an S.r.l. or S.p.A. also needs `LEGAL_SHARE_CAPITAL` and `LEGAL_SINGLE_MEMBER`. |
| `LEGAL_REGISTERED_OFFICE` | text | _blank_ | Live | | The registered office (sede legale), as a full postal address. |
| `LEGAL_COMPANY_REGISTRATION` | text | _blank_ | Live | | The company register entry and VAT number, such as `Reg. Imprese di ... n. ... - P.IVA ...`. |
| `LEGAL_SHARE_CAPITAL` | text | _blank_ | Live (S.r.l., S.p.A.) | | The share capital, printed as `Cap. soc. ...` when set (Cod. Civ. 2250 c.2). `doctor` also warns while it is blank. |
| `LEGAL_SINGLE_MEMBER` | text | _blank_ | Live (S.r.l., S.p.A.) | | `yes` or `no`: whether the company has a single member. `yes` prints `Socio unico` (Cod. Civ. 2250 c.4, decision L6). Any other value stops start-up. |
| `PRIVACY_NOTICE_URL` | text | _blank_ | Live | | The page holding the full privacy notice (GDPR Art. 13 and 14), linked in every footer. It must start with `https://`; any other value stops start-up. |
| `PRIVACY_CONTACT_EMAIL` | text | _blank_ | Live | | Where a person writes about their data, printed in every footer (decision L3.2). A value that is not an email address stops start-up. |

## Country lock

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `ENABLED_COUNTRIES` | text | _blank_ | Live | | The countries live email may go to, as two-letter codes, comma-separated; `GB` covers `.uk`. A recipient's country is read from the end of its domain. Blank means no live email at all. Fill it in only from counsel's signed country table (decision L1.1; DEPLOY section 7). |
| `UNKNOWN_COUNTRY_ALLOWED` | true/false | `false` | | | Whether a recipient on `.com`, `.eu` or another domain that shows no country may be emailed. Off, such a recipient counts as needing consent and is refused. Counsel decides. |
| `ITALIAN_COUNTRIES` | text | `IT` | | | The countries written to in Italian, as two-letter codes, comma-separated, read from the end of the recipient's domain as above, or from the company: its website's domain, its own page, or the AI (`COMPANY_COUNTRY_AI`). It sets the language of every email to a contact: the first email (`templates/email_template_it.html`), both follow-ups, a retry, Direct Send, the legal footer and notice, and the unsubscribe page; once a contact was sent an email, it keeps that email's language. Everyone else, a `.com` or `.eu` address included, gets English. Blank means English for everyone; a value that is not a two-letter code stops start-up. |

## Opt-out link, tracking and the dashboard

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `BASE_URL` | text | `http://127.0.0.1:5000` | Live (one of two) | | The dashboard's address as recipients would reach it. A public address puts a web unsubscribe link in every email; only an `https://` one also gives one-click unsubscribe and counts as an opt-out route. Otherwise the link is a mailto to the sending mailbox, which only `IMAP_ENABLED` reads. A public address also exposes the dashboard (DEPLOY section 3). |
| `UNSUBSCRIBE_REQUESTS_PER_MINUTE` | whole number | `30` | | | How many requests one address may make to the unsubscribe page in any minute; at least 1. Beyond that it is answered 429 with a `Retry-After`, except a one-click opt-out for a real link, which is always applied. The address is the connection's own, so behind a reverse proxy every recipient shares the proxy's. |
| `OPEN_TRACKING_ENABLED` | true/false | `false` | | | Adds a tracking pixel to campaign emails while `BASE_URL` is public. Keep it off for cold outreach: a pixel needs the recipient's prior consent (ePrivacy Art. 5(3)), and `doctor` warns while it is on. While it is off, the pixel's address `/track/...` answers 404 and records nothing; while it is on, one address may load it 60 times a minute (then 429). |
| `DASHBOARD_HOST` | text | `127.0.0.1` | | | The address the dashboard listens on. Anything but `127.0.0.1`, `localhost` or `::1` exposes it to other machines. The Docker image sets `0.0.0.0`. |
| `DASHBOARD_PORT` | whole number | `5000` | | | The dashboard's port. |
| `DASHBOARD_USERNAME` | text | `admin` | | | The dashboard's login name. |
| `DASHBOARD_PASSWORD` | text | _blank_ | Dashboard start | | The dashboard's password (HTTP Basic), at least 12 characters. Blank, only spaces, or shorter, and the dashboard refuses to start, wherever it listens (this machine too), and `doctor` fails. After 10 wrong passwords from one address within 15 minutes, every request from that address is answered 429 (with `Retry-After`) until the oldest of them is 15 minutes old; the right password waits too. The address is the connection's own, so behind a reverse proxy or tunnel everyone shares the proxy's. The unsubscribe and tracking pages never ask for it. |
| `DASHBOARD_REVIEWERS` | text | _blank_ | | | Named reviewers besides the administrator, as `name:password` pairs separated by commas. Each logs in to the dashboard with its own name, which is recorded as who approved or rejected each email. A password under 12 characters, or the administrator's name, is ignored. Only the administrator may change `AUTONOMY_*` from the dashboard. |

## Retention

The periods are the developer's proposals, pending counsel (decision L5.1;
`docs/COMPLIANCE.md` section 6).

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `RETENTION_ENABLED` | true/false | `true` | | | Applies the periods below: once a day while the agent runs live, and whenever `main.py retention` runs (DEPLOY section 8). Off, the retention job erases nothing, the footer no longer says how long data is kept, `main.py retention` refuses, and `doctor` warns. |
| `FIRST_CONTACT_MAX_AGE_DAYS` | whole number | `30` | | | Days after collection within which the first email must go, 1 to 30: it carries the privacy notice, due within a month (GDPR Art. 14(3)(a), decision L1.8). Past it the first email is refused and the contact is erased at the next run. Continuous mode also stops collecting while the waiting contacts could not all be emailed within it. |
| `RETENTION_DAYS_CONTACTED` | whole number | `365` | | | Days after the last email after which a contact who never replied is erased, unless a follow-up or retry is still pending; at least 30. Every footer states it while retention is on. |
| `RETENTION_DAYS_ENGAGED` | whole number | `730` | | | Days after the last exchange (the last email, reply or lead alert) after which a contact who replied or became a lead is erased; at least 30. |
| `LOG_RETENTION_DAYS` | whole number | `30` | | | How many rotated files each command's log in `logs/` keeps; at least 1. A log rotates at midnight, but only when its command writes after midnight, and the files beyond this number are then deleted. They are counted, not dated: the period holds only for a command that writes every day. |
| `RETENTION_DAYS_DISCARDED` | whole number | `90` | | | No longer read; `FIRST_CONTACT_MAX_AGE_DAYS` replaced it. Still accepted, and still refused below 30, so an older `.env` keeps loading. |

## Clients and competitors

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `CLIENT_FILTER_ENABLED` | true/false | `true` | | | Never cold-email an existing customer: a contact whose organisation matches a name on the client list is not emailed. On, live sending is refused and `doctor` fails while the list cannot be loaded. |
| `CLIENT_LIST_PATH` | text | `data/clients.xlsx` | Live | | The client's own customer list, needed while `CLIENT_FILTER_ENABLED` is on: an `.xlsx` workbook with one company name per row in the first column of the first sheet. A relative path starts at the project folder. The agent re-reads it when it changes, and the dashboard's Mark as client adds to it. It is git-ignored and left out of the Docker image (README, Client list). |
| `COMPETITOR_FILTER_ENABLED` | true/false | `true` | | | Never pitch to a named competitor: a contact whose organisation matches a name on the competitor list is not emailed. |
| `COMPETITOR_LIST_PATH` | text | _blank_ | | | The competitor list, in the client list's format. Blank turns the name check off. While a list is set and cannot be loaded (missing, unreadable, or screening no company), live sending is refused and `doctor` fails until the file is fixed, which the agent picks up without a restart; dry runs still work. |
| `LISTS_KEY` | text | _blank_ | | | Encrypts the client and competitor lists on disk, so a copy of the folder without the key shows neither. Set, the agent reads only an encrypted list (made with `py -3.15 main.py lists encrypt <file>`), in memory only, writes Mark as client back encrypted, and refuses a plain list as it refuses a missing one: live sending stops and `doctor` fails. Blank, the lists are plain files and `doctor` warns. A wrong key or a changed file is refused, never read as an empty list. At least 32 characters, or every command refuses to start; make it with `py -3.15 -c "import secrets; print(secrets.token_urlsafe(48))"`. Keep a copy apart from the folder and its backups: without it the lists must be encrypted again from the original workbooks (DEPLOY section 13). It does not protect against someone who has the whole disk and `.env` (BitLocker does). |

## Discovery

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `TARGET_QUERIES` | text | _blank_ | | | Comma-separated search queries for a single run without `--query`, from the command line or the dashboard. Blank, a single run takes up to five searches queued with Add Query or `add-query`, or else the free query generator, which searches Omega IFS's own buyer phrases (Italian, German and English, from `client/omega.json`) first and then its market categories; words for what Omega itself sells (fasteners, screws, cold forming) are never searched, as they find competitors. In continuous mode they are only the topics of AI company discovery, which is skipped while this is blank. |
| `HEURISTIC_DISCOVERY_ENABLED` | true/false | `true` | | | The free query generator, at no API cost: continuous mode's discovery when no AI suggests queries, and a single run's source when nothing else is set. It starts from Omega's own buyer phrases in `client/omega.json`. |
| `LEARNING_ENABLED` | true/false | `true` | | | Biases the free query generator toward the product kinds and countries that have produced results. |
| `MAX_PENDING_BACKLOG` | whole number | `300` | | yes | Continuous mode skips discovery while at least this many contacts wait to be emailed, and works through them first. |
| `SCRAPE_RATE_LIMIT` | number | `2.0` | | | Seconds the scraper waits before each page it fetches; at least 0.5. |
| `MAX_RESULTS_PER_QUERY` | whole number | `20` | | | Search results asked for per query. |
| `BLOCKED_EMAIL_PREFIXES` | text | `noreply,no-reply,support,admin,webmaster,abuse,postmaster,mailer-daemon` | | | Mailbox names never collected, comma-separated: an address is skipped when the part before the @ is one of them, or starts with one followed by `.`, `_`, `-` or `+`. Blank uses the same list. |

## Qualifying contacts

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `VERIFY_EMAILS` | true/false | `true` | | | Checks each address before it is emailed, as `VERIFY_MX` and `VERIFY_SMTP_PROBE` say. |
| `VERIFY_MX` | true/false | `true` | | | Checks that the address's domain has a mail server (MX). An address that fails is discarded; one whose check is inconclusive waits for another try. Off, no address is checked at all. |
| `VERIFY_SMTP_PROBE` | true/false | `false` | | | Also asks the recipient's mail server whether the mailbox exists, without sending an email. Only with `VERIFY_MX` on. A mail server whose address is not on the public internet (this machine, the office network and the like) is never contacted, and the address fails the check. |
| `LEAD_SCORING_ENABLED` | true/false | `true` | | | Scores each contact from its role, address, company name and the page it was found on. The backlog is emailed highest score first. |
| `MIN_LEAD_SCORE_TO_EMAIL` | whole number | `15` | | yes | The lowest score a contact needs to be emailed; a contact scoring lower is discarded. |
| `RELEVANCE_FILTER_ENABLED` | true/false | `false` | | | Drops a contact before it is emailed when the address is a placeholder, the domain or mailbox is outside Europe, the company describes itself as a fastener maker or distributor (a competitor) or uses a non-target word. |
| `RELEVANCE_REQUIRE_VERTICAL` | true/false | `true` | | | With the relevance filter on, also drops a contact whose company name, description, page or domain shows none of the `RELEVANCE_VERTICALS` words. |
| `RELEVANCE_VERTICALS` | text | _blank_ | | | The words, comma-separated, that place a company in Omega IFS's market. Blank, the gate uses the market words in `client/omega.json` (`verticals`: automotive, furniture and window hardware, electrical, appliances and similar, in English, Italian and German), read by `modules/relevance.py`; a list replaces it, to narrow the gate. Omega's own buyer phrases always count. |

## Email content

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `EMAIL_SUBJECT` | text | `Special cold-formed parts to your drawing \| Omega IFS` | | | The subject of first emails in English, and of Italian ones while `EMAIL_SUBJECT_IT` is blank. `{company_name}` becomes `COMPANY_NAME`, and `{org}` the recipient's company (`your brand` when it is not known). |
| `EMAIL_SUBJECT_IT` | text | `Particolari speciali stampati a freddo su disegno \| Omega IFS` | | | The subject of first emails to recipients written to in Italian (`ITALIAN_COUNTRIES`), with the placeholders of `EMAIL_SUBJECT`; `{org}` falls back to `la Sua azienda`. Blank, they get the English subject, A/B variants included. |
| `AB_TESTING_ENABLED` | true/false | `false` | | | Each English first email takes a random body from the `templates/email_template*.html` files other than the Italian `*_it` ones, and a random subject from `EMAIL_SUBJECTS`, and records which it used. An Italian recipient always gets `templates/email_template_it.html`. |
| `EMAIL_SUBJECTS` | text | _blank_ | | | Subject variants for A/B testing, comma-separated, with the placeholders of `EMAIL_SUBJECT`; a variant cannot contain a comma. Blank uses `EMAIL_SUBJECT`. |
| `ATTACH_PROFILE` | true/false | `false` | | | Attaches the Company Profile PDF (`PDF_PATH`) to every first email, never to a follow-up. On, a live first email is refused while the PDF is missing, a folder, empty or unreadable, and `doctor` fails; it also warns that an attachment on a cold email is a spam signal. `false` sends no PDF and, with `PROFILE_URL` set, links the profile in a template that has `{{body}}`; Omega's first email attaches nothing and says nothing is attached. |
| `PROFILE_URL` | text | `https://www.omegaifs.it` | | | The company profile's `https://` address, linked in first emails whose template has `{{body}}` while `ATTACH_PROFILE` is `false`; `doctor` then warns while it is blank or not https. |
| `PDF_PATH` | text | `assets/presentation.pdf` | Live | | The Company Profile PDF, optional: needed only while `ATTACH_PROFILE` is on, which it is not for Omega. It is looked for at each first email, so a file put in place is picked up without a restart. It is git-ignored, so add it yourself; a relative path starts at the project folder. |
| `SEQUENCES_ENABLED` | true/false | `false` | | | Follow-up emails: two of them (`templates/followup_1.html` and `followup_2.html`), the first 3 days after the first email and the second 4 days after that. |
| `DEFAULT_SEQUENCE_NAME` | text | `Standard Outreach` | | | The name the follow-up sequence is stored under; the agent creates it when it first starts with sequences on. |

## Pacing and the send window

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `WARMUP_ENABLED` | true/false | `true` | | | Ramps each mailbox's daily allowance up from `WARMUP_START_DAILY`, so a new mailbox does not send its full rate from day one. Off, there is no daily limit, and `doctor` warns. |
| `WARMUP_START_DAILY` | whole number | `5` | | | Emails a mailbox may send on its first day; at least 1. `doctor` warns above 10. |
| `WARMUP_INCREMENT` | whole number | `3` | | | How many more it may send each day after that. 0 keeps the rate flat; a negative value is refused. `doctor` warns above 5. |
| `WARMUP_MAX_DAILY` | whole number | `100` | | | The ceiling of the daily allowance; at least 1. |
| `MAX_PER_DOMAIN_PER_DAY` | whole number | `3` | | yes | Emails per recipient domain per day; at least 1. |
| `EMAILS_PER_HOUR` | whole number | `30` | | yes | Sets the shortest gap between sends to 3600 / N seconds: at 30, two minutes. At most 100; 0 removes this floor. |
| `DELAY_BETWEEN_EMAILS` | number | `5.0` | | yes | Seconds between sends, 0 or more; the gap `EMAILS_PER_HOUR` sets wins when it is longer. |
| `SEND_JITTER_SECONDS` | number | `0.0` | | yes | Adds a random 0 to N seconds to each gap, so sends are not perfectly regular; 0 or more. |
| `SEND_WINDOW_ENABLED` | true/false | `true` | | | Sends only inside the window below, in `APP_TIMEZONE`. The inbox is still read outside it. |
| `SEND_WINDOW_START_HOUR` | whole number | `8` | | yes | The hour sending starts, 0 to 24. |
| `SEND_WINDOW_END_HOUR` | whole number | `19` | | yes | The hour sending stops, 0 to 24 and after the start: sends go from the start hour up to, not including, the end hour. A window across midnight is not supported. |
| `SEND_WINDOW_DAYS` | text | `0,1,2,3,4` | | yes | The days sending is allowed, as digits, comma-separated: 0 is Monday and 6 is Sunday. Anything else is refused. |
| `APP_TIMEZONE` | text | `Europe/Rome` | | | The time zone the agent runs in: the send window, the daily caps and the stored times use it. A name that cannot be resolved falls back to UTC, with an error in the log. |

## Reputation guards and retries

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `BOUNCE_GUARD_ENABLED` | true/false | `true` | | | Pauses all sending when the bounce rate of the last 7 days goes above `MAX_BOUNCE_RATE`. It counts the bounces the inbox monitor reads and the refusals at sending. The pause holds until the agent is restarted. |
| `MAX_BOUNCE_RATE` | number | `0.05` | | yes | The bounce rate that pauses sending, as a fraction above 0: 0.05 is 5%. A value at which the guard could never trip is refused. |
| `BOUNCE_GUARD_MIN_SENDS` | whole number | `20` | | yes | How many sends in the last 7 days the guard waits for before it judges the rate. A value so large that the guard could never trip is refused. |
| `DOMAIN_BOUNCE_BACKOFF_ENABLED` | true/false | `true` | | | Skips a recipient domain whose own bounce rate of the last 7 days is above `MAX_DOMAIN_BOUNCE_RATE`, while other domains carry on. |
| `MAX_DOMAIN_BOUNCE_RATE` | number | `0.3` | | yes | The bounce rate that backs a domain off, as a fraction above 0: 0.3 is 30%. A value at which no domain could ever be backed off is refused. |
| `DOMAIN_BOUNCE_MIN_SENDS` | whole number | `5` | | yes | How many sends to a domain in the last 7 days come before its rate is judged; at least 1, and a value so large that no domain could ever be backed off is refused. |
| `BLACKLIST_CHECK_ENABLED` | true/false | `true` | | | Checks the sending domain against public spam blocklists and pauses all sending while it is listed. Sending resumes once a check finds it clean on every list. |
| `RETRY_ENABLED` | true/false | `true` | | | Retries a send that failed. |
| `RETRY_MAX_ATTEMPTS` | whole number | `3` | | | How many times a failed send is retried. |
| `RETRY_BASE_DELAY_MINUTES` | number | `5.0` | | | Minutes before the first retry; each later wait is 6 times longer (5, 30, then 180 minutes). |

## AI (optional)

The AI runs only on a model on Omega IFS's own server (or another machine on
its private network); no outside AI company receives any data. The settings
`GEMINI_API_KEY`, `GROQ_API_KEY` and `GROQ_MODEL` no longer exist: a `.env`
that still has one of these lines stops start-up with a message naming the
line to delete, and so does one of these names set in the environment (as
Docker's `env_file` does with every line of `.env`). "The AI on" below means `LLM_PROVIDER=openai_compatible` with
`LLM_BASE_URL` and `LLM_MODEL` set.

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `LLM_PROVIDER` | text | _blank_ | | | `openai_compatible` turns the AI on: it then asks the model on Omega's own server at `LLM_BASE_URL`, a server that speaks the OpenAI chat interface, such as Ollama. Blank means no AI. Read in any case. Any other value, such as the old `gemini` or `groq`, stops start-up: no outside AI company may receive data. |
| `LLM_BASE_URL` | text | _blank_ | | | The model server's address up to and including `/v1`, for example `http://127.0.0.1:11434/v1` for Ollama on the same machine. Required when `LLM_PROVIDER` is `openai_compatible`, and read only then: `doctor` warns when it is set while `LLM_PROVIDER` is blank. It must point at this machine or the private network: every address its host resolves to must be loopback (`127.x`, `::1`) or private (`10.x`, `172.16.x` to `172.31.x`, `192.168.x`, `fc00::/7`). A public, link-local, multicast or unspecified address stops start-up. A name that cannot be looked up at start-up does not stop the agent (opt-outs are still read), but `doctor` reports it and no AI question is sent until it resolves; an IP address avoids the lookup. The address is resolved again before every AI call, a call to an address that is no longer private sends nothing, and the call goes to the address that was checked, never to the name looked up a second time. http or https, with no user name, password, query or fragment, and not ending in `/chat/completions`; trailing slashes are dropped. `doctor` warns when the path does not end in `/v1`. |
| `LLM_MODEL` | text | _blank_ | | | The model's name as that server knows it, for example `qwen2.5:14b`. Required when `LLM_PROVIDER` is `openai_compatible`; there is no default. |
| `LLM_REASONING_EFFORT` | text | `none` | | | Sent as `reasoning_effort` in each chat request to `LLM_BASE_URL`: `none`, `low`, `medium` or `high`, in any case, or blank to send no such field (for a server that refuses it). Read only when `LLM_PROVIDER` is `openai_compatible`; any other value then stops start-up. A model that reasons first can spend minutes on a one-word question unless this is `none`. |
| `LLM_API_KEY` | text | _blank_ | | | Sent as a Bearer token to `LLM_BASE_URL` when set; when blank no Authorization header is sent. Ollama needs none. Never printed. |
| `LLM_TIMEOUT_SECONDS` | whole number | `30` | | | How long one call to `LLM_BASE_URL` may take in all, from 5 to 300 seconds: the agent stops waiting when it runs out, however steadily the server sends its header lines or its bytes, and whether or not it falls silent part-way. It must be a whole number (anything else stops start-up), but the 5 to 300 range is checked only when `LLM_PROVIDER` is `openai_compatible`. A local model can be slow to load. |
| `AI_DPA_REFERENCE` | text | _blank_ | | | Where the AI provider's data processing agreement is filed. While it is blank, no AI call that carries a recipient's data is made, so the switches below do nothing, and `doctor` warns while one of them is on: `COMPANY_COUNTRY_AI`, on by default, counts once the AI is on. With the AI only on Omega's own server there is no outside processor; whether this gate is still needed is the owner's decision, and until then it works as before (for example `own server, no outside processor`). |
| `AI_PERSONALIZATION_ENABLED` | true/false | `false` | | | The AI writes the opening line from the recipient's company, in a template that has `{{opening_line}}`. None of the shipped templates has one; only the built-in fallback, used while `templates/email_template.html` is missing, does. Needs the AI on and `AI_DPA_REFERENCE`. |
| `AI_HUMANIZE_EMAILS` | true/false | `false` | | | The AI writes the body, in a template that has `{{body}}`, instead of the hand-written copy, which it falls back to when it fails. None of the shipped templates has one: the first email is Omega's own fixed text, and the follow-ups keep their own copy. Needs the AI on and `AI_DPA_REFERENCE`. |
| `AI_RELEVANCE_ENABLED` | true/false | `false` | | | The AI judges whether each contact's company is a target; only a clear no drops it. Needs the AI on and `AI_DPA_REFERENCE`. |
| `COMPANY_COUNTRY_AI` | true/false | `true` | | | When neither the address, the company website nor its page shows the company's country, the AI is asked for it from what the fit check already sends (company name, address domain, page description); an Italian answer gets the Italian email. Asked once per contact, before its first email, and never for an address the country lock (ENABLED_COUNTRIES) refuses. Needs the AI on and `AI_DPA_REFERENCE`. |
| `AI_REPLY_CLASSIFICATION_ENABLED` | true/false | `false` | | | When the keyword rules cannot tell whether a reply shows interest, the AI decides from the reply's own text (600 characters). Needs the AI on and `AI_DPA_REFERENCE`. |
| `AI_STRATEGIST_ENABLED` | true/false | `false` | | | The AI suggests discovery queries in continuous mode. It sees no recipient's data. Needs the AI on. |

## Running

| Setting | Type | Default | Required | Dashboard | What it does |
| --- | --- | --- | --- | --- | --- |
| `DRY_RUN` | true/false | `false` | | | Every run is a simulation: emails are rendered and logged as dry runs, no prospect is emailed, and retention does not run. A dry run still searches and stores contacts, and with `IMAP_ENABLED=true` it reads the inbox, acts on it and sends lead alerts. The dashboard's Start Agent and Direct Send honour it too. `main.py run --dry-run` does the same for one run. |
| `LOOP_DELAY_SECONDS` | number | `300.0` | | | Seconds between loops in continuous mode, at least 5; `run --loop-delay` overrides it. Outside the send window the loop sleeps longer, until the window opens or the next inbox check is due. |
| `DB_PATH` | text | `data/agent.db` | | | The SQLite database. Its folder also holds the journal (`journal.jsonl`), `backups/`, `exports/` and, under `spool/`, held opt-outs, sends refused while the database was locked (`refused_sends/`) and erasures whose file steps have not all landed (`journal/`). A relative path starts at the project folder. |
| `DEBUG` | true/false | `false` | | | Debug logging. It never turns on Flask's debugger (which runs any code it is sent): the dashboard always runs without it, and `FLASK_DEBUG` in the environment does not switch it on either. |
