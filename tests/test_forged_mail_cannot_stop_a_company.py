"""Mail that SUPPLECO's own mail server found forged cannot stop a company.

Anyone can write an email that looks as if it came from a prospect: "please
remove me", as anna.rossi@brand.de, sent from any server. The agent took
such an opt-out from someone it had emailed as the company's answer and
blocked the whole company; a made-up bounce report marked a working address
dead. Forging one email silenced a prospect's whole firm.

The receiving server records what it checked in an Authentication-Results
header. Only the TOP-MOST one is read: the receiving server adds its own
above everything the message arrived with, and a sender can write anything
below it. When it says dmarc=fail, spf=fail together with dkim=fail, or
compauth=fail (Microsoft 365's verdict); or when the From domain has no
DMARC policy (dmarc=none or no dmarc result) and neither SPF nor DKIM passed
for the From domain itself (a pass for the forger's own domain does not
count):

- an opt-out or data request still stops that one address (honouring a
  forged opt-out harms no one; ignoring a real one breaks GDPR Art. 21),
  but never the company;
- a bounce report marks nobody bounced;
- each is recorded (MAIL_UNAUTHENTICATED) for a person.

Without the header, nothing changes.
"""

import pytest

from modules import optout_spool
from modules.database import Database
from modules.imap_monitor import ImapMonitor
from tests.test_imap_inbox_scan import FakeIMAP

OBJECTOR = "anna.rossi@brand.de"
COLLEAGUE = "info@brand.de"
DEAD = "dead@brand.de"

FAIL_DMARC = (
    "Authentication-Results: spf=pass (sender IP is 203.0.113.9) "
    "smtp.mailfrom=attacker.example; dkim=none (message not signed) "
    "header.d=none;dmarc=fail action=none header.from=brand.de;compauth=fail "
    "reason=001\r\n"
)
FAIL_SPF_AND_DKIM = (
    "Authentication-Results: mx.corp.com; spf=fail smtp.mailfrom=brand.de; "
    "dkim=fail header.d=brand.de\r\n"
)
# A forger with a domain of their own, writing as a prospect whose domain
# publishes no DMARC record: SPF and DKIM pass, but for the forger's domain.
OWN_DOMAIN_NO_DMARC = (
    "Authentication-Results: mx.corp.com; spf=pass smtp.mailfrom=forger.example; "
    "dkim=pass header.d=forger.example; dmarc=none header.from=brand.de\r\n"
)
OWN_DOMAIN_COMPAUTH_FAIL = (
    "Authentication-Results: spf=pass (sender IP is 203.0.113.9) "
    "smtp.mailfrom=forger.example; dkim=pass (signature was verified) "
    "header.d=forger.example;dmarc=none action=none header.from=brand.de;"
    "compauth=fail reason=601\r\n"
)
# Genuine mail from a prospect without DMARC, sent through a mailing service
# (envelope on the service's domain) but signed with the prospect's own key.
GENUINE_NO_DMARC = (
    "Authentication-Results: mx.corp.com; spf=pass smtp.mailfrom=bounces.esp.example; "
    "dkim=pass header.d=brand.de; dmarc=none header.from=brand.de\r\n"
)
PASS = (
    "Authentication-Results: spf=pass (sender IP is 198.51.100.7) "
    "smtp.mailfrom=brand.de; dkim=pass (signature was verified) "
    "header.d=brand.de;dmarc=pass action=none header.from=brand.de;"
    "compauth=pass reason=100\r\n"
)


def _msg(extra_headers):
    import email

    return email.message_from_bytes(
        (f"{extra_headers}From: {OBJECTOR}\r\nSubject: hi\r\n\r\nbody\r\n").encode()
    )


def _verdict(extra_headers):
    m = ImapMonitor.__new__(ImapMonitor)
    return m._failed_authentication(_msg(extra_headers))


