# User manual - Omega IFS Outreach Agent

For the people at Omega who use the agent day to day. No technical
knowledge is needed. Installing, updating, backups and anything that goes
wrong with the computer itself belong to IT: they use `docs/DEPLOY.md` and
`docs/INCIDENT_PLAYBOOK.md`.

The pictures in this manual were made with invented companies and people.
Your screens show your own data.

## 1. What the agent does, and what it does not do

**It does:**

- search the web for European manufacturers that buy small metal parts
  made to their drawings (automotive Tier-1 suppliers, window and furniture
  hardware makers, electrical equipment and appliance makers and similar),
  starting from Omega IFS's own buyer keywords and the searches you add;
- read the companies' own websites for a business email address;
- check each address (does the domain take mail, is it a real business
  address, is the company a customer or a competitor of Omega) and give
  each contact a score;
- send Omega's first email, in Italian to Italian companies and in English
  to the others, with no attachment (the Company Profile PDF is optional and
  off for Omega), from Omega's mailbox, within
  safe daily limits and office hours;
- send the follow-ups, if they are switched on;
- read the mailbox for replies, bounces and opt-outs, stop emailing anyone
  who says no or whose address does not exist, and tell a named colleague
  when a prospect replies with interest;
- keep a record of everything it did, which you see on the dashboard.

**It does not:**

- email an existing Omega customer or a listed competitor, a personal
  mailbox (Gmail, Libero and the like), a certified-mail (PEC) address, or
  anyone in a country that is not switched on;
- email again anyone who opted out, whose email bounced, or who was blocked
  or erased: that is permanent;
- answer replies. A person at Omega answers every reply;
- send anything to an outside AI company. If an AI is used at all, it runs
  on Omega's own server;
- start again by itself after the computer restarts. Someone has to start
  it (section 3).

## 2. Who does what

| Who | What |
| --- | --- |
| You (the daily user) | Look at the dashboard, start and stop the agent, add searches, mark clients, block a contact, answer replies, watch the warnings. |
| The person named for privacy | Answers data requests (section 7) with IT. |
| IT | Installs and updates the agent, the mailbox and its sign-in, backups, the lists' key, and everything in `docs/DEPLOY.md` and `docs/INCIDENT_PLAYBOOK.md`. |

## 3. Starting and stopping

1. IT starts the dashboard on the agent's computer (`main.py dashboard`).
2. Open it in a browser on that computer: <http://127.0.0.1:5000>. If IT
   has set up access from other computers, use the address IT gives you.
3. Type the dashboard's user name and password (IT gives them to you).
   After 10 wrong passwords from one computer, that computer is locked out
   for 15 minutes ("Too many wrong passwords. Try again later.").
4. Press **Start Agent** (top right). The agent now runs in a loop: it
   searches, checks and sends, then waits, then starts again.
5. Press **Stop** to stop it. It finishes the search or email it is on,
   then stops. Nothing is lost.

The agent runs only while the dashboard runs. After the computer restarts,
someone has to start the dashboard and press Start again.

**Dry run.** While IT keeps the agent in dry-run mode, nothing is emailed:
the agent does everything else and shows "DRY RUN - nothing is sent" on the
Ready to send card. Use it to try things safely.

**Live sending blocked.** The line at the bottom left of every page says
whether live emails can go out. When it says "Live sending blocked - see
Ready to send", the Ready to send card (section 4.1) says why.

## 4. The dashboard, tab by tab

