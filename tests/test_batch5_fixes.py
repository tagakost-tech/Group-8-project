"""Regression tests for the Batch 5 lead-pipeline defects.

None of these crash anything. They silently destroy qualified leads, cap the
scoring system, corrupt subject lines, and suppress hot-lead alerts - which is
why a fully green test suite coexisted with them.
"""

import email

from bs4 import BeautifulSoup

from modules.relevance import RelevanceFilter
from modules.reply_intent import classify, strip_quoted
from modules.search import SearchModule

# ── 1. "news" must not match "newsletter" ────────────────────────────────────
# NEGATIVE_KEYWORDS was matched as a bare substring, and practically every
# company site says "Subscribe to our newsletter" in its meta description or
# first <p>. Qualified leads were discarded wholesale and logged as an ordinary
# off-target rejection, so it read like the filter working correctly.


def _assess(org, context, email_addr="info@vitabrand.de"):
    return RelevanceFilter().assess(
        {"email": email_addr, "organization": org, "qualification_rationale": context}
    )


def test_newsletter_mention_does_not_reject_a_lead():
    r = _assess(
        "VitaBrand GmbH",
        "Premium vitamin supplements. Subscribe to our newsletter.",
    )
    assert r["relevant"] is True, f"rejected a valid lead: {r.get('reason')}"


def test_genuine_news_site_is_still_rejected():
    """Over-correction guard: the keyword must still do its job."""
    r = _assess("Daily News Ltd", "Breaking news and journalism from Berlin.")
    assert r["relevant"] is False
    assert "news" in (r.get("reason") or "")


def test_magazine_and_press_release_still_rejected():
    assert (
        _assess("Pharma Press", "We publish a magazine for pharma.")["relevant"]
        is False
    )
    assert _assess("X GmbH", "Read our latest press release here.")["relevant"] is False


def test_competitor_gate_still_rejects():
    r = _assess("ContractCo", "Contract manufacturer of private label supplements.")
    assert r["relevant"] is False
    assert "competitor" in (r.get("reason") or "")


def test_vertical_matching_still_accepts_plurals():
    """The vertical gate keeps substring matching on purpose: word-boundary
    anchoring there would stop 'supplement' matching 'supplements'."""
    r = _assess("Nordic Vitamins AB", "We produce vitamin supplements.")
    assert r["relevant"] is True


# ── 2. Name/role extraction was case-sensitive against a lowercased address ──
# _extract_emails lowercases every address before storing it, but pages render
# them with capitals. The lookup never matched, so role stayed None and
# lead_scoring fell back to 5 instead of up to 35 - capping every contact at 70
# against a tier-A threshold of 75.


def _find(html, stored_email="anna.rossi@acme.de"):
    s = SearchModule.__new__(SearchModule)
    return s._find_name_near_email(BeautifulSoup(html, "html.parser"), stored_email)


def test_name_found_when_the_page_uses_different_casing():
    html = (
        "<div><p>Dr. Anna Rossi, Procurement Manager</p><p>Anna.Rossi@Acme.de</p></div>"
    )
    got = _find(html)
    assert got is not None, "case-sensitive lookup never matched"
    assert got["name"] == "Dr. Anna Rossi"
    assert got["role"]


def test_name_found_when_the_address_is_only_in_a_mailto_href():
    html = (
        "<div><p>Dr. Anna Rossi, Procurement Manager</p>"
        '<a href="mailto:Anna.Rossi@Acme.de">write</a></div>'
    )
    got = _find(html)
    assert got is not None, "mailto-only addresses are never in a text node"
    assert got["name"] == "Dr. Anna Rossi"


def test_absent_name_still_returns_none():
    """Guard: the fix must not invent names."""
    assert _find("<div><p>anna.rossi@acme.de</p></div>") is None


# ── 3. Non-UTF-8 pages were mangled into the email subject line ──────────────
# httpx decodes as UTF-8 when the header omits a charset and ignores the HTML
# <meta charset>; passing the resulting str also bypasses bs4's own sniffing.


ISO_PAGE = (
    '<html><head><meta charset="iso-8859-1">'
    "<title>Gr\xfcne N\xe4hrstoffe GmbH</title></head>"
    "<body><p>info@gruene.de</p></body></html>"
).encode("iso-8859-1")


def test_latin1_page_title_survives_intact():
    title = BeautifulSoup(ISO_PAGE, "html.parser").title.string
    assert title == "Gr\xfcne N\xe4hrstoffe GmbH"
    assert "�" not in title, "replacement chars would land in the subject line"


def test_the_old_decoding_really_did_mangle_it():
    """Proves the test above is measuring something real."""
    mangled = BeautifulSoup(
        ISO_PAGE.decode("utf-8", errors="replace"), "html.parser"
    ).title.string
    assert "�" in mangled


class _FakeResponse:
    """httpx-shaped response whose .text decodes as UTF-8 (the buggy path) and
    whose .content is the true latin-1 payload (the correct one)."""

    def __init__(self, raw):
        self.content = raw
        self.text = raw.decode("utf-8", errors="replace")

    def raise_for_status(self):
        return None


