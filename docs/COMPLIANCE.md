# Compliance — GDPR / ePrivacy (B2B cold outreach)

This agent performs **B2B** cold outreach to **business** contacts at European
companies on behalf of SUPPLECO (contract manufacturer of food supplements &
nutraceuticals), an Italian company. This document records what every email
carries and the controls the software enforces, and says plainly where a
control depends on configuration.

It is a technical record, not legal advice. The legal questions are in
`docs/privacy/decisions_for_counsel.md` (the counsel sheet), each with the
developer's proposed answer. Every one stays open until SUPPLECO's counsel
answers it, and this record cites them by id (L1.1 and so on). The drafts
counsel completes, among them the legitimate-interest assessment, the privacy
notice and the request procedure, are in `docs/privacy/`.

## 1. Controller identity

The controller is SUPPLECO. Its identity is configuration, never hard-coded,
because only SUPPLECO can supply it:

| `.env` key                   | What it holds                                                                  |
| ---------------------------- | ------------------------------------------------------------------------------ |
| `LEGAL_ENTITY_NAME`          | Registered name, e.g. "SUPPLECO S.r.l."                                          |
| `LEGAL_REGISTERED_OFFICE`    | Sede legale, full postal address                                               |
| `LEGAL_COMPANY_REGISTRATION` | "Reg. Imprese di … n. … – P.IVA …"                                             |
| `LEGAL_SHARE_CAPITAL`        | Capitale sociale (required for an S.p.A./S.r.l.)                               |
| `LEGAL_SINGLE_MEMBER`        | `yes` or `no`: whether an S.p.A./S.r.l. has a single member (required for one) |
| `PRIVACY_NOTICE_URL`         | The page holding the full Art. 13/14 notice, an `https://` address             |
| `PRIVACY_CONTACT_EMAIL`      | Where a person writes about their data                                         |

**While `LEGAL_ENTITY_NAME`, `LEGAL_REGISTERED_OFFICE`,
`LEGAL_COMPANY_REGISTRATION`, `PRIVACY_NOTICE_URL` or `PRIVACY_CONTACT_EMAIL` is
blank, live sending is refused** on every path (campaign, follow-ups, retries,
Direct Send), `doctor` reports `[FAIL]` and exits 1, and the dashboard log shows
`LEGAL_IDENTITY_MISSING`. When the entity name shows an S.r.l. or an S.p.A.,
`LEGAL_SHARE_CAPITAL` and `LEGAL_SINGLE_MEMBER` are required too; for any other
company a blank share capital only makes `doctor` warn. A `PRIVACY_NOTICE_URL`
that does not start with `https://` stops the settings loading at all. Dry runs
still work. The privacy notice itself is SUPPLECO's document, for counsel to
approve (L3.1), and must be published at that URL: nothing checks that it is.
What the footer must state about the company is decision L6.

## 2. Lawful basis and the country lock

Whether SUPPLECO may email a company without its prior consent depends on the
recipient's country, and in some countries on whether the address is generic
(info@) or names an employee. That is counsel's decision, country by country
(L1.1, with L1.2 and L1.3), and no country is decided yet. The review's
research, for counsel to confirm, reads Italy, Germany, Austria and Spain,
among others, as needing prior consent for both kinds of address. Where
counsel allows legitimate interest, the assessment is completed per country
from the outline in `docs/privacy/lia_template.md` (L2.1).

The code does not assume an answer:

- **The country lock.** Live email goes only to recipients whose domain ends
  in a country code listed in `ENABLED_COUNTRIES` (`GB` covers `.uk`). Blank,
  the default, means no live email at all: the send check refuses every live
  send, the dashboard log shows `COUNTRY_LOCK`, and `doctor` fails. Addresses
  on `.com`, `.eu` and other generic domains show no country, count as needing
  consent, and are refused unless `UNKNOWN_COUNTRY_ALLOWED=true`. Both are set
  only from counsel's signed table (`docs/DEPLOY.md` section 7). Whether a
  domain ending may stand for the country at all is L1.15.
- **The basis recorded with each contact.** When a contact is stored, it
  records how its address was found and the basis that source gives, with the
  rules version (`SOURCE_CHANNELS` and `RULE_VERSION` in
  `modules/compliance.py`). The company's own website, a contact, legal or
  about page on it, and a third-party directory or listing give legitimate
  interest. For a Direct Send the operator chooses the channel the contact came
  through: an inbound request gives consent, an existing customer the art.
  130(4) exception, a trade fair or a referral legitimate interest. A live send
  to a contact with no recorded basis is refused. The categories and the basis
  each gives are the developer's proposals (L1.4, L1.5). The country lock does
  not read the basis: a consent recorded for a contact does not open a country
  that is off.
- **Until counsel answers.** The country lock stops sending, not collecting.
  The counsel sheet proposes, as an operating rule, that the agent is not run
  at all until the country table is signed (L1.1) and L1.13 is answered
  (L1.9). That includes a dry run, which is not a harmless preview: it
  searches the web and stores every contact it finds, whatever its country,
  and with `IMAP_ENABLED=true` it reads the mailbox, acts on the opt-outs and
  requests in it, and sends lead alerts. Only sending and retention are
  skipped.

