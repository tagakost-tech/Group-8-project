"""Direct tests for ImapMonitor.check_inbox - the end-to-end scan.

check_inbox is the only path that turns inbox mail into suppressions, hot-lead
alerts and 'responded' marks, yet every previous test exercised its helpers in
isolation. That left the assembly untested: the classification could be right
while the wiring silently dropped a bounce. These tests drive the whole method
through a fake IMAP server so the wiring itself is covered.
"""

import re
from datetime import datetime, timedelta, timezone

from conftest import sent_to
from modules import clock
from modules.database import Database
from modules.imap_monitor import ImapMonitor


class FakeIMAP:
    """Minimal stand-in for imaplib.IMAP4_SSL: enough for check_inbox."""

    def __init__(self, messages, select_status="OK", search_status="OK"):
        self.messages = messages  # list[bytes] of raw RFC822 mail
        self.select_status = select_status
        self.search_status = search_status
        self.searched = []
        self.logged_out = False

    def list(self, *args):
        # INBOX alone: the junk folder is tests/test_the_junk_folder_is_read_too.py's.
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def select(self, folder, readonly=False):
        return self.select_status, [b"1"]

    def search(self, charset, criteria):
        self.searched.append(criteria)
        if self.search_status != "OK":
            return self.search_status, [b""]
        ids = b" ".join(str(i + 1).encode() for i in range(len(self.messages)))
        return "OK", [ids]

    def fetch(self, msg_id, parts):
        idx = int(msg_id) - 1
        return "OK", [(b"1 (RFC822 {})", self.messages[idx])]

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


def _monitor(tmp_path, messages, **kw):
    m = ImapMonitor(
        Database(str(tmp_path / "i.db")), "imap.test", 993, "me@corp.com", "pw"
    )
    fake = FakeIMAP(messages, **kw)
    m._connect = lambda: fake
    return m, fake


def _raw(frm, subject, body, extra_headers="", to="me@corp.com"):
    return (
        f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\n"
        f"Date: Mon, 1 Jun 2026 10:00:00 +0200\r\n{extra_headers}\r\n{body}"
    ).encode()


def _dsn_raw(recipient, action, status, subject="Delivery Status Notification"):
    return _multi_dsn_raw([(recipient, action, status)], subject=subject)


def _multi_dsn_raw(groups, subject="Delivery Status Notification"):
    """A DSN carrying one per-recipient block per entry in `groups`.

    Real reports look like this whenever the address we mailed expands to more
    than one mailbox (a distribution group, an alias with several members), and
    they carry a Reporting-MTA preamble before the per-recipient blocks.
    """
    blocks = "".join(
        f"Final-Recipient: rfc822; {rcpt}\r\nAction: {action}\r\n"
        f"Status: {status}\r\n\r\n"
        for rcpt, action, status in groups
    )
    return (
        f"From: postmaster@outlook.com\r\nTo: me@corp.com\r\n"
        f"Subject: {subject}\r\n"
        f"Content-Type: multipart/report; report-type=delivery-status; "
        f'boundary="B"\r\n\r\n'
        f"--B\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
        f"--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        f"Reporting-MTA: dns; mail.outlook.com\r\n\r\n"
        f"{blocks}--B--\r\n"
    ).encode()


# ── The scan turns mail into the right buckets ────────────────────────────


def test_hard_bounce_is_collected_with_the_dsn_recipient(tmp_path):
    m, _ = _monitor(tmp_path, [_dsn_raw("dead@brand.de", "failed", "5.1.1")])
    res = m.check_inbox()
    assert res["ok"] is True
    assert [b["email"] for b in res["bounces"]] == ["dead@brand.de"]
    assert res["replies"] == [] and res["unsubscribes"] == []


def test_delay_dsn_is_not_a_bounce(tmp_path):
    """The regression that once suppressed live prospects: M365 sends a
    'Delivery Status Notification (Delay)' while the message is still queued."""
    m, _ = _monitor(
        tmp_path,
        [
            _dsn_raw(
                "slow@brand.de",
                "delayed",
                "4.4.7",
                subject="Delivery Status Notification (Delay)",
            )
        ],
    )
    res = m.check_inbox()
    assert res["bounces"] == []
    assert res["replies"] == []  # not a reply either - it is machine noise


