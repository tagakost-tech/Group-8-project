# Deliverability strategy & technical-review notes

Reference for the engineering review. Covers how the agent protects inbox
placement, what it enforces automatically, and the open infrastructure
decisions.

## What the agent enforces automatically

- **Authentication-aware:** the dashboard's _Deliverability & Reputation_ panel
  and `python main.py doctor` check SPF/DMARC for the actual sending domain
  (not DKIM), with the same lookup and the same words; a lookup that could
  not be made reads "could not be checked just now", never "no record".
- **Warm-up ramp:** daily volume starts low and grows (`WARMUP_*`), resetting on
  the **Italy** calendar day.
- **Per-domain throttle:** at most `MAX_PER_DOMAIN_PER_DAY` to any one domain.
- **Send window:** sends only during configured business hours/days (Italy time)
  — no 2 a.m. blasts.
- **Pacing:** sends are at least `3600 / EMAILS_PER_HOUR` seconds apart
  (default 30/hour = 120 s; `DELAY_BETWEEN_EMAILS` applies only if larger),
  plus random jitter when `SEND_JITTER_SECONDS` > 0 (default 0). A contact the
  agent refuses before anything is sent costs no delay. One pooled SMTP login
  per mailbox per batch (not a fresh login per email).
- **Circuit breaker:** if the bounce rate exceeds `MAX_BOUNCE_RATE`, the agent
  **pauses all sending** and logs it (providers want bounces < 2%, spam < 0.3%).
- **List hygiene:** MX verification when a discovered contact is qualified
  (`VERIFY_EMAILS`/`VERIFY_MX`, on by default; not for Direct Send). An address
  the receiving server rejects permanently is suppressed at once; DSN hard
  bounces and opt-outs sent by email are suppressed by the IMAP monitor, which
  runs only with `IMAP_ENABLED=true` (default false) while the agent runs, and
  reads the inbox and the Junk Email folder.
- **Content:** deliverability linter flags spam-trigger words; subjects carry
  the recipient's company wherever the subject contains `{org}` (the default
  follow-up subjects do; the default `EMAIL_SUBJECT` does not), and rotate only
  with `AB_TESTING_ENABLED=true` and several `EMAIL_SUBJECTS`. The first email
  is Omega IFS's own fixed text, in Italian for the countries in
  `ITALIAN_COUNTRIES` (default `IT`), by the address or the company, and in
  English for everyone else; the
  follow-ups are short and plain, in the same language.
- **Compliant unsubscribe:** `mailto:` opt-out + `List-Unsubscribe` header on
  every email. Mail to the mailto is acted on only by the IMAP monitor
  (`IMAP_ENABLED=true`, default false). The signed web link replaces the
  mailto in the body when `BASE_URL` is public, and one-click (RFC 8058) is
  advertised when that URL is HTTPS. While neither `IMAP_ENABLED=true` nor a
  public `https://` `BASE_URL` is set, `doctor` fails and live sending is
  refused: a public `http://` address does not count. With
  `IMAP_ENABLED=true`, live sending also stops while an inbox the agent reads,
  or its junk folder, has had no complete read within three check intervals,
  as when its login fails; every pass reads the inbox before its first send.
- **Blacklist monitoring:** the sending domain is checked against Spamhaus DBL
  and SURBL (`BLACKLIST_CHECK_ENABLED`); the panel shows the verdict. A blocked
  DNS resolver reports _unavailable_ rather than a false "clean", so a real
  listing can never hide behind a green tick. If the check itself cannot be
  loaded (a broken install), the agent does not start; if it is lost while
  the agent runs, sending pauses until the install is repaired.
- **Multiple mailboxes:** extra sending accounts (`SENDER_2_EMAIL`…) are rotated
  round-robin, **each with its own warm-up ramp**, so total daily volume is the
  sum of the per-mailbox caps rather than one mailbox being pushed too hard.
- **Inbox placement test:** the dashboard's _Spam / inbox test_ card is
  disabled until counsel's country table: a live Direct Send needs a channel,
  and the country lock refuses a mail-tester.com address, which shows no
  country. Meanwhile, a test message sent from the sending mailbox to
  mail-tester.com scores the domain's SPF, DKIM, DMARC and blocklist status,
  though not the agent's own headers and footer.

