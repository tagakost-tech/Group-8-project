"""Regression tests for the Batch 6 robustness defects.

These are the failures that stop the agent dead or burn leads irrecoverably,
without ever raising anything a caller can see.
"""

import smtplib
from email.message import EmailMessage

from modules.email_sender import EmailSender
from modules.imap_monitor import ImapMonitor

# ── 1. IMAP had no socket timeout ────────────────────────────────────────────
# IMAP4_SSL(host, port) with no timeout= is fully blocking, and _maybe_check_imap
# runs on the SAME thread as the send loop. One half-open connection froze
# sending, bounce processing, opt-outs and hot-lead alerts, with no log line -
# and the IMAP_STALE alarm could not fire because it lives inside the frozen call.


def test_imap_connect_passes_a_socket_timeout(monkeypatch):
    """Behavioural: intercept the real IMAP4_SSL call and inspect its kwargs."""
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

    assert "timeout" in captured, (
        "IMAP4_SSL was called with no timeout - the socket is fully blocking "
        "and one half-open connection freezes the send loop"
    )
    assert captured["timeout"] > 0


def test_imap_timeout_is_a_sane_value():
    assert 5 <= ImapMonitor.SOCKET_TIMEOUT <= 120


# ── 2. One undecodable header poisoned the mailbox for 14 days ───────────────
# decode_header() raises HeaderParseError on a malformed encoded-word, and
# errors="replace" does NOT protect against an unknown charset NAME:
# b"..".decode("unknown-8bit") raises LookupError. unknown-8bit is a registered
# charset MTAs emit in DSN subject lines, so bounces were the likely carrier.
# The exception hit the generic handler, the message was never expunged, so
# every later scan re-fetched and re-failed on it: the watermark never advanced,
# the lookback grew to the 14-day maximum, and IMAP_STALE falsely claimed
# bounces and opt-outs were not being processed.


def _decode(value):
    return ImapMonitor.__new__(ImapMonitor)._decode_header_value(value)


def test_unknown_8bit_charset_does_not_raise():
    """The specific charset MTAs put in DSN subjects."""
    out = _decode("=?unknown-8bit?Q?Undeliverable=3A_test?=")
    assert "Undeliverable" in out


def test_malformed_encoded_word_does_not_raise():
    out = _decode("=?utf-8?B?!!!not-base64!!!?=")
    assert isinstance(out, str) and out


def test_bogus_charset_name_does_not_raise():
    assert "hello" in _decode("=?not-a-real-charset?Q?hello?=")


def test_normal_headers_are_unchanged():
    """Guard: the fix must not degrade ordinary decoding."""
    assert _decode("Re: Your enquiry") == "Re: Your enquiry"
    assert "\xfc" in _decode("=?utf-8?B?R3LDvG5l?=")  # umlaut survives
    assert _decode(None) == ""


# ── 3. SMTP auth failure burned every remaining lead ─────────────────────────
# Each contact opens a fresh connection and a fresh LOGIN, so an expired App
# Password produced hundreds of failed logins in minutes - enough to lock the
# account - while every contact was marked 'failed'. get_pending_contacts()
# selects only status='pending', so those leads were gone for good.


class _AuthFailSMTP:
    def __init__(self):
        self.logins = 0

    def noop(self):
        return (250, b"OK")

    def send_message(self, msg):
        self.logins += 1
        raise smtplib.SMTPAuthenticationError(
            535, b"Username and Password not accepted"
        )

    def close(self):
        pass

    def quit(self):
        pass


def _sender(db, fake):
    s = EmailSender(db, "smtp.test", 587, "me@test.de", "pw")
    s._smtp = fake
    s._new_connection = lambda: fake
    return s


def test_auth_failure_latches_a_circuit_breaker(db):
    fake = _AuthFailSMTP()
    sender = _sender(db, fake)
    assert sender.auth_failed is False

    sender._deliver(EmailMessage())

    assert sender.auth_failed is True, (
        "an auth failure must latch, or every remaining contact retries the "
        "same broken login"
    )


def test_auth_failure_leaves_the_contact_pending(db):
    """The credentials are broken; that says nothing about this address."""
    fake = _AuthFailSMTP()
    sender = _sender(db, fake)
    sid = db.log_search("q", source="t")
    cid = db.add_contact(sid, "victim@brand.de")

    sender.dispatch_to_contact(
        contact={"id": cid, "email": "victim@brand.de", "name": "N"},
        subject="s",
        html_body="<p>hi</p>",
        plain_body="hi",
    )

    pending = [c["email"] for c in db.get_pending_contacts()]
    assert "victim@brand.de" in pending, (
        "marking it 'failed' removes it from get_pending_contacts() forever - "
        "the lead is unrecoverable even after the password is fixed"
    )


def test_auth_failure_is_not_treated_as_a_permanent_address_rejection(db):
    """It must never suppress the recipient."""
    fake = _AuthFailSMTP()
    sender = _sender(db, fake)
    sid = db.log_search("q", source="t")
    cid = db.add_contact(sid, "nosuppress@brand.de")

    sender.dispatch_to_contact(
        contact={"id": cid, "email": "nosuppress@brand.de", "name": "N"},
        subject="s",
        html_body="<p>hi</p>",
        plain_body="hi",
    )
    assert not db.is_suppressed("nosuppress@brand.de")


