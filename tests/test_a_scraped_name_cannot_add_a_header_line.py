"""A scraped organisation cannot add a line to an email's header.

The subject of the first email and of the follow-ups names the recipient's
organisation, which is whatever a page calls itself (its og:site_name or its
<title>), and the operator's lead alert names it too. A header value may hold
an RFC 2047 encoded word ("=?utf-8?b?...?="), which the email package decodes
when it parses the value, and Python 3.11.9, the last Windows installer of
3.11, writes a CR or LF decoded from one back out as a real line break
(CVE-2024-6923, fixed in 3.11.10). A page whose og:site_name read
"=?utf-8?q?Hello=0D=0AX-Injected:_yes?=" gave each of those emails a header
line of its own, and a blank line ended the header block early, so the rest
of the headers went out as body text. The library refuses a raw CR or LF, so
nothing on the way objected: the raw value holds none.

Three places now stand in the way. create_email, which builds every outgoing
message, refuses one whose headers, once decoded or as written, hold a line
break. A subject built from an organisation leaves out one holding an encoded
word or a control character, for the default it uses when there is none, so
the email still goes. And the page parser reads control characters in a site
name as spaces and caps the name at 100 characters, on the og:site_name and
<title> paths.
"""

import html
import logging
import smtplib
import unicodedata

import pytest
from bs4 import BeautifulSoup

from agent import OutreachAgent
from modules.database import Database
from modules.email_sender import EmailSender
from modules.search import SearchModule

# Each decodes to a line break the raw value does not hold.
WORDS = {
    "base64 word": "=?utf-8?b?SGVsbG8NClgtSW5qZWN0ZWQ6IHllcw==?=",  # Hello CRLF X-Injected: yes
    "q word": "=?utf-8?q?Hello=0D=0AX-Injected:_yes?=",
    "blank line": "=?utf-8?b?SGVsbG8NCg0K?=",  # Hello CRLF CRLF
    "touching the name": "Brand=?utf-8?q?Hello=0D=0AX-Injected:_yes?=",
    "upper-case base64 word": "=?UTF-8?B?SGVsbG8NClgtSW5qZWN0ZWQ6IHllcw==?=",
    "upper-case q word": "=?UTF-8?Q?Hello=0D=0AX-Injected:_yes?=",
}
# "Hello" and one CR LF: at the end of a header it is one line by a count of
# str.splitlines(), and still ends the header block on the wire.
TRAILING = "=?utf-8?b?SGVsbG8NCg==?="
# Line breaks to str.splitlines() that would not end a line on the wire: a CR
# LF a space then continues, and characters that are not a CR or an LF.
OTHER_BREAKS = {
    "before a space": "=?utf-8?q?Hello=0D=0A_There?=",
    "vertical tab": "=?utf-8?q?Hello=0BThere?=",
    "next line": "=?utf-8?q?Hello=C2=85There?=",
    "line separator": "=?utf-8?q?Hello=E2=80=A8There?=",
    "line separator at the end": "=?utf-8?q?Hello=E2=80=A8?=",
}


class _Wire:
    """An SMTP session that keeps each message it is handed, so no test opens
    a real one."""

    def __init__(self):
        self.sent = []

    def noop(self):
        return (250, b"ok")

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


def _agent(settings_factory, tmp_path, **kw):
    """The real agent and send path, with only the SMTP session replaced."""
    settings = {
        "db_path": str(tmp_path / "a.db"),
        "verify_emails": False,
        "send_window_enabled": False,
        "blacklist_check_enabled": False,
        "imap_enabled": False,
        "ai_humanize_emails": False,
        "sequences_enabled": True,
        "smtp_username": "outreach@suppleco.example",
        "lead_alert_email": "sales@suppleco.example",
        "email_subject": "Supplements for {org}",
    }
    a = OutreachAgent(settings_factory(**{**settings, **kw}))
    wire = _Wire()
    a.email_sender._new_connection = lambda: wire
    a._running = True
    a._interruptible_sleep = lambda *_a, **_k: None
    return a, wire