## 3. What every email carries

A legal footer is rendered by `modules/compliance.py` and filled into every
template — first email, follow-ups, A/B variants and the built-in fallback —
so an edited template cannot quietly lose it. The dashboard template editor
refuses to save a template without `{{legal_footer}}` or `{{unsubscribe_url}}`,
`doctor` fails while a template on disk lacks either, and a live send is
refused for a template without the footer. The wording is the developer's
draft (L3.2), in three paragraphs:

| Footer content                                                                                                                                                                                                                              | Requirement                                                    | Counsel    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- | ---------- |
| Legal name, registered office, Registro Imprese / P.IVA; the share capital when set; "Socio unico" when `LEGAL_SINGLE_MEMBER=yes`                                                                                                           | Cod. Civ. Art. 2250; D.Lgs. 70/2003 Art. 8; GDPR Art. 14(1)(a) | L6         |
| "This is a commercial communication", opening the second paragraph                                                                                                                                                                          | D.Lgs. 70/2003 Arts. 8 and 9                                   | L3.4       |
| The data held (name, job title and business email address) and where it came from: the host of the page it was found on; for an address an operator entered, the channel it came through; otherwise "publicly available business sources"   | GDPR Art. 14(2)(f)                                             | L1.4, L1.5 |
| The purpose, and the lawful basis in a sentence of its own for each basis: legitimate interest (Art. 6(1)(f)) with the interest pursued; consent, which may be withdrawn; or an existing customer (Art. 130(4) of the Italian privacy code) | GDPR Art. 14(1)(c), (2)(b)                                     | L3.2       |
| Who receives the data: "our email provider"; while an AI switch that sees recipients' data is on, that an AI model running on our own server helps, "so no outside AI company receives them"                                                | GDPR Art. 14                                                   | L2.4       |
| How long the data is kept: erased `RETENTION_DAYS_CONTACTED` days after the last email if there is no reply, stated only while retention is on                                                                                              | GDPR Art. 14(2)(a)                                             | L5.1       |
| The rights of access, rectification, erasure and restriction, a complaint to a data protection authority (the Garante, for Italy), the privacy contact and a link to the full notice                                                        | GDPR Art. 14 (layered notice)                                  | L3.1, L2.6 |
| The third paragraph, on its own: "You may object to these emails at any time", with the unsubscribe link, or a reply saying "unsubscribe"                                                                                                   | GDPR Art. 21(4); D.Lgs. 70/2003 Art. 8                         | L3.2       |

Each email records the version of the footer it carried, one per basis
(`notice-li-2`, `notice-consent-2`, `notice-customer-2`; `NOTICE_VERSIONS`), and
a test fails if the wording changes without a new version. A recipient whose
domain is in `ITALIAN_COUNTRIES` (default `IT`) gets every email, footer
included, in Italian: the footer is the same draft translated sentence for
sentence, also for counsel (L3.2), recorded as `notice-li-2-it`,
`notice-consent-2-it` or `notice-customer-2-it` (`NOTICE_VERSIONS_IT`).
Version 1 (`notice-li-1` and so on) said "an AI service"; version 2, from 16
November 2026, says the AI is a model running on SUPPLECO's own server and that
no outside AI company receives the data. Only the AI sentence changed.
So does a contact whose company is shown to be in one of those countries,
by its website's domain, by its own page (an Italian VAT number or legal
form), or by the AI (`COMPANY_COUNTRY_AI`), which is sent only what the fit
check is sent. Everyone else gets English. A contact keeps the language of
the first email it was sent, read from that email's notice version.

The plain-text part of the email keeps every link target, so the opt-out works
in text-only clients too. Every email also carries a `List-Unsubscribe` header
(mailto, plus the web link and RFC 8058 one-click when `BASE_URL` is public
HTTPS).

## 4. Who is contacted

| Control                 | How it is met                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Business addresses only | Consumer mailbox providers across Europe (gmail, libero, gmx, web.de, orange.fr, wanadoo, t-online, seznam, wp.pl …) are never harvested, and the send check refuses them on every path, Direct Send included.                                                                                                                                                                                                                       |
| No certified mail       | PEC addresses (legal correspondence) are never harvested, and the send check refuses them on every path.                                                                                                                                                                                                                                                                                                                             |
| Enabled countries only  | The country lock (section 2) refuses every live send to a recipient outside the enabled countries.                                                                                                                                                                                                                                                                                                                                   |
| Relevant recipients     | Lead scoring; the relevance filter (European, supplement vertical, competitor keywords) when `RELEVANCE_FILTER_ENABLED=true` - it is off by default; competitors screened by SUPPLECO's named list once `COMPETITOR_LIST_PATH` points at it (blank by default; `doctor` then shows `[SKIP] none configured`); existing clients screened by the client list, without which live sending is refused while `CLIENT_FILTER_ENABLED` is on. |
| A company that said no  | After an opt-out by email or a data request from someone the agent emailed, a one-click unsubscribe or an erasure, no address at that company is emailed or collected again (section 7).                                                                                                                                                                                                                                             |
| Provenance              | Each contact records the page it was found on (none for a Direct Send), its source channel, its basis and the rules version (section 2). After each live send, `consent_log` records the basis it relied on, once per address and basis, with the contact, the channel and the rules version.                                                                                                                                        |
| robots.txt              | Read before a site's pages are fetched, and its rules for all crawlers (`User-agent: *`) are obeyed. The fetcher presents itself as a Chrome browser (its User-Agent and TLS fingerprint are Chrome's, `modules/search.py`), so a site cannot write a rule for this agent by name. A robots.txt that cannot be fetched or read counts as allowing everything.                                                                        |

