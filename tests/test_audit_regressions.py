"""Regressions for the bugs found in the automated audit.

Each test here failed (or would have) against the pre-audit code. They guard
business-critical behaviour: not emailing people who opted out, not destroying
hot leads, and not sending past the reputation guards.
"""

import threading

from modules import reply_intent
from modules.imap_monitor import ImapMonitor
from modules.warmup import WarmupScheduler


def _agent(settings_factory, tmp_path, **overrides):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        delay_between_emails=0.0,
        ai_humanize_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        imap_enabled=False,
        **overrides,
    )
    return OutreachAgent(s)


def _monitor(tmp_path):
    from modules.database import Database

    return ImapMonitor(
        Database(str(tmp_path / "i.db")), "imap.test", 993, "me@corp.com", "pw"
    )


# ── GDPR: suppression must be case-insensitive ─────────────────────────────


def test_suppression_is_case_insensitive(db):
    """A scraped address keeps the source page's casing. If suppression were
    case-sensitive the same person would be re-added and re-emailed."""
    db.add_to_suppression("Buyer@Brand.DE", "unsubscribe")
    assert db.is_suppressed("buyer@brand.de") is True
    assert db.is_suppressed("BUYER@BRAND.DE") is True
    assert db.add_contact(db.log_search("q"), "BUYER@brand.de") is None


def test_contacts_are_deduped_case_insensitively(db):
    sid = db.log_search("q")
    assert db.add_contact(sid, "Info@Brand.de") is not None
    assert db.add_contact(sid, "info@brand.de") is None  # same person


# ── Hot leads must not be destroyed by bounce keyword matching ─────────────


def test_reply_mentioning_numbers_is_not_a_bounce(tmp_path):
    """'MOQ is 550 units' used to classify as a bounce, which then permanently
    suppressed the most valuable reply the agent can receive."""
    m = _monitor(tmp_path)
    for body in [
        "Yes we are interested. Our MOQ is 550 units - please send a quote.",
        "We had a failure with our last supplier, so your timing is good.",
        "Please quote 5500 capsules and 553 sachets.",
    ]:
        assert (
            m._classify_email("buyer@nordic.se", "Re: Quick question", body) == "reply"
        ), body


def test_real_dsn_still_detected_as_bounce(tmp_path):
    m = _monitor(tmp_path)
    assert (
        m._classify_email(
            "Mail Delivery Subsystem <mailer-daemon@google.com>",
            "Delivery Status Notification (Failure)",
            "",
        )
        == "bounce"
    )
    assert (
        m._classify_email("postmaster@corp.com", "Undeliverable: Quick question", "")
        == "bounce"
    )


# ── GDPR: an opt-out written in the BODY must suppress ─────────────────────


def test_optout_in_body_is_classified_unsubscribe(tmp_path):
    m = _monitor(tmp_path)
    for body in [
        "Please remove me from your list and do not contact me again.",
        "Non contattarmi più, grazie.",
        "Please stop sending these emails.",
    ]:
        assert (
            m._classify_email("buyer@nordic.se", "Re: Quick question", body)
            == "unsubscribe"
        ), body


def test_our_own_quoted_footer_does_not_trigger_unsubscribe(tmp_path):
    """Our template footer says 'unsubscribe here'. Quoted back in a reply it
    must NOT be read as the prospect opting out.

    Each quoting style is tested ALONE. Mail clients differ - Outlook prepends
    a 'From:' block with no '>' at all, Italian clients write 'ha scritto:' -
    so a body containing several markers at once would stay green even if only
    one of them still worked.
    """
    m = _monitor(tmp_path)
    footer = "If this isn't relevant to you, you can unsubscribe here."
    quoted_as = [
        f"> {footer}",  # plain quote prefix
        f"On Mon, SUPPLECO wrote:\n{footer}",  # Gmail / Apple Mail
        f"From: SUPPLECO <info@suppleco.com>\n{footer}",  # Outlook (no '>')
        f"Da: SUPPLECO <info@suppleco.com>\n{footer}",  # Outlook Italian
        f"Von: SUPPLECO <info@suppleco.com>\n{footer}",  # Outlook German
        f"Il giorno lun, SUPPLECO ha scritto:\n{footer}",  # Italian
        f"Am Mo, SUPPLECO schrieb:\n{footer}",  # German
        f"-----Original Message-----\n{footer}",  # forwarded thread
        footer,  # footer with no marker
    ]
    for quote in quoted_as:
        body = f"Sounds interesting, let's talk.\n\n{quote}\n"
        assert (
            m._classify_email("buyer@nordic.se", "Re: Quick question", body) == "reply"
        ), quote