def _page(tag):
    return BeautifulSoup(f"<html><head>{tag}</head></html>", "html.parser")


def _scraped(a, tag):
    """A contact stored the way a search stores one: the organisation is what
    the real page parser reads from the page."""
    org = a.search._extract_org_name(_page(tag), "https://www.brand.de/kontakt")
    return _stored(a, org)


def _stored(a, org):
    cid = a.db.add_contact(
        a.db.log_search("q"),
        email="buyer@brand.de",
        name="Maria Rossi",
        organization=org,
    )
    return a.db.get_contact_by_id(cid)


def _first_email(a, wire, contact):
    assert a._process_contact(contact, dry_run=False), "the first email did not go"
    return wire.sent[-1]


def _enrolment_due(a):
    a.db.conn.execute(
        "UPDATE sequence_enrollments SET enrolled_at = '2000-01-01 00:00:00'"
    )
    a.db.conn.commit()


def _step(a):
    return a.db.conn.execute(
        "SELECT current_step FROM sequence_enrollments"
    ).fetchone()[0]


def _follow_up(a, wire, contact):
    """The real follow-up loop, over the default sequence's first step: the
    email goes and the enrolment moves on, so it is not tried again."""
    a.sequences.enroll_contact(contact["id"], a.sequence_id)
    _enrolment_due(a)
    before = len(wire.sent)
    a._process_sequences(dry_run=False)
    assert len(wire.sent) == before + 1, "the follow-up did not go"
    assert _step(a) == 1
    return wire.sent[-1]


def _lead_alert(a, wire, contact):
    a._handle_interested_replies(
        [
            {
                "email": contact["email"],
                "from": f"Maria Rossi <{contact['email']}>",
                "subject": "Re: Supplements",
                "body": "Yes, we are interested - please send a quote.",
            }
        ]
    )
    assert a.db.was_lead_alerted(contact["email"]), "the lead alert did not go"
    return wire.sent[-1]


COMPOSE = {
    "first email": _first_email,
    "follow-up": _follow_up,
    "lead alert": _lead_alert,
}
DEFAULT_SUBJECT = {
    "first email": "Supplements for your brand",
    "follow-up": "Following up — your brand",
    "lead alert": "Interested lead: Unknown company <buyer@brand.de>",
}


def _header_names(msg) -> list[str]:
    """The name on each line of the header block as_bytes() writes, which
    ends at the first empty line. A folded header continues on lines that
    start with whitespace."""
    lines = msg.as_bytes().splitlines()
    head = lines[: lines.index(b"")]
    return [line.split(b":", 1)[0].decode() for line in head if line[:1] not in b" \t"]


@pytest.mark.parametrize("compose", COMPOSE)
@pytest.mark.parametrize("word", WORDS.values(), ids=WORDS.keys())
def test_a_site_name_holding_an_encoded_word_adds_no_header_line(
    settings_factory, tmp_path, word, compose
):
    """The page to the wire: the parser reads the name, the database stores
    it, and the real send path builds and serialises the message."""
    a, wire = _agent(settings_factory, tmp_path)
    contact = _scraped(a, f'<meta property="og:site_name" content="{word}">')
    assert contact["organization"] == word
    msg = COMPOSE[compose](a, wire, contact)
    names = _header_names(msg)
    assert "X-Injected" not in names
    assert names.count("Subject") == 1
    assert names == list(msg.keys()), "the header block is not the message's own"
    assert str(msg["Subject"]) == DEFAULT_SUBJECT[compose]


