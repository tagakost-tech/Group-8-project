"""No live email goes out while an inbox the agent reads cannot be read.

With IMAP on, the inbox monitor is what reads an opt-out or a data request
sent back by email, and the bounces the bounce guard counts. The send check
took IMAP being switched on as proof that the inbox was read, so a mailbox
whose login failed - Exchange answering "Basic authentication is disabled",
an expired password - logged IMAP_ERROR on every check while live mail kept
going out and every objection emailed back went unread (GDPR Art. 21(3)).

Live sending now waits for a complete read of every inbox within three check
intervals. The record is the scan watermark in the database, so the
dashboard's Direct Send agent, which reads no mail itself, is held by the
campaign agent's reads, and doctor fails on the same rule. A send loop reads
the inbox when a read falls due, so a long batch keeps the read fresh. Dry
runs still work, and with IMAP off the opt-out route rule applies alone.
"""

import imaplib
import sqlite3
from datetime import timedelta

import pytest

import main
from agent import OutreachAgent
from modules import clock

LOCAL = "http://127.0.0.1:5000"
INBOX = "outreach@suppleco.example"


class _BasicAuthDisabled:
    """Exchange Online with password IMAP switched off."""

    def __init__(self, *args, **kwargs):
        pass

    def login(self, user, password):
        raise imaplib.IMAP4.error("Basic authentication is disabled.")


def _quiet_inbox(events):
    """A mailbox that logs in and holds no new mail; each read is recorded."""

    class QuietInbox:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, user, password):
            events.append("read")
            return "OK", [b"Logged in"]

        def list(self, *args):
            return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

        def select(self, folder, readonly=False):
            return "OK", [b"0"]

        def search(self, charset, criteria):
            return "OK", [b""]

        def logout(self):
            return "BYE", [b""]

    return QuietInbox


def _agent(settings_factory, tmp_path, monkeypatch, **overrides):
    overrides.setdefault("imap_enabled", True)
    agent = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            ai_humanize_emails=False,
            smtp_username=INBOX,
            smtp_password="pw",
            imap_check_interval_minutes=30,
            **overrides,
        )
    )
    agent.dispatched = []
    monkeypatch.setattr(
        agent.email_sender,
        "dispatch_to_contact",
        lambda **k: agent.dispatched.append(("sent", k["contact"]["email"])) or 1,
    )
    return agent


def _contact(agent, email="info@brand.de"):
    sid = agent.db.log_search("q", source="test")
    return agent.db.get_contact_by_id(
        agent.db.add_contact(sid, email, organization="Brand")
    )


def _check_inbox(agent):
    agent._last_imap_check = 0.0  # due now
    agent._maybe_check_imap()


def _record_read(agent, when, inbox=None):
    for monitor in agent.imap_monitors:
        if inbox in (None, monitor.username):
            agent.db.set_state(monitor._watermark_key, when.isoformat())


def _rows(agent, action):
    return agent.db.conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE action = ?", (action,)
    ).fetchone()[0]


def test_an_inbox_that_cannot_log_in_stops_live_sends(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch, base_url=LOCAL)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _BasicAuthDisabled)
    for _ in range(5):
        _check_inbox(agent)
    reason = agent._send_blocker(False)
    assert reason and INBOX in reason
    assert agent._process_contact(_contact(agent), dry_run=False) is False
    assert agent.dispatched == []


def test_live_sends_resume_once_a_complete_read_succeeds(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch, base_url=LOCAL)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _BasicAuthDisabled)
    _check_inbox(agent)
    assert agent._send_blocker(False)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _quiet_inbox(agent.dispatched))
    _check_inbox(agent)
    assert agent._send_blocker(False) is None
    assert agent._process_contact(_contact(agent), dry_run=False) is True


def test_a_public_unsubscribe_page_does_not_excuse_an_unread_inbox(
    settings_factory, tmp_path, monkeypatch
):
    """A recipient may still object by replying, and IMAP is on to read it."""
    agent = _agent(
        settings_factory, tmp_path, monkeypatch, base_url="https://out.suppleco.example"
    )
    assert agent._send_blocker(False)


