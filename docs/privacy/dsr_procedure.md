# Answering data-subject requests (outline)

Status: outline for counsel. Step G11 is built, and this outline describes the agent as handed to SUPPLECO in November 2026. It gives a procedure for access (Art. 15), erasure (17) and objection (21). Rectification (16), restriction (18) and portability (20) have no command of their own: `main.py dsr add` registers them with the kind "other", and until the outline adds steps for them they are handled by hand by the named person. Who answers requests and who confirms erasures is L5.5.

Every request is answered without undue delay, and at the latest within one month of receipt. That month can be extended by two further months where necessary, taking into account the complexity and number of the requests. The person is told of any extension, with the reasons, within the first month (Art. 12(3)).

## The commands

None of these commands checks who is asking (L5.3).

| Command                                                        | What it does                                                                                     | What it prints                                                                                                        | What it stores                                                 |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `main.py dsr list` (`--all` for closed ones too)               | Lists the open requests                                                                          | Each request's number, kind, masked address, dates received and due, and status; OVERDUE when an open one is past due | Nothing                                                        |
| `main.py dsr show <id>`                                        | Shows one request                                                                                | The full address, for the operator only (never logged), with the kind, channel, dates, status and outcome             | Nothing                                                        |
| `main.py dsr add <address> --kind <kind> --received <when>`    | Registers a request that came another way; the kind is access, erasure or other                  | Its number and due date                                                                                               | A register entry, due one calendar month after it arrived      |
| `main.py dsr export <id>`                                      | Writes the access answer (see 3)                                                                 | The folder it wrote                                                                                                   | `records.json` and `cover.md` in `data/exports/access-<code>/` |
| `main.py dsr erase <id>`                                       | Asks the operator to type ERASE, then erases the requester's data and closes the request (see 3) | The masked address when it asks, then only the token                                                                  | The erasure                                                    |
| `main.py dsr close <id> --outcome "<text>"`                    | Closes a request                                                                                 | Its number and outcome                                                                                                | The date and the outcome                                       |
| `main.py gdpr-export <address>`                                | Writes the access answer for an address typed in, with no request                                | The folder it wrote                                                                                                   | As `dsr export`                                                |
| `main.py gdpr-erase <address>`                                 | Erases an address typed in, at once, without asking                                              | The token, what it changed, and the checklist of places the code cannot reach (see 4)                                 | The erasure                                                    |
| `main.py suppress-domain list`, `lift <domain> --reason "..."` | Lists company blocks, or lifts one (L1.6)                                                        | The plain blocks and a count of those kept as tokens, or the lift                                                     | A lift in the journal, and its reason in the activity log      |
| `main.py doctor`                                               | The preflight check                                                                              | A warning naming each overdue request by number                                                                       | Nothing                                                        |

When a retention run has already replaced a request's address with its token (see 1), `dsr show`, `dsr export` and `dsr erase` find the address through the token on the do-not-contact list, or report that the person is already erased.

## 1. Receiving and registering

- Requests arrive as replies to the outreach, at the privacy contact address (L2.6), or by post or phone.
- **Replies.** With inbox reading on (`IMAP_ENABLED`, off by default), the agent reads the Inbox and Junk Email folders of each sending mailbox. It recognises erasure and access requests in English, Italian, German, French and Spanish, before the opt-out phrases. A question such as "where did you get my address?" counts as an access request. For each one, at once, it:
  - registers it once, however often the message is read, with its due date;
  - records it as an opt-out: the address goes on the do-not-contact list, and the contact's follow-ups and queued retries end;
  - blocks the company's domain if the agent had emailed the person, never a free-mail or PEC domain (L1.6);
  - never passes it to a lead alert or an AI provider.