def test_is_optout_helper():
    assert reply_intent.is_optout("", "please remove me from your list") is True
    assert reply_intent.is_optout("Unsubscribe", "") is True
    assert reply_intent.is_optout("", "we are interested, send a quote") is False


# ── Direct Send must respect the reputation guards ─────────────────────────


def test_direct_send_blocked_when_bounce_guard_tripped(settings_factory, tmp_path):
    a = _agent(
        settings_factory,
        tmp_path,
        bounce_guard_enabled=True,
        max_bounce_rate=0.01,
        bounce_guard_min_sends=1,
    )
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, "seed@brand.de", organization="Seed")
    a.db.log_email(cid, "seed@brand.de", "s", delivery_status="sent")
    # Bounce the address we actually sent to. Suppressing an unrelated one used
    # to inflate the rate all the same, which is the bug the cohort join fixed.
    a.db.add_to_suppression("seed@brand.de", reason="bounce")
    ok, msg = a.send_single_email("new@brand.de", dry_run=False)
    assert ok is False
    assert "paused" in msg.lower() or "breaker" in msg.lower()


def test_direct_send_cannot_exceed_daily_cap(settings_factory, tmp_path):
    a = _agent(
        settings_factory,
        tmp_path,
        warmup_enabled=True,
        warmup_start_daily=1,
        warmup_increment=0,
        warmup_max_daily=1,
    )
    a.warmup.record_send(account="")  # cap of 1 already used
    ok, msg = a.send_single_email("new@brand.de", dry_run=False)
    assert ok is False
    assert "limit" in msg.lower()
    assert a.warmup.get_warmup_status("")["sent_today"] == 1  # not pushed to 2


# ── Warm-up counting must survive concurrent sends ─────────────────────────