@pytest.mark.parametrize(
    "org", ["Brand GmbH", "R&D GmbH", "Lüdenscheider Naturkost GmbH"]
)
def test_an_ordinary_organisation_keeps_its_subject(settings_factory, tmp_path, org):
    a, wire = _agent(settings_factory, tmp_path)
    contact = _scraped(
        a, f'<meta property="og:site_name" content="{html.escape(org)}">'
    )
    assert contact["organization"] == org
    expected = {
        "first email": f"Supplements for {org}",
        "follow-up": f"Following up — {org}",
        "lead alert": f"Interested lead: {org} <buyer@brand.de>",
    }
    for compose, subject in expected.items():
        msg = COMPOSE[compose](a, wire, contact)
        assert str(msg["Subject"]) == subject
        assert _header_names(msg) == list(msg.keys())


@pytest.mark.parametrize(
    "org",
    [
        "Brand GmbH\nKontakt",
        "Brand\x1b[31m GmbH",
        "Brand\u2028GmbH",
        "Brand\x85GmbH",
        "Brand\x9b31m GmbH",
        "Brand\x7fGmbH",
        "   ",
    ],
    ids=[
        "line feed",
        "escape",
        "line separator",
        "next line",
        "C1 escape",
        "delete",
        "spaces alone",
    ],
)
def test_an_organisation_holding_a_control_character_gets_the_default(
    settings_factory, tmp_path, org
):
    """Stored before the page parser read control characters as spaces (a
    <title> written over two lines), or typed into Direct Send. The library
    refused a raw line break in the subject, so that contact's email was
    never built and the lead's alert was lost; an escape sequence went out."""
    a, wire = _agent(settings_factory, tmp_path)
    contact = _stored(a, org)
    for compose, subject in DEFAULT_SUBJECT.items():
        msg = COMPOSE[compose](a, wire, contact)
        assert str(msg["Subject"]) == subject


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def _sender(tmp_path):
    return EmailSender(
        Database(str(tmp_path / "s.db")),
        "smtp.test",
        587,
        "outreach@suppleco.example",
        "pw",
    )


@pytest.mark.parametrize("header", ["subject", "in_reply_to"])
@pytest.mark.parametrize(
    "word",
    [*WORDS.values(), TRAILING, *OTHER_BREAKS.values()],
    ids=[*WORDS, "trailing line break", *OTHER_BREAKS],
)
def test_create_email_refuses_a_header_that_decodes_to_a_line_break(
    tmp_path, header, word
):
    """Whatever built the value: a follow-up's In-Reply-To is decoded the
    same way as the Subject."""
    values = {"subject": "Supplements", "in_reply_to": "<first@suppleco.example>"}
    values[header] = f"{values[header]} {word}"
    with pytest.raises(ValueError, match="line break"):
        _sender(tmp_path).create_email(
            "buyer@brand.de",
            values["subject"],
            "<p>h</p>",
            "h",
            in_reply_to=values["in_reply_to"],
        )


def test_create_email_refuses_a_subject_ending_in_a_raw_line_break(tmp_path):
    """The library counts the lines of a raw value, so it lets this one by,
    and the line break it writes ends the header block."""
    with pytest.raises(ValueError, match="line break"):
        _sender(tmp_path).create_email(
            "buyer@brand.de", "Supplements\r\n", "<p>h</p>", "h"
        )


def test_create_email_refuses_any_header_that_decodes_to_a_line_break(tmp_path):
    """Every header is read, not only those a name is put in: here the
    List-Unsubscribe built from the unsubscribe link."""
    with pytest.raises(ValueError, match="line break"):
        _sender(tmp_path).create_email(
            "buyer@brand.de",
            "Supplements",
            "<p>h</p>",
            "h",
            unsubscribe_url="https://outreach.suppleco.example/u/"
            "=?utf-8?q?=0D=0AX-Injected:_yes?=",
        )


def test_create_email_keeps_an_encoded_word_that_decodes_to_one_line(tmp_path):
    msg = _sender(tmp_path).create_email(
        "buyer@brand.de", "Caffè =?utf-8?q?per_tutti?=", "<p>h</p>", "h"
    )
    assert str(msg["Subject"]) == "Caffè per tutti"