@pytest.mark.parametrize(
    "headers,forged",
    [
        ("", False),
        (PASS, False),
        (FAIL_DMARC, True),
        (FAIL_SPF_AND_DKIM, True),
        ("Authentication-Results: mx.google.com; dmarc=fail (p=REJECT) header.from=brand.de\r\n", True),
        ("Authentication-Results: mx.corp.com; DMARC=FAIL header.from=brand.de\r\n", True),
        ("Authentication-Results: mx.corp.com;\r\n\tdkim=fail header.d=brand.de;\r\n\tspf=fail\r\n", True),
        # One test failing is not a forgery: a forwarded message fails SPF
        # but keeps its DKIM signature.
        ("Authentication-Results: mx.corp.com; spf=fail smtp.mailfrom=brand.de; dkim=pass header.d=brand.de\r\n", False),
        ("Authentication-Results: mx.corp.com; spf=pass smtp.mailfrom=brand.de; dkim=fail header.d=brand.de\r\n", False),
        ("Authentication-Results: mx.corp.com; spf=fail; dkim=fail header.d=x; dkim=pass header.d=brand.de\r\n", False),
        ("Authentication-Results: mx.corp.com; dmarc=temperror\r\n", False),
        # No DMARC policy, and nothing passed for brand.de itself.
        ("Authentication-Results: mx.corp.com; spf=softfail; dkim=fail\r\n", True),
        ("Authentication-Results: mx.corp.com; spf=fail; dkim=none\r\n", True),
        ("Authentication-Results: mx.corp.com; dmarc=none\r\n", True),
        ("Authentication-Results: mx.corp.com; none\r\n", True),
        ("Authentication-Results: mx.corp.com; spf=pass; dkim=fail header.d=brand.de\r\n", True),
        # A verdict inside a comment, or as a property value, is not one.
        ("Authentication-Results: mx.corp.com; spf=pass (dmarc=fail would apply) smtp.mailfrom=brand.de\r\n", False),
        ("Authentication-Results: mx.corp.com; spf=pass (relay; dmarc=fail) smtp.mailfrom=brand.de\r\n", False),
        ("Authentication-Results: mx.corp.com; spf=pass (a (nested; dmarc=fail) note) smtp.mailfrom=brand.de\r\n", False),
        ("Authentication-Results: mx.corp.com; spf=pass smtp.mailfrom=dmarc=fail.example; dkim=pass header.d=brand.de\r\n", False),
    ],
)
def test_the_verdict_is_read_from_the_header(headers, forged):
    assert bool(_verdict(headers)) is forged


def test_only_the_top_most_header_counts():
    """The sender can add any Authentication-Results it likes, but only BELOW
    the receiving server's own, which is added on top."""
    sender_says_pass = PASS
    server_says_fail = FAIL_DMARC
    assert _verdict(server_says_fail + sender_says_pass) == "dmarc=fail"
    sender_says_fail = FAIL_DMARC
    server_says_pass = PASS
    assert _verdict(server_says_pass + sender_says_fail) == ""


def test_a_header_naming_supplecos_own_domain_lower_down_is_not_believed():
    planted = "Authentication-Results: corp.com; spf=pass; dkim=pass; dmarc=pass\r\n"
    assert _verdict(FAIL_DMARC + planted) == "dmarc=fail"


NO_ALIGNED_PASS = "no DMARC policy, and no SPF or DKIM pass for the From domain"
_AR = "Authentication-Results: mx.corp.com; "