def test_soft_bounce_is_dropped_not_suppressed(tmp_path):
    # A genuine soft bounce is failed + 4.x.x: the MTA gave up, but on a
    # TRANSIENT condition. ('delayed' + 4.x.x is still queued - that is
    # dsn_info, covered by test_delay_dsn_is_not_a_bounce above.)
    m, _ = _monitor(tmp_path, [_dsn_raw("full@brand.de", "failed", "4.2.2")])
    res = m.check_inbox()
    assert res["bounces"] == []
    # A full run must leave the address contactable: 4.2.2 is a full mailbox,
    # which clears. (Feeding process_bounces the empty list asserted above
    # would prove nothing - the loop body would never run.)
    m.run_check()
    assert m.db.is_suppressed("full@brand.de") is False


# ── Multi-recipient DSNs: one report, several verdicts ────────────────────


def test_hard_failure_wins_over_a_delivered_sibling(tmp_path):
    """The address we mailed expanded to a distribution group: member 1 was
    delivered, member 2 does not exist. Reading the first block found would
    call the whole report 'delivered' and drop a hard bounce - the bounce rate
    then stays artificially low and the circuit breaker can never fire."""
    m, _ = _monitor(
        tmp_path,
        [
            _multi_dsn_raw(
                [
                    ("ok.member@brand.de", "delivered", "2.0.0"),
                    ("dead.member@brand.de", "failed", "5.1.1"),
                ]
            )
        ],
    )
    res = m.check_inbox()
    assert [b["email"] for b in res["bounces"]] == ["dead.member@brand.de"]


def test_block_order_does_not_change_the_verdict(tmp_path):
    """Same report, failing block first. Must give the same answer, or the
    test above would be passing on positional luck rather than on ranking."""
    m, _ = _monitor(
        tmp_path,
        [
            _multi_dsn_raw(
                [
                    ("dead.member@brand.de", "failed", "5.1.1"),
                    ("ok.member@brand.de", "delivered", "2.0.0"),
                ]
            )
        ],
    )
    res = m.check_inbox()
    assert [b["email"] for b in res["bounces"]] == ["dead.member@brand.de"]


def test_suppression_targets_the_failed_member_not_the_delivered_one(tmp_path):
    """The inverse risk, and the worse one: pairing the failure verdict with a
    sibling's address permanently suppresses a prospect who received the mail."""
    m, _ = _monitor(
        tmp_path,
        [
            _multi_dsn_raw(
                [
                    ("ok.member@brand.de", "delivered", "2.0.0"),
                    ("dead.member@brand.de", "failed", "5.1.1"),
                ]
            )
        ],
    )
    sent_to(m.db, "ok.member@brand.de")
    sent_to(m.db, "dead.member@brand.de")
    m.run_check()
    assert m.db.is_suppressed("dead.member@brand.de") is True
    assert m.db.is_suppressed("ok.member@brand.de") is False


def test_all_members_delivered_is_not_a_bounce(tmp_path):
    m, _ = _monitor(
        tmp_path,
        [
            _multi_dsn_raw(
                [
                    ("a@brand.de", "delivered", "2.0.0"),
                    ("b@brand.de", "delivered", "2.0.0"),
                ]
            )
        ],
    )
    res = m.check_inbox()
    assert res["bounces"] == [] and res["replies"] == []


def test_transient_sibling_does_not_downgrade_a_hard_failure(tmp_path):
    """A 4.x.x member is still in flight; a 5.x.x member is dead. The dead one
    must still be suppressed rather than deferred behind the retry."""
    m, _ = _monitor(
        tmp_path,
        [
            _multi_dsn_raw(
                [
                    ("slow@brand.de", "delayed", "4.4.7"),
                    ("dead@brand.de", "failed", "5.1.1"),
                ]
            )
        ],
    )
    res = m.check_inbox()
    assert [b["email"] for b in res["bounces"]] == ["dead@brand.de"]


def test_unsubscribe_request_is_collected(tmp_path):
    m, _ = _monitor(
        tmp_path,
        [_raw("buyer@brand.de", "Re: Quick question", "Please unsubscribe me.")],
    )
    res = m.check_inbox()
    assert [u["email"] for u in res["unsubscribes"]] == ["buyer@brand.de"]
    assert res["replies"] == []


