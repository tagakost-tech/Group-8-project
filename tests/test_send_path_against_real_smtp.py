"""The send path, against a server that actually speaks SMTP.

Every other test in this suite fakes sending by replacing `_deliver` or
`dispatch_to_contact` with something that returns a tuple. That covers the
bookkeeping and skips the decision that matters most: whether a server's answer
means "this address is dead, suppress it and never write again" or "try later".

That decision is made from real smtplib exceptions and real response codes, and
until now it had never seen either. Both directions are expensive. A transient
4xx read as permanent suppresses a live prospect with no undo - 235 good
contacts were retired that way once. A permanent 5xx read as transient asks the
same server the same question until the sending domain's reputation pays.

These tests assert on what the DATABASE says afterwards, not on a return value:
suppressed, queued for retry, or neither.

NOT under test: TLS trust. The sink presents a self-signed certificate, so
verification is relaxed for the duration. Certificate validation is smtplib's
job and is not what these tests are about.
"""

from __future__ import annotations

import smtplib
import ssl

import pytest
from smtp_sink import SmtpSink

from modules.database import Database
from modules.email_sender import EmailSender


@pytest.fixture
def trust_anything(monkeypatch):
    """Accept the sink's self-signed certificate."""

    def _permissive(*a, **kw):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    monkeypatch.setattr(ssl, "create_default_context", _permissive)


def _sender(db, port):
    return EmailSender(
        db=db,
        smtp_host="127.0.0.1",
        smtp_port=port,
        smtp_username="agent@suppleco.test",
        smtp_password="not-a-real-password",
        sender_name="Marco",
        company_name="SUPPLECO",
    )


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "smtp.db"))


def _contact(db, email):
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, email, name="Anna", organization="Brand GmbH")
    return dict(db.get_contact_by_id(cid))


def _send(sender, contact):
    return sender.dispatch_to_contact(
        contact,
        subject="A question",
        html_body="<p>Hello</p>",
        plain_body="Hello",
    )


# ── The happy path actually reaches a socket ───────────────────────────────


def test_a_message_is_delivered_and_recorded(db, trust_anything):
    with SmtpSink() as sink:
        sender = _sender(db, sink.port)
        contact = _contact(db, "buyer@brand.de")

        _send(sender, contact)
        sender.close()

    assert len(sink.received) == 1, "nothing reached the server"
    assert sink.received[0]["recipients"] == ["buyer@brand.de"]
    assert db.is_suppressed("buyer@brand.de") is False
    assert db.get_contact_by_id(contact["id"])["status"] == "emailed"


def test_the_message_body_is_what_we_think_it_is(db, trust_anything):
    """The wire is the only place to check this. Everything upstream is a
    template render nobody has read end to end."""
    with SmtpSink() as sink:
        sender = _sender(db, sink.port)
        _send(sender, _contact(db, "buyer@brand.de"))
        sender.close()

    body = sink.received[0]["body"]
    assert "A question" in body
    assert "buyer@brand.de" in body
    # RFC 8058 one-click unsubscribe. Gmail and Outlook require it for bulk
    # senders, and this is the only place it can be confirmed to reach the
    # wire rather than merely being assembled somewhere upstream.
    assert "List-Unsubscribe" in body, (
        "the one-click unsubscribe header never reached the message"
    )
    # NOTE: no Message-ID assertion. The codebase does not set one - checked
    # across every module, not just this file. Microsoft 365 will add one on
    # submission so mail still delivers, but follow-ups cannot thread to the
    # original without a Message-ID we chose and stored (In-Reply-To /
    # References). Reported rather than fixed here: it changes outgoing
    # headers and interacts with how follow-up sequences should thread.


# ── A permanent rejection must suppress, and only then ─────────────────────


def test_a_550_suppresses_the_address(db, trust_anything):
    """5.1.1 unknown user is the server saying the mailbox does not exist.
    Writing again costs reputation and tells us nothing new."""
    with SmtpSink(
        rcpt_responses={"dead@brand.de": "550 5.1.1 <dead@brand.de> unknown user"}
    ) as sink:
        sender = _sender(db, sink.port)
        contact = _contact(db, "dead@brand.de")
        _send(sender, contact)
        sender.close()

    assert sink.received == [], "a rejected recipient still received the body"
    assert db.is_suppressed("dead@brand.de") is True, (
        "a hard rejection did not suppress the address - it will be retried"
    )


