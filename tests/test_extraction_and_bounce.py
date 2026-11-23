"""Tests for the search email-extraction hardening and the IMAP bounce/
auto-reply parsing fixes."""
import email as _email

from conftest import sent_to
from modules.database import Database
from modules.imap_monitor import ImapMonitor
from modules.search import SearchModule


def _search(tmp_path):
    return SearchModule(Database(str(tmp_path / "s.db")))


def _monitor(tmp_path):
    return ImapMonitor(Database(str(tmp_path / "i.db")), "imap.test", 993,
                       "me@corp.com", "pw")


# ── search.py: email extraction (SF1-SF4) ─────────────────────────────────

def test_ignores_script_and_style_emails(tmp_path):
    sm = _search(tmp_path)
    html = ('<html><body><p>Write <a href="mailto:sales@brand.de">us</a></p>'
            '<script>var dsn="deadbeef1234@sentry.wixpress.com";</script>'
            '<style>.a{color:red}</style></body></html>')
    emails = sm._extract_emails(html)
    assert "sales@brand.de" in emails                 # mailto captured
    assert not any("sentry" in e for e in emails)     # script config ignored


def test_rejects_asset_filename_email(tmp_path):
    sm = _search(tmp_path)
    emails = sm._extract_emails("<p>logo@2x.png and real info@brand.it</p>")
    assert "info@brand.it" in emails
    assert "logo@2x.png" not in emails


def test_decodes_entities_and_deobfuscation(tmp_path):
    sm = _search(tmp_path)
    emails = sm._extract_emails("<p>anna&#64;brand.de and mark [at] firm [dot] com</p>")
    assert "anna@brand.de" in emails
    assert "mark@firm.com" in emails


def test_blocked_prefix_word_boundary(tmp_path):
    sm = _search(tmp_path)
    assert sm._is_blocked_prefix("noreply")
    assert sm._is_blocked_prefix("no-reply")
    assert sm._is_blocked_prefix("support.team")
    assert not sm._is_blocked_prefix("supporter")     # must NOT be blocked
    assert not sm._is_blocked_prefix("adminaide")


# ── imap_monitor.py: auto-reply + bounce recipient (IF1, IF2) ─────────────

def test_auto_reply_detected_by_header(tmp_path):
    m = _monitor(tmp_path)
    msg = _email.message_from_string("Auto-Submitted: auto-replied\nSubject: hi\n\nbody")
    assert m._is_auto_reply(msg, "hi", "body") is True


def test_auto_reply_detected_by_subject(tmp_path):
    m = _monitor(tmp_path)
    msg = _email.message_from_string("Subject: Out of Office\n\nback monday")
    assert m._is_auto_reply(msg, "Out of Office: back Monday", "") is True


def test_genuine_reply_not_flagged_auto(tmp_path):
    m = _monitor(tmp_path)
    msg = _email.message_from_string("Subject: Re: your note\n\nSure, let's chat")
    assert m._is_auto_reply(msg, "Re: your note", "Sure, let's chat") is False


def test_bounce_recipient_from_dsn(tmp_path):
    m = _monitor(tmp_path)
    raw = (
        "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\n"
        "To: me@corp.com\n"
        "Subject: Delivery Status Notification (Failure)\n"
        "Content-Type: multipart/report; boundary=BOUND\n\n"
        "--BOUND\n"
        "Content-Type: text/plain\n\n"
        "Your message could not be delivered.\n\n"
        "--BOUND\n"
        "Content-Type: message/delivery-status\n\n"
        "Final-Recipient: rfc822; buyer@lostbrand.de\n"
        "Action: failed\nStatus: 5.1.1\n\n"
        "--BOUND--\n"
    )
    msg = _email.message_from_string(raw)
    assert m._extract_bounced_recipient(msg) == "buyer@lostbrand.de"


def test_process_bounces_suppresses_recipient_not_own_address(tmp_path):
    m = _monitor(tmp_path)
    sent_to(m.db, "buyer@lostbrand.de")
    m.process_bounces([{"email": "buyer@lostbrand.de", "to": "me@corp.com"}])
    assert m.db.is_suppressed("buyer@lostbrand.de")
    # An unresolved bounce (only the daemon address) must NOT suppress us.
    m.process_bounces([{"email": "mailer-daemon@googlemail.com", "to": "me@corp.com"}])
    assert not m.db.is_suppressed("me@corp.com")