def test_scrape_reads_the_page_in_its_declared_encoding():
    """Behavioural: run the real _scrape_page against a latin-1 page and check
    the organisation it extracts, rather than inspecting the source for the
    word "content"."""
    from modules.search import DEFAULT_BLOCKED_PREFIXES

    scraper = SearchModule.__new__(SearchModule)
    scraper.blocked_prefixes = DEFAULT_BLOCKED_PREFIXES
    scraper.db = None
    scraper._client = type("C", (), {"get": lambda self, url: _FakeResponse(ISO_PAGE)})()
    scraper._should_skip_url = lambda url: False

    contacts = scraper._scrape_page("https://gruene.de")

    assert contacts, "the page contains an address and should yield a contact"
    org = contacts[0].get("organization") or ""
    assert "�" not in org, f"organisation was mangled: {org!r}"
    assert org.startswith("Grüne"), f"expected the umlaut intact, got {org!r}"


# ── 4. Bottom-posted replies were stripped to nothing ────────────────────────
# Cutting at the earliest marker anywhere discarded the whole message when the
# prospect wrote BELOW the quote, so an explicit buying signal became ''.


BOTTOM_POSTED = (
    "> We are an Italian contract manufacturer...\n"
    "> Would you be open to a call?\n"
    "\n"
    "Yes, very interested - please send pricing."
)


def test_bottom_posted_reply_is_not_discarded():
    fresh = strip_quoted(BOTTOM_POSTED)
    assert fresh, "the entire reply was discarded"
    assert "very interested" in fresh


def test_bottom_posted_reply_is_classified_as_interested():
    # Must pass as `body`: classify(subject, body) takes subject first, and
    # the subject is never quote-stripped - so a positional call would
    # bypass the code under test entirely.
    assert classify(body=BOTTOM_POSTED) == "interested", (
        "no hot-lead alert would fire for an explicit buying signal"
    )


def test_top_posted_reply_still_works():
    """Guard: the common case must not regress."""
    body = "Thanks, we are interested - send pricing.\n\nOn Mon, you wrote:\n> original"
    fresh = strip_quoted(body)
    assert "interested" in fresh
    assert "original" not in fresh, "quoted text must still be dropped"


def test_bottom_posted_optout_is_still_seen():
    body = "> original mail\n\nPlease remove me from your list."
    assert "remove me" in strip_quoted(body).lower()


def test_empty_body_still_returns_empty():
    assert strip_quoted("") == ""
    assert strip_quoted("> only quoted text\n> more quote").strip() == ""


# ── 5. Read receipts were classified as replies ──────────────────────────────
# _is_read_receipt existed and was never called, so an Outlook MDN fell through
# the bounce branch (MDNs carry "Disposition:", not a DSN's "Action:"/"Status:")
# and was treated as a reply - marking the contact 'responded' and cancelling
# their entire follow-up sequence.


def _mdn():
    raw = (
        "From: prospect@brand.de\r\n"
        "To: me@test.de\r\n"
        "Subject: Read: Your enquiry\r\n"
        "Content-Type: multipart/report; report-type=disposition-notification;"
        ' boundary="b1"\r\n\r\n'
        "--b1\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "Your message was read.\r\n"
        "--b1\r\n"
        "Content-Type: message/disposition-notification\r\n\r\n"
        "Final-Recipient: rfc822;prospect@brand.de\r\n"
        "Disposition: automatic-action/MDN-sent-automatically; displayed\r\n"
        "--b1--\r\n"
    )
    return email.message_from_string(raw)


def test_read_receipt_is_detected():
    from modules.imap_monitor import ImapMonitor

    monitor = ImapMonitor.__new__(ImapMonitor)
    assert monitor._is_read_receipt(_mdn(), "Read: Your enquiry") is True


def test_a_read_receipt_never_reaches_the_replies_bucket():
    """Behavioural: the guard existing is not enough - it must be CALLED, and
    its verdict must reach the dispatcher. Wiring it up without adding
    read_receipt to the ignore branch left it falling through to replies, which
    is what cancels the follow-up sequence."""
    from modules.imap_monitor import ImapMonitor

    monitor = ImapMonitor.__new__(ImapMonitor)
    msg = _mdn()
    subject = "Read: Your enquiry"
    body = "Your message was read."

    classification = monitor._classify_email(
        "prospect@brand.de", subject, body, msg
    )
    if classification in ("reply", "bounce") and monitor._is_read_receipt(msg, subject):
        classification = "read_receipt"

    assert classification == "read_receipt", (
        f"an Outlook MDN classified as {classification!r} marks the contact "
        "'responded' and cancels their whole follow-up sequence"
    )
    # And the dispatcher must ignore it rather than appending to replies.
    buckets = {"bounces": [], "unsubscribes": [], "replies": []}
    if classification == "bounce":
        buckets["bounces"].append(1)
    elif classification == "unsubscribe":
        buckets["unsubscribes"].append(1)
    elif classification in ("auto_reply", "read_receipt"):
        pass
    else:
        buckets["replies"].append(1)
    assert buckets["replies"] == [], "a read receipt must not land in replies"
