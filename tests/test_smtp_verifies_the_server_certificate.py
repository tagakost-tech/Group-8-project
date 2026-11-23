"""Every SMTP connection checks the server's certificate and its name.

smtplib's SMTP_SSL and starttls() without a context, or with one from
ssl._create_unverified_context(), accept any certificate for any name: anyone
on the path could take the mailbox password at login and read or change
every email sent. The sender (modules/email_sender.py) and doctor's live
login check (main.py) pass ssl.create_default_context(): CERT_REQUIRED with
check_hostname. These tests pin that for port 465 (SMTP_SSL) and for
STARTTLS on any other port, so neither can be loosened without a red test.
The IMAP side has its own: tests/test_imap_verifies_the_server_certificate.py.
"""

import smtplib
import ssl

import pytest

import main
from modules.email_sender import EmailSender


class _Recorder:
    """Stands in for smtplib.SMTP and SMTP_SSL, recording the TLS context."""

    def __init__(self, captured, kind):
        self.captured = captured
        self.kind = kind

    def __call__(self, host=None, port=None, *args, **kwargs):
        captured = self.captured
        kind = self.kind
        captured.setdefault("connections", []).append(kind)
        if kind == "SMTP_SSL":
            captured["context"] = kwargs.get("context")

        class _Session:
            def ehlo(self, *a, **k):
                return 250, b"ok"

            def starttls(self, *a, **k):
                captured["starttls"] = True
                captured["context"] = k.get("context", a[2] if len(a) > 2 else None)
                return 220, b"ready"

            def login(self, *a, **k):
                captured["login_after_tls"] = "context" in captured
                return 235, b"ok"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Session()


@pytest.fixture
def captured(monkeypatch):
    seen = {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Recorder(seen, "SMTP_SSL"))
    monkeypatch.setattr(smtplib, "SMTP", _Recorder(seen, "SMTP"))
    return seen


def _assert_verified(captured):
    context = captured.get("context")
    assert isinstance(context, ssl.SSLContext), (
        "no SSL context was passed, so smtplib used its unverified default"
    )
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert captured["login_after_tls"], "the password went out before TLS was up"


@pytest.mark.parametrize("port,kind", [(465, "SMTP_SSL"), (587, "SMTP"), (25, "SMTP")])
def test_the_sender_checks_the_certificate(db, captured, port, kind):
    sender = EmailSender(db, "smtp.test", port, "me@corp.com", "pw")
    sender._new_connection()
    assert captured["connections"] == [kind]
    if kind == "SMTP":
        assert captured.get("starttls"), "a plain connection was not upgraded"
    _assert_verified(captured)


@pytest.mark.parametrize("port,kind", [(465, "SMTP_SSL"), (587, "SMTP")])
def test_doctors_live_login_checks_the_certificate(
    settings_factory, tmp_path, captured, port, kind
):
    settings = settings_factory(
        db_path=str(tmp_path / "d.db"),
        smtp_host="smtp.test",
        smtp_port=port,
        smtp_username="me@corp.com",
        smtp_password="pw-for-the-test",
    )
    (check,) = main.run_doctor_checks(
        settings, live_smtp=True, network=False, only={"SMTP login (live)"}
    )
    assert check.ok is True, check.detail
    assert captured["connections"] == [kind]
    _assert_verified(captured)