def test_create_email_keeps_a_long_header_the_library_folds(tmp_path):
    """A folded line starts with whitespace: it is the same header, not a
    line of its own."""
    msg = _sender(tmp_path).create_email(
        "buyer@brand.de",
        "Supplements for " + " ".join(["Vital Brand GmbH"] * 20),
        "<p>h</p>",
        "h",
        unsubscribe_url="https://outreach.suppleco.example/unsubscribe/" + "x" * 300,
    )
    assert len(msg.policy.fold("Subject", msg["Subject"]).splitlines()) > 1
    assert _header_names(msg) == list(msg.keys())


# The parser calls the part after "buyer@brand.de" an invalid mailbox, or text
# it skips: str() of the header shows none of it, as_bytes() writes it all.
ADDRESSES = {
    "second address": "buyer@brand.de, >=?utf-8?q?=0d=0ax-injected:_yes?=@x.de",
    "after the address": "buyer@brand.de> =?utf-8?q?=0d=0ax-injected:_yes?=",
    "blank line": "buyer@brand.de, >=?utf-8?q?=0d=0a=0d=0a?=@x.de",
    # Written with its CR LF encoded when the message is folded for a server
    # without SMTPUTF8, and raw when folded for one with it.
    "with SMTPUTF8": "müller@brand.de, >=?utf-8?q?=c3=bc=0d=0ax-injected:_yes?=@x.de",
    # A lone CR, written out as one, ends the header block.
    "carriage return": "buyer@brand.de> =?utf-8?q?=0d?=",
    # Folded without SMTPUTF8, the line breaks after the "@" with no space to
    # continue it; folded with it, it is one line.
    "folded without SMTPUTF8": "müller@brand.deè=?utf-8?b?w7wNCng6IHk=?=",
}


@pytest.mark.parametrize("to", ADDRESSES.values(), ids=ADDRESSES.keys())
def test_create_email_refuses_an_address_it_would_write_with_a_line_break(tmp_path, to):
    with pytest.raises(ValueError, match="line break"):
        _sender(tmp_path).create_email(to, "Supplements", "<p>h</p>", "h")


@pytest.mark.parametrize("to", ADDRESSES.values(), ids=ADDRESSES.keys())
def test_a_direct_send_to_such_an_address_sends_nothing(settings_factory, tmp_path, to):
    """The scraper's addresses cannot hold one: a mailto: link is cut at its
    first "?" and the result validated. Direct Send's own check asks for little
    more than an "@", and goes through the same builder."""
    a, wire = _agent(settings_factory, tmp_path)
    ok, _message = a.send_single_email(to, channel="referral")
    assert not ok
    assert wire.sent == []


class _SmtpWire(smtplib.SMTP):
    """smtplib's own send_message, which folds and writes the message itself,
    with only the server replaced: it keeps the bytes it is handed."""

    def __init__(self, smtputf8):
        super().__init__()
        self.does_esmtp = True
        self.esmtp_features = {"8bitmime": ""}
        if smtputf8:
            self.esmtp_features["smtputf8"] = ""
        self.sent = []

    def ehlo_or_helo_if_needed(self):
        pass

    def noop(self):
        return (250, b"ok")

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        self.sent.append(msg)
        return {}

    def quit(self):
        pass

    def close(self):
        pass


def _wire_header_names(data: bytes) -> list[str]:
    """The name on each line of the header block a server reads, which ends at
    the first empty line; a lone CR ends a line too."""
    lines = data.splitlines()
    head = lines[: lines.index(b"")]
    return [line.split(b":", 1)[0].decode() for line in head if line[:1] not in b" \t"]


