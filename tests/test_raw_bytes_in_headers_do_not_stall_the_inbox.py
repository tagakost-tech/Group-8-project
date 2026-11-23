"""Raw 8-bit bytes in a header the classifier reads do not stall the inbox.

For a header value carrying raw 8-bit bytes, such as an accented file name in
Content-Type or a localised Auto-Submitted, email.message_from_bytes (compat32)
returns an email.header.Header, not a str. _is_read_receipt and _is_auto_reply
called .lower() on Content-Type, Auto-Submitted and Precedence as they came,
and the AttributeError made the whole message unreadable: the reply, bounce or
read receipt was never acted on, the read was incomplete and the scan
watermark held. Live sending waits for a complete read, so one such message,
in the inbox or in the junk folder, held every live send until it aged out of
the 14-day look-back. These headers are now read as text, decoded like From
and Subject, by a decoder that keeps its promise never to raise.
"""

import pytest

from conftest import sent_to
from modules.database import Database
from modules.imap_monitor import ImapMonitor, inbox_read, watermark_key
from tests.test_imap_inbox_scan import FakeIMAP
from tests.test_the_junk_folder_is_read_too import FolderIMAP

ME = "me@corp.com"
BUYER = "buyer@brand.fr"
DEAD = "dead@brand.de"
INTERESTED = "Thanks, we are interested. Can you send prices?"
ACCENTED_NAME = b'Content-Type: text/plain; name="R\xc3\xa9sum\xc3\xa9.txt"\r\n'
ACCENTED_PARAM = b'; description="R\xc3\xa9sum\xc3\xa9"'
# Charset names that pass the codec lookup and still refuse to decode, with a
# plain UnicodeError and a plain ValueError, neither a UnicodeDecodeError.
REFUSING_WORDS = [b"=?undefined?Q?x?=", b"=?a\x00b?Q?x?="]


def _monitor(tmp_path, fake):
    db = Database(str(tmp_path / "h.db"))
    cid = db.add_contact(db.log_search("q"), BUYER)
    db.update_contact_status(cid, "emailed")
    monitor = ImapMonitor(db, "imap.test", 993, ME, "pw")
    monitor._connect = lambda: fake
    return monitor, db


def _reply(header: bytes = b"") -> bytes:
    return (
        (
            f"From: {BUYER}\r\nTo: {ME}\r\nSubject: Re: Quick question\r\n"
            "Date: Mon, 1 Jun 2026 10:00:00 +0200\r\n"
        ).encode()
        + header
        + f"\r\n{INTERESTED}".encode()
    )


def _bounce(param: bytes) -> bytes:
    return (
        b"From: postmaster@outlook.com\r\nTo: me@corp.com\r\n"
        b"Subject: Delivery Status Notification\r\n"
        b'Content-Type: multipart/report; report-type=delivery-status; boundary="B"'
        + param
        + b"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
        b"--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        b"Reporting-MTA: dns; mail.outlook.com\r\n\r\n"
        b"Final-Recipient: rfc822; dead@brand.de\r\nAction: failed\r\n"
        b"Status: 5.1.1\r\n\r\n--B--\r\n"
    )


def _read_receipt(param: bytes) -> bytes:
    # The subject check does not know this subject and no part is typed
    # message/disposition-notification, so only the text of the Content-Type
    # header can tell this is a receipt rather than a reply.
    return (
        b"From: buyer@brand.fr\r\nTo: me@corp.com\r\n"
        b"Subject: Return Receipt (displayed) - Quick question\r\n"
        b"Content-Type: multipart/report; report-type=disposition-notification; "
        b'boundary="M"' + param + b"\r\n\r\n"
        b"--M\r\nContent-Type: text/plain\r\n\r\nYour message was displayed.\r\n"
        b"--M--\r\n"
    )


def _assert_read_completely(summary, db):
    assert summary["ok"] is True, summary["error"]
    assert summary["error"] is None
    assert db.get_state(watermark_key(ME))
    # What the incomplete read cost: the send check refused all live mail.
    assert inbox_read(db, [ME], 30)[0] is True


def test_a_reply_with_an_ascii_content_type_is_read_completely(tmp_path):
    raw = _reply(b'Content-Type: text/plain; name="Resume.txt"\r\n')
    monitor, db = _monitor(tmp_path, FakeIMAP([raw]))

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == 1
    assert db.get_contact_by_email(BUYER)["status"] == "responded"


@pytest.mark.parametrize(
    "header",
    [ACCENTED_NAME, b'Content-Type: text/plain; name="R\xe9sum\xe9.txt"\r\n'],
    ids=["utf8", "latin1"],
)
def test_a_reply_with_raw_bytes_in_its_content_type_is_read_completely(
    tmp_path, header
):
    monitor, db = _monitor(tmp_path, FakeIMAP([_reply(header)]))

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == 1
    assert summary["reply_entries"][0]["body"] == INTERESTED
    assert db.get_contact_by_email(BUYER)["status"] == "responded"


def test_an_auto_reply_with_an_ascii_auto_submitted_is_read_completely(tmp_path):
    raw = _reply(b"Auto-Submitted: auto-replied\r\n")
    monitor, db = _monitor(tmp_path, FakeIMAP([raw]))

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == 0
    assert db.get_contact_by_email(BUYER)["status"] == "emailed"