@pytest.mark.parametrize("minutes_ago,refused", [(85, False), (95, True)])
def test_a_complete_read_counts_for_three_check_intervals(
    settings_factory, tmp_path, monkeypatch, minutes_ago, refused
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    _record_read(agent, clock.now() - timedelta(minutes=minutes_ago))
    assert bool(agent._send_blocker(False)) is refused


def test_every_inbox_the_agent_reads_must_have_been_read(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(
        settings_factory,
        tmp_path,
        monkeypatch,
        sender_2_email="second@suppleco.example",
        sender_2_password="pw2",
    )
    _record_read(agent, clock.now(), inbox=INBOX)
    reason = agent._send_blocker(False)
    assert reason and "second@suppleco.example" in reason
    assert INBOX not in reason


def test_an_unreadable_record_of_the_reads_refuses_live_sends(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    _record_read(agent, clock.now())

    def locked(key, default=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(agent.db, "get_state", locked)
    assert agent._send_blocker(False)


def test_a_send_batch_reads_the_inbox_first_when_a_read_is_due(
    settings_factory, tmp_path, monkeypatch
):
    """The first send after a start, or one at the end of a batch longer than
    the check interval, waits for the read instead of going out unread."""
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    _record_read(agent, clock.now() - timedelta(hours=2))
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _quiet_inbox(agent.dispatched))
    agent._running = True
    agent._email_contacts([_contact(agent)], dry_run=False)
    assert agent.dispatched == ["read", ("sent", "info@brand.de")]


def test_direct_send_is_held_by_the_campaign_agents_reads(
    settings_factory, tmp_path, monkeypatch
):
    campaign = _agent(settings_factory, tmp_path, monkeypatch)
    direct = _agent(settings_factory, tmp_path, monkeypatch)
    ok, message = direct.send_single_email("info@brand.de", channel="referral")
    assert ok is False and INBOX in message
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _quiet_inbox(campaign.dispatched))
    _check_inbox(campaign)
    ok, message = direct.send_single_email("info@brand.de", channel="referral")
    assert ok is True, message


def test_dry_runs_are_never_refused_for_it(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    assert agent._send_blocker(True) is None


def test_with_imap_off_the_opt_out_route_rule_applies_alone(
    settings_factory, tmp_path, monkeypatch
):
    """Whether a public unsubscribe page alone is enough is counsel's
    question (L5.5); an inbox nobody switched on is not refused here."""
    agent = _agent(
        settings_factory,
        tmp_path,
        monkeypatch,
        imap_enabled=False,
        base_url="https://out.suppleco.example",
    )
    assert agent._send_blocker(False) is None
    assert agent._process_contact(_contact(agent), dry_run=False) is True


def test_the_refusal_reaches_the_dashboard_once_per_outage(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    agent._send_blocker(False)
    agent._send_blocker(False)
    assert _rows(agent, "INBOX_UNREAD") == 1
    _record_read(agent, clock.now())
    assert agent._send_blocker(False) is None
    _record_read(agent, clock.now() - timedelta(hours=2))
    agent._send_blocker(False)
    assert _rows(agent, "INBOX_UNREAD") == 2


def test_an_inbox_never_read_raises_the_stale_alarm_once(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _BasicAuthDisabled)
    for _ in range(3):
        _check_inbox(agent)
    assert _rows(agent, "IMAP_STALE") == 1


def _doctor(settings):
    checks = {c.label: c for c in main.run_doctor_checks(settings, network=False)}
    return checks["Inbox read"]


@pytest.mark.parametrize("minutes_ago", [None, 120, 5])
def test_doctor_reports_the_same_rule(
    settings_factory, tmp_path, monkeypatch, minutes_ago
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    if minutes_ago is not None:
        _record_read(agent, clock.now() - timedelta(minutes=minutes_ago))
    check = _doctor(agent.settings)
    assert check.blocking, "the send check refuses it, so doctor must say so"
    assert check.ok is (agent._send_blocker(False) is None)


def test_doctor_fails_while_the_login_fails(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _BasicAuthDisabled)
    _check_inbox(agent)
    check = _doctor(agent.settings)
    assert check.ok is False and INBOX in check.detail


def test_doctor_skips_it_with_imap_off(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch, imap_enabled=False)
    assert _doctor(agent.settings).ok is None