@pytest.mark.parametrize("smtputf8", [True, False], ids=["SMTPUTF8", "no SMTPUTF8"])
@pytest.mark.parametrize("to", ADDRESSES.values(), ids=ADDRESSES.keys())
def test_smtplib_is_handed_no_such_address(settings_factory, tmp_path, to, smtputf8):
    """smtplib folds the message again, for SMTPUTF8 with its non-ASCII text
    written raw, so a check of one fold alone misses what the other writes."""
    a, _wire = _agent(settings_factory, tmp_path)
    smtp = _SmtpWire(smtputf8)
    a.email_sender._new_connection = lambda: smtp
    ok, _message = a.send_single_email(to, channel="referral")
    assert not ok
    assert smtp.sent == []


@pytest.mark.parametrize("smtputf8", [True, False], ids=["SMTPUTF8", "no SMTPUTF8"])
@pytest.mark.parametrize("to", ["buyer@brand.de", "müller@brand.de"])
def test_an_ordinary_address_reaches_smtplib_with_its_own_headers(
    settings_factory, tmp_path, to, smtputf8
):
    a, _wire = _agent(settings_factory, tmp_path)
    smtp = _SmtpWire(smtputf8)
    a.email_sender._new_connection = lambda: smtp
    ok, message = a.send_single_email(to, channel="referral")
    # One rule decides what an address is, and it takes no letter outside
    # ASCII before the @: such an address is refused before any connection,
    # whatever the server says it accepts.
    if to.isascii():
        assert ok, message
        (data,) = smtp.sent
        names = _wire_header_names(data)
        assert names.count("To") == 1
        assert "X-Injected" not in names
    else:
        assert not ok
        assert smtp.sent == []


def _email_errors(a):
    return [
        r for r in a.db.get_recent_activity(limit=50) if r["action"] == "EMAIL_ERROR"
    ]


def _retries(a):
    rows = a.db.conn.execute(
        "SELECT status, attempt, max_attempts FROM retry_queue"
    ).fetchall()
    return [tuple(r) for r in rows]


def _retries_due(a):
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()


def test_a_refused_first_email_is_logged_once_a_try_and_its_retries_run_out(
    settings_factory, tmp_path, caplog
):
    """What create_email's refusal leaves on the campaign's send path. Past
    the subject's own check only a subject template can carry an encoded word,
    so the operator's template holds one here. Nothing reaches the server, the
    contact is not emailed, its retries end, and nothing that pauses the
    campaign moves."""
    a, wire = _agent(
        settings_factory, tmp_path, email_subject=f"Supplements {WORDS['q word']}"
    )
    contact = _stored(a, "Brand GmbH")
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert a._process_contact(contact, dry_run=False) is False
    assert len(_errors(caplog)) == 1
    assert len(_email_errors(a)) == 1
    assert _retries(a) == [("pending", 0, 3)]

    for _ in range(4):
        _retries_due(a)
        a._process_retries()
    assert _retries(a) == [("exhausted", 3, 3)]
    assert len(_email_errors(a)) == 4

    assert wire.sent == []
    assert a.db.get_contact_by_id(contact["id"])["status"] == "failed"
    assert not a.db.has_been_emailed(contact["id"])
    assert not a.db.is_suppressed("buyer@brand.de")
    assert a.db.get_deliverability_metrics()["bounced"] == 0
    assert not a._sending_paused


def test_a_refused_follow_up_sends_nothing_and_counts_as_no_bounce(
    settings_factory, tmp_path, caplog
):
    """The follow-up loop's side of a refusal. With one real send behind it
    and a minimum of one, the bounce guard really judges the rate here: a
    refusal filed as a bounce would trip it for the whole campaign."""
    a, wire = _agent(settings_factory, tmp_path, bounce_guard_min_sends=1)
    contact = _stored(a, "Brand GmbH")
    _first_email(a, wire, contact)
    a.db.conn.execute(
        "UPDATE sequence_steps SET subject = ?", (f"Following up {WORDS['q word']}",)
    )
    a.db.conn.commit()
    _enrolment_due(a)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        a._process_sequences(dry_run=False)
    assert len(_errors(caplog)) == 1
    assert len(_email_errors(a)) == 1
    assert len(wire.sent) == 1, "only the first email went"
    assert _step(a) == 0, "the step that did not go was counted as sent"
    assert _retries(a) == [], "a follow-up has no first email to retry"
    assert not a.db.is_suppressed("buyer@brand.de")
    metrics = a.db.get_deliverability_metrics()
    assert (metrics["recent_sent"], metrics["bounce_rate"]) == (1, 0.0)
    assert not a._bounce_guard_tripped()
    assert not a._sending_paused


