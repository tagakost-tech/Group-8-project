"""A huge message is never downloaded whole, and its opt-out still counts.

The inbox scan fetched every message whole (RFC822), on the send loop's own
thread: a 40 MB attachment, sent once or a hundred times, was read in full
on every scan. Now the size is asked first (RFC822.SIZE); above 10 MB only
the header and the first 256 KB of the body are fetched (BODY.PEEK, so
nothing is marked read). That is where an opt-out, a data request or a
bounce report sits, so none of them is lost.
"""

import re

import pytest

from modules.database import Database
from modules.imap_monitor import ImapMonitor

OBJECTOR = "anna.rossi@brand.de"
COLLEAGUE = "info@brand.de"
DEAD = "dead@brand.de"
STAMP = b'"01-Jun-2026 10:00:00 +0200"'
HUGE = 11 * 1024 * 1024


class FakeServer:
    """An IMAP server that answers RFC822.SIZE and partial fetches as a real
    one does, and records every fetch."""

    def __init__(self, messages, text_first=False):
        self.messages = messages
        self.text_first = text_first
        self.fetched = []

    def list(self, *args):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def select(self, folder, readonly=False):
        return "OK", [str(len(self.messages)).encode()]

    def search(self, charset, criteria):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, msg_id, parts):
        n = int(msg_id)
        raw = self.messages[n - 1]
        self.fetched.append((n, parts))
        if parts == "(RFC822.SIZE)":
            return "OK", [f"{n} (RFC822.SIZE {len(raw)})".encode()]
        if parts == "(INTERNALDATE RFC822)":
            line = f"{n} (INTERNALDATE ".encode() + STAMP + f" RFC822 {{{len(raw)}}}".encode()
            return "OK", [(line, raw), b")"]
        partial = re.fullmatch(
            r"\(INTERNALDATE BODY\.PEEK\[HEADER\] BODY\.PEEK\[TEXT\]<0\.(\d+)>\)", parts
        )
        assert partial, parts
        head, _, body = raw.partition(b"\r\n\r\n")
        head += b"\r\n\r\n"
        text = body[: int(partial.group(1))]
        header_item = (
            f"{n} (INTERNALDATE ".encode() + STAMP + f" BODY[HEADER] {{{len(head)}}}".encode(),
            head,
        )
        text_item = (f" BODY[TEXT]<0> {{{len(text)}}}".encode(), text)
        items = [text_item, header_item] if self.text_first else [header_item, text_item]
        return "OK", [*items, b")"]

    def logout(self):
        return "BYE", [b""]


def _attachment(size):
    line = "QUFB" * 19 + "\r\n"
    return line * (size // len(line) + 1)


def _huge_opt_out(frm=OBJECTOR):
    return (
        f"From: Anna Rossi <{frm}>\r\nTo: me@corp.com\r\nSubject: Re: hello\r\n"
        "Date: Mon, 1 Jun 2026 10:00:00 +0200\r\n"
        'Content-Type: multipart/mixed; boundary="M"\r\n\r\n'
        "--M\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        "Please remove me from your list.\r\n"
        "--M\r\nContent-Type: application/pdf\r\n"
        'Content-Disposition: attachment; filename="catalogue.pdf"\r\n'
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{_attachment(HUGE)}--M--\r\n"
    ).encode()


def _huge_bounce():
    return (
        "From: postmaster@outlook.com\r\nTo: me@corp.com\r\n"
        "Subject: Undeliverable: hello\r\n"
        "Content-Type: multipart/report; report-type=delivery-status; "
        'boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
        "--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; mail.outlook.com\r\n\r\n"
        f"Final-Recipient: rfc822; {DEAD}\r\nAction: failed\r\nStatus: 5.1.1\r\n\r\n"
        "--B\r\nContent-Type: message/rfc822\r\n\r\n"
        f"From: me@corp.com\r\nTo: {DEAD}\r\nSubject: hello\r\n"
        'Content-Type: multipart/mixed; boundary="O"\r\n\r\n'
        "--O\r\nContent-Type: application/pdf\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{_attachment(HUGE)}--O--\r\n--B--\r\n"
    ).encode()


def _small_opt_out(frm=COLLEAGUE):
    return (
        f"From: {frm}\r\nTo: me@corp.com\r\nSubject: Re: hello\r\n"
        "Date: Mon, 1 Jun 2026 10:00:00 +0200\r\n\r\nPlease remove me from your list.\r\n"
    ).encode()


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "h.db"))
    sid = database.log_search("q", source="test")
    for address in (OBJECTOR, COLLEAGUE, DEAD, "someone@other.de"):
        cid = database.add_contact(sid, address, organization="Brand")
        database.log_email(cid, address, "Hello", delivery_status="sent")
    return database


