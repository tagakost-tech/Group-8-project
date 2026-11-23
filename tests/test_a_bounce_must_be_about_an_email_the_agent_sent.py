"""A bounce report suppresses an address only if it is about an email the
agent sent to that address.

Anyone can write a report that says a prospect's address is dead, and one
the receiving server did not find forged suppressed the prospect for good.
Now a report is matched against the agent's own sends:
- when it gives the original message's Message-ID (the attached original,
  whole or as its headers only, Gmail's X-Original-Message-ID, or the
  report's own In-Reply-To and References), that must be the Message-ID of a
  send to the address, however old;
- when it gives none, the agent must have sent to the address in the last
  30 days;
- a send whose Message-ID could not be recorded counts by its date;
- otherwise the report is logged as unmatched and suppresses nothing.
The authentication rules stay as they were, and a genuine hard bounce for a
real send, as Exchange Online and Gmail write them, still suppresses the
address and marks the contact bounced (cardinal rule 1).
"""

import email
import smtplib
from datetime import timedelta

import pytest

from conftest import sent_to
from modules import clock
from modules.database import Database
from modules.email_sender import EmailSender
from modules.imap_monitor import ImapMonitor
from tests.test_imap_inbox_scan import FakeIMAP

DEAD = "dead@brand.de"
OTHER = "buyer@other-brand.de"
MAILBOX = "outreach@suppleco.test"


@pytest.fixture(autouse=True)
def _no_real_smtp(monkeypatch):
    def refused(*_a, **_k):
        raise AssertionError("a test reached for a real SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", refused)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refused)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "b.db"))


class _Server:
    """A fake SMTP server that keeps every message it accepts."""

    def __init__(self):
        self.messages = []

    def noop(self):
        return (250, b"2.0.0 OK")

    def send_message(self, msg):
        self.messages.append(msg)
        return {}

    def quit(self):
        pass

    def close(self):
        pass


def _contact(db, email=DEAD):
    return db.add_contact(db.log_search("q"), email, organization="Brand GmbH")


def _really_send(db, email=DEAD):
    """A send through the send path itself; returns the message that went."""
    server = _Server()
    sender = EmailSender(
        db=db,
        smtp_host="smtp.test",
        smtp_port=587,
        smtp_username=MAILBOX,
        smtp_password="pw",
        sender_name="Agent",
        company_name="SUPPLECO",
    )
    sender._new_connection = lambda: server
    assert sender.dispatch_to_contact(
        contact=db.get_contact_by_email(email),
        subject="Supplements for your brand",
        html_body="<p>Hello</p>",
        plain_body="Hello",
        notice_version="2026-11",
    )
    (msg,) = server.messages
    return msg


def _original_headers(message_id, to=DEAD):
    return (
        f"Message-ID: {message_id}\r\n"
        f"From: Agent <{MAILBOX}>\r\n"
        f"To: {to}\r\n"
        "Subject: Supplements for your brand\r\n"
        "Date: Tue, 17 Nov 2026 09:12:00 +0100\r\n"
        "MIME-Version: 1.0\r\n"
    )