The legal-identity, opt-out-route, country-lock and client-list blocks, the
suppression and company-block checks, the address/client/competitor screens,
the recorded-basis check, warm-up, the per-domain cap, domain backoff, and the
bounce and blocklist breakers apply to **every** send path - first emails,
follow-ups, retries and manual Direct Sends alike - because all four run one
shared check (`DispatchPipeline.send_verdict`) and the same send
(`_process_contact`). A first email more than `FIRST_CONTACT_MAX_AGE_DAYS`
after collection is refused too, and so is a live first email through a
mailbox without `SENDER_TITLE` or, while `ATTACH_PROFILE` is on, without the
Company Profile PDF (optional, and off for Omega). A contact with a send still `preparing` (cut off by a
stop or a locked database) is held until that send is settled, 26 minutes at
most, and never emailed again by it. The send also stops if an erasure, an
opt-out or a block lands while the email is being prepared. Direct Send differs on purpose: the send
window is advisory (an out-of-hours send is logged `MANUAL_OUT_OF_WINDOW`), it
always uses the primary mailbox, it takes exactly one address, a live send
needs the channel the contact came through, a preview (dry run) skips the
reputation and pacing checks and adds no contact for an address not yet held,
and the operator may override a filter verdict (low score, off-target) but
never an opt-out or another closed contact. With `DRY_RUN=true` every Direct
Send is a preview.

## Open infrastructure decisions (operator, not code)

These are the questions a reviewer will ask. Current status and recommendation:

1. **Sending identity — open.** Mail goes out as Omega IFS from a company
   mailbox; which mailbox is IT's decision. A Microsoft 365 mailbox signs in
   with a certificate, not a password (`docs/DEPLOY.md` section 12):
   Exchange Online refuses password logins over IMAP (F-17), and password
   SMTP AUTH is switched off by default at the end of December 2026 (F-18).
   A mailbox at another provider signs in with its password. The review
   found an earlier placeholder client's domain delivering its mail to
   another provider, not to Exchange Online (F-20), so replies, bounces and
   opt-outs could never reach the inbox the agent reads; Omega's sending
   domain has not been checked for this. A real company domain
   can carry **SPF + DKIM + DMARC**; that placeholder domain had no DKIM and
   no DMARC record (F-21). Microsoft treats bulk mail from an ordinary mailbox as
   best-effort use, and a restricted mailbox can send nothing until an admin
   releases it, so the outreach should not run from someone's everyday mailbox
   (F-22). _Operator to-do:_ choose the mailbox and its sign-in, publish
   SPF/DKIM/DMARC for its domain (the dashboard panel shows SPF/DMARC live),
   and make sure replies to it reach the inbox the agent reads.

2. **The company-profile attachment.** The PDF is optional, and off for
   Omega: its first email attaches nothing and mentions no attachment, so
   with `ATTACH_PROFILE=false`, the default, no first email carries a PDF.
   Turned on, every first email carries the PDF at `PDF_PATH`, and a live
   first email is refused without it. A PDF on a **cold first email** hurts
   deliverability (attachments are a spam/malware signal and are best sent
   after a reply; F-23), and `doctor` warns while it is attached.
   `PROFILE_URL` (`https://www.omegaifs.it` by default) is linked only in a
   template that has `{{body}}`; the shipped first email has none.

3. **Warm-up pace.** The defaults start at 5/day and add 3/day, up to 100
   (start low; 100/day is reached on the 33rd sending day, about 6-7 weeks of
   weekday sending). `doctor` warns when a new mailbox is
   set to start above 10/day or grow by more than 5/day. Raise
   `WARMUP_START_DAILY` only after the domain has reputation.

4. **Monitoring.** The agent watches bounce rate and blocklist status itself.
   Additionally recommend enrolling the sending domain in **Google Postmaster
   Tools** to see domain reputation and the real spam-complaint rate as Gmail
   measures it (that number can't be observed from the sending side).

5. **Scaling volume.** Add mailboxes (`SENDER_2_EMAIL`…) rather than pushing one
   mailbox harder. Each new mailbox warms up from scratch (5/day, +3 per
   sending day), so it reaches its full 100/day only after 33 sending days. For real resilience, spread mailboxes across more than one
   domain/subdomain so a reputation hit doesn't take everything down at once.

## Reviewer quick-answers

- **Volume:** ~100/day cap — **below** the 5,000/day "bulk sender" threshold, so
  the strict Google/Yahoo 2024 mandates don't fully apply. Their one-click
  and opt-out parts are met only with a public HTTPS `BASE_URL` and
  `IMAP_ENABLED=true`; SPF/DKIM/DMARC depend on the domain's DNS (the agent
  checks SPF and DMARC only), so confirm all three before claiming compliance.
- **Data store:** SQLite (WAL, `busy_timeout`) — appropriate for this scale;
  all writes are transactional.
- **AI:** only a model on the company's own server, through
  `LLM_PROVIDER=openai_compatible`; no outside AI company. The shipped emails use no AI
  text: the first email is Omega IFS's fixed text. In an operator's template
  that asks for an AI body or opening line, the content falls back to
  hand-written copy if the model is unavailable, so sending never blocks on
  AI.
- **Tests:** more than 1,700 automated tests (`python -m pytest`), plus a
  mutation audit (`scripts/mutation_audit.py`) that breaks the key guards on
  purpose and checks that a test catches each one.