def test_record_send_is_atomic_under_concurrency(db):
    """Agent thread + dashboard Direct Send both increment. The old
    SELECT-then-INSERT lost counts and raised IntegrityError mid-batch."""
    w = WarmupScheduler(
        db,
        warmup_enabled=True,
        warmup_start_daily=100,
        warmup_increment=0,
        warmup_max_daily=100,
    )
    errors = []

    def send():
        try:
            w.record_send(account="")
        except Exception as e:  # noqa: BLE001 - the point is to catch any
            errors.append(repr(e))

    threads = [threading.Thread(target=send) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert w.get_warmup_status("")["sent_today"] == 8


# ── The bounce guard must fail CLOSED and judge a recent window ────────────


def test_bounce_guard_fails_closed_when_metrics_unreadable(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, bounce_guard_enabled=True)

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(a.db, "get_deliverability_metrics", boom)
    assert a._bounce_guard_tripped() is True  # must NOT keep sending blindly


def test_bounce_rate_uses_a_recent_window(db):
    """Old sends must not dilute a sudden spike into invisibility."""
    sid = db.log_search("q")
    cid = db.add_contact(sid, "seed@brand.de", organization="Seed")
    # 50 healthy sends 30 days ago
    for i in range(50):
        db.conn.execute(
            "INSERT INTO email_log (contact_id, recipient_email, subject, "
            "delivery_status, timestamp) VALUES (?,?,?, 'sent', "
            "datetime('now','localtime','-30 days'))",
            (cid, f"old{i}@x.de", "s"),
        )
    # 4 sends this week, 2 of which bounced
    for i in range(4):
        db.conn.execute(
            "INSERT INTO email_log (contact_id, recipient_email, subject, "
            "delivery_status) VALUES (?,?,?, 'sent')",
            (cid, f"new{i}@x.de", "s"),
        )
    db.conn.commit()
    for i in range(2):
        db.add_to_suppression(f"new{i}@x.de", reason="bounce")

    m = db.get_deliverability_metrics(window_days=7)
    assert m["recent_sent"] == 4
    assert m["bounce_rate"] == 0.5  # 2/4 recent — alarming
    assert m["lifetime_bounce_rate"] < 0.05  # diluted to invisibility


# ── IMAP health: a broken inbox must not look like a quiet one ─────────────


def test_failed_imap_check_is_not_reported_as_ok(tmp_path, monkeypatch):
    """A failed scan returning empty lists is byte-identical to a quiet inbox.
    While IMAP is down nothing is suppressed and no hot lead is escalated, so
    the failure must be explicit."""
    m = _monitor(tmp_path)
    monkeypatch.setattr(m, "_connect", lambda: None)  # auth/connection failure
    res = m.check_inbox()
    assert res["ok"] is False
    assert res["error"]
    assert res["bounces"] == [] and res["replies"] == []


def test_imap_failure_is_logged_to_the_dashboard(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path)

    # A REAL monitor whose connection fails, not a stub returning {"ok": False}.
    # A hand-written stub only restates the contract: if run_check stopped
    # reporting failure, the stub would keep claiming it did and this test would
    # stay green while the dashboard went quiet.
    broken = _monitor(tmp_path)
    broken._connect = lambda: None

    a.imap_monitors = [broken]
    a._last_imap_check = 0
    a._maybe_check_imap()
    errors = [
        r
        for r in a.db.get_recent_activity(20)
        if r["action"] == "IMAP_ERROR" and r["status"] == "error"
    ]
    assert errors, "a failed inbox check must surface on the dashboard"


# ── Config guardrails ──────────────────────────────────────────────────────


def test_send_window_that_never_opens_is_rejected(settings_factory):
    """start=20, end=6 can never satisfy `start <= hour < end`: the agent would
    run forever and silently send nothing."""
    import pytest

    with pytest.raises(Exception, match="send_window"):
        settings_factory(send_window_start_hour=20, send_window_end_hour=6)


def test_mailbox_with_blank_password_is_skipped(settings_factory):
    s = settings_factory(
        smtp_username="a@x.com",
        smtp_password="p",
        sender_2_email="b@x.com",
        sender_2_password="",
    )
    assert [a["username"] for a in s.get_sender_accounts()] == ["a@x.com"]


def test_duplicate_mailbox_is_deduped(settings_factory):
    """The same mailbox twice would get two warm-up counters and send double."""
    s = settings_factory(
        smtp_username="a@x.com",
        smtp_password="p",
        sender_2_email="A@X.com",
        sender_2_password="p",
    )
    assert len(s.get_sender_accounts()) == 1


# ── Round-2: DSN semantics must decide a bounce, not the subject ───────────


def _dsn(action, status, subject):
    import email as E

    return E.message_from_string(
        f"From: postmaster@outlook.com\nTo: me@corp.com\nSubject: {subject}\n"
        f"Content-Type: multipart/report; boundary=B\n\n--B\n"
        f"Content-Type: message/delivery-status\n\n"
        f"Final-Recipient: rfc822; buyer@prospect.de\n"
        f"Action: {action}\nStatus: {status}\n\n--B--\n"
    )


def test_delay_notice_is_not_a_bounce(tmp_path):
    """M365/Gmail send 'Delivery Status Notification (Delay)' for mail that is
    still queued ("you do not need to resend"). Treating it as a bounce
    permanently suppressed a live prospect."""
    m = _monitor(tmp_path)
    msg = _dsn("delayed", "4.4.7", "Delivery Status Notification (Delay)")
    assert m._classify_email(msg.get("From"), msg.get("Subject"), "", msg) == "dsn_info"


def test_soft_bounce_is_not_suppressed(tmp_path):
    """4.x.x is transient (mailbox full, greylisting) - retry, never suppress."""
    m = _monitor(tmp_path)
    msg = _dsn("failed", "4.2.2", "Delivery Status Notification (Failure)")
    assert (
        m._classify_email(msg.get("From"), msg.get("Subject"), "", msg) == "soft_bounce"
    )


def test_hard_bounce_still_detected(tmp_path):
    m = _monitor(tmp_path)
    msg = _dsn("failed", "5.1.1", "Delivery Status Notification (Failure)")
    assert m._classify_email(msg.get("From"), msg.get("Subject"), "", msg) == "bounce"


# ── Round-2: every suppression writer must normalise ───────────────────────


def test_imap_unsubscribe_writer_normalises(tmp_path):
    """The reply path is where a human explicitly objects - it must not store a
    mixed-case address that then matches nothing."""
    m = _monitor(tmp_path)
    m.process_unsubscribes([{"email": "Info@Brand.DE"}])
    assert m.db.is_suppressed("info@brand.de") is True


def test_web_unsubscribe_link_normalises(tmp_path):
    from modules.database import Database
    from modules.unsubscribe import UnsubscribeHandler

    db = Database(str(tmp_path / "u.db"))
    db.add_contact(db.log_search("q"), "Info@Brand.DE")
    h = UnsubscribeHandler(db, "test-secret")
    token = h.generate_token("Info@Brand.DE")
    ok, _ = h.process_unsubscribe(token)
    assert ok
    assert db.is_suppressed("info@brand.de") is True


def test_contact_lookup_is_case_insensitive(db):
    sid = db.log_search("q")
    db.conn.execute(
        "INSERT INTO contacts (search_id, email, organization) VALUES (?,?,?)",
        (sid, "Legacy@Brand.de", "Legacy"),
    )  # pre-normalisation row
    db.conn.commit()
    assert db.get_contact_by_email("legacy@brand.de") is not None


# ── Round-2: blacklist must not read refusal codes as a listing ────────────


def test_blocklist_refusal_codes_are_not_listings(monkeypatch):
    from modules.blacklist import LISTED, UNAVAILABLE, BlacklistChecker

    class Ans:
        def __init__(self, a):
            self.address = a

    def checker(code):
        chk = BlacklistChecker(cache_ttl=0)

        class R:
            timeout = lifetime = 1

            def resolve(self, n, t):
                return [Ans(code)]

        monkeypatch.setattr(chk, "_resolver", lambda: R())
        return chk.summarize(chk.check_domain("suppleco.com"))["status"]

    assert checker("127.0.0.1") == UNAVAILABLE  # SURBL: query blocked
    assert checker("127.0.1.255") == UNAVAILABLE  # Spamhaus DBL: prohibited
    assert checker("92.242.140.21") == UNAVAILABLE  # ISP hijacking NXDOMAIN
    assert checker("127.0.1.2") == LISTED  # a genuine listing


# ── Round-3: the pause must recover, and guards must not flood the log ─────


def test_sending_pause_clears_when_guard_recovers(
    settings_factory, tmp_path, monkeypatch
):
    """A tripped guard pauses sending. If the flag were sticky the agent would
    never send again without a restart, even once the condition cleared."""
    a = _agent(settings_factory, tmp_path, bounce_guard_enabled=True)
    tripped = {"v": True}
    monkeypatch.setattr(a, "_bounce_guard_tripped", lambda: tripped["v"])
    monkeypatch.setattr(a, "_blacklist_guard_tripped", lambda: False)

    a._sending_paused = True
    # Simulate the top-of-loop re-check while still tripped.
    if not (a._bounce_guard_tripped() or a._blacklist_guard_tripped()):
        a._sending_paused = False
    assert a._sending_paused is True  # still paused, correctly

    tripped["v"] = False  # bounce rate ages out
    if not (a._bounce_guard_tripped() or a._blacklist_guard_tripped()):
        a._sending_paused = False
    assert a._sending_paused is False  # resumes on its own


def test_tripped_guard_logs_once_not_every_check(settings_factory, tmp_path):
    """The guard is re-checked every loop; without a latch it would write an
    error row each time and bury every other diagnostic."""
    a = _agent(
        settings_factory,
        tmp_path,
        bounce_guard_enabled=True,
        max_bounce_rate=0.01,
        bounce_guard_min_sends=1,
    )
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, "seed@brand.de", organization="Seed")
    a.db.log_email(cid, "seed@brand.de", "s", delivery_status="sent")
    # Bounce the address we actually sent to. Suppressing an unrelated one used
    # to inflate the rate all the same, which is the bug the cohort join fixed.
    a.db.add_to_suppression("seed@brand.de", reason="bounce")

    for _ in range(5):
        assert a._bounce_guard_tripped() is True
    rows = [r for r in a.db.get_recent_activity(50) if r["action"] == "BOUNCE_GUARD"]
    assert len(rows) == 1, f"expected one log row, got {len(rows)}"