def _exchange_ndr(message_id, to=DEAD, attach="message/rfc822", threaded=True):
    """An Exchange Online non-delivery report as it reaches the sending
    mailbox: a human part, the delivery-status part, and the original's
    headers attached (as message/rfc822, or text/rfc822-headers)."""
    thread = (
        f"In-Reply-To: {message_id}\r\nReferences: {message_id}\r\n" if threaded else ""
    )
    original = (
        f"--ndr\r\nContent-Type: {attach}\r\n\r\n{_original_headers(message_id, to)}"
        if attach
        else ""
    )
    return (
        "From: Microsoft Outlook "
        "<MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@suppleco.test>\r\n"
        f"To: <{MAILBOX}>\r\n"
        "Date: Tue, 17 Nov 2026 09:12:05 +0000\r\n"
        "Subject: Undeliverable: Supplements for your brand\r\n"
        "Content-Type: multipart/report; report-type=delivery-status;\r\n"
        '\tboundary="ndr"\r\n'
        "Content-Language: en-US\r\n"
        "Message-ID: <ndr-1@AM0PR01MB1234.eurprd01.prod.exchangelabs.com>\r\n"
        f"{thread}"
        "MIME-Version: 1.0\r\n"
        "X-MS-Exchange-Message-Is-Ndr:\r\n"
        "\r\n"
        "--ndr\r\nContent-Type: text/plain; charset=us-ascii\r\n\r\n"
        f"Your message to {to} couldn't be delivered.\r\n"
        f"{to.split('@')[0]} wasn't found at {to.split('@')[1]}.\r\n\r\n"
        "--ndr\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns;AM0PR01MB1234.eurprd01.prod.exchangelabs.com\r\n"
        "Received-From-MTA: dns;AM0PR01MB1234.eurprd01.prod.exchangelabs.com\r\n"
        "Arrival-Date: Tue, 17 Nov 2026 09:12:03 +0000\r\n\r\n"
        f"Final-Recipient: rfc822;{to}\r\n"
        "Action: failed\r\n"
        "Status: 5.1.10\r\n"
        "Diagnostic-Code: smtp;550 5.1.10 RESOLVER.ADR.RecipientNotFound; "
        "Recipient not found by SMTP address lookup\r\n"
        "Remote-MTA: dns;mx.brand.de\r\n\r\n"
        f"{original}--ndr--\r\n"
    ).encode()


def _gmail_dsn(message_id, to=DEAD, attach="text/rfc822-headers", threaded=True):
    """Gmail's Delivery Status Notification (Failure), as mailer-daemon at
    googlemail.com sends it, signed and passing for its own domain."""
    thread = (
        f"References: {message_id}\r\nIn-Reply-To: {message_id}\r\n" if threaded else ""
    )
    original = (
        f"--gm\r\nContent-Type: {attach}\r\n\r\n{_original_headers(message_id, to)}"
        if attach
        else ""
    )
    return (
        "Authentication-Results: mx.suppleco.test; dkim=pass "
        "header.d=googlemail.com; spf=pass smtp.mailfrom=googlemail.com; "
        "dmarc=pass header.from=googlemail.com\r\n"
        "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\n"
        f"To: {MAILBOX}\r\n"
        "Subject: Delivery Status Notification (Failure)\r\n"
        "Date: Tue, 17 Nov 2026 01:12:04 -0800 (PST)\r\n"
        "Message-ID: <6390a1b2.050a0220.1c2d.0000.GMR@mx.google.com>\r\n"
        f"{thread}"
        f"X-Failed-Recipients: {to}\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; boundary="gm"; '
        "report-type=delivery-status\r\n\r\n"
        "--gm\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n"
        f"Address not found\r\nYour message wasn't delivered to {to} because "
        "the address couldn't be found.\r\n\r\n"
        "--gm\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; googlemail.com\r\n"
        f"Received-From-MTA: dns; {MAILBOX}\r\n"
        "Arrival-Date: Tue, 17 Nov 2026 01:12:03 -0800 (PST)\r\n"
        f"X-Original-Message-ID: {message_id}\r\n\r\n"
        f"Final-Recipient: rfc822; {to}\r\n"
        "Action: failed\r\n"
        "Status: 5.1.1\r\n"
        "Remote-MTA: dns; mx.brand.de. (192.0.2.10, the server for the domain "
        "brand.de.)\r\n"
        "Diagnostic-Code: smtp; 550-5.1.1 The email account that you tried to "
        "reach does not exist.\r\n"
        "Last-Attempt-Date: Tue, 17 Nov 2026 01:12:04 -0800 (PST)\r\n\r\n"
        f"{original}--gm--\r\n"
    ).encode()


def _read(db, *raw):
    monitor = ImapMonitor(db, "imap.test", 993, MAILBOX, "pw")
    fake = FakeIMAP(list(raw))
    monitor._connect = lambda: fake
    summary = monitor.run_check()
    assert summary["bounces"] == len(raw), "setup: each is read as a bounce"
    return summary