def _monitor(db, server):
    m = ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw")
    m._connect = lambda: server
    return m


def _whole_fetches(server, n):
    return [p for i, p in server.fetched if i == n and "RFC822)" in p and "SIZE" not in p]


def test_a_huge_opt_out_is_honoured_without_downloading_it(db):
    server = FakeServer([_huge_opt_out()])
    summary = _monitor(db, server).run_check()

    assert summary["ok"] is True
    assert db.is_suppressed(OBJECTOR)
    assert db.get_contact_by_email(OBJECTOR)["status"] == "unsubscribed"
    assert db.is_blocked(COLLEAGUE), "an objection from someone we emailed stops the company"
    assert _whole_fetches(server, 1) == []
    assert server.fetched == [
        (1, "(RFC822.SIZE)"),
        (1, "(INTERNALDATE BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.262144>)"),
    ]


def test_a_huge_bounce_report_still_marks_the_address(db):
    server = FakeServer([_huge_bounce()])
    _monitor(db, server).run_check()

    assert db.is_suppressed(DEAD)
    assert db.get_contact_by_email(DEAD)["status"] == "bounced"
    assert _whole_fetches(server, 1) == []


def test_the_parts_are_read_in_whatever_order_the_server_sends_them(db):
    server = FakeServer([_huge_opt_out()], text_first=True)
    _monitor(db, server).run_check()
    assert db.is_suppressed(OBJECTOR)


def test_a_small_message_is_still_read_whole(db):
    server = FakeServer([_small_opt_out()])
    _monitor(db, server).run_check()

    assert db.is_suppressed(COLLEAGUE)
    assert server.fetched == [(1, "(RFC822.SIZE)"), (1, "(INTERNALDATE RFC822)")]


def test_a_message_at_exactly_the_limit_is_read_whole(db):
    raw = _small_opt_out()
    raw += b"x" * (ImapMonitor.LARGE_MESSAGE_BYTES - len(raw))
    server = FakeServer([raw])
    _monitor(db, server).run_check()
    assert _whole_fetches(server, 1) == ["(INTERNALDATE RFC822)"]


def test_large_and_small_messages_in_one_scan(db):
    server = FakeServer([_huge_opt_out(), _small_opt_out("someone@other.de"), _huge_bounce()])
    summary = _monitor(db, server).run_check()
    assert summary["ok"] is True
    assert db.is_suppressed(OBJECTOR)
    assert db.is_suppressed("someone@other.de")
    assert db.is_suppressed(DEAD)
    assert _whole_fetches(server, 1) == [] and _whole_fetches(server, 3) == []
    assert _whole_fetches(server, 2) == ["(INTERNALDATE RFC822)"]


def test_a_size_written_inside_a_message_is_not_believed(db):
    """The size is read from the server's line, never from the message: mail
    whose own text says "RFC822.SIZE 1" is still judged by its real size."""
    raw = _huge_opt_out().replace(b"Please remove me", b"RFC822.SIZE 1 Please remove me")

    class Lying(FakeServer):
        def fetch(self, msg_id, parts):
            if parts == "(RFC822.SIZE)":
                self.fetched.append((int(msg_id), parts))
                return "OK", [(b"1 (RFC822.SIZE " + str(len(raw)).encode() + b")", raw)]
            return super().fetch(msg_id, parts)

    server = Lying([raw])
    _monitor(db, server).run_check()
    assert _whole_fetches(server, 1) == []


def test_a_partial_answer_without_the_header_is_an_unread_message(db):
    """The read is then incomplete, so the watermark holds and the next scan
    tries again; nothing is guessed from a body without its header."""

    class Headless(FakeServer):
        def fetch(self, msg_id, parts):
            answer = super().fetch(msg_id, parts)
            if "BODY.PEEK" in parts:
                return "OK", [
                    item
                    for item in answer[1]
                    if not (isinstance(item, tuple) and b"BODY[HEADER]" in item[0])
                ]
            return answer

    m = _monitor(db, Headless([_huge_opt_out()]))
    summary = m.run_check()
    assert summary["ok"] is False
    assert "1/1 messages could not be read" in summary["error"]
    assert db.get_state(m._watermark_key) is None
    assert not db.is_suppressed(OBJECTOR)