@pytest.mark.parametrize(
    "headers,verdict",
    [
        (FAIL_SPF_AND_DKIM, "spf=fail and dkim=fail"),
        (_AR + "spf=fail; dkim=fail; dmarc=temperror\r\n", "spf=fail and dkim=fail"),
        (OWN_DOMAIN_COMPAUTH_FAIL, "compauth=fail"),
        # Microsoft's verdict counts even where the rest reads as a pass.
        (
            (
                "Authentication-Results: spf=pass smtp.mailfrom=brand.de; dkim=pass "
                "header.d=brand.de;dmarc=bestguesspass action=none header.from=brand.de;"
                "compauth=fail reason=000\r\n"
            ),
            "compauth=fail",
        ),
        (OWN_DOMAIN_NO_DMARC, NO_ALIGNED_PASS),
        (_AR + "spf=pass smtp.mailfrom=forger.example; dkim=pass header.d=forger.example\r\n", NO_ALIGNED_PASS),
        # Look-alikes of brand.de are other domains.
        (_AR + "dkim=pass header.d=brand.de.forger.example; dmarc=none\r\n", NO_ALIGNED_PASS),
        (_AR + "dkim=pass header.d=notbrand.de; dmarc=none\r\n", NO_ALIGNED_PASS),
        (_AR + "dkim=pass header.d=de; dmarc=none\r\n", NO_ALIGNED_PASS),
        (_AR + "spf=pass smtp.mailfrom=anna@brand.de.forger.example\r\n", NO_ALIGNED_PASS),
        # A domain named inside a comment, or by a result that did not pass,
        # is not a pass for it.
        (_AR + "spf=pass (smtp.mailfrom=brand.de) smtp.mailfrom=forger.example\r\n", NO_ALIGNED_PASS),
        (_AR + "dkim=fail header.d=brand.de; spf=none smtp.mailfrom=brand.de\r\n", NO_ALIGNED_PASS),
        (_AR + "dkim=pass header.s=s1; spf=pass\r\n", NO_ALIGNED_PASS),
        # An aligned pass from a domain without DMARC is genuine.
        (GENUINE_NO_DMARC, ""),
        (_AR + "dkim=pass header.d=brand.de\r\n", ""),
        (_AR + "dkim=pass header.d=BRAND.DE.; dmarc=none\r\n", ""),
        (_AR + 'dkim=pass header.d="brand.de"; dmarc=none\r\n', ""),
        (_AR + "dkim=pass header.d=mail.brand.de; dmarc=none\r\n", ""),
        (_AR + "dkim=pass header.i=@brand.de; dmarc=none\r\n", ""),
        (_AR + "spf=pass smtp.mailfrom=anna.rossi@brand.de; dkim=none; dmarc=none\r\n", ""),
        (_AR + "spf=pass smtp.mailfrom=bounce.brand.de; dmarc=none\r\n", ""),
        # A bounce report has an empty envelope sender; SPF then checks the
        # server's HELO name.
        (_AR + "spf=pass smtp.mailfrom=<> smtp.helo=mx.brand.de; dmarc=none\r\n", ""),
        (_AR + "spf=pass smtp.mailfrom= smtp.helo=mx.brand.de\r\n", ""),
        (_AR + "spf=pass smtp.mailfrom=<> smtp.helo=mx.forger.example\r\n", NO_ALIGNED_PASS),
        # With a DMARC verdict of pass, DMARC already checked the alignment.
        (PASS, ""),
        (_AR + "spf=pass smtp.mailfrom=esp.example; dmarc=pass header.from=brand.de\r\n", ""),
    ],
)
def test_without_dmarc_only_a_pass_for_the_from_domain_counts(headers, verdict):
    assert _verdict(headers) == verdict