- A request that meets a busy database is held on disk, and registered when the hold is applied.
- Because the request is also an opt-out, the next daily retention run erases the contact, even while an access request is open, and replaces the address in its register entry with the token. Whether retention may erase a contact whose access request is open is L5.5. For an erasure request, the do-not-contact entry stays a plain address until `dsr erase` runs, so run it for every erasure request, even after that retention run (see 3).
- With inbox reading off, requests sent by email go unread; whether live sending may rely on the web unsubscribe page alone is L5.5. With inbox reading on, live sending stops while an inbox the agent reads, or its Junk Email folder, has had no complete read within three check intervals, as when the agent cannot log in to the mailbox. After the agent has been stopped, it reads the inbox before it sends again. It reads back at most 14 days, and reports a longer outage: read the mailbox for the rest of that time by hand.
- **The privacy contact address** is read by the agent only if it is one of the sending mailboxes. Otherwise, and for requests by post or phone, the named person registers each one: `main.py dsr add <address> --kind <kind> --received <when it arrived>`.
- `dsr add` only registers: it stops no email (L1.6). An erasure request is stopped for good by `dsr erase` (see 3). No command records an objection that arrives this way as an opt-out (L5.5). The dashboard's Block stops email to a stored contact, but Unblock can undo it. A restore from an older backup keeps it (the journal holds it).
- **The deadline.** Each request is dated from when it arrived, not from when the agent read it: for a reply, when the mail server received it, or failing that the date the message carries; for a request held while the database was busy, when it was held; for one registered by hand, the date given to `--received`. It is due one calendar month later. `dsr list` marks an open request past its due date OVERDUE, and the preflight check warns, naming it by number.
- The register has no place for an extension: after the first month, `dsr list` and the preflight check report an extended request as overdue (L5.5).
- Requests found in the July mailbox are already late; how they are answered is L4.3.

## 2. Identity

- A request from the address SUPPLECO emailed is answered without further identification.
- More information is asked for only where there is reasonable doubt (Art. 12(6)), for example a request from a different address (template 2).
- A request found in a reply is registered under the address it came from, whether or not SUPPLECO emailed it. Nothing checks who is asking (L5.3).
- When SUPPLECO's mail server found the email possibly forged (its `Authentication-Results` header says `dmarc=fail`, both `spf=fail` and `dkim=fail`, or `compauth=fail`; or the sender's domain has no DMARC record and neither SPF nor DKIM passed for that domain; or the header cannot be read), the request is still registered and the address still stops, but the activity log shows a `MAIL_UNAUTHENTICATED` line for it. That is reasonable doubt: before sending any data, confirm with the person at the address (template 2).

## 3. Acting

### Access

- `main.py dsr export <id>` writes the answer to `data/exports/access-<code>/`. The folder is named by the address's keyed token, so no address appears in a file listing. It holds:
  - `records.json`: every database record held under the address or its token: the contact, the emails and their opens, follow-ups and retries, the do-not-contact entry, the basis record, any lead alert, the person's requests, and the activity-log lines that name the address;
  - `cover.md`: the developer's draft of the Art. 15 letter (template 3; L3.1, L5.5).
- The answer leaves out the following. Template 3's letter names all of them but the held opt-out files and the search words:
  - the record of the unsubscribe links sent to the address (step G20);
  - a company block, and the daily send counts kept under the company's domain;
  - the log files, where the address keeps only its first letter and domain;
  - held opt-out files, the backups and the journal;
  - the search words that found the contact.
