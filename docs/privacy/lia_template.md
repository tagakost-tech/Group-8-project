# Legitimate-interest assessment (template)

Status: template for counsel. It is used only in countries where counsel decides that legitimate interest may be the basis (decision L1.1). In Italy the Garante holds that it never is for promotional email (see L1.2 in `decisions_for_counsel.md`). The facts below describe the agent as handed to Omega IFS in November 2026.

Art. 6(1)(f) allows processing that is necessary for a legitimate interest, unless the person's interests or rights override it. The three-part test below, and the duty to document it, come from accountability (Arts. 5(2) and 24), case law and EDPB guidance, not from the Article's text. The EDPB's page shows only version 1.0 of its guidelines, published for consultation (9 October to 20 November 2024); no final version was found (L2.1).

## Part 1: the interest

- **The interest:** offering Omega IFS's cold-formed fasteners and precision parts to companies that may need them.
- **Is it lawful, clear and present?** To be stated.

## Part 2: necessity

- Is email to a business address necessary for that interest, or would a less intrusive means do?
- How the product limits the data. In place, with periods that are proposals pending counsel:
  - the source channel, the basis it gives and the rules version, recorded with every contact when it is stored. A contact without a known channel is refused, and one an operator adds by hand needs a channel from a fixed list (L1.4, L1.5; step G13, commits 4a9730b and 1234c55). A contact added by hand still has no source page;
  - a first email only within 30 days of collection: after that the email is refused, and the contact is erased at the next daily run (L1.8; step G12, commit 1e034c6);
  - a retention period for every contact, clients included: 30 days if never emailed, 365 days after the last email if there is no reply, 730 days after the last exchange if there is one, and the next daily run after an objection or a decline. The agent's daily live run applies them, and so does `main.py retention`, without the agent (L5.1; step G12, commits 1e034c6, 9d1b224, ff49d6a and 9b3ca5e).
- Not in place: targeting on company fit only, without scoring people by job title or address shape (L2.10, no step yet). Today the score favours named senior people. Its company points and the default searches are aimed at the market words in `client/omega.json`: manufacturers in automotive, furniture and window hardware, electrical equipment, appliances and similar industries.
- Off by default: the relevance filter, which before a campaign's first email drops a contact whose company shows no sign of Omega IFS's target verticals, looks like a competing manufacturer, or has a non-European address (`config.py:210-220`; `modules/relevance.py:565-736`; `modules/backlog_draining.py:86`, `:256`). Turning it on costs leads, so whether live sends use it is for sales to decide (F-05).

## Part 3: balancing

- **What the person reasonably expects:** an address the company published for business contact, about the person's own work.
- **How the agent finds and reads websites (L1.11):**
  - Search results come from search engines' result pages, read through `ddgs`, an unofficial library that scrapes them (`modules/search.py:19-27`, `:545-599`). When a search finds nothing, the agent has no pages for that query; it asks no other service.
  - The agent fetches every page as a Chrome browser on Windows would. Its User-Agent header names Chrome 125 (`:290-298`), and with the `curl_cffi` library, which the requirements install, the connection also copies Chrome's TLS handshake (`:29-39`, `:213-251`). The code's own comment says this gets past bot protection that stops a plain client: on 23 target sites, the share it could read rose from 83% to 91% (`:30-36`). Without `curl_cffi` it falls back to a plain client "with reduced reach" (`:254-270`).
  - Before each page, the agent reads the site's robots.txt and skips what it disallows (`:986-988`). It matches the rules against its Chrome User-Agent, which Python's robots.txt parser reads as "Mozilla" (`:358-359`): rules for all robots apply, but the agent has no name of its own that a site could address a rule to.
  - The check fails open: a robots.txt that cannot be fetched, does not answer 200 or cannot be parsed counts as allowing everything (`:327-335`, `:349-350`, `:360-384`). The code's own comment says the balancing test is "materially weaker if the agent takes data from sites that asked it not to" (`:319-323`).
  - Still open: whether the agent should introduce itself honestly, with a User-Agent that names it and gives a web address for contacting Omega IFS, and keep browser impersonation behind a setting that is off by default. This is the developer's proposal, and the code is unchanged. Fewer bot-protected sites could then be read, so Omega weighs that cost, and counsel the facts above under L1.11.
- **The safeguards:**
  - the notice in the first email, in a footer with one version per basis (step G14);
  - objection in one step that stops all email to the company (L1.6, step G5c, done): by the one-click Unsubscribe button, or by email from someone the agent emailed. An erasure blocks the company too, and so does a data request found in a reply from someone the agent emailed (step G11). An interested reply never does. A plain decline, and a click on the unsubscribe link, stop only that address (L1.16);
  - no open tracking (L1.7);
  - AI only on Omega's own server, behind three locks (L2.4). No outside AI company receives data: the only AI is a model on Omega's own server, at an address that must be on this machine or the private network. Nothing reaches it until it is set up. Each use that sees a recipient's data has its own switch, off by default except the company-country question (`COMPANY_COUNTRY_AI`), which sends no more than the fit check. None of them sends anything while `AI_DPA_REFERENCE`, the reference of the provider's processing agreement, is blank (steps G10, G17). Every prompt is first stripped of addresses, phone numbers and the query part of web addresses. Reply reading sends the reply's subject and at most 600 characters of the person's own words, with the quoted original removed and the text cut at the first sign-off line; a signature with no such line above it is kept. Website text, page addresses and domains can still name a person. With only the server set up, the agent can still send it search topics, which name no one.
- **Profiling:** whether the lead score ranks people (L2.10) and how that weighs in the balance.
- **The outcome:** to be signed by counsel.

## Country annex (one per enabled country)

Each annex records:

- the national rule on email marketing, and the source for it;
- whether generic and named addresses are both covered (L1.3);
- the contact sources allowed (L1.4, L1.14);
- the language of the notice (L3.3);
- the date and the signatory.