## 5. Tracking

**Open tracking is off by default** (`OPEN_TRACKING_ENABLED=false`) and should
stay off for cold outreach: a tracking pixel reads information from the
recipient's device, which ePrivacy Art. 5(3) / Codice Privacy Art. 122 allow
only with prior consent — applied to email pixels by the Garante
(provvedimento 284, 17 April 2026) following EDPB Guidelines 2/2023. `doctor`
warns if it is switched on. Even when on, an open records only its time: the
reader's IP address and User-Agent are not stored. While it is off, the
pixel's address (`/track/...`) does not exist (404) and records nothing.
Counsel is asked to confirm that it stays off (L1.7).

## 6. Retention (GDPR Art. 5(1)(e))

Once a day (never on a dry run) the agent erases, with the same routine GDPR
erasure uses, and `main.py retention` does the same without the agent
(`DEPLOY.md` section 8). The periods are the developer's proposals in the
counsel sheet (L1.8, L1.16, L5.1), pending counsel:

- contacts **not emailed within `FIRST_CONTACT_MAX_AGE_DAYS`** (default 30, at
  most a month) of collection. A first email past that limit is refused, and
  collection pauses while the backlog could not be emailed within it;
- contacts **emailed who never replied**, `RETENTION_DAYS_CONTACTED`
  (default 365) after the last email, unless a follow-up or retry is still
  pending. That hold ends `RETENTION_DAYS_CONTACTED` plus the longest
  follow-up delay (4 days with the default sequence) after the last email
  actually sent, so a follow-up or retry that never goes, with follow-ups
  switched off for example, does not keep the contact longer; a follow-up
  the country lock refuses ends the sequence;
- contacts **who replied or became leads**, `RETENTION_DAYS_ENGAGED`
  (default 730) after the last exchange;
- an **objection** (an opt-out, a data request in a reply, or a decline from
  someone emailed), at the next run;
- open-tracking device data after 30 days; addresses in activity-log lines
  masked after 180 days; a lead alert's reply text after 30 days; a closed
  data-subject request after 1,095 days; an access answer after 30 days;
  daily send counts per domain after 30 days; a bounce after 24 months.

Clients are no exception. `RETENTION_DAYS_DISCARDED` is no longer read.
**The suppression list keeps every objection** — it is what keeps an opt-out
honoured; only a bounce is released, and never one an operator had also
blocked. An address erased for retention is not re-added if it is scraped
again. `doctor` warns when retention is off, and the footer states the
period only while it is on.

If erasing one contact fails part-way, that erasure is rolled back: the
contact stays as it was, the contacts erased before it stay erased, the day
is not marked done, and the next loop tries again. While it keeps failing,
the agent lists it under the dashboard's Recent Errors as `RETENTION_FAILED`,
once a day.

## 7. Rights of the data subject

**Right to object / opt-out (Art. 21).** Four routes, all ending on the
suppression list, after which no follow-up or retry goes out:

- the web unsubscribe link in the email, and the one-click unsubscribe that
  mail apps show next to the sender: both need a public `BASE_URL`, and
  one-click needs `https://`;
- the `mailto:` unsubscribe (used while `BASE_URL` is not public) and any reply
  saying "unsubscribe" in EN/IT/DE/FR/ES (in Italian also "disiscrivimi",
  "toglietemi dalla lista", "non scrivetemi più" and the like), or only
  "stop" — **processed only when `IMAP_ENABLED=true`**, at the next inbox
  check, which reads the inbox and the Junk Email folder, and which every
  pass makes before its first send;
- a plain decline ("not interested") from someone the agent emailed, which is
  recorded as an objection (L1.16);
- the operator blocking the contact in the dashboard.

