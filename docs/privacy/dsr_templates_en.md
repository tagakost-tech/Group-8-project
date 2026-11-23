# Answers to data-subject requests (drafts)

Status: the developer's drafts for counsel. The wording is counsel's, and no answer is sent until counsel approves it (L5.5). In English only (L3.3). The emails themselves now go in Italian to the countries in `ITALIAN_COUNTRIES` (default `IT`) and in English to the rest; whether these answers need an Italian version is for counsel. They go with `dsr_procedure.md`; a number in brackets, such as (3), points to one of its sections.

How to fill them:

- `{id}`, `{received}` and `{due}` are the request's number, the date it arrived and its due date, as `main.py dsr show <id>` prints them.
- `{controller}` is Omega IFS's legal entity name (`LEGAL_ENTITY_NAME`, L6). `{privacy contact}` is the privacy contact address (`PRIVACY_CONTACT_EMAIL`, L2.6).
- Text in square brackets is a choice the sender makes, or waits for the decision it names.
- The named person sends each answer (L5.5), and signs it with their name, `{controller}` and `{privacy contact}`.

## 1. Acknowledgement

Sent when a request is registered. Subject: "Your request about your personal data (our reference {id})".

> Thank you for your message of {received}. We have registered it as a request [for access to / for the erasure of] the personal data {controller} holds about you, under the reference {id}.
>
> We will answer by {due}, one month after your request reached us. If we need more time, we will tell you before then, and why.
>
> [For a request made by replying to our email:] We have stopped all email to your address[, and to your company's domain].

Keep the last sentence only for a request found in a reply: one registered by hand with `dsr add` stops no email (1). Keep the company's domain only where the agent had emailed the person and the domain is not a free-mail or PEC domain (1).

## 2. Request for more information

Only where there is reasonable doubt about who is asking (Art. 12(6)), for example a request from an address Omega IFS did not email (2).

> Thank you for your message of {received} about the personal data {controller} holds (our reference {id}). Before we act on it, we need to be sure that it comes from the person the data is about, because [it was sent from an address we did not write to].
>
> Please [reply from the address your request is about / send us what counsel sets under L5.3].
>
> [What this letter says about the deadline while we wait for your reply: counsel to set (L5.3).]

## 3. Access cover letter

`main.py dsr export <id>` writes this letter itself, as `cover.md` beside `records.json` (3). Its wording is the developer's draft too (L3.1, L5.5). The code fills in the purposes, the data held with a count of records per table, the source pages, the scoring and the rights. Before the letter is sent, the named person completes the four points below.

**Where it came from.** Whenever no source page is recorded, the code writes "No source page is recorded: the address was entered by hand." Replace that sentence, using the contact's `source_channel` in `records.json`. For an address added with a channel:

> Your address reached us through [your company's request to us / a trade fair / our dealings with your company, as our customer / a referral].

For a contact a retention run has already erased, whose channel is kept but whose page is not:

> We found your address on [your company's own website / a contact, legal or about page of your company's website / a third-party directory or listing]. The page itself is no longer recorded: it was erased with your other details.

**Recipients.** The code names only Microsoft and the sales staff. Add the provider that first receives mail sent to our domain (L2.5), any address-verification service (L2.5), and the developer and any hosting provider with access to our data (L2.7). No outside AI company is a recipient: the AI runs on a model on Omega IFS's own server. If AI was in use, say so in those words; the sentence on transfers outside the European Economic Area stays the one counsel approves.

**How long we keep it.** The code refers the person to the notice. Replace that with the periods that apply to them, from the notice's section "How long we keep it" (L5.1).

**What the answer does not hold.** Add this paragraph:

> This answer covers our database. It does not include: the record of the unsubscribe links we sent you, which we keep until your details are erased; [your company's domain, if your objection or request blocked it, and our daily count of emails to that domain, kept for 30 days]; our log files, which keep only the first letter of your address and its domain, until each file is deleted after 30 days; and the copies in our backups, of which we keep the newest 14 (L5.7), and in the journal we keep so that restoring a backup neither undoes opt-outs, blocks, bounces and erasures nor sends you the same email twice.

## 4. Erasure confirmation

Sent once `main.py dsr erase <id>` has run and its checklist is done (3 and 4).

> As you asked on {received} (our reference {id}), we have erased the personal data {controller} held about you.
>
> So that we never collect or email your address again, we keep it only as a code made from it with a secret key: on our do-not-contact list, and in place of your address in our records. The code cannot be turned back into your address. [We keep the block on your company's domain only as a code in the same way.] [L5.2: what else is kept beside the code, if counsel keeps it: the dates of our emails and of your request, and the score our system gave your contact.] We keep the record of your request under the same code for 1,095 days after we close it (L5.1).
>
> Our log files keep only the first letter of your address and its domain, until each file is deleted after 30 days. Copies made before the erasure remain in our backups until they are replaced; we keep the newest 14 (L5.7). If we ever restore one, we apply your erasure to it again.
>
> [L5.4: what happens to copies of our emails to you, and of your replies, in our mailbox and in those of our sales staff, once IT has set the mailbox retention.]
>
> [Art. 19, L5.6: we have told (the recipients of your data) of the erasure.]

## 5. Extension notice

Sent before {due}, and only where the request is complex or there are many (Art. 12(3)).

> We received your request on {received} (our reference {id}). We need more time to answer it, because [the reasons]. We will answer by [a date no later than two months after {due}].

The register has no place for an extension: after {due}, `main.py dsr list` and the preflight check report the request as overdue (1). Where the extension is recorded is for counsel (L5.5).

## 6. Refusal with reasons

Sent by {due} (Art. 12(4)) where counsel decides not to act on a request: for example one that is manifestly unfounded or excessive (Art. 12(5)), or an erasure that an exception covers (Art. 17(3)). An objection to direct marketing is never refused (Art. 21(3)).

> We received your request on {received} (our reference {id}). We are not acting on it, because [the reasons, as counsel words them]. You may complain to a supervisory authority, in Italy the Garante per la protezione dei dati personali, and seek a judicial remedy.

Then close the request with `main.py dsr close <id> --outcome "<the reason in a few words>"`.