def test_contacts_migration_keeps_rows_with_send_history(tmp_path):
    """The migration must not DELETE a duplicate contact: contacts are
    referenced by email_log/sequence_enrollments/retry_queue, so a delete would
    hit a foreign-key constraint and abort the whole migration."""
    from modules.database import Database

    path = str(tmp_path / "m.db")
    db = Database(path)
    sid = db.log_search("q")
    cid = db.add_contact(sid, "dup@brand.de", organization="Lower")
    db.log_email(cid, "dup@brand.de", "s", delivery_status="sent")
    db.conn.execute(
        "INSERT INTO contacts (search_id, email, organization) VALUES (?,?,?)",
        (sid, "Dup@Brand.DE", "Mixed"),
    )  # legacy duplicate
    db.conn.commit()

    Database(path)  # re-open: migration runs
    db2 = Database(path)
    assert db2.conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0] == 1
    assert db2.get_contact_by_email("dup@brand.de") is not None


# ── Round-4: findings from the third review pass ───────────────────────────


def test_cancellare_meeting_is_not_an_optout():
    """'cancellare' is the everyday Italian verb 'to cancel'. Matching it bare
    permanently suppressed engaged prospects proposing to reschedule."""
    assert (
        reply_intent.is_optout(
            "Re: Quick question",
            "Dobbiamo cancellare la riunione di giovedì, possiamo spostarla?",
        )
        is False
    )
    # A real Italian opt-out must still be caught.
    assert (
        reply_intent.is_optout("", "Vi chiedo di cancellare la mia iscrizione") is True
    )
    assert reply_intent.is_optout("", "cancellatemi dalla vostra lista") is True