# The envelope sender's local part is the forger's to choose, and a server
# that writes the whole address copies it into the header. In quotes it may
# hold ';', '(', ')' or '@' (RFC 5321).
@pytest.mark.parametrize(
    "headers,verdict",
    [
        (_AR + 'spf=pass smtp.mailfrom="x;dkim=pass header.d=brand.de;"@forger.example; dmarc=none\r\n', NO_ALIGNED_PASS),
        (_AR + 'spf=pass smtp.mailfrom="anna@brand.de"@forger.example; dmarc=none\r\n', NO_ALIGNED_PASS),
        (_AR + 'spf=pass smtp.mailfrom="\\"anna@brand.de\\"@forger.example"; dmarc=none\r\n', NO_ALIGNED_PASS),
        (
            _AR + 'spf=pass (google.com: domain of "a);dkim=pass header.d=brand.de;("@forger.example '
            "designates 203.0.113.9 as permitted sender) smtp.mailfrom=forger.example; dmarc=none\r\n",
            NO_ALIGNED_PASS,
        ),
        (_AR + 'spf=pass smtp.mailfrom="a;dmarc=pass"@forger.example\r\n', NO_ALIGNED_PASS),
        (_AR + 'spf=fail smtp.mailfrom="x;dmarc=pass;"@brand.de; dkim=pass header.d=brand.de; dmarc=fail\r\n', "dmarc=fail"),
        # A quoted local part of the prospect's own address is still theirs.
        (_AR + 'spf=pass smtp.mailfrom="anna rossi"@brand.de; dmarc=none\r\n', ""),
        (_AR + 'spf=pass smtp.mailfrom="anna;(x)"@brand.de; dmarc=none\r\n', ""),
        (_AR + 'spf=pass smtp.mailfrom="anna@brand.de"; dmarc=none\r\n', ""),
    ],
)
def test_a_quoted_sender_address_cannot_write_its_own_result(headers, verdict):
    assert _verdict(headers) == verdict


@pytest.mark.parametrize(
    "headers",
    [
        _AR + "dkim=pass header.d=brand.de (never closed; dmarc=fail\r\n",
        _AR + 'dkim=pass header.d=brand.de; spf=pass smtp.mailfrom="never closed\r\n',
    ],
)
def test_a_header_that_cannot_be_read_is_not_trusted(headers):
    assert _verdict(headers) == "the Authentication-Results header could not be read"


def test_a_message_with_no_from_address_has_nothing_to_align_with():
    import email

    m = ImapMonitor.__new__(ImapMonitor)
    msg = email.message_from_bytes(
        (_AR + "dkim=pass header.d=brand.de\r\nSubject: hi\r\n\r\nbody\r\n").encode()
    )
    assert m._failed_authentication(msg) == NO_ALIGNED_PASS


# ── What the scan does with a forged message ──────────────────────────────


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "f.db"))
    sid = database.log_search("q", source="test")
    for address in (OBJECTOR, COLLEAGUE, DEAD):
        cid = database.add_contact(sid, address, organization="Brand")
        database.log_email(cid, address, "Hello", delivery_status="sent")
    return database


def _scan(db, messages):
    m = ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw")
    fake = FakeIMAP(messages)
    m._connect = lambda: fake
    return m.run_check()


def _opt_out(auth="", body="Please remove me from your list."):
    return (
        f"{auth}From: Anna Rossi <{OBJECTOR}>\r\nTo: me@corp.com\r\n"
        f"Subject: Re: hello\r\nDate: Mon, 1 Jun 2026 10:00:00 +0200\r\n\r\n{body}\r\n"
    ).encode()


def _bounce(auth=""):
    return (
        f"{auth}From: postmaster@brand.de\r\nTo: me@corp.com\r\n"
        "Subject: Undeliverable: hello\r\n"
        "Content-Type: multipart/report; report-type=delivery-status; "
        'boundary="B"\r\n\r\n'
        "--B\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
        "--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; mx.brand.de\r\n\r\n"
        f"Final-Recipient: rfc822; {DEAD}\r\nAction: failed\r\nStatus: 5.1.1\r\n\r\n"
        "--B--\r\n"
    ).encode()


def _flagged(db):
    return db.get_activity_since("MAIL_UNAUTHENTICATED", "2000-01-01 00:00:00")