def test_real_reply_is_collected_with_its_body(tmp_path):
    m, _ = _monitor(
        tmp_path,
        [_raw("buyer@brand.de", "Re: Quick question", "Yes, send me a quote.")],
    )
    res = m.check_inbox()
    assert len(res["replies"]) == 1
    # The body must survive: hot-lead intent is judged from it.
    assert "quote" in res["replies"][0]["body"]


def test_out_of_office_is_not_a_reply(tmp_path):
    """An auto-responder must not mark the contact 'responded' - that would
    cancel the follow-up sequence for someone who never read the mail."""
    m, _ = _monitor(
        tmp_path,
        [
            _raw(
                "buyer@brand.de",
                "Automatic reply: Out of office",
                "I am away until Monday.",
                extra_headers="Auto-Submitted: auto-replied\r\n",
            )
        ],
    )
    res = m.check_inbox()
    assert res["replies"] == [] and res["bounces"] == []


def test_unparseable_bounce_does_not_suppress_the_daemon(tmp_path):
    """Without a Final-Recipient we must NOT fall back to the From address -
    suppressing 'mailer-daemon@' would be pointless and suppressing a prospect
    would be permanent."""
    m, _ = _monitor(
        tmp_path,
        [
            _raw(
                "mailer-daemon@outlook.com",
                "Undeliverable: Quick question",
                "Your message could not be delivered.",
            )
        ],
    )
    res = m.check_inbox()
    assert res["bounces"] == []
    assert res["ok"] is True


def test_mixed_inbox_is_split_correctly(tmp_path):
    m, _ = _monitor(
        tmp_path,
        [
            _dsn_raw("dead@brand.de", "failed", "5.1.1"),
            _raw("a@brand.de", "Re: Hi", "Unsubscribe please"),
            _raw("b@brand.de", "Re: Hi", "Sounds interesting, call me"),
            _dsn_raw("slow@brand.de", "delayed", "4.4.7"),
        ],
    )
    res = m.check_inbox()
    assert len(res["bounces"]) == 1
    assert len(res["unsubscribes"]) == 1
    assert len(res["replies"]) == 1


# ── Health flags: silence from a broken mailbox is not an empty inbox ──────


def test_connection_failure_is_reported_not_silent(tmp_path):
    m, _ = _monitor(tmp_path, [])
    m._connect = lambda: None
    res = m.check_inbox()
    assert res["ok"] is False and res["error"]


def test_folder_select_failure_is_reported(tmp_path):
    m, _ = _monitor(tmp_path, [], select_status="NO")
    res = m.check_inbox()
    assert res["ok"] is False and "folder" in res["error"]


def test_search_failure_is_reported(tmp_path):
    m, _ = _monitor(tmp_path, [], search_status="NO")
    res = m.check_inbox()
    assert res["ok"] is False and res["error"] == "search failed"


def test_genuinely_empty_inbox_is_ok(tmp_path):
    m, fake = _monitor(tmp_path, [])
    res = m.check_inbox()
    assert res["ok"] is True and res["error"] is None
    assert fake.logged_out is True


def test_one_broken_message_does_not_abort_the_scan(tmp_path):
    """A single unparseable message must not cost us the rest of the mailbox -
    but the pass is no longer COMPLETE, so the watermark must not advance past
    a message that might have been a bounce or an opt-out."""
    m, fake = _monitor(
        tmp_path,
        [b"", _dsn_raw("dead@brand.de", "failed", "5.1.1")],
    )
    original = fake.fetch

    def flaky(msg_id, parts):
        if msg_id == b"1":
            raise ValueError("corrupt")
        return original(msg_id, parts)

    fake.fetch = flaky
    res = m.check_inbox()
    assert [b["email"] for b in res["bounces"]] == ["dead@brand.de"]  # kept going
    assert res["ok"] is False and res["skipped"] == 1  # but not clean


def test_a_dead_connection_mid_scan_is_not_a_complete_pass(tmp_path):
    """The failure that made the watermark dangerous: IMAP4.abort leaves the
    connection unusable, so every remaining fetch fails too. Swallowing them
    walked the rest of the mailbox and then declared the scan complete -
    stamping a watermark over mail that was never read, which put those
    bounces and opt-outs permanently out of reach."""
    import imaplib

    msgs = [_dsn_raw(f"dead{i}@brand.de", "failed", "5.1.1") for i in range(4)]
    m, fake = _monitor(tmp_path, msgs)
    original = fake.fetch
    attempted = []

    def dying(msg_id, parts):
        attempted.append(msg_id)
        if int(msg_id) >= 2:
            raise imaplib.IMAP4.abort("socket error: EOF")
        return original(msg_id, parts)

    fake.fetch = dying
    summary = m.run_check()

    assert summary["ok"] is False
    assert summary["error"]
    # The watermark must NOT move: the next scan has to see the lost messages.
    assert m.db.get_state(m._watermark_key) is None
    # And it must stop AT the failure rather than walking the rest of a dead
    # connection, asking for two more messages it can never receive. (Each
    # message takes two fetches now: its size, then the message.)
    assert set(attempted) == {b"1", b"2"}