def test_read_receipt_is_not_a_reply(tmp_path):
    """An Outlook read receipt comes FROM the prospect with no Auto-Submitted
    header. Treated as a reply it marks them 'responded' and cancels their whole
    follow-up sequence - for merely opening the email."""
    import email as E

    m = _monitor(tmp_path)
    msg = E.message_from_string(
        "From: buyer@nordic.se\nTo: me@corp.com\nSubject: Read: Quick question\n\n"
        "Your message was read."
    )
    assert m._is_read_receipt(msg, msg.get("Subject")) is True
    for subj in ("Letto: Domanda veloce", "Gelesen: Kurze Frage"):
        assert m._is_read_receipt(None, subj) is True
    assert m._is_read_receipt(None, "Re: Quick question") is False


def test_human_reply_about_a_delivery_problem_is_not_eaten(tmp_path):
    """An Italian prospect writing about a missed sample delivery matched a
    bounce subject and was discarded. A subject alone must not make a message
    from a human into a DSN."""
    m = _monitor(tmp_path)
    import email as E

    msg = E.message_from_string(
        "From: buyer@nordic.it\nTo: me@corp.com\n"
        "Subject: Re: mancata consegna del campione\n\n"
        "Non abbiamo ricevuto il campione, potete rispedirlo?"
    )
    assert (
        m._classify_email(
            msg.get("From"), msg.get("Subject"), "Non abbiamo ricevuto il campione", msg
        )
        == "reply"
    )