@pytest.mark.parametrize(
    "auth",
    [FAIL_DMARC, FAIL_SPF_AND_DKIM, OWN_DOMAIN_NO_DMARC, OWN_DOMAIN_COMPAUTH_FAIL],
)
def test_a_forged_opt_out_stops_the_address_but_not_the_company(db, auth):
    summary = _scan(db, [_opt_out(auth)])

    assert summary["ok"] is True
    assert db.is_suppressed(OBJECTOR), "the opt-out itself is always honoured"
    assert db.get_contact_by_email(OBJECTOR)["status"] == "unsubscribed"
    assert not db.is_blocked(COLLEAGUE), "a forged opt-out never stops the company"
    (row,) = _flagged(db)
    assert OBJECTOR in row["details"]


@pytest.mark.parametrize("auth", ["", PASS, GENUINE_NO_DMARC])
def test_an_authentic_or_unchecked_opt_out_still_stops_the_company(db, auth):
    _scan(db, [_opt_out(auth)])
    assert db.is_suppressed(OBJECTOR)
    assert db.is_blocked(COLLEAGUE)
    assert _flagged(db) == []


@pytest.mark.parametrize("auth", [FAIL_DMARC, OWN_DOMAIN_NO_DMARC, OWN_DOMAIN_COMPAUTH_FAIL])
def test_a_forged_bounce_marks_nobody_bounced(db, auth):
    summary = _scan(db, [_bounce(auth)])

    assert summary["ok"] is True
    assert not db.is_suppressed(DEAD)
    assert db.get_contact_by_email(DEAD)["status"] != "bounced"
    (row,) = _flagged(db)
    assert DEAD in row["details"]


@pytest.mark.parametrize("auth", ["", PASS, GENUINE_NO_DMARC])
def test_an_authentic_or_unchecked_bounce_still_marks_the_address(db, auth):
    _scan(db, [_bounce(auth)])
    assert db.is_suppressed(DEAD)
    assert db.get_contact_by_email(DEAD)["status"] == "bounced"


def test_a_forged_data_request_is_registered_for_the_address_only(db):
    _scan(db, [_opt_out(FAIL_DMARC, body="Vi chiedo di cancellare tutti i miei dati personali.")])

    assert db.is_suppressed(OBJECTOR)
    assert [r["kind"] for r in db.list_dsrs()] == ["erasure"]
    assert not db.is_blocked(COLLEAGUE)
    (row,) = _flagged(db)
    assert "check who sent it" in row["details"]


def test_an_unchecked_data_request_still_stops_the_company(db):
    _scan(db, [_opt_out(body="Vi chiedo di cancellare tutti i miei dati personali.")])
    assert db.is_blocked(COLLEAGUE)


def test_a_forged_opt_out_held_by_a_locked_database_stays_one_address(db, monkeypatch):
    import sqlite3

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    m = ImapMonitor.__new__(ImapMonitor)
    m.db = db
    with monkeypatch.context() as mp:
        mp.setattr(db, "record_opt_out", locked)
        m.process_unsubscribes(
            [{"email": OBJECTOR, "from": OBJECTOR, "unauthenticated": "dmarc=fail"}]
        )
    optout_spool.replay_optout_spool(db)
    assert db.is_suppressed(OBJECTOR)
    assert not db.is_blocked(COLLEAGUE)


def test_a_forged_data_request_held_by_a_locked_database_stays_one_address(
    db, monkeypatch
):
    import sqlite3

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    m = ImapMonitor.__new__(ImapMonitor)
    m.db = db
    with monkeypatch.context() as mp:
        mp.setattr(db, "record_opt_out", locked)
        m.process_requests(
            [
                {
                    "email": OBJECTOR,
                    "from": OBJECTOR,
                    "request_kind": "erasure",
                    "source_ref": "<x@brand.de>",
                    "unauthenticated": "dmarc=fail",
                }
            ]
        )
    optout_spool.replay_optout_spool(db)
    assert db.is_suppressed(OBJECTOR)
    assert not db.is_blocked(COLLEAGUE)