The main app is now at `/` (screens Today, Review, Leads,
Markets); the full dashboard this section describes is at `/classic`
(**Classic dashboard** at the foot of the app's sidebar).

The tabs are on the left. At the top of every tab:

- **Add Query**: type a search, for example `automotive door lock manufacturer in Germany`,
  and press Add Query. The agent runs it on its next pass.
- **Start Agent** and **Stop** (section 3).

The figures refresh by themselves every few seconds; the charts, Opens
Tracked and Warmup Status once a minute.

### 4.1 Overview

![The Overview tab](manual/01-overview.png)

- **Total Contacts**: every contact the agent has found and kept.
- **Emails Sent**: emails that really went out (dry runs are not counted).
- **Opens Tracked**: emails known to be opened. Usually 0: open tracking is
  off by default, and many mail programs hide opens anyway.
- **Warmup Status**: "Day 1 (0/5)" means the mailbox is on its first day of
  warm-up, has sent 0 emails today, and may send 5 today. The limit grows
  each day, so a new mailbox is not taken for a spammer.
- **Ready to send**: one row per thing a live email needs. **OK** is fine.
  **Problem** says what is missing and what it stops. **Not checked** means
  that check is switched off. The red line at the top sums up what stops
  live first emails now. Most rows are fixed by IT (a missing setting or
  file); tell IT what the row says. **Check again** re-runs the checks.
  The row **Journal replayed** fails only after a restore from a backup:
  call IT, do not try to start the agent.
- **Privacy and inbox - to act on**:
  - *Inbox gap*: a time the mailbox was not read (so a reply or opt-out may
    not have been seen yet). Tell IT.
  - *Held opt-outs*: opt-outs that came in while the database was busy.
    They are applied at the next start, and those addresses are not emailed
    meanwhile.
  - *Erasures finished*: whether every erasure is complete.
  - *Data-subject requests*: open requests and whether one is overdue
    (section 7).
- **Emails Sent per Day**, **Lead Funnel** (found, emailed, opened,
  replied), **Contact Status** (how many contacts are in each state) and
  **Success Rate**: the trend over the last weeks.
- **Live Activity**: what the agent did, newest at the bottom. Untick
  **Follow** to stop the list from scrolling while you read.

### 4.2 Insights

![The Insights tab](manual/02-insights.png)

- **Deliverability & Reputation**: the sending address; whether its domain
  has SPF and DMARC records (they help Omega IFS's email reach the inbox;
  "could not be checked just now" means the check could not run, not that
  they are missing); emails sent; the **bounce rate** (emails that did not
  arrive; above the limit shown, the agent pauses all sending by itself);
  unsubscribes; how many addresses are suppressed (never emailed again);
  the **circuit breaker** ("armed" is normal; "TRIPPED - sending paused"
  means sending stopped by itself because of bounces); the **send window** (the hours and days the
  agent may send, and whether it is open now); whether the Company Profile
  is attached; and whether the mailbox is on a spam blocklist (**Re-check
  blocklists** checks again).
- **Interested Leads**: prospects who replied with interest. Each is also
  emailed to the colleague named in `LEAD_ALERT_EMAIL`; if nobody is named,
  the panel says so. Answer them yourself, from the mailbox.
- **Active Features**: which parts of the agent are switched on (green) or
  off (grey). IT switches them.
- **A/B Performance**: when two versions of the first email are tried, how
  each does.
- **Follow-up Sequences**: contacts in follow-ups, finished, cancelled (for
  example because they replied or opted out).
- **Retry Queue**: emails that failed for a passing reason and will be tried
  again; "exhausted" ones were given up.
- **What the agent has learned**: which kinds of companies and countries
  reply best. The agent uses this to choose its next searches.
- **Per-Domain Sends (Today)**: how many emails went to each company today,
  and the limit per company.
- **Settings**: the dials you may change without IT, such as emails per
  hour, the delay between emails, the send hours and days, the limit per
  company, and the minimum score to email. A change reaches the running
  agent at its next pass. A value that is not allowed is refused with the
  reason. Mailbox, passwords and switches stay with IT.
- **Recent Errors**: the newest 20 errors of the last 14 days. Section 8
  says what the common ones mean.

### 4.3 Contacts

![The Contacts tab](manual/03-contacts.png)

Every contact found, with its score, name, address, company, status and the
date it was found. The buttons above the table filter by status; the search
box finds a name, address or company.

The status of a contact:

| Status | Meaning |
| --- | --- |
| Pending | Waiting for its first email. |
| Emailed | The first email went out. |
| Responded / Replied | The person replied. Follow-ups stop. |
| Unverified | The address could not be confirmed; not emailed. |
| Low score | Not a good enough fit to email (the minimum is a setting). |
| Irrelevant / Off-target | Not a company that builds products with metal parts, or not in a target market. |
| Failed | The email could not be sent. |
| Blocked, unsubscribed, bounced and other closed states | Never emailed again. |

The small **EN** or **IT** tag beside an address is the language of its
emails. For a contact not yet emailed you can change it.

The buttons on each row:

- **The arrow** opens the row: why the contact was or was not emailed, and
  its history.
- **Client**: this company is an Omega IFS customer. It is added to the client
  list (after you confirm), and the agent never emails it as a prospect.
- **Send**: put the contact back in the queue. A contact that a check
  turned down (low score, off-target, unverified) is checked again before
  anything is sent.
- **Block**: never email this contact. The message offers an undo; later,
  **Unblock** on the row undoes it. A block made because the person asked
  must never be undone.

### 4.4 Campaigns

![The Campaigns tab](manual/04-campaigns.png)

Every email the agent sent or tried: date, recipient, the mailbox it was
sent from, subject, status and opens. The statuses say what happened:

| Status | Meaning |
| --- | --- |
| sent / emailed | It went out. |
| Sending - not settled | Being sent right now, or the computer stopped in the middle. It is never sent twice; after 26 minutes it becomes the next line. |
| Sent, not confirmed - not repeated | It may have gone out. It is not sent again. |
| Refused by the server | The recipient's mail server said no. |
| failed | It could not be sent. |
| Preview only - not sent | A dry run. |

### 4.5 Direct Send

![The Direct Send tab](manual/05-direct-send.png)

To email one chosen business address now, with the same email the agent
sends. Fill in the address, optionally the name and the company, and **how
this contact reached Omega IFS** (an inbound request, a trade fair, an existing
customer, a referral): a live send needs it. Press **Dry run (no send)** to
see the email first, then **Send Email**. Tick the box to add the contact to
the automatic follow-ups.

Direct Send refuses, with the reason: personal and certified-mail
addresses, anyone who opted out, bounced or was blocked, clients and listed
competitors, a country that is not switched on, a second email to someone
already emailed (it asks first), and any send while the agent has paused
sending.

The **Spam / inbox test** card is switched off on purpose (it says why).

### 4.6 Template Editor

![The Template Editor tab](manual/06-template-editor.png)

The text of the emails: the first email and the two follow-ups, in English
and in Italian. Choose one in **Text to edit**, change it, look at the
**Live Preview**, then **Save Template**. The next email uses the new text;
no restart is needed.

Be careful: the two markers `{{legal_footer}}` and `{{unsubscribe_url}}`
(the legal footer and the opt-out link) must stay in every template. If one
is removed, live sending is refused until it is back (the Ready to send row
"Legal footer in templates"). Ask
Omega IFS's counsel before you change what the email says about privacy.

### 4.7 Search Logs

![The Search Logs tab](manual/07-search-logs.png)

Every search the agent ran: when, the words, where it searched, how many
results and how many websites it read, and whether it finished.

## 5. Your routine

**Every working morning (5 minutes):**

1. Open the dashboard. Is the agent running (Stop is lit, Start is not)?
   If not, press Start Agent.
2. Overview: the bottom-left line. If live sending is blocked, read the Ready
   to send card and tell IT what the red rows say.
3. Insights, **Recent Errors**: anything new since yesterday? Look it up in
   section 8.
4. Insights, **Interested Leads**, and the mailbox: answer every interested
   reply the same day.
5. Overview, **Privacy and inbox**: an overdue data request is urgent
   (section 7).

**Every week (20 minutes):**

1. Insights, **Deliverability & Reputation**: the bounce rate stays well
   under the limit, the circuit breaker is "armed", no blocklist.
2. Contacts: look through the newest contacts. Mark the customers you
   recognise with **Client**; **Block** anyone who must not be emailed.
3. Search Logs and **What the agent has learned**: add searches for the
   markets that work (Add Query).
4. Campaigns: read a few emails that went out; are they right?
5. Ask IT whether the weekly security check and the backups ran.

## 6. The client and competitor lists

The agent never emails a company on the client list (Omega IFS's customers) or
on the competitor list. Both lists are kept encrypted on the agent's
computer, so a copy of the folder without the key shows neither.

- **One new customer:** find any of its contacts on the Contacts tab and
  press **Client**. The company is added to the encrypted list at once.
- **Many changes, or a new competitor:** ask IT. IT opens the list for a
  moment, you make the changes in Excel, and IT encrypts it again and
  deletes the plain copy (`docs/DEPLOY.md` section 13, "Changing a list by
  hand"). Never keep a plain copy of either list on the computer, in email
  or on a shared drive.
- **If a list cannot be read** (the Ready to send row "Client list" or
  "Competitor list" shows Problem), live sending stops until it is fixed.
  This is on purpose: a list read as empty would let the agent email
  Omega's customers. Call IT.

## 7. Replies, opt-outs and data requests

- **Replies.** The agent reads the mailbox. A reply with interest becomes an
  Interested Lead and an alert. A person at Omega IFS answers every reply, from
  the mailbox. The agent stops the follow-ups for anyone who replied.
- **Opt-outs.** Someone can say no by clicking the link at the foot of the
  email, by replying (for example "stop", "unsubscribe", "cancellami"), or
  by asking you. The first two are handled by the agent. If someone asks you
  by phone or another way, press **Block** on their contact. A no is
  permanent: never Unblock it, and never add the person again by hand.
- **Bounces.** An email that cannot be delivered marks the address as
  bounced, and it is never emailed again.
- **Data requests** (someone asks what Omega holds about them, or to be
  erased). The law gives one month to answer. A request that arrives in the
  agent's mailbox is registered by the agent and shows in **Privacy and
  inbox**. A request that arrives another way (post, phone, another
  mailbox) must be registered the same day. The named person and IT answer
  them with the `main.py dsr` commands, as `docs/privacy/dsr_procedure.md`
  says. Give every such request to the named person at once.

## 8. Common errors and warnings

You see these in **Recent Errors** (Insights), in **Live Activity**, or as
a message after pressing a button.

| What you see | What it means | What to do |
| --- | --- | --- |
| A red row on **Ready to send** | Something a live email needs is missing. Dry runs still work. | Tell IT the exact words. |
| `AGENT_HALTED` (SMTP authentication failed) | The mailbox refused the agent's sign-in. Sending stopped. | Call IT. Do not keep pressing Start. |
| `IMAP_ERROR`, `IMAP_STALE`, `INBOX_UNREAD` | The mailbox could not be read. Replies, bounces and opt-outs are not being seen, so live sending stops. | Call IT the same day. |
| `BOUNCE_GUARD`, circuit breaker "TRIPPED" | Too many emails bounced, so all sending paused by itself. | Tell IT and the owner; do not change the bounce limit to get going again. |
| `DOMAIN_BACKOFF` | Emails to one company bounced; that company is skipped for now. | Nothing. |
| `WARMUP_LIMIT` | Today's limit is reached. | Nothing; it goes on tomorrow. |
| `COUNTRY_LOCK`, `OPT_OUT_ROUTE_MISSING`, `CLIENT_LIST_MISSING`, `LEGAL_IDENTITY_MISSING`, `PROFILE_PDF_MISSING` | A live send was refused because a setting or a file is missing. | Tell IT. |
| `GUARD_UNAVAILABLE` ("Nothing was started or sent") | A part that protects sending is broken, so the agent will not start. | Call IT. |
| `COMPONENT_MISSING` | An extra (for example the follow-ups) could not be loaded; the agent runs without it. | Tell IT. |
| `JOURNAL_NOT_REPLAYED` ("restored from a backup ... replay-journal") | The database was restored and is not complete yet. Start and Send are refused. | Call IT (`docs/INCIDENT_PLAYBOOK.md`, incident 4). |
| `EMAIL_UNCONFIRMED`, "Sent, not confirmed - not repeated" | An email may or may not have gone out. It is never sent twice. | Nothing. |
| `RETRY_EXHAUSTED` | An email failed several times and was given up. | Nothing, unless there are many: then tell IT. |
| `LEAD_ALERT_UNCONFIGURED` | A prospect replied, but nobody is named to be told. | Read the mailbox now; ask IT to set `LEAD_ALERT_EMAIL`. |
| `MAIL_UNAUTHENTICATED`, `BOUNCE_UNMATCHED` | An email in the mailbox looked forged, or a bounce was about an email the agent did not send. The agent did not trust it fully. | Nothing, unless many: tell IT. |
| `RETENTION_FAILED` | The daily clean-up of old data failed. | Tell IT. |
| `SECURITY_CHECK` | The weekly check found a program part that needs a security update, or could not run. | Tell IT this week. |
| "Too many wrong passwords. Try again later." | 10 wrong passwords from this computer. | Wait 15 minutes. If it was not you, tell IT. |
| "Cross-origin request rejected" | A button was pressed from another page or tool, not the dashboard itself. | Reload the dashboard and try again. |

## 9. When to call IT at once

Stop the agent first (press **Stop**), then call IT, when:

- emails went to the wrong people, or many at once by mistake;
- someone says they received an email they should not have (a customer, a
  person who said no);
- the mailbox may have been taken over (strange sent emails, password
  messages from Microsoft);
- the computer, a backup or a copy of the data is lost or stolen;
- the dashboard asks for a restore ("Journal replayed" fails).

IT follows `docs/INCIDENT_PLAYBOOK.md`, which says what to do in the first
hour and the first day. For installing, updating, backups and the
Microsoft 365 sign-in, IT uses `docs/DEPLOY.md`.