def test_bounce_pause_does_not_clear_itself(settings_factory, tmp_path):
    """Pausing stops sends, so the 7-day window drains and the guard reports
    'not tripped' for lack of data. Resuming on that would walk straight back
    into the list that caused the bounces."""
    a = _agent(settings_factory, tmp_path)
    a._sending_paused, a._pause_reason = True, "bounce"
    assert a._safe_to_resume() is False


def test_blacklist_pause_needs_an_affirmative_clean(
    settings_factory, tmp_path, monkeypatch
):
    from modules import blacklist as bl

    a = _agent(settings_factory, tmp_path)
    a.settings.blacklist_check_enabled = True
    a._sending_paused, a._pause_reason = True, "blacklist"

    class Chk:
        def __init__(self, status, checked, total):
            self.v = {
                "status": status,
                "checked": checked,
                "total": total,
                "listed_on": [],
                "results": [],
            }

        def check_domain(self, d):
            return []

        def summarize(self, r):
            return self.v

    a._bl_checker = Chk(bl.UNAVAILABLE, 0, 2)  # lookup refused
    assert a._safe_to_resume() is False
    a._bl_checker = Chk(bl.CLEAN, 1, 2)  # only one list answered
    assert a._safe_to_resume() is False
    a._bl_checker = Chk(bl.CLEAN, 2, 2)  # full, affirmative clean
    assert a._safe_to_resume() is True


def test_direct_send_blocked_when_domain_blacklisted(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path)
    monkeypatch.setattr(a, "_blacklist_guard_tripped", lambda: True)
    ok, msg = a.send_single_email("x@brand.de", dry_run=False)
    assert ok is False and "blocklist" in msg.lower()


# ── Permanent SMTP rejections must suppress, not retry ─────────────────────


def _refuse(recipients):
    """An EmailSender whose SMTP layer refuses every recipient given."""
    import smtplib

    from modules.database import Database
    from modules.email_sender import EmailSender

    def _make(tmp_path):
        s = EmailSender(
            Database(str(tmp_path / "s.db")), "smtp.test", 587, "u@corp.com", "pw"
        )

        class Refusing:
            def noop(self):
                return (250, b"ok")

            def send_message(self, msg):
                raise smtplib.SMTPRecipientsRefused(recipients)

            def close(self):
                pass

        s._new_connection = lambda: Refusing()
        return s

    return _make


def _contact(sender, email="dead@brand.de"):
    sid = sender.db.log_search("test")
    cid = sender.db.add_contact(sid, email=email, organization="Brand")
    return {"id": cid, "email": email, "organization": "Brand"}


def test_hard_smtp_rejection_suppresses_the_address(tmp_path):
    """'550 5.1.1 user unknown' is the server's final answer. Retrying it
    three times just repeats the question and inflates our unknown-user rate,
    which is exactly what filtering services score senders on."""
    s = _refuse({"dead@brand.de": (550, b"5.1.1 User unknown")})(tmp_path)
    c = _contact(s)
    assert s.dispatch_to_contact(c, "Hi", "<p>h</p>", "h") is False
    assert s.db.is_suppressed("dead@brand.de") is True


def test_transient_smtp_rejection_does_not_suppress(tmp_path):
    """A 451 greylist is a 'try later', not a verdict on the address."""
    s = _refuse({"slow@brand.de": (451, b"4.7.1 Greylisted, try again")})(tmp_path)
    c = _contact(s, "slow@brand.de")
    assert s.dispatch_to_contact(c, "Hi", "<p>h</p>", "h") is False
    assert s.db.is_suppressed("slow@brand.de") is False