def test_a_refused_lead_alert_is_logged_once_and_the_next_reply_still_alerted(
    settings_factory, tmp_path, caplog
):
    """The alert's subject names the address as well. The scraper's pattern
    takes no "=" or "?", so only an address an operator typed can hold an
    encoded word; create_email refuses that subject and the alert's caller
    contains the refusal per reply. Nothing is recorded for that lead and the
    next one is still escalated."""
    a, wire = _agent(settings_factory, tmp_path)
    odd = "=?utf-8?q?a=0d=0ax-injected=3a_yes?=@brand.de"
    a.db.add_contact(a.db.log_search("q"), email=odd, organization="Brand GmbH")
    a.db.add_contact(
        a.db.log_search("q"), email="buyer@nordic.se", organization="Nordic"
    )
    reply = {
        "subject": "Re: Supplements",
        "body": "Yes, we are interested - please send a quote.",
    }
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        a._handle_interested_replies(
            [{**reply, "email": odd}, {**reply, "email": "buyer@nordic.se"}]
        )
    assert not a.db.was_lead_alerted(odd)
    assert a.db.was_lead_alerted("buyer@nordic.se")
    assert [str(m["Subject"]) for m in wire.sent] == [
        "Interested lead: Nordic <buyer@nordic.se>"
    ]
    assert len(_errors(caplog)) == 1


# ── The name the page parser reads ─────────────────────────────────────────


@pytest.fixture
def sm(db):
    return SearchModule(db, rate_limit=0.0)


@pytest.mark.parametrize(
    "tag",
    [
        '<meta property="og:site_name" content="{}">',
        "<title>{}</title>",
    ],
    ids=["og:site_name", "title"],
)
def test_a_long_site_name_is_capped_at_100_characters(sm, tag):
    name = "Vital Brand GmbH " + "x" * 250
    found = sm._extract_org_name(_page(tag.format(name)), "https://brand.de")
    assert found == name[:100]


@pytest.mark.parametrize(
    "tag",
    [
        '<meta property="og:site_name" content="Vital&#10;Brand&#13;&#10;GmbH\x1b">',
        '<meta property="og:site_name" content="Vital\u2028Brand\x85GmbH">',
        '<meta property="og:site_name" content="Vital\x7fBrand\u2029GmbH">',
        "<title>Vital\nBrand GmbH\x1b\t| Kontakt</title>",
        "<title>Vital\u2028Brand GmbH \x85 | Kontakt</title>",
    ],
    ids=[
        "og:site_name",
        "og:site_name, Unicode",
        "og:site_name, delete and paragraph separator",
        "title",
        "title, Unicode",
    ],
)
def test_control_characters_in_a_site_name_are_read_as_spaces(sm, tag):
    """A <title> written over several lines is common; a tab or a line break
    then sits beside the separator, and the name kept it."""
    name = sm._extract_org_name(_page(tag), "https://brand.de")
    assert name == "Vital Brand GmbH"
    assert not any(unicodedata.category(c) == "Cc" for c in name)


def test_a_site_name_of_control_characters_alone_gives_way_to_the_title(sm):
    page = _page(
        '<meta property="og:site_name" content="&#10;&#13;\x1b">'
        "<title>Brand GmbH</title>"
    )
    assert sm._extract_org_name(page, "https://brand.de") == "Brand GmbH"