@pytest.mark.parametrize(
    "header",
    [
        b"Auto-Submitted: auto-r\xc3\xa9plique\r\n",
        b"X-Autoreply: oui \xc3\xa9t\xc3\xa9\r\n",
    ],
    ids=["auto-submitted", "x-autoreply"],
)
def test_an_auto_reply_with_raw_utf8_in_its_marker_is_read_completely(tmp_path, header):
    monitor, db = _monitor(tmp_path, FakeIMAP([_reply(header)]))

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == 0
    assert db.get_contact_by_email(BUYER)["status"] == "emailed"


@pytest.mark.parametrize(
    "header",
    [
        b"Auto-Submitted: Auto-Replied\r\n",
        b"Auto-Submitted: \xd0\xb0\xd0\xb2\xd1\x82\xd0\xbe\r\n",
        b"Precedence: auto_reply\r\n",
        b"Precedence: Auto-Reply\r\n",
        b"Precedence: AUTO_REPLY\r\n",
    ],
    ids=[
        "auto-submitted-mixed-case",
        "auto-submitted-all-8bit",
        "precedence",
        "precedence-mixed-case",
        "precedence-upper-case",
    ],
)
def test_an_auto_reply_marker_in_any_case_or_script_is_not_a_reply(tmp_path, header):
    monitor, db = _monitor(tmp_path, FakeIMAP([_reply(header)]))

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == 0
    assert db.get_contact_by_email(BUYER)["status"] == "emailed"


@pytest.mark.parametrize("value", [b"no", b"No", b"NO"])
def test_auto_submitted_no_in_any_case_is_a_reply(tmp_path, value):
    monitor, db = _monitor(
        tmp_path, FakeIMAP([_reply(b"Auto-Submitted: " + value + b"\r\n")])
    )

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == 1
    assert db.get_contact_by_email(BUYER)["status"] == "responded"


def test_a_reply_with_raw_utf8_in_precedence_is_read_completely(tmp_path):
    raw = _reply(b"Precedence: bulk (priorit\xc3\xa9 basse)\r\n")
    monitor, db = _monitor(tmp_path, FakeIMAP([raw]))

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == 1
    assert db.get_contact_by_email(BUYER)["status"] == "responded"


@pytest.mark.parametrize("param", [b"", ACCENTED_PARAM], ids=["ascii", "utf8"])
def test_a_bounce_is_read_and_suppresses_whatever_its_content_type_carries(
    tmp_path, param
):
    monitor, db = _monitor(tmp_path, FakeIMAP([_bounce(param)]))
    sent_to(db, DEAD)

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["bounces"] == 1
    assert db.is_suppressed(DEAD)


@pytest.mark.parametrize("param", [b"", ACCENTED_PARAM], ids=["ascii", "utf8"])
@pytest.mark.parametrize(
    "report_type", [b"disposition-notification", b"Disposition-Notification"]
)
def test_a_read_receipt_known_only_by_its_content_type_is_not_a_reply(
    tmp_path, param, report_type
):
    raw = _read_receipt(param).replace(b"disposition-notification", report_type)
    monitor, db = _monitor(tmp_path, FakeIMAP([raw]))

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == 0
    assert db.get_contact_by_email(BUYER)["status"] == "emailed"


def test_a_junk_reply_with_raw_utf8_in_its_content_type_is_read_completely(tmp_path):
    fake = FolderIMAP(
        {"INBOX": [], "Junk Email": [_reply(ACCENTED_NAME)]}, junk_flag="Junk Email"
    )
    monitor, db = _monitor(tmp_path, fake)

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert db.get_state(watermark_key(ME) + "::junk")
    assert summary["replies"] == 1
    assert db.get_contact_by_email(BUYER)["status"] == "responded"


@pytest.mark.parametrize("word", REFUSING_WORDS, ids=["undefined", "nul"])
@pytest.mark.parametrize(
    ("header", "replies"),
    [
        (b'Content-Type: text/plain; name="%s"\r\n', 1),
        (b"Auto-Submitted: %s\r\n", 0),
        (b"Precedence: %s\r\n", 1),
    ],
    ids=["content-type", "auto-submitted", "precedence"],
)
def test_reading_a_header_as_text_adds_no_new_way_to_stall(
    tmp_path, header, replies, word
):
    """These headers were never decoded before, so an ASCII value could not
    fail here. The decoding that saves the 8-bit ones must not cost that."""
    monitor, db = _monitor(tmp_path, FakeIMAP([_reply(header % word)]))

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == replies


@pytest.mark.parametrize("word", REFUSING_WORDS, ids=["undefined", "nul"])
@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"Subject: Re: Quick question", b"Subject: Re: %s"),
        (b"From: buyer@brand.fr", b"From: %s <buyer@brand.fr>"),
    ],
    ids=["subject", "from"],
)
def test_a_charset_that_refuses_to_decode_does_not_cost_the_reply(
    tmp_path, old, new, word
):
    monitor, db = _monitor(tmp_path, FakeIMAP([_reply().replace(old, new % word)]))

    summary = monitor.run_check()

    _assert_read_completely(summary, db)
    assert summary["replies"] == 1
    assert db.get_contact_by_email(BUYER)["status"] == "responded"