Live sending is refused while nothing would read an opt-out: no public
`https://` `BASE_URL` and `IMAP_ENABLED=false`. `doctor` then fails ("Opt-out
route"), and the dashboard log shows `OPT_OUT_ROUTE_MISSING`. A public `http://`
address gives a web link but does not count as a route. With
`IMAP_ENABLED=true`, live sending also stops while an inbox the agent reads,
or its junk folder, has had no complete read within three check intervals
(`INBOX_UNREAD`; `doctor`'s "Inbox read" fails), as when its login fails. A
read reaches back at most 14 days: after a longer outage the stretch it
could not reach is recorded as `IMAP_GAP`, with the dates to check by hand,
and `doctor` warns ("Inbox gap"). Whether the web page alone, with the inbox
unread, is enough is L5.5.

The sender of an opt-out, a request or a reply is read from the email's From
header, in any of its forms. A message from a sender that is not exactly one
address is still recorded, and a `SENDER_UNPARSED` row asks a person to check
who wrote.

An opt-out by link or by email also marks the contact `unsubscribed`, records
how the person opted out, and ends their follow-ups and queued retries at once.
An opt-out from a contact the operator had already blocked replaces the block
on the suppression list, so the dashboard's Unblock - which can only reverse
the operator's own block - cannot remove it. A recipient written to in
Italian lands on an Italian unsubscribe page. A click on a link the database
holds no record of, as after a restore, is kept for a year, if the token is
one the agent's key made, and applied before any send to its recipient;
a made-up link is not kept.

Opt-outs, blocks and the other records are stamped in Italian time
(`APP_TIMEZONE`) whatever the host's clock says.

Every send path checks the suppression list, and every path also stops for
anyone who replied, became a client, bounced or was erased (Direct Send
included, though the operator may override a filter verdict such as a low
score). The send checks again just before it writes anything about the person
and just before it goes, so an erasure, an opt-out or a block that lands
while the email is prepared stops it.

**Company blocks (L1.6).** One person's no can stop email to their whole
company, which is blocked by its email domain (`modules/company_block.py`): no
address there is then emailed or collected. A block is set by the one-click
unsubscribe; by an objection sent by email, or a data request in a reply, from
someone the agent emailed; and by an erasure, which keeps the block only as a
keyed token. A click on the web link in the email text, a plain decline, a
bounce or an interested reply never blocks a company, and neither does a
request registered by hand. Free-mail and PEC domains are never blocked whole.
`main.py suppress-domain list` shows the blocks (those kept as tokens only as a
count), and `main.py suppress-domain lift <domain> --reason "..."` lifts one
and logs the reason. Who may lift a block, and on what grounds, is L1.6.

**Data-subject requests (Arts. 12-22).** Replies are checked for erasure and
access requests in EN/IT/DE/FR/ES, before the opt-out phrases; "where did you
get my address?" counts as an access request. Each request is registered once,
however often the inbox is read, with a deadline one calendar month after the
message arrived. The address is suppressed at once and the contact marked
`unsubscribed`, and the company is blocked if the agent had emailed the person.
The dashboard log shows `DSR_RECEIVED`, and `doctor` warns about an overdue
request. The register is worked from the command line:

- `main.py dsr list` shows the open requests and their deadlines, with the
  addresses masked (`--all` adds the closed ones); `dsr show <id>` shows one in
  full.
- `main.py dsr add <email> --kind access|erasure|other` registers a request
  received by post, by phone or at the privacy contact, with `--received` for
  the day it arrived. It only registers the request: it stops no email.
- `main.py dsr export <id>` writes the access answer (below).
- `main.py dsr close <id> --outcome "..."` records how a request ended.
- `main.py dsr erase <id>` asks the operator to type ERASE, erases as
  `gdpr-erase` does, and closes the request. Unlike `gdpr-erase`, it prints no
  list of the places it cannot reach (L5.6).
- On a request whose address was already erased, `dsr show`, `dsr export` and
  `dsr erase` find the address again, or report that it is already erased.

A request found in a reply also counts as an opt-out, so the next retention
run, once a day, erases the contact even while an access request is open.
Export the answer before that run, the day the request arrives: after it the
contact is already erased, and the answer can show at most what an erasure
keeps. Whether retention should wait for an open request, and who answers
requests and confirms erasures, is L5.5. None of these commands checks who is
asking (L5.3). The procedure is `docs/privacy/dsr_procedure.md`, a draft for
counsel.

**Access (Art. 15).** `main.py gdpr-export <email>`, or `dsr export <id>` for a
registered request, writes the records the database holds about the address
— contacts, emails, suppression, sequence enrolments, opens, the basis record,
the prospect's own reply text (lead alerts), retry records, requests and the
activity-log lines naming it — to `records.json`, with a draft Art. 15 cover
letter (`cover.md`). The folder is under `data/exports/`, beside the database,
named by the address's token, and retention deletes it 30 days after it was
written. It leaves out two things the database holds, the record of the
unsubscribe links sent to the address and the search that found the contact,
and the log files and held opt-out files outside it (L5.3).

**Erasure (Art. 17).** `main.py gdpr-erase <email>`, or `dsr erase <id>`:

| Data                                                           | What happens                                                                                                                                                                                                 |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Suppression list                                               | The address is kept only as its erasure token, replacing any plain entry (an opt-out, a bounce or a block), so the person is never collected or emailed again: the developer's reading of Art. 21(3) (L5.2). |
| Company                                                        | Blocked, kept only as a keyed domain token; its daily send counts are deleted.                                                                                                                               |
| Contact                                                        | Anonymised in place: address → erasure token; name, role, company name, source page and the qualification and verification notes cleared.                                                                    |
| Emails sent                                                    | Address → token; subject and body preview → "[erased]"; SMTP responses and errors have the address redacted.                                                                                                 |
| Opens                                                          | IP address and User-Agent cleared.                                                                                                                                                                           |
| Basis record, lead alert (their reply), unsubscribe links sent | Deleted.                                                                                                                                                                                                     |
| Follow-ups, retries                                            | Active enrolments and queued retries cancelled; the address redacted from retry errors.                                                                                                                      |
| Request register                                               | Kept under the token; the address, name and company redacted from the outcome text.                                                                                                                          |
| Access answer, held opt-out files                              | Deleted.                                                                                                                                                                                                     |
| Activity log                                                   | **Rows kept** as the audit trail (accountability, Art. 5(2)); the address in them is replaced by the erasure token, and the person's name and company in those rows by "[erased]".                           |

The erasure token is a keyed hash of the address (HMAC-SHA256 with
`ERASURE_KEY`; `erasure_token` in `modules/database.py`). It keeps the audit
trail readable and lets a re-collected address be recognised. With the key it
can only confirm whether a given address was erased; without the key, not even
that. It is still a **pseudonym, not anonymisation** (L5.2). The key is backed
up apart from the database and never changed (`docs/DEPLOY.md` section 6;
L5.10).

The database erasure is committed first. Its steps on files follow, each
tried whatever became of the others: rewriting the journal, and deleting the
access answer and the held opt-outs. A file another program holds open does
not stop the erasure half-way: a record by tokens only stays in
`data/spool/journal/` until every step has landed, `doctor` reports it
("Erasures finished"), and the next erasure, backup or `replay-journal`
finishes it (`docs/DEPLOY.md` section 9).

`gdpr-erase` then lists what it cannot reach: the sending mailbox (Sent Items,
Inbox and Junk Email), the lead-alert recipients' mailboxes and the log files,
and reminds the operator to tell each recipient of the data (Art. 19; L5.4,
L5.6). Retention erases with the same routine, but adds nothing to the
suppression list and blocks no company (section 6).

## 8. Operator responsibilities (not enforced by code)

- Keep the agent off, dry runs included, while the operating rule the counsel
  sheet proposes holds (section 2; L1.9).
- Fill in the legal identity, and publish the privacy notice it links to once
  counsel has approved it (L3.1).
- Set `ENABLED_COUNTRIES` and `UNKNOWN_COUNTRY_ALLOWED` only from counsel's
  signed country table (L1.1, L1.15).
- Have the legitimate-interest assessment completed for each country where it
  is relied on (L2.1), and review it periodically and before any change of
  target market.
- Honour objections across **all** of SUPPLECO's systems, not just this tool
  (L5.12).
- Answer access and erasure requests within one month (Art. 12(3)), from the
  register (`main.py dsr list`).
- Schedule the daily retention run and the backups (`docs/DEPLOY.md` sections
  8 and 9).
- Send from an authenticated business domain with SPF, DKIM and DMARC (see
  `DELIVERABILITY.md`).
- Keep open tracking off unless recipients have consented.

## 9. Security measures (GDPR Art. 32)

- **The AI stays on SUPPLECO's own machines.** The only AI the agent can use is
  a model on SUPPLECO's own server (`LLM_PROVIDER=openai_compatible`, for
  example Ollama). No cloud AI service is supported: a `.env` that still
  holds one of the old cloud AI settings, `GEMINI_API_KEY`, `GROQ_API_KEY`
  or `GROQ_MODEL`, stops start-up, naming the
  line to delete, and so does one set in the environment (Docker's
  `env_file` puts `.env` there). No outside AI company receives any data.
- **The AI's address must be private.** `LLM_BASE_URL` is accepted only when
  every address its host resolves to is on this machine (`127.x`, `::1`) or
  the private network (`10.x`, `172.16.x` to `172.31.x`, `192.168.x`,
  `fc00::/7`). Public, link-local, multicast and unspecified addresses are
  refused. The check runs when the settings load, in `doctor`, and again
  before every AI question, so a DNS answer that changes later cannot send a
  prompt outside; a refused question is not sent at all. The question then
  goes to the address the check found, not to the name looked up again.
- **No detours.** AI questions follow no redirect, go through no proxy named
  in the environment, and always have a time limit (`LLM_TIMEOUT_SECONDS`).
- **Errors do not print secrets.** A configuration error names the setting
  and what is wrong, never the value: a password typed under a misspelt name
  is not printed to the screen or the Docker log. Secret settings
  (`SMTP_PASSWORD`, `IMAP_PASSWORD`, the `SENDER_n_PASSWORD` slots,
  `SENDER_ACCOUNTS`, `UNSUBSCRIBE_SECRET`, `ERASURE_KEY`,
  `DASHBOARD_PASSWORD`, `LLM_API_KEY`, `MICROSOFT_CERTIFICATE_PASSPHRASE`,
  `LISTS_KEY`) stay out of every printed copy of the settings. A test finds every setting
  named like a password, passphrase, secret, key or token by itself, so a
  new one cannot be forgotten.
- **The two keys are long.** The agent refuses to start with an
  `UNSUBSCRIBE_SECRET` shorter than 32 characters, and a new database refuses
  an `ERASURE_KEY` shorter than 32 characters. A database that already keeps
  a shorter key still opens for the other commands (the key can never
  change), but `doctor` fails, and the agent and the dashboard refuse to
  start, until it is started again with a new database and a long key.
- **The client and competitor lists can be kept encrypted.** With
  `LISTS_KEY` set (at least 32 characters), each list is kept on disk only
  encrypted (Fernet, from the `cryptography` package), read into memory
  only, and written back encrypted when the dashboard marks a client. A
  plain list, a wrong key or a changed file is refused, and live sending
  stops as for a missing list; it is never read as an empty list. A copy of
  the folder without the key shows neither list. It does not protect against
  someone with the whole disk and `.env`: that is disk encryption
  (`docs/DEPLOY.md` section 13). Blank, `doctor` warns that the lists are
  plain.
- **The agent and the dashboard check the security rules at every start.**
  The secrets, the AI address and the Microsoft sign-in settings are refused
  when the settings load, for every command; a blank `ERASURE_KEY` when it
  is set; the dashboard password when the dashboard starts. The two erasure
  key checks of `doctor` ("Erasure key", "Erasure key matches the database")
  now also run before the agent or the dashboard opens the database, and
  stop it in words.
- **A restore cannot quietly lose an opt-out.** `main.py restore-drill`
  restores a backup into a temporary folder, replays the journal as a real
  restore would, and fails unless every opt-out, company block, erasure and
  held opt-out the live database and the journal know of is in the restored
  copy, no one erased is back in clear, and the erasure key matches. IT runs
  it every three months (`docs/DEPLOY.md` section 9). A restore also keeps
  the company of someone erased after the backup only as its code, even when
  the backup did not hold that person, and the drill checks it with the
  journal alone when the live database is lost.
- **A restore keeps every block and bounce too.** The dashboard's Block and
  Unblock, a bounce and a refusal by the recipient's server are written to
  the journal at the moment they are saved, as opt-outs and company blocks
  already were (an erased person only as their code). `main.py
  replay-journal` applies them again in the order they happened, so of a
  block and its unblock the later one holds, and never lifts anything but
  an operator's own block. It also replaces an erased colleague's company
  domain with its code in the backup's activity log and deletes its daily
  send counts, as the erasure did. The restore drill counts what is missing
  by kind: opt-outs, company blocks, address blocks, bounces, erasures.
- **A restore does not make the agent send an email twice.** Every email
  sent is written to the journal when it is recorded as sent: who it went
  to, which message, which follow-up step, when, from which mailbox and with
  which notice version. No subject and no text, and an erased person only as
  their code. A dry run and an email the server refused are not written.
  `main.py replay-journal` puts back each email the restored database does
  not know: the contact counts as emailed at that time, the follow-up step
  is moved on, the basis is recorded again, and today's counts per company
  domain and per mailbox include it. It starts no follow-ups, reopens no
  contact that is closed, and changes nothing when it is run a second time.
  An email to an address the backup does not hold is kept under the
  address's code, never the address (table `prior_sends`); if the agent
  collects that address again, it starts as already emailed, and a Direct
  Send to it asks first. The restore drill counts the emails sent since the
  backup and fails if one is missing after the replay.
- **There is a written plan for incidents**: `docs/INCIDENT_PLAYBOOK.md`
  (a mailbox takeover, wrong people emailed, a data leak or a lost laptop,
  a restore).
- **Logs hold no secrets.** Besides addresses, unsubscribe tokens and IP
  addresses (IPv4 and IPv6), every log line is cleaned of API keys
  (`sk-...`, `gsk_...`, `AIza...`), long values after `key=`, `token=` and
  `secret=` (or a quoted name and a colon, as in JSON), anything after
  `password=` (all of a quoted one), and the value of an HTTP
  `Authorization`, `api-key` or `x-api-key` header, a signed token on its own
  (`eyJ...`) and an XOAUTH2 sign-in answer. The name stays, so the
  line still says what was there. An IPv6 address of fewer than three
  groups (`::1`, `2001::1`) is not masked.
- **Data folders are private in the container.** On Linux (the Docker
  image), `data/` and its `backups/`, `exports/` and `spool/` folders are
  created readable by the app's own user only (0700), and the journal and
  the database file 0600.
  On Windows the folders take the permissions of the install folder, which
  SUPPLECO's IT sets.
- **The container is locked down.** `docker-compose.yml` publishes the
  dashboard on this machine only (`127.0.0.1`), runs the image read-only
  (only `data/`, `logs/`, `assets/`, `templates/` and `/tmp` can be
  written), drops every Linux capability and forbids gaining new ones. The
  image is built from an exact Python version and leaves out the
  documentation, the tests, the scripts and every spreadsheet, PDF and
  Markdown file. Every required package has an upper version limit.
- **The dashboard always has a password.** It refuses to start, wherever it
  listens (this machine included), without a `DASHBOARD_PASSWORD` of at
  least 12 characters, and `doctor` fails on the same rule. One address that
  types 10 wrong passwords within 15 minutes is shut out (answer 429) until
  the oldest of them is 15 minutes old.
- **The dashboard only takes changes from its own page.** A request that
  changes something (start, stop, send, save, settings) must say it comes
  from the dashboard's own address (the browser's `Origin` header, or else
  `Referer`); one that says nothing, or names another site, is refused. It
  must also be JSON. Bodies over 1 MB are refused (413). A wrong type in a
  field (a number where text belongs) gets a plain 400, never a crash.