def _status(db, email=DEAD):
    return db.get_contact_by_email(email)["status"]


def _unmatched(db):
    return [
        r["details"]
        for r in db.get_recent_activity(100)
        if r["action"] == "BOUNCE_UNMATCHED"
    ]


# ── Genuine bounces for real sends (cardinal rule 1) ──


@pytest.mark.parametrize(
    ("attach", "threaded"),
    [
        ("message/rfc822", True),
        ("text/rfc822-headers", True),
        ("message/rfc822", False),
        (None, True),
    ],
    ids=["original", "headers-only", "original-unthreaded", "threaded-only"],
)
def test_an_exchange_online_ndr_for_a_real_send_suppresses(db, attach, threaded):
    _contact(db)
    sent = _really_send(db)

    _read(db, _exchange_ndr(sent["Message-ID"], attach=attach, threaded=threaded))

    assert db.is_suppressed(DEAD)
    assert _status(db) == "bounced"
    assert _unmatched(db) == []


@pytest.mark.parametrize("attach", ["text/rfc822-headers", "message/rfc822"])
def test_a_gmail_dsn_for_a_real_send_suppresses(db, attach):
    _contact(db)
    sent = _really_send(db)

    _read(db, _gmail_dsn(sent["Message-ID"], attach=attach))

    assert db.is_suppressed(DEAD)
    assert _status(db) == "bounced"


def test_the_send_path_records_the_message_id_it_sent(db):
    _contact(db)
    sent = _really_send(db)

    (row,) = db.conn.execute(
        "SELECT message_id FROM email_log WHERE recipient_email = ?", (DEAD,)
    ).fetchall()
    assert row[0] == str(sent["Message-ID"])