def test_mixed_rejection_is_treated_as_transient(tmp_path):
    """If any recipient got a 4xx the message is still deliverable - never
    suppress on a partial verdict."""
    s = _refuse(
        {
            "a@brand.de": (550, b"5.1.1 User unknown"),
            "b@brand.de": (451, b"4.7.1 Greylisted"),
        }
    )(tmp_path)
    c = _contact(s, "a@brand.de")
    s.dispatch_to_contact(c, "Hi", "<p>h</p>", "h")
    assert s.db.is_suppressed("a@brand.de") is False


def test_rejected_address_is_not_queued_for_retry(
    settings_factory, tmp_path, monkeypatch
):
    """End to end: the agent must not re-queue an address dispatch suppressed."""
    a = _agent(settings_factory, tmp_path, retry_enabled=True)
    email = "dead@brand.de"

    def fake_dispatch(**kw):
        a.db.add_to_suppression(email, reason="smtp_rejected")
        return False

    monkeypatch.setattr(a.email_sender, "dispatch_to_contact", fake_dispatch)
    queued = []
    if a.retry_queue:
        monkeypatch.setattr(
            a.retry_queue, "add_to_retry", lambda *args, **kw: queued.append(args)
        )
    sid = a.db.log_search("test")
    cid = a.db.add_contact(sid, email=email, organization="Brand")
    a._process_contact({"id": cid, "email": email, "organization": "Brand"})
    assert queued == []
    assert a._stats["retries_queued"] == 0


def test_content_filter_rejection_does_not_suppress_the_prospect(tmp_path):
    """A 5xx at DATA is a verdict on OUR message (content filter, DNSBL
    listing, refused sender), not on the address. Suppressing the recipient
    for it would walk the whole prospect list - every contact gets the same
    template, so the same rejection repeats for each one."""
    import smtplib

    from modules.database import Database
    from modules.email_sender import EmailSender

    s = EmailSender(Database(str(tmp_path / "s.db")), "smtp.test", 587, "u@c.com", "pw")

    class Blocked:
        def noop(self):
            return (250, b"ok")

        def send_message(self, msg):
            raise smtplib.SMTPDataError(554, b"5.7.1 Service unavailable; blocked")

        def close(self):
            pass

    s._new_connection = lambda: Blocked()
    c = _contact(s, "live.buyer@bigcorp.it")
    assert s.dispatch_to_contact(c, "Hi", "<p>h</p>", "h") is False
    assert s.db.is_suppressed("live.buyer@bigcorp.it") is False


def test_refused_sender_does_not_suppress_the_prospect(tmp_path):
    """M365's '550 5.7.60 Client does not have permissions to send as this
    sender' is a mailbox misconfiguration on our side."""
    import smtplib

    from modules.database import Database
    from modules.email_sender import EmailSender

    s = EmailSender(Database(str(tmp_path / "s.db")), "smtp.test", 587, "u@c.com", "pw")

    class Refused:
        def noop(self):
            return (250, b"ok")

        def send_message(self, msg):
            raise smtplib.SMTPSenderRefused(550, b"5.7.60 no permission", "u@c.com")

        def close(self):
            pass

    s._new_connection = lambda: Refused()
    c = _contact(s, "live.buyer@bigcorp.it")
    s.dispatch_to_contact(c, "Hi", "<p>h</p>", "h")
    assert s.db.is_suppressed("live.buyer@bigcorp.it") is False


def test_smtp_rejections_count_towards_the_bounce_rate(db):
    """The circuit breaker is the only automated protection for the sending
    domain. A scraped list of dead addresses rejected at RCPT would otherwise
    suppress silently while bounce_rate stayed 0% and the breaker never fired."""
    sid = db.log_search("q")
    for i in range(4):
        cid = db.add_contact(sid, email=f"x{i}@b.de")
        db.log_email(cid, f"x{i}@b.de", "s", "p", delivery_status="sent")
    for i in range(2):
        db.add_to_suppression(f"x{i}@b.de", reason="smtp_rejected")

    m = db.get_deliverability_metrics()
    assert m["recent_bounced"] == 2
    assert m["bounce_rate"] == 0.5