def test_a_fetch_the_server_refuses_is_not_a_complete_pass(tmp_path):
    """NO on fetch means the server REFUSED to serve the message (RFC 3501
    "can't fetch that data") - it still exists and may be an unread bounce.
    This test previously asserted the opposite (ok=True) under the name
    "expunged", locking the loss in: the scan looked healthy, no IMAP_ERROR
    row was written, and the staleness alarm was refreshed."""
    m, fake = _monitor(tmp_path, [_dsn_raw("dead@brand.de", "failed", "5.1.1")])

    def refused(msg_id, parts):
        return "NO", [b"FETCH error"]

    fake.fetch = refused
    res = m.check_inbox()
    assert res["ok"] is False and res["skipped"] == 1
    # And the watermark holds, so the next scan sees the message again.
    m.run_check()
    assert m.db.get_state(m._watermark_key) is None


def test_a_genuinely_expunged_message_is_also_not_a_complete_pass(tmp_path):
    """What expunged ACTUALLY looks like on the wire: imaplib returns
    ('OK', [None]) when the untagged FETCH response never arrived. The raw
    payload is None, which must count as skipped, not crash the scan."""
    m, fake = _monitor(tmp_path, [_dsn_raw("dead@brand.de", "failed", "5.1.1")])

    def expunged(msg_id, parts):
        return "OK", [None]

    fake.fetch = expunged
    res = m.check_inbox()
    assert res["ok"] is False and res["skipped"] == 1


# ── Watermark: an outage must not create a permanent blind spot ───────────


def _searched_cutoff(fake):
    """Parse the SINCE date the monitor actually sent, WITHOUT strftime/%b.

    Parsing with the same locale-dependent %b that produced the string would
    round-trip happily even if the monitor emitted 'giu' instead of 'Jun' -
    which a real IMAP server rejects outright (RFC 3501 requires English).
    """
    criteria = fake.searched[0]
    assert re.fullmatch(r'\(SINCE "\d{2}-[A-Z][a-z]{2}-\d{4}"\)', criteria), criteria
    day, mon, year = criteria.split('"')[1].split("-")
    months = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ]
    assert mon in months, f"non-English month in IMAP date: {mon}"
    # An IMAP date header carries no zone, so the parsed value is naive.
    return datetime(int(year), months.index(mon) + 1, int(day))  # noqa: DTZ001


def test_window_widens_after_a_gap(tmp_path):
    """A weekend of downtime would otherwise lose every bounce and opt-out
    older than 24h - and IMAP is the only source of either."""
    m, fake = _monitor(tmp_path, [])
    m.db.set_state(m._watermark_key, (clock.now() - timedelta(days=4)).isoformat())
    m.check_inbox()
    cutoff = _searched_cutoff(fake)
    assert (clock.now().replace(tzinfo=None) - cutoff).days >= 4


def test_window_is_capped_for_an_ancient_watermark(tmp_path):
    m, fake = _monitor(tmp_path, [])
    m.db.set_state(m._watermark_key, (clock.now() - timedelta(days=400)).isoformat())
    m.check_inbox()
    cutoff = _searched_cutoff(fake)
    gap_days = (clock.now().replace(tzinfo=None) - cutoff).days
    assert gap_days <= m.MAX_LOOKBACK_HOURS // 24 + 1


def test_corrupt_watermark_falls_back_to_the_default_window(tmp_path):
    m, fake = _monitor(tmp_path, [])
    m.db.set_state(m._watermark_key, "not-a-date")
    m.check_inbox()
    cutoff = _searched_cutoff(fake)
    assert (clock.now().replace(tzinfo=None) - cutoff).days <= 2


