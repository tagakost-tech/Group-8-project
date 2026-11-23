"""An opt-out is filed under its sender, whatever form the From header takes.

The sender was the text between the first '<' and the first '>' of the
decoded From header, or else the whole header. An opt-out from
'anna@brand.it (Anna Rossi)' was filed under that whole string, one from
'"Rossi, Anna <Vendite>" <anna@brand.it>' under 'vendite', and one from
'"Anna > Sales" <anna@brand.it>' was dropped, since the slice came out empty.
An encoded display name that decodes to 'Anna <Sales>' gave 'sales'. Each
time the sender stayed mailable after objecting (GDPR Art. 21), and a data
request or a reply was lost the same way. The raw header is now read with
the standard library's address parser, and the result is lower-cased and
checked with is_address. A sender it cannot read is recorded for review, as
an unparseable bounce is, instead of being filed under nothing. Whatever the
From header holds, the message must not become one the scan can never finish:
that holds the watermark, and with it every live send.
"""

import base64
import sqlite3

import pytest

from conftest import sent_to
from tests.test_imap_inbox_scan import _dsn_raw, _monitor, _raw
from tests.test_the_junk_folder_is_read_too import FolderIMAP
from tests.test_the_junk_folder_is_read_too import _monitor as _junk_monitor

ANNA = "anna@brand.it"
OPT_OUT = "Please remove me."
REQUEST = "Please delete my personal data."
REPLY = "Yes, send me a quote."

UNUSUAL = [
    "anna@brand.it (Anna Rossi)",
    '"Rossi, Anna <Vendite>" <anna@brand.it>',
    '"Anna > Sales" <anna@brand.it>',
    "=?utf-8?q?Anna_=3CSales=3E?= <anna@brand.it>",
]

UNREADABLE = ["Anna Rossi", "undisclosed-recipients:;", "", "anna@brand"]

# Deeper than the recursion limit of the parser's comment and group handling.
NESTED_COMMENTS = "(" * 5000 + ")" * 5000
TOO_DEEP = {
    "nested comments": NESTED_COMMENTS,
    "nested comments before an address": f"{NESTED_COMMENTS} <{ANNA}>",
    "comments never closed": "(" * 5000,
    "nested groups": "a:" * 5000,
}

# An encoded word in utf-7 can decode to a lone surrogate, which SQLite refuses.
LONE_SURROGATE = "=?utf-7?q?+2AA-?="


def _emailed(db):
    cid = db.add_contact(db.log_search("q"), ANNA, organization="Brand")
    db.log_email(cid, ANNA, "Hello", delivery_status="sent")
    return cid


def _suppressed(db):
    return [r[0] for r in db.conn.execute("SELECT email FROM suppression_list")]


def _review_rows(db):
    return [r for r in db.get_recent_activity(50) if r["action"] == "SENDER_UNPARSED"]


@pytest.mark.parametrize("sender", UNUSUAL)
def test_an_opt_out_is_filed_under_the_sender(tmp_path, sender):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", OPT_OUT)])
    cid = _emailed(monitor.db)

    monitor.run_check()

    assert _suppressed(monitor.db) == [ANNA]
    assert monitor.db.get_contact_by_id(cid)["status"] == "unsubscribed"


@pytest.mark.parametrize("sender", UNUSUAL)
def test_a_data_request_is_registered_under_the_sender(tmp_path, sender):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", REQUEST)])

    monitor.run_check()

    assert [r["email"] for r in monitor.db.list_dsrs()] == [ANNA]
    assert _suppressed(monitor.db) == [ANNA]


@pytest.mark.parametrize("sender", UNUSUAL)
def test_a_reply_reaches_the_contact(tmp_path, sender):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", REPLY)])
    cid = _emailed(monitor.db)

    monitor.run_check()

    assert monitor.db.get_contact_by_id(cid)["status"] == "responded"


@pytest.mark.parametrize(
    "sender",
    [
        "Anna Rossi <anna@brand.it>",
        "Anna@Brand.IT",
        # Unquoted, which parseaddr alone reads as two addresses.
        "Rossi, Anna <anna@brand.it>",
        # Raw 8-bit, which the email package returns as a Header object.
        "Müller <anna@brand.it>",
        "=?utf-8?q?M=C3=BCller=2C_Anna?=\r\n <anna@brand.it>",
        # Decoded, this display name reads as another address.
        "=?utf-8?q?vendite=40brand=2Eit?= <anna@brand.it>",
    ],
)
def test_the_forms_read_before_are_still_read(tmp_path, sender):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", OPT_OUT)])

    summary = monitor.run_check()

    assert _suppressed(monitor.db) == [ANNA]
    assert summary["ok"] is True


LONG_NAME = "Anna Maria Rossi, Ufficio Acquisti Integratori Alimentari " * 6
LONG_SENDERS = {
    "quoted": f'"{LONG_NAME}" <{ANNA}>',
    "encoded": "\r\n ".join(
        "=?utf-8?b?" + base64.b64encode(f"Àñña Rössì {i} ".encode()).decode() + "?="
        for i in range(20)
    )
    + f" <{ANNA}>",
}