- Make the export before the next daily retention run where you can (the developer's proposal, L5.5). A request found in a reply is also an opt-out, so that run erases the contact. An answer made after it holds only what the erasure left, such as the token, the score, the source channel and the dates.
- Complete `cover.md` from template 3 before it is sent. The code writes "entered by hand" whenever no source page is recorded, as for an address added by hand with a channel, such as a referral, or a contact a retention run has erased. It names only Microsoft and the sales staff as recipients, and gives the periods only by a link to the notice.
- The named person sends the answer, then closes the request: `main.py dsr close <id> --outcome "<what was sent>"`. The folder is deleted 30 days after it was written, or at once if the person is erased on request.

### Erasure

- `main.py dsr erase <id>` asks the operator to type ERASE, then erases and closes the request with the outcome "erased". It erases as `gdpr-erase` does (steps G6, G7, G16):
  - the address stays on the do-not-contact list only as its keyed token, replacing any plain entry, and the company's domain is blocked as a keyed token (never a free-mail or PEC domain);
  - the name, job title, company, source page and similar details are cleared; the emails' subject and text become "[erased]"; and the address is replaced by the token everywhere else;
  - follow-ups and queued retries end; the lead alert, the basis record, the unsubscribe-link records, the company's daily send counts, any access answer and any held opt-out file are deleted;
  - the person's earlier lines in the journal are rewritten as tokens, and the database's log is emptied.
- When a retention run went first and the register holds only the token, `dsr erase` finds the address through the token on the do-not-contact list and erases it. When it finds none, it reports that the person is already erased, and closes the request without erasing again.
- `dsr erase` does not print the checklist that `gdpr-erase` prints, so work through it after every `dsr erase`:
  - the sending mailbox: Sent Items, Inbox and Junk Email;
  - the mailboxes of the lead-alert recipients (`LEAD_ALERT_EMAIL`);
  - the log files in `logs/`: each mention keeps its first letter and domain until the file is deleted, after `LOG_RETENTION_DAYS` (30) days;
  - each recipient of the data, to be told of the erasure (Art. 19; see 4).
- Backups are not on the checklist. A backup taken before the erasure holds the data until it is replaced; the newest 14 are kept. If one is restored, `main.py replay-journal` erases the person again (step G16, L5.7).
- What stays with the token is listed in L5.2. The erasure confirmation is template 4.

### Objection (L1.6, step G5c)

- The address goes on the do-not-contact list, and the contact is erased at the next daily run (L1.16).
- The company's domain is blocked too when the objection comes by the one-click Unsubscribe button, or by email from someone the agent emailed. A data request found in a reply from someone the agent emailed blocks it as well, and so does an erasure, which keeps the block only as a keyed token. An interested reply never blocks it, and a plain decline stops only the address (L1.16). An email SUPPLECO's mail server found possibly forged (see section 2) never blocks the domain: it stops only its own address.
- With a public `BASE_URL`, a click on the unsubscribe link in the email stops only that address. Without one, the link is an email to the main mailbox, read as an objection by email.
- A link sent after the last backup still works after a restore and `main.py replay-journal` (step G16).
- Free-mail and PEC domains are never blocked whole.
- An operator can lift a company block with a written reason, which is logged (`suppress-domain lift`). The person's own address stays on the do-not-contact list.
- An objection by post or phone: see 1.
- Passing the objection on to sales and the lead-alert recipients is L5.12.

## 4. Telling recipients (Art. 19)

The product tells no one. Art. 4(9) defines a recipient as anyone the data are disclosed to, whether a third party or not, which covers processors (Art. 4(10)). The list therefore covers:

- lead-alert recipients, and a Teams channel if alerts go there (L5.11);
- the provider that first receives mail sent to suppleco.com (L2.5);
- any address-verification service (L2.5); there is no outside AI provider to tell, as the AI runs on SUPPLECO's own server;
- a CRM, if decision S5 names one;
- the copies in the sending mailbox (L5.4).

`main.py gdpr-erase` prints a checklist naming the sending mailbox, the lead-alert recipients' mailboxes and the log files, with a reminder to tell each recipient (step G6); `dsr erase` does not (see 3). Backups are not on it: after a restore, `replay-journal` erases the person again (G16). The other recipients, and a step where the operator confirms each has been told, have no step yet (L5.6).

## 5. Closing

- `main.py dsr close <id> --outcome "<text>"` records the date and the outcome. `dsr erase` closes its own request.
- After an erasure, the register keeps the request only under the token (steps G7, G11).
- A request registered or closed, by hand or from a reply, is also written to the journal, so a restore from an older backup keeps it (step G16).
- The register keeps a closed request for 1,095 days (about three years), and an open one however old: a proposal with no legal source, for counsel to set (L5.1).

## Templates

The drafts are in `dsr_templates_en.md`, for counsel to approve (L5.5). The code writes only the access cover letter (`cover.md`); the named person sends every answer.

1. Acknowledgement.
2. Request for more information, used only on reasonable doubt.
3. Access cover letter: what to add to the `cover.md` that `dsr export` writes.
4. Erasure confirmation, including what happens to backups (L5.7) and to mailbox copies (L5.4).
5. Extension notice, with the reasons.
6. Refusal with reasons.
