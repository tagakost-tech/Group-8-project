"""A failed or refused send keeps the server's reason where it can be seen.

The server's answer reached the email log, but the contact itself was set to
'failed' or 'bounced' with its reason cleared, so the contact on its own said
nothing about what went wrong. A login failure kept only a fixed "check
credentials" string: Microsoft 365 answers 535 for both a wrong password and
"SMTP AUTH is disabled for the tenant", and only the server's own text tells
the two apart. And a message that could not even be built logged an error
naming neither the contact nor the address.
"""

import smtplib

import pytest

from modules.database import Database
from modules.email_sender import EmailSender


@pytest.fixture
def sender(tmp_path):
    s = EmailSender(
        Database(str(tmp_path / "s.db")), "smtp.test", 465, "u@test.com", "pw"
    )
    s.cid = s.db.add_contact(s.db.log_search("q"), "buyer@brand.de")
    return s


def _dispatch(s):
    return s.dispatch_to_contact(
        contact=s.db.get_contact_by_id(s.cid),
        subject="s",
        html_body="<p>h</p>",
        plain_body="h",
    )


def test_a_refused_address_keeps_the_servers_reason(sender, monkeypatch):
    refusal = "Recipient refused: 550 5.1.1 User unknown"
    monkeypatch.setattr(
        sender, "_deliver", lambda msg, blocked=None: (False, refusal, True, False)
    )
    _dispatch(sender)
    row = sender.db.get_contact_by_id(sender.cid)
    assert row["status"] == "bounced"
    assert "5.1.1 User unknown" in (row["discard_reason"] or "")


def test_a_failed_send_keeps_the_servers_reason(sender, monkeypatch):
    failure = "SMTP error: (451, b'4.7.0 Try again later')"
    monkeypatch.setattr(
        sender, "_deliver", lambda msg, blocked=None: (False, failure, False, False)
    )
    _dispatch(sender)
    row = sender.db.get_contact_by_id(sender.cid)
    assert row["status"] == "failed"
    assert "Try again later" in (row["discard_reason"] or "")


def test_a_login_failure_keeps_the_servers_text(sender, monkeypatch):
    text = b"5.7.139 Authentication unsuccessful, SmtpClientAuthentication is disabled"

    def refuse():
        raise smtplib.SMTPAuthenticationError(535, text)

    monkeypatch.setattr(sender, "_ensure_connection", refuse)
    _dispatch(sender)
    stored = sender.db.conn.execute("SELECT smtp_response FROM email_log").fetchone()[0]
    assert "SmtpClientAuthentication is disabled" in stored


def test_a_message_that_cannot_be_built_names_the_recipient(sender, monkeypatch):
    def broken(*_a, **_k):
        raise ValueError("header contains a newline")

    monkeypatch.setattr(sender, "create_email", broken)
    _dispatch(sender)
    errors = [
        r["details"]
        for r in sender.db.get_recent_activity(limit=20)
        if r["action"] == "EMAIL_ERROR"
    ]
    assert errors and "buyer@brand.de" in errors[0]
