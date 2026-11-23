"""The inbox connection must check who it is talking to.

imaplib.IMAP4_SSL without an ssl_context falls back to the standard
library's UNVERIFIED context: no certificate check and no host-name check.
Anyone on the path could present any certificate, read the mailbox password
at LOGIN and every reply after it - opt-outs and erasure requests included
(GDPR Art. 32). SMTP was already verified; IMAP was not.
"""

import ssl

from modules.imap_monitor import ImapMonitor


def _context_used(monkeypatch):
    captured = {}

    class _FakeIMAP:
        def __init__(self, host, port, **kwargs):
            captured.update(kwargs)

        def login(self, u, p):
            return ("OK", [b""])

    monkeypatch.setattr("modules.imap_monitor.imaplib.IMAP4_SSL", _FakeIMAP)
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.imap_host, monitor.imap_port = "imap.test", 993
    monitor.username, monitor.password = "u", "p"
    monitor._connect()
    return captured.get("ssl_context")


def test_the_certificate_and_host_name_are_checked(monkeypatch):
    context = _context_used(monkeypatch)
    assert isinstance(context, ssl.SSLContext), (
        "IMAP4_SSL got no ssl_context, so it used the unverified default"
    )
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