- **The page runs only its own code.** A Content-Security-Policy lets the
  page load scripts, styles and fonts only from the dashboard itself. The
  dashboard serves its two libraries, Chart.js 4.4.1 and Font Awesome
  6.4.0, from `static/vendor/`: copies checked against the hashes their
  publishers list, and checked again by the tests
  (`static/vendor/README.md`). So the page asks no other server for
  anything, and a hacked CDN cannot change it. Inline scripts are refused,
  so a company name scraped from a hostile page cannot run as code. The
  template preview runs in a sandbox with no scripts and no access to the
  dashboard.
- **A production web server, and no debugger.** The dashboard is served by
  waitress, which refuses a body over 1 MB before reading it. Flask's own
  development server runs only when asked for (`--dev-server`), and only
  when nothing outside this computer can reach the dashboard. Neither ever
  runs Werkzeug's debugger (which runs any code it is sent), whatever
  `DEBUG` says.
- **The tracking address is closed while tracking is off.** `/track/...`
  answers 404 then; while it is on, one address may load it 60 times a
  minute.
- **Page fetches reach only the public internet.** Before the agent fetches
  a web page (a search result, a company's contact page, its `robots.txt`),
  it looks up the site's address and refuses it unless every address the
  name has is public: never this machine (`127.x`,
  `::1`), the office network (`10.x`, `172.16-31.x`, `192.168.x`,
  `fc00::/7`), link-local addresses (`169.254.x`, where cloud servers keep
  their secrets), multicast, reserved or unspecified ones, also when written
  as a number or inside an IPv6 address. Redirects are followed by the agent
  itself, at most five, each one checked the same way; only `http` and
  `https` are followed. The connection goes to the address that was checked,
  so a DNS answer that changes a moment later cannot move it. A host name
  written with anything but letters, digits, dots, dashes and underscores
  (for example a `%` escape) is refused, because the two web libraries read
  such a name differently. At most 2 MB of a page is read; the rest is
  never downloaded. A compressed page is unpacked a little at a time, so a
  tiny "zip bomb" cannot fill the memory before that limit is checked. Page
  fetches ignore any proxy named in the environment. A request for a page
  takes at most 15 seconds, also when a site sends its page one byte at a
  time. When two parts of the agent fetch pages at the same moment, each
  connection still goes to the address checked for it.
- **No company-name lookup service.** When a web search finds nothing, the
  agent has no pages for that query and asks no one else.
- **The optional mailbox check stays on the public internet too.** With
  `VERIFY_SMTP_PROBE` on, the agent asks the prospect's mail server whether
  the mailbox exists. It first checks that the mail server's address is
  public, by the same rules as page fetches, and connects only to the
  address it checked. A mail server on a private address is not contacted,
  and that email address fails the check.
- **Huge emails are read in part.** The inbox scan asks each message's size
  first. Above 10 MB it reads only the headers and the first 256 KB of the
  text, without marking the message read: an opt-out, a data request or a
  bounce report is short text at the start, so it is still recognised.
- **Forged mail cannot stop a company.** SUPPLECO's mail server writes what it
  checked into an `Authentication-Results` header. The agent reads only the
  top-most one (the server adds its own on top; a sender can write fake ones
  only below it). When it says `dmarc=fail`, or both `spf=fail` and
  `dkim=fail`, or `compauth=fail` (Microsoft 365's own verdict), the message
  is treated as possibly forged. So is a message from a domain with no DMARC
  record (`dmarc=none`, or no DMARC result) unless SPF or DKIM passed for
  the sender's own domain (or a part of it, such as `mail.brand.de` for
  `brand.de`): a forger can pass both checks for a domain of their own, and
  that pass does not count. A sender's address copied into the header
  cannot add a result of its own: text in quotes (which an address may
  hold, with `;` and `@` inside) is read as part of the address. A header
  the agent cannot read (a bracket or a quote that never closes) is also
  treated as possibly forged. For a possibly forged message, an opt-out or a
  data request in it still stops that one address (honouring it harms no
  one), but never the whole company, and a bounce report in it marks no
  address as bounced. Each such message is listed on the dashboard's
  activity log as `MAIL_UNAUTHENTICATED`; for a data request, check who sent
  it before answering. Mail without the header is handled as before.
- **A bounce report must be about an email the agent sent.** When the report
  names the original email's Message-ID, the agent must have sent that email
  to that address; when it names none, the agent must have emailed the
  address in the last 30 days. Anything else marks no one bounced and shows
  as `BOUNCE_UNMATCHED` in the activity log. A genuine bounce for an email
  the agent sent still marks the address at once.
- **Unsubscribe links are the opaque kind only.** A link must carry the
  opaque code (`u` and 32 hex digits) recorded when the email was made. The
  old form (the encoded address and a signature), which anyone holding
  `UNSUBSCRIBE_SECRET` could make for any address, is refused; no email with
  one was ever sent.
- **No mailbox password for Microsoft 365.** With Microsoft sign-in set up
  (`MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID`,
  `MICROSOFT_CERTIFICATE_PATH`), every Microsoft 365 mailbox signs in to
  IMAP and SMTP with a token from Microsoft, got with a certificate whose
  private key stays on the agent's machine, and the agent refuses to start
  while a password is still set for such a mailbox, under whatever Microsoft
  server name it is listed (`smtp.office365.com`, `outlook.office365.com`,
  `smtp-mail.outlook.com`, ...). A stolen copy of `.env`
  then holds no mailbox password. The token comes only from
  `login.microsoftonline.com` (certificate checked, no redirect, no proxy,
  time limit), lives in memory for about an hour and is never logged. The
  key file must be readable by the agent only (refused otherwise on Linux
  and in Docker), is never committed or built into the image, and SUPPLECO's
  IT gives the app access to the outreach mailboxes only. A refused sign-in
  stops sending as a wrong password does. `doctor` warns while a Microsoft
  365 mailbox still uses a password and 30 days before the certificate ends.
  Steps for IT: `docs/DEPLOY.md` section 12.
- **Mail servers must prove who they are.** Sending (SMTP on port 465, or
  STARTTLS on any other port), `doctor`'s live login check and inbox reading
  (IMAP) all check the server's certificate and its name; a test fails if
  any of them stops doing so.
- **A guard that cannot be loaded stops the start.** The parts that limit,
  check or stop sending are: the warm-up scheduler (daily limit), the domain
  throttle (limit per company), the relevance filter, the email verifier,
  lead scoring (minimum score), the unsubscribe handler, the inbox monitor
  (reads opt-outs, data requests and bounces), the blocklist check, and the
  AI relevance check while it is switched on and an AI server is set. If
  the settings switch one on and it cannot be loaded (a broken install: a
  package missing after an update, a file lost), no agent is built and
  nothing is sent. `main.py run` prints `Refusing to start` with the name of
  the part, and the dashboard's Start and Send answer the same. All three
  write it to the log file and list it under Recent Errors as
  `GUARD_UNAVAILABLE`, so an agent started by a scheduled task is not just
  seen as "not running". Before, the agent logged one warning
  and sent with that guard off. The extras (retry queue, follow-ups, open
  tracking, the AI that words the emails, discovery, learning) only add mail
  or a record: the agent runs without one, and lists it under Recent Errors
  as `COMPONENT_MISSING`. If the blocklist check is lost while the agent
  runs, sending pauses and stays paused. The client and competitor lists
  were already safe: while a list that is switched on is not loaded, every
  live send is refused (`CLIENT_LIST_MISSING`, `COMPETITOR_LIST_MISSING`).
- **A write that fails half-way is undone.** The retention job, an erasure
  on request, the follow-up sequences and the "replied" marks write on one
  long-lived database connection. If one of them fails part-way, what it had
  not saved is rolled back before anything else happens. Before, the
  half-done write kept the database locked for everyone else, and the next
  save on that connection, for anything, stored it as if it were complete:
  an erasure could be recorded as done with the person's address still in
  other tables. An erasure that was rolled back is found again and done
  whole by the next retention run; the day is not marked done. Opt-outs and
  bounces are saved on their own connections and are not touched.
- **The dashboard keeps showing an error.** Recent Errors lists the errors
  of the last 14 days, newest first. Before, it looked only at the newest
  200 log lines of every kind, so an error disappeared after a busy hour and
  the panel said there were none.
- **Still contacted outside the machine.** Web search (DuckDuckGo through the
  `ddgs` library), the pages of the companies found, DNS and DNS blocklist
  lookups, the sending and receiving mail servers, the prospects' mail
  servers (only with `VERIFY_SMTP_PROBE` on), Microsoft's sign-in server
  (`login.microsoftonline.com`, only
  with Microsoft sign-in; it receives the app's ids and signed request, no
  personal data), and pypi.org when IT runs `main.py security-check` (it
  sends the names and versions of the installed packages, no personal data).
  The dashboard page loads nothing from a CDN. None of them is an AI
  service.