@pytest.mark.parametrize(
    "code",
    [
        "421 4.7.0 too many connections, try later",
        "450 4.2.1 mailbox temporarily unavailable",
        "451 4.3.0 greylisted, please retry",
        "452 4.2.2 mailbox full",
    ],
)
def test_a_transient_failure_never_suppresses(db, trust_anything, code):
    """The 4xx family means "not now". Suppression has no undo, so reading any
    of these as permanent permanently loses a live prospect."""
    with SmtpSink(rcpt_responses={"slow@brand.de": code}) as sink:
        sender = _sender(db, sink.port)
        contact = _contact(db, "slow@brand.de")
        _send(sender, contact)
        sender.close()

    assert db.is_suppressed("slow@brand.de") is False, (
        f"a transient {code.split()[0]} suppressed the address permanently"
    )


def test_a_disconnect_mid_message_does_not_suppress(db, trust_anything):
    """The server vanished after accepting the recipient but before confirming
    the body. The message may or may not have been queued - which is precisely
    why it cannot be read as a bounce."""
    with SmtpSink(drop_on_data=True) as sink:
        sender = _sender(db, sink.port)
        contact = _contact(db, "flaky@brand.de")
        _send(sender, contact)
        sender.close()

    assert db.is_suppressed("flaky@brand.de") is False


# ── Authentication ─────────────────────────────────────────────────────────


def test_a_rejected_login_does_not_blame_the_recipient(db, trust_anything):
    """Wrong credentials are OUR problem. Suppressing the prospect, or marking
    them undeliverable, would destroy good contacts over a typo in .env."""
    with SmtpSink(auth_response="535 5.7.8 authentication failed") as sink:
        sender = _sender(db, sink.port)
        contact = _contact(db, "innocent@brand.de")
        _send(sender, contact)
        sender.close()

    assert db.is_suppressed("innocent@brand.de") is False, (
        "a bad password suppressed a perfectly good address"
    )
    assert db.get_contact_by_id(contact["id"])["status"] != "bounced"


def test_a_rejected_login_is_latched_not_retried_per_contact(db, trust_anything):
    """Every attempt with bad credentials is another failed login against the
    provider, which is how a mailbox gets locked. The sender must notice once."""
    with SmtpSink(auth_response="535 5.7.8 authentication failed") as sink:
        sender = _sender(db, sink.port)
        for i in range(3):
            _send(sender, _contact(db, f"p{i}@brand.de"))
        sender.close()

    assert getattr(sender, "auth_failed", False) is True, (
        "the sender did not latch the authentication failure"
    )


# ── Connection reuse ───────────────────────────────────────────────────────


def test_the_connection_is_reused_across_sends(db, trust_anything):
    """One login per batch, not per email. Re-authenticating for every message
    is both slow and a pattern providers rate-limit."""
    with SmtpSink() as sink:
        sender = _sender(db, sink.port)
        for i in range(3):
            _send(sender, _contact(db, f"buyer{i}@brand.de"))
        sender.close()

    assert len(sink.received) == 3
    assert sorted(r["recipients"][0] for r in sink.received) == [
        "buyer0@brand.de",
        "buyer1@brand.de",
        "buyer2@brand.de",
    ]


def test_a_dead_pooled_connection_is_replaced(db, trust_anything):
    """The provider idles connections out. The next send must reconnect rather
    than fail - a stale socket is not a delivery problem."""
    with SmtpSink() as sink:
        sender = _sender(db, sink.port)
        _send(sender, _contact(db, "first@brand.de"))

        # Kill the pooled connection the way an idle timeout would.
        if sender._smtp is not None:
            try:
                sender._smtp.close()
            except (smtplib.SMTPException, OSError):
                pass

        _send(sender, _contact(db, "second@brand.de"))
        sender.close()

    assert len(sink.received) == 2, "the send after a dropped connection was lost"