@pytest.mark.parametrize("sender", LONG_SENDERS.values(), ids=LONG_SENDERS.keys())
def test_an_opt_out_with_a_long_display_name_is_filed_under_the_sender(
    tmp_path, sender
):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", OPT_OUT)])

    monitor.run_check()

    assert _suppressed(monitor.db) == [ANNA]
    assert _review_rows(monitor.db) == []


def test_of_two_senders_the_first_is_the_one_who_opted_out(tmp_path):
    monitor, _ = _monitor(
        tmp_path, [_raw(f"{ANNA}, bob@brand.it", "Re: hello", OPT_OUT)]
    )

    monitor.run_check()

    assert _suppressed(monitor.db) == [ANNA]


def test_the_scan_reports_the_sender_in_lower_case(tmp_path):
    monitor, _ = _monitor(
        tmp_path, [_raw("Anna Rossi <Anna@Brand.IT>", "Re: hello", OPT_OUT)]
    )

    assert [u["email"] for u in monitor.check_inbox()["unsubscribes"]] == [ANNA]


@pytest.mark.parametrize("body", [OPT_OUT, REQUEST, REPLY])
@pytest.mark.parametrize("sender", UNREADABLE)
def test_a_sender_that_cannot_be_read_is_kept_for_review(tmp_path, sender, body):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", body)])

    summary = monitor.run_check()

    rows = _review_rows(monitor.db)
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert "Re: hello" in rows[0]["details"] and "INBOX" in rows[0]["details"]
    # Filed under nothing rather than under a string that names no one.
    assert _suppressed(monitor.db) == []
    assert monitor.db.list_dsrs() == []
    # Not counted as handled either: nothing was done for anyone.
    assert (summary["unsubscribes"], summary["requests"], summary["replies"]) == (
        0,
        0,
        0,
    )
    assert summary["reply_entries"] == []
    # Kept for review is acted on: the read is complete.
    assert summary["ok"] is True


def test_a_sender_in_the_junk_folder_names_that_folder_for_review(tmp_path):
    fake = FolderIMAP(
        {"INBOX": [], "Junk Email": [_raw("Anna Rossi", "Re: hello", OPT_OUT)]},
        junk_flag="Junk Email",
    )
    monitor, db = _junk_monitor(tmp_path, fake)

    monitor.run_check()

    rows = _review_rows(db)
    assert len(rows) == 1
    assert "Junk Email" in rows[0]["details"]


def test_a_message_with_no_from_header_is_kept_for_review(tmp_path):
    raw = f"To: me@corp.com\r\nSubject: Re: hello\r\n\r\n{OPT_OUT}".encode()
    monitor, _ = _monitor(tmp_path, [raw])

    monitor.run_check()

    rows = _review_rows(monitor.db)
    assert len(rows) == 1
    assert "(no From)" in rows[0]["details"]


@pytest.mark.parametrize("sender", TOO_DEEP.values(), ids=TOO_DEEP.keys())
def test_a_from_header_the_parser_cannot_finish_does_not_stop_the_scan(
    tmp_path, sender
):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", OPT_OUT)])

    summary = monitor.run_check()

    assert summary["ok"] is True
    assert len(_review_rows(monitor.db)) == 1
    assert _suppressed(monitor.db) == []


@pytest.mark.parametrize(
    ("sender", "subject"),
    [("Anna Rossi", LONE_SURROGATE), (LONE_SURROGATE, "Re: hello")],
    ids=["in the subject", "in the sender"],
)
def test_a_review_row_is_written_whatever_the_headers_decode_to(
    tmp_path, sender, subject
):
    monitor, _ = _monitor(tmp_path, [_raw(sender, subject, OPT_OUT)])

    summary = monitor.run_check()

    assert summary["ok"] is True
    assert len(_review_rows(monitor.db)) == 1


def test_a_review_row_that_cannot_be_written_is_carried_by_the_log(
    tmp_path, monkeypatch, caplog
):
    monitor, _ = _monitor(tmp_path, [_raw("Anna Rossi", "Re: hello", OPT_OUT)])

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(monitor.db, "log_activity", locked)

    summary = monitor.run_check()

    assert summary["ok"] is True
    assert "Anna Rossi" in caplog.text
    assert "Re: hello" in caplog.text


@pytest.mark.parametrize(
    ("subject", "extra_headers"),
    [
        ("Automatic reply: hello", "Auto-Submitted: auto-replied\r\n"),
        ("Read: hello", ""),
    ],
    ids=["an out-of-office", "a read receipt"],
)
def test_mail_that_asks_for_nothing_is_not_sent_for_review(
    tmp_path, subject, extra_headers
):
    monitor, _ = _monitor(
        tmp_path,
        [
            _raw(
                "Anna Rossi",
                subject,
                "I am away until Monday.",
                extra_headers=extra_headers,
            )
        ],
    )

    monitor.run_check()

    assert _review_rows(monitor.db) == []


def test_a_bounce_from_a_daemon_with_no_address_is_still_a_bounce(tmp_path):
    raw = _dsn_raw("dead@brand.de", "failed", "5.1.1").replace(
        b"postmaster@outlook.com", b"MAILER-DAEMON"
    )
    monitor, _ = _monitor(tmp_path, [raw])
    sent_to(monitor.db, "dead@brand.de")

    monitor.run_check()

    assert _suppressed(monitor.db) == ["dead@brand.de"]
    assert _review_rows(monitor.db) == []