def test_watermark_advances_only_after_a_complete_run(tmp_path):
    m, _ = _monitor(tmp_path, [_raw("b@brand.de", "Re: Hi", "thanks")])
    assert m.db.get_state(m._watermark_key) is None
    # In UTC: in the hour repeated when the clocks go back, adding a second
    # to a local time drops its fold and moves it back an hour.
    before = clock.now().astimezone(timezone.utc)
    summary = m.run_check()
    stored = m.db.get_state(m._watermark_key)
    assert stored is not None
    # Must be the moment the scan STARTED, not when it finished - mail that
    # arrives mid-pass has to fall inside the next window, not through a gap.
    assert datetime.fromisoformat(stored) <= before + timedelta(seconds=1)
    assert summary["ok"] is True


def test_failed_scan_leaves_the_watermark_untouched(tmp_path):
    """Otherwise a broken scan would 'consume' the window it never read."""
    m, _ = _monitor(tmp_path, [], search_status="NO")
    m.db.set_state(m._watermark_key, "2026-06-01T10:00:00")
    m.run_check()
    assert m.db.get_state(m._watermark_key) == "2026-06-01T10:00:00"


def test_permanent_status_without_an_action_line_still_wins(tmp_path):
    """Not every MTA emits 'Action:'. A 5.x.x block scored equal to 'delivered'
    left the winner decided by block order - dropping the bounce AND suppressing
    the delivered sibling instead."""
    m, _ = _monitor(
        tmp_path,
        [
            _multi_dsn_raw(
                [
                    ("ok.member@brand.de", "delivered", "2.0.0"),
                    ("gone.member@brand.de", None, "5.1.1"),
                ]
            )
        ],
    )
    sent_to(m.db, "ok.member@brand.de")
    sent_to(m.db, "gone.member@brand.de")
    m.run_check()
    assert m.db.is_suppressed("gone.member@brand.de") is True
    assert m.db.is_suppressed("ok.member@brand.de") is False


def test_imap_since_date_is_locale_independent(tmp_path):
    """RFC 3501 requires English month abbreviations. strftime('%b') follows
    the process locale - on an Italian host (where this deploys) it emits
    'giu', the server answers BAD, and inbox monitoring stops dead."""
    import locale

    try:
        locale.setlocale(locale.LC_TIME, "it_IT")
    except locale.Error:
        locale.setlocale(locale.LC_TIME, "")
    try:
        m, fake = _monitor(tmp_path, [])
        m.check_inbox()
        _searched_cutoff(fake)  # asserts the English DD-Mon-YYYY shape
    finally:
        locale.setlocale(locale.LC_TIME, "C")


def test_bracketed_final_recipient_is_parsed(tmp_path):
    """Plenty of MTAs write '<dead@brand.de>'. Failing to parse it emptied the
    verdict's recipient, and suppression then fell back to scanning the whole
    message - picking the FIRST address anywhere, which in a multi-recipient
    report is the delivered sibling."""
    raw = (
        b"From: postmaster@outlook.com\r\nTo: me@corp.com\r\n"
        b"Subject: Undeliverable\r\n"
        b'Content-Type: multipart/report; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: message/delivery-status\r\n\r\n"
        b"Final-Recipient: rfc822; ok.member@brand.de\r\n"
        b"Action: delivered\r\nStatus: 2.0.0\r\n\r\n"
        b"Final-Recipient: rfc822; <dead.member@brand.de>\r\n"
        b"Action: failed\r\nStatus: 5.1.1\r\n\r\n--B--\r\n"
    )
    m, _ = _monitor(tmp_path, [raw])
    sent_to(m.db, "ok.member@brand.de")
    sent_to(m.db, "dead.member@brand.de")
    m.run_check()
    assert m.db.is_suppressed("dead.member@brand.de") is True
    assert m.db.is_suppressed("ok.member@brand.de") is False


def test_watermark_read_failure_is_announced(tmp_path):
    """Falling back to a 24h window is the safe behaviour; doing it silently is
    not - a broken watermark looks exactly like a first-ever run."""
    m, _ = _monitor(tmp_path, [])

    def boom(_key, default=None):
        raise RuntimeError("database is locked")

    m.db.get_state = boom
    for _ in range(3):
        assert m._lookback_hours(24) == 24  # safe fallback

    rows = [r for r in m.db.get_recent_activity(20) if r["action"] == "WATERMARK_ERROR"]
    assert rows, "a persistently unreadable watermark must reach the dashboard"
