"""Tests for imap_monitor pure classification helpers (no network)."""

from modules.imap_monitor import ImapMonitor


def _monitor():
    return ImapMonitor(
        db=None, imap_host="h", imap_port=993, username="u", password="p"
    )


def test_classify_bounce():
    m = _monitor()
    assert m._classify_email("mailer-daemon@x.com", "Undeliverable", "") == "bounce"
    # An explicit failure qualifier is a bounce...
    assert (
        m._classify_email("x@y.com", "Delivery Status Notification (Failure)", "")
        == "bounce"
    )


def test_bare_delivery_status_notification_is_not_a_bounce():
    """Microsoft 365 and Gmail use "Delivery Status Notification" for DELAY
    reports too ("(Delay)" - still queued, "you do not need to resend"). With no
    failure qualifier and no machine-readable DSN verdict the subject alone
    cannot tell them apart, so it must not suppress a live prospect."""
    m = _monitor()
    assert (
        m._classify_email("x@y.com", "Delivery Status Notification", "") == "dsn_info"
    )
    assert (
        m._classify_email(
            "postmaster@x.com", "Delivery Status Notification (Delay)", ""
        )
        == "dsn_info"
    )


def test_classify_unsubscribe():
    assert (
        _monitor()._classify_email("john@x.com", "Please unsubscribe me", "")
        == "unsubscribe"
    )


def test_classify_reply():
    assert (
        _monitor()._classify_email("john@x.com", "Re: your email", "Sounds interesting")
        == "reply"
    )


def test_extract_email_address():
    m = _monitor()
    assert m._extract_email_address("John Doe <john@example.com>") == "john@example.com"
    assert m._extract_email_address("plain@example.com") == "plain@example.com"