def test_the_agent_halts_the_batch_on_auth_failure(tmp_path, settings_factory):
    """Behavioural: give the agent three contacts and a broken login, then
    count how many sends it attempts. Without the breaker it tries all three -
    and in production that is hundreds of failed logins in minutes."""
    from agent import OutreachAgent

    agent = OutreachAgent(settings_factory(db_path=str(tmp_path / "auth.db")))
    agent._running = True

    # Neutralise the unrelated guards so the loop reaches the send path
    # deterministically regardless of wall-clock time or DNS.
    agent._within_send_window = lambda: True
    agent._bounce_guard_tripped = lambda: False
    agent._blacklist_guard_tripped = lambda: False

    attempts = []

    def _fail(*a, **kw):
        attempts.append(1)
        for _k, _n, es in agent.senders:
            es.auth_failed = True
        return False

    agent._process_contact = _fail

    sid = agent.db.log_search("q", source="t")
    contacts = []
    for i in range(3):
        cid = agent.db.add_contact(sid, f"c{i}@brand.de")
        contacts.append({"id": cid, "email": f"c{i}@brand.de", "lead_score": 90})

    agent._email_contacts(contacts)

    assert len(attempts) == 1, (
        f"attempted {len(attempts)} sends after an auth failure; the batch must "
        "halt on the first, or every remaining lead is burned"
    )


# ── 4. The bounce-guard pause leaked ─────────────────────────────────────────
# _sending_paused was enforced only on the pending-contact path. Follow-ups,
# retries and manual sends re-derived safety per item via _bounce_guard_tripped(),
# which returns False when recent_sent is below bounce_guard_min_sends - and a
# pause DRAINS that 7-day window toward zero. So within a week the guard reported
# "not tripped" for lack of data and mail resumed to the very list that caused
# the bounces, while the dashboard still displayed "paused".


def _agent(tmp_path, settings_factory, **kw):
    from agent import OutreachAgent

    return OutreachAgent(settings_factory(db_path=str(tmp_path / "p.db"), **kw))


def test_manual_send_is_refused_while_paused(tmp_path, settings_factory):
    agent = _agent(tmp_path, settings_factory)
    agent._sending_paused, agent._pause_reason = True, "bounce"

    ok, message = agent.send_single_email("someone@brand.de")

    assert ok is False
    assert "paused" in message.lower()


def test_manual_send_works_when_not_paused(tmp_path, settings_factory):
    """Guard: the pause check must not block ordinary sends."""
    agent = _agent(tmp_path, settings_factory)
    agent._sending_paused = False
    _ok, message = agent.send_single_email("someone@brand.de", dry_run=True)
    assert "paused" not in message.lower()


def _one_iteration(agent):
    """Run exactly one pass of run_continuous, recording what it invoked."""
    called = set()
    # Full isolation. run_continuous otherwise performs LIVE web scraping and
    # real SMTP sends: an earlier version of this helper hit real search engines
    # and attempted delivery to a genuine address it had just scraped. Nothing
    # here may touch the network.
    agent.search.search_and_extract = lambda *a, **kw: []
    agent.run_single = lambda *a, **kw: called.add("discovery") or agent._stats
    agent._blacklist_guard_tripped = lambda: False
    agent._bounce_guard_tripped = lambda: False
    agent._within_send_window = lambda: True
    agent._process_pending_contacts = lambda *a, **kw: called.add("pending")
    agent._process_sequences = lambda *a, **kw: called.add("sequences")
    agent._process_retries = lambda *a, **kw: called.add("retries")
    agent._maybe_check_imap = lambda *a, **kw: called.add("imap")
    # Stop after the first pass instead of looping forever.
    agent._interruptible_sleep = lambda *a, **kw: agent.stop()
    agent.run_continuous(queries=[], dry_run=False, loop_delay=0)
    return called


def test_followups_and_retries_do_not_run_while_paused(tmp_path, settings_factory):
    """Behavioural: a paused agent must not send follow-ups or retries.

    They previously re-derived safety from _bounce_guard_tripped(), which
    reports "not tripped" once the 7-day window drains during the pause.
    """
    from agent import OutreachAgent

    agent = OutreachAgent(settings_factory(db_path=str(tmp_path / "gate.db")))
    agent._sending_paused, agent._pause_reason = True, "bounce"

    ran = _one_iteration(agent)

    assert "sequences" not in ran, "follow-ups sent while paused"
    assert "retries" not in ran, "retries sent while paused"
    assert "pending" not in ran, "backlog sent while paused"


def test_imap_still_runs_while_paused(tmp_path, settings_factory):
    """A pause must NOT stop inbox reading: that is how bounces and opt-outs
    get processed, which is exactly what a paused agent needs to keep doing."""
    from agent import OutreachAgent

    agent = OutreachAgent(settings_factory(db_path=str(tmp_path / "imap.db")))
    agent._sending_paused, agent._pause_reason = True, "bounce"

    ran = _one_iteration(agent)

    assert "imap" in ran, (
        "inbox checking stopped while paused - bounces and opt-outs would go "
        "unprocessed for the entire pause"
    )


def test_everything_runs_when_not_paused(tmp_path, settings_factory):
    """Guard: the gating must not disable these outside a pause."""
    from agent import OutreachAgent

    agent = OutreachAgent(settings_factory(db_path=str(tmp_path / "ok.db")))
    agent._sending_paused = False

    ran = _one_iteration(agent)

    assert {"pending", "sequences", "imap"} <= ran, f"only ran: {ran}"