def test_an_old_send_matched_by_its_message_id_suppresses(db):
    _contact(db)
    sent = _really_send(db)
    db.conn.execute(
        "UPDATE email_log SET timestamp = ?",
        ((clock.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    db.conn.commit()

    _read(db, _exchange_ndr(sent["Message-ID"]))

    assert db.is_suppressed(DEAD)


def test_a_send_whose_message_id_was_not_recorded_counts_by_its_date(db):
    """Recording the Message-ID is best-effort in the send path: a locked
    database must not let a real bounce for that send be ignored."""
    cid = _contact(db)
    db.log_email(cid, DEAD, "A first email", delivery_status="sent")

    _read(db, _exchange_ndr("<lost-in-the-send@suppleco.test>"))

    assert db.is_suppressed(DEAD)


def test_a_send_a_restore_kept_by_the_token_is_matched_by_its_message_id(db):
    message_id = sent_to(db, DEAD)
    assert db.get_contact_by_email(DEAD) is None, "setup: kept by the token only"

    _read(db, _gmail_dsn(message_id))

    assert db.is_suppressed(DEAD)


def test_a_report_with_no_message_id_matches_a_send_in_the_last_30_days(db):
    cid = _contact(db)
    db.log_email(cid, DEAD, "A first email", delivery_status="sent", message_id="<a@x>")

    _read(db, _exchange_ndr("", attach=None, threaded=False))

    assert db.is_suppressed(DEAD)


def test_the_message_id_is_compared_without_its_brackets(db):
    cid = _contact(db)
    db.log_email(
        cid,
        DEAD,
        "A first email",
        delivery_status="sent",
        message_id="<m1@suppleco.test>",
    )
    raw = _exchange_ndr("m1@suppleco.test", attach=None)

    _read(db, raw)

    assert db.is_suppressed(DEAD)


# ── Reports about no email of ours suppress no one ──


@pytest.mark.parametrize(
    "report",
    [
        lambda m: _exchange_ndr(m, attach="message/rfc822", threaded=False),
        lambda m: _exchange_ndr(m, attach="text/rfc822-headers", threaded=False),
        lambda m: _exchange_ndr(m, attach=None).replace(
            f"References: {m}\r\n".encode(), b""
        ),
        lambda m: _gmail_dsn(m, attach=None, threaded=False),
    ],
    ids=["attached-original", "attached-headers", "in-reply-to", "x-original-id"],
)
def test_a_message_id_the_agent_never_sent_is_read_wherever_it_is(db, report):
    """Each place a report gives the original's Message-ID is read: one it
    missed would leave the report with none, matched by its date alone."""
    _contact(db)
    _really_send(db)

    _read(db, report("<forged-2@attacker.example>"))

    assert not db.is_suppressed(DEAD)
    assert len(_unmatched(db)) == 1


def test_a_message_id_only_in_the_references_is_read(db):
    _contact(db)
    _really_send(db)
    raw = _exchange_ndr("", attach=None, threaded=False).replace(
        b"MIME-Version: 1.0\r\nX-MS",
        b"References: <older@suppleco.test> <forged-3@attacker.example>\r\n"
        b"MIME-Version: 1.0\r\nX-MS",
    )

    _read(db, raw)

    assert not db.is_suppressed(DEAD)
    assert len(_unmatched(db)) == 1


def test_a_report_naming_a_message_the_agent_never_sent_suppresses_nothing(db):
    _contact(db)
    _really_send(db)

    _read(db, _gmail_dsn("<forged-1@attacker.example>"))

    assert not db.is_suppressed(DEAD)
    assert _status(db) == "emailed"
    (logged,) = _unmatched(db)
    assert "matches no email the agent sent" in logged
    assert "the Message-ID it names" in logged


def test_a_report_naming_a_send_to_someone_else_suppresses_nothing(db):
    """A Message-ID a recipient saw does not let them bounce another address."""
    _contact(db)
    _contact(db, OTHER)
    sent_elsewhere = _really_send(db, OTHER)

    _read(db, _exchange_ndr(sent_elsewhere["Message-ID"], to=DEAD))

    assert not db.is_suppressed(DEAD)
    assert not db.is_suppressed(OTHER)
    assert len(_unmatched(db)) == 1


def test_a_report_with_no_message_id_for_an_address_never_sent_to_suppresses_nothing(
    db,
):
    _contact(db)

    _read(db, _exchange_ndr("", attach=None, threaded=False))

    assert not db.is_suppressed(DEAD)
    assert _status(db) == "pending"
    (logged,) = _unmatched(db)
    assert "by its date" in logged


def test_a_report_with_no_message_id_after_30_days_suppresses_nothing(db):
    cid = _contact(db)
    db.log_email(
        cid,
        DEAD,
        "A first email",
        delivery_status="sent",
        message_id="<a@x>",
        timestamp=(clock.now() - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S"),
    )

    _read(db, _exchange_ndr("", attach=None, threaded=False))

    assert not db.is_suppressed(DEAD)


@pytest.mark.parametrize("status", ["failed", "rejected", "dry_run"])
def test_an_email_that_never_went_out_is_no_match(db, status):
    cid = _contact(db)
    db.log_email(cid, DEAD, "A first email", delivery_status=status, message_id="<a@x>")

    _read(db, _exchange_ndr("<a@x>"))

    assert not db.is_suppressed(DEAD)


def test_a_forged_report_with_a_real_message_id_is_still_refused(db):
    """The authentication rules come first, as before."""
    _contact(db)
    sent = _really_send(db)
    raw = _exchange_ndr(sent["Message-ID"])
    raw = b"Authentication-Results: mx.suppleco.test; dmarc=fail\r\n" + raw

    _read(db, raw)

    assert not db.is_suppressed(DEAD)
    assert _unmatched(db) == [], "refused as forged, before any matching"


def test_the_report_gives_every_message_id_it_holds():
    raw = _gmail_dsn("<m1@suppleco.test>", attach="message/rfc822")
    msg = email.message_from_bytes(raw)
    assert ImapMonitor._original_message_ids(msg) == ["m1@suppleco.test"]
    bare = email.message_from_bytes(
        b"From: postmaster@x.test\r\nIn-Reply-To: m2@suppleco.test\r\n\r\nbody"
    )
    assert ImapMonitor._original_message_ids(bare) == ["m2@suppleco.test"]
