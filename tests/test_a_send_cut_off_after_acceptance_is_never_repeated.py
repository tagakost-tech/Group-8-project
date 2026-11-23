"""A send cut off after the server accepted it is never sent again, and a send
the server refused is never recorded as sent.

dispatch_to_contact writes the email_log row as 'preparing', hands the
message to SMTP, and only after the server's answer marks the row sent and
the contact emailed. A process killed in between - the dashboard stopped
mid-send, Ctrl+C on `main.py run`, a reboot - leaves the row 'preparing' and
the contact as it was, and nothing reads 'preparing' back: has_been_emailed
counts sent rows only, the backlog selects on contacts.status, a follow-up
stays due until its enrolment advances, and a claimed retry goes back to the
queue once it is half an hour overdue. So the next process could send the
same email to the same person again.

The kill is simulated at the server's acceptance. The fake server records
the message, copies the database as committed at that moment, and raises an
exception no handler in the send path catches. Once it has escaped, the
database is put back to that copy: a killed process runs no finally block,
and process_retries has one that returns its claimed rows to the queue. The
next process is a new agent on the same database file, running one pass of
the continuous loop - backlog, follow-ups, inbox, retries - against a server
that records everything it accepts. Another lead waits in the backlog, so a
pass that sends nothing at all cannot pass for a fix.

A row that young may also be a send another process is making right now, so
it is held, never rewritten. One older than any live send can take is
settled as an unconfirmed send is, and reported once.

The row is also the claim on the send: a process that lost it to another one
sends nothing and writes nothing about the contact. A follow-up carries its
step on the row, so a step that may have gone out is moved on, not sent. And
a process that knows nothing went out says so: a refused send whose result
met a locked database is kept in memory and written first thing next time, so
only a process that died leaves a row to settle.

Nothing here reaches a real server: every sender is stubbed, and a test that
forgets one meets _no_real_smtp.
"""

import json
import smtplib
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

import main
from agent import OutreachAgent
from modules import clock
from modules import database as database_module
from modules import email_sender as sender_module
from modules.analytics import Analytics
from modules.contact_explainer import explain_contact
from modules.database import Database, _instant
from modules.export import DataExporter
from modules.imap_monitor import ImapMonitor
from modules.optout_spool import replay_optout_spool, spool_optout
from modules.retention import apply_retention
from modules.retry_queue import RetryQueue
from modules.sequences import SequenceEngine

PERSON = "buyer@brand.de"
BYSTANDER = "purchasing@other-brand.de"
SECOND = "sales@third-brand.de"
COLLEAGUE = "sales@brand.de"

# The dashboard's words for a send that has not settled, wherever it appears.
UNSETTLED = "still going out, or was cut off after it may have reached them"


@pytest.fixture(autouse=True)
def _no_real_smtp(monkeypatch):
    """A test whose sender is not stubbed fails here, not at a real server."""

    def refused(*_a, **_k):
        raise AssertionError("a test reached for a real SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", refused)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refused)


class _Killed(BaseException):
    """The process dying. The send path's handlers catch Exception at the
    widest, so nothing after the raise runs as part of the send."""


class _Server:
    """A fake SMTP server that records every message it accepts.

    ``kill`` runs once the server holds a message, before smtplib could hand
    its answer back to the sender. ``refuse`` turns every message away, but
    for the ``spared`` addresses; ``on_refuse`` runs just before it does.
    """

    def __init__(self, kill=None, refuse=None, on_refuse=None, spared=()):
        self.accepted = []
        self.messages = []
        self._kill = kill
        self._refuse = refuse
        self._on_refuse = on_refuse
        self._spared = spared

    def noop(self):
        return (250, b"2.0.0 OK")

    def send_message(self, msg):
        to = str(msg["To"])
        if self._refuse and to not in self._spared:
            if self._on_refuse:
                self._on_refuse()
            raise self._refuse
        self.accepted.append(to)
        self.messages.append(msg)
        if self._kill:
            self._kill()
        return {}

    def quit(self):
        pass

    def close(self):
        pass


class _DropsAfterAccepting(_Server):
    """The server takes the message and the connection dies before its answer:
    the path that records a real 'sent_unconfirmed'."""

    def send_message(self, msg):
        self.accepted.append(str(msg["To"]))
        raise smtplib.SMTPServerDisconnected(
            "Connection unexpectedly closed: timed out"
        )


def _settings(settings_factory, tmp_path, **config):
    return settings_factory(
        db_path=str(tmp_path / "agent.db"),
        smtp_username="agent@suppleco.test",
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        ai_humanize_emails=False,
        heuristic_discovery_enabled=False,
        min_lead_score_to_email=0,
        # No AI, whatever this machine's environment names: a pass must
        # not reach the network.
        llm_provider="",
        **config,
    )


def _query(db_path, sql, params=()):
    raw = sqlite3.connect(db_path)
    try:
        return raw.execute(sql, params).fetchall()
    finally:
        raw.close()


def _update(db_path, sql, params=()):
    raw = sqlite3.connect(db_path)
    try:
        changed = raw.execute(sql, params).rowcount
        raw.commit()
        return changed
    finally:
        raw.close()


def _record(db_path, contact_id):
    """The person's email_log statuses, oldest first, and contact status."""
    rows = _query(
        db_path,
        "SELECT delivery_status FROM email_log WHERE contact_id = ? ORDER BY id",
        (contact_id,),
    )
    ((status,),) = _query(
        db_path, "SELECT status FROM contacts WHERE id = ?", (contact_id,)
    )
    return [r[0] for r in rows], status


def _process(settings, server):
    """A new agent process on the database, sending through ``server``."""
    a = OutreachAgent(settings)
    a.search.search_and_extract = lambda *_a, **_k: []
    for _key, _name, sender in a.senders:
        sender._new_connection = lambda: server
    return a


def _one_pass(a):
    """What a started agent does: one pass of the continuous loop."""

    def stop_after_the_pass(_loop_delay):
        a.stop()
        return 0.0

    a._next_loop_delay = stop_after_the_pass
    try:
        a.run_continuous(dry_run=False, loop_delay=60)
    finally:
        a.db.close_thread_conn()
    crashed = _query(
        a.db.db_path, "SELECT details FROM activity_log WHERE action = 'LOOP_ERROR'"
    )
    assert not crashed, f"the pass failed, so it shows nothing: {crashed}"


def _dry_pass(a):
    """One pass of the continuous loop as a dry run."""

    def stop_after_the_pass(_loop_delay):
        a.stop()
        return 0.0

    a._next_loop_delay = stop_after_the_pass
    try:
        a.run_continuous(dry_run=True, loop_delay=60)
    finally:
        a.db.close_thread_conn()


def _backlog_only(a):
    """The campaign's phase of a pass, with no loop around it: no settling
    before it and nothing written when it ends."""
    a._running = True
    try:
        a._process_pending_contacts(False)
    finally:
        a._running = False
        a.db.close_thread_conn()


def _copy_database(src_path, dst_path):
    src, dst = sqlite3.connect(src_path), sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def _killed_at_acceptance(settings, tmp_path, run=_one_pass):
    """Run a process whose first accepted message is the last thing it does,
    and leave the database as its death left it. Returns the server. ``run``
    is what the process was doing: one pass of the loop, unless named."""
    db_path = settings.get_abs_db_path()
    at_acceptance = str(tmp_path / "committed_at_acceptance.db")

    def kill():
        _copy_database(db_path, at_acceptance)
        raise _Killed

    server = _Server(kill=kill)
    try:
        run(_process(settings, server))
    except _Killed:
        _copy_database(at_acceptance, db_path)
    else:
        pytest.fail("no message reached the server, so no send was cut off")
    return server


def _lead(db_path, email, organization):
    """A lead a search stored: pending, never emailed."""
    db = Database(db_path)
    sid = db.log_search("contract manufacturing", source="test", status="completed")
    return db.add_contact(sid, email, organization=organization)


def _rank(db_path, email, score):
    """Fix where a lead stands in the backlog: qualified, so no pass scores it
    again."""
    ranked = _update(
        db_path,
        "UPDATE contacts SET lead_score = ?, qualified_at = ? WHERE email = ?",
        (score, clock.db_now(), email),
    )
    assert ranked == 1, f"{email} is not in the backlog"


def _age_the_cut_off_send(db_path, age=timedelta(minutes=27)):
    """Date the killed send's row ``age`` ago. A literal age, on purpose: the
    bound is 26 minutes, and an age worked out from the product's constant
    would only ever compare that constant with itself."""
    then = clock.now().astimezone(timezone.utc) - age
    stamp = then.astimezone(clock.get_timezone())
    aged = _update(
        db_path,
        "UPDATE email_log SET timestamp = ? WHERE delivery_status = 'preparing'",
        (stamp.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    assert aged == 1, "the killed send left no 'preparing' row"


def _reported(db_path):
    """The activity rows that report a settled send, as (status, details)."""
    return _query(
        db_path,
        "SELECT status, details FROM activity_log WHERE action = 'EMAIL_INTERRUPTED'",
    )


def _enrolment(db_path, contact_id):
    return _query(
        db_path,
        "SELECT current_step, status FROM sequence_enrollments "
        "WHERE contact_id = ? ORDER BY id",
        (contact_id,),
    )


def _retries(db_path, contact_id):
    return _query(
        db_path,
        "SELECT status, attempt FROM retry_queue WHERE contact_id = ?",
        (contact_id,),
    )


def _steps(db_path):
    """The id of each step of the default sequence, first step first."""
    return [
        r[0]
        for r in _query(db_path, "SELECT id FROM sequence_steps ORDER BY step_number")
    ]


def _set_due(db_path):
    """Make the next step of every active enrolment due."""
    moved = _update(
        db_path,
        "UPDATE sequence_enrollments SET "
        "enrolled_at = datetime('now', '-30 days'), "
        "last_sent_at = datetime('now', '-30 days') "
        "WHERE status = 'active'",
    )
    assert moved, "no active enrolment to make due"


def _their_row(db_path, contact_id, status, step=None):
    """A row another process wrote: its send, as it committed it."""
    _update(
        db_path,
        "INSERT INTO email_log (contact_id, recipient_email, subject, "
        "delivery_status, timestamp, sequence_step_id) "
        "VALUES (?, ?, 'Theirs', ?, ?, ?)",
        (contact_id, PERSON, status, clock.db_now(), step),
    )


def _locked(*_a, **_k):
    raise sqlite3.OperationalError("database is locked")


class _LockedOnce:
    """A write that meets a locked database once, and works after that."""

    def __init__(self, real):
        self.real = real
        self.left = 1

    def __call__(self, *a, **k):
        if self.left:
            self.left -= 1
            raise sqlite3.OperationalError("database is locked")
        return self.real(*a, **k)


class _WriteLock:
    """A real SQLite write lock, held by another connection."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None

    def take(self):
        self.conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self.conn.execute("BEGIN IMMEDIATE")

    def release(self):
        if self.conn is not None:
            self.conn.rollback()
            self.conn.close()
            self.conn = None


def _impatient(monkeypatch):
    """Writes give up on a real lock in a tenth of a second, not five."""
    real = Database._get_conn

    def quick(self):
        conn = real(self)
        conn.execute("PRAGMA busy_timeout=100;")
        return conn

    monkeypatch.setattr(Database, "_get_conn", quick)


# ── The sends a next process could repeat ──────────────────────────────────


def _first_email_due(settings):
    return _lead(settings.get_abs_db_path(), PERSON, "Brand GmbH")


def _follow_up_due(settings):
    """Emailed four days ago: the first follow-up is due."""
    contact_id = _first_email_due(settings)
    server = _Server()
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON], "the first email was never sent"
    enrolled = _update(
        settings.get_abs_db_path(),
        "UPDATE sequence_enrollments SET enrolled_at = datetime('now', '-4 days')",
    )
    assert enrolled == 1, "the first email started no follow-ups"
    return contact_id


def _retry_due(settings):
    """Greylisted on the first attempt; the retry fell due two hours ago."""
    contact_id = _first_email_due(settings)
    greylisted = _Server(
        refuse=smtplib.SMTPRecipientsRefused({PERSON: (451, b"4.7.1 Try later")})
    )
    _one_pass(_process(settings, greylisted))
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    queued = _update(
        settings.get_abs_db_path(),
        "UPDATE retry_queue SET next_retry_at = ? WHERE status = 'pending'",
        (two_hours_ago.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    assert queued == 1, "the greylisted first email was not queued for a retry"
    return contact_id


SENDS = [
    pytest.param("first email", {}, _first_email_due, id="first-email"),
    pytest.param(
        "follow-up", {"sequences_enabled": True}, _follow_up_due, id="follow-up"
    ),
    pytest.param("retry", {}, _retry_due, id="retry"),
]

# What a held send leaves of each send's own bookkeeping: as it was, because
# the send it waits for may yet fail and leave them to go out.
HELD = {
    "first email": ([], []),
    "follow-up": ([(0, "active")], []),
    "retry": ([], [("pending", 0)]),
}


@pytest.mark.parametrize(("send", "config", "make_due"), SENDS)
def test_a_send_cut_off_after_the_server_accepted_it_is_not_sent_again(
    send, config, make_due, settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, **config)
    db_path = settings.get_abs_db_path()
    contact_id = make_due(settings)

    killed = _killed_at_acceptance(settings, tmp_path)
    assert killed.accepted == [PERSON], f"the {send} never reached the server"
    rows, status = _record(db_path, contact_id)

    # Ranked below the person, so a pass that stopped at the held contact
    # would never reach it.
    _rank(db_path, PERSON, 90)
    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    _rank(db_path, BYSTANDER, 10)
    server = _Server()
    _one_pass(_process(settings, server))

    assert BYSTANDER in server.accepted, (
        "the next process emailed nobody, not even a lead it had never "
        "emailed, so it shows nothing"
    )
    assert PERSON not in server.accepted, (
        f"the next process sent the {send} to {PERSON} again. The killed send "
        f"left email_log {rows} and the contact '{status}', and nothing "
        "reconciled it."
    )
    assert _record(db_path, contact_id) == (rows, status), (
        f"the {send} was cut off moments ago, as another process's send could "
        "still be going out, and its row or contact was rewritten anyway"
    )
    enrolment, retries = HELD[send]
    assert _enrolment(db_path, contact_id) == enrolment, "the hold moved the follow-up"
    assert _retries(db_path, contact_id) == retries, "the hold spent the retry"
    assert _reported(db_path) == [], "a send that may still finish was reported"


@pytest.mark.parametrize(("send", "config", "make_due"), SENDS)
def test_a_send_that_completes_is_recorded_and_not_sent_again(
    send, config, make_due, settings_factory, tmp_path
):
    """The control: the same two processes, with no kill between them."""
    settings = _settings(settings_factory, tmp_path, **config)
    db_path = settings.get_abs_db_path()
    contact_id = make_due(settings)

    server = _Server()
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON], f"the {send} was never sent"
    rows, status = _record(db_path, contact_id)
    assert (rows[-1], status) == ("sent", "emailed"), (rows, status)

    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON, BYSTANDER], (
        f"after a delivered {send}, the next process sent {server.accepted[1:]}"
    )


def test_a_held_retry_does_not_stop_the_retries_behind_it(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _lead(db_path, PERSON, "Brand GmbH")
    second = _lead(db_path, SECOND, "Third Brand GmbH")
    greylisted = _Server(
        refuse=smtplib.SMTPRecipientsRefused({"x": (451, b"4.7.1 Try later")})
    )
    _one_pass(_process(settings, greylisted))
    for who, hours in ((contact_id, 2), (second, 1)):
        when = datetime.now(timezone.utc) - timedelta(hours=hours)
        _update(
            db_path,
            "UPDATE retry_queue SET next_retry_at = ? WHERE contact_id = ?",
            (when.strftime("%Y-%m-%dT%H:%M:%SZ"), who),
        )
    killed = _killed_at_acceptance(settings, tmp_path)
    assert killed.accepted == [PERSON], "this needs the held retry first"
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [SECOND]


def test_a_held_follow_up_does_not_stop_the_follow_ups_behind_it(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    _lead(db_path, PERSON, "Brand GmbH")
    first = _Server()
    _one_pass(_process(settings, first))
    _lead(db_path, SECOND, "Third Brand GmbH")
    _one_pass(_process(settings, first))
    assert first.accepted == [PERSON, SECOND]
    enrolled = _update(
        db_path,
        "UPDATE sequence_enrollments SET enrolled_at = datetime('now', '-4 days')",
    )
    assert enrolled == 2
    killed = _killed_at_acceptance(settings, tmp_path)
    assert killed.accepted == [PERSON], "this needs the held follow-up first"
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [SECOND]


@pytest.mark.parametrize(
    ("send", "config", "make_due"),
    [p for p in SENDS if p.id in ("follow-up", "retry")],
)
def test_a_held_send_still_goes_out_if_the_live_send_then_fails(
    send, config, make_due, settings_factory, tmp_path
):
    """The promise of the hold: nothing is spent while the send it waits for
    could still come back refused."""
    settings = _settings(settings_factory, tmp_path, **config)
    db_path = settings.get_abs_db_path()
    make_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    held = _Server()
    _one_pass(_process(settings, held))
    assert held.accepted == []
    answered = _update(
        db_path,
        "UPDATE email_log SET delivery_status = 'failed', "
        "error_message = 'Recipient refused' WHERE delivery_status = 'preparing'",
    )
    assert answered == 1
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [PERSON], f"the hold spent the {send}"


# ── The bound: a row that could still finish is left alone ─────────────────


@pytest.mark.parametrize("minutes", [2, 10, 25])
def test_a_row_younger_than_the_26_minute_bound_is_held_and_unreported(
    minutes, settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path, timedelta(minutes=minutes))
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == []
    assert _record(db_path, contact_id) == (["preparing"], "pending")
    assert _reported(db_path) == []


def test_a_row_older_than_the_26_minute_bound_is_settled(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path, timedelta(minutes=27))

    _one_pass(_process(settings, _Server()))

    assert _record(db_path, contact_id) == (["sent_unconfirmed"], "emailed")
    assert len(_reported(db_path)) == 1


@pytest.mark.parametrize("port", [465, 587])
def test_the_connection_timeout_is_the_one_the_bound_assumes(
    port, settings_factory, tmp_path, monkeypatch
):
    """Port 465 and STARTTLS on 587, Microsoft 365's submission port, open
    their connections on different lines, and the bound needs both."""
    made = MagicMock()
    monkeypatch.setattr(sender_module.smtplib, "SMTP", made)
    monkeypatch.setattr(sender_module.smtplib, "SMTP_SSL", made)
    settings = _settings(settings_factory, tmp_path, smtp_port=port)
    sender = OutreachAgent(settings).senders[0][2]
    assert sender.smtp_port == port, "the port did not reach the sender"

    sender._new_connection()

    assert made.call_args.kwargs["timeout"] == sender_module.SMTP_TIMEOUT == 30


# ── Settled once no live send could still be making it ─────────────────────

# Where each send's own bookkeeping stands once it is settled, as an
# unconfirmed send leaves it: the follow-up step it sent is behind it, the
# retry it made has succeeded, and nothing is started that was not asked for.
MOVED_ON = {
    "first email": (_enrolment, []),
    "follow-up": (_enrolment, [(1, "active")]),
    "retry": (_retries, [("succeeded", 1)]),
}


@pytest.mark.parametrize(("send", "config", "make_due"), SENDS)
def test_a_send_cut_off_longer_ago_than_a_send_can_take_is_settled_as_unconfirmed(
    send, config, make_due, settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, **config)
    db_path = settings.get_abs_db_path()
    contact_id = make_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)

    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    server = _Server()
    _one_pass(_process(settings, server))

    assert server.accepted == [BYSTANDER], f"after the {send}, {server.accepted}"
    rows, status = _record(db_path, contact_id)
    assert (rows[-1], status) == ("sent_unconfirmed", "emailed"), (rows, status)
    read, expected = MOVED_ON[send]
    assert read(db_path, contact_id) == expected
    ((level, details),) = _reported(db_path)
    assert level == "warning" and PERSON in details, details

    _one_pass(_process(settings, server))
    assert server.accepted == [BYSTANDER], "a settled send was sent again"
    assert len(_reported(db_path)) == 1, "a settled send was reported twice"


@pytest.mark.parametrize(("send", "config", "make_due"), SENDS)
def test_a_send_held_by_one_process_and_settled_by_the_next_ends_the_same(
    send, config, make_due, settings_factory, tmp_path
):
    """The hold has put the claimed retry back by then, which settling must
    close as it closes a claimed one."""
    settings = _settings(settings_factory, tmp_path, **config)
    db_path = settings.get_abs_db_path()
    contact_id = make_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    server = _Server()
    _one_pass(_process(settings, server))
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, server))

    assert server.accepted == []
    rows, status = _record(db_path, contact_id)
    assert (rows[-1], status) == ("sent_unconfirmed", "emailed"), (rows, status)
    read, expected = MOVED_ON[send]
    assert read(db_path, contact_id) == expected
    assert len(_reported(db_path)) == 1


def test_settling_itself_moves_the_step_on(settings_factory, tmp_path):
    """Not left to the follow-up phase's own check, which does not run while the
    agent is paused or outside its send window."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)

    _process(settings, _Server())._reconcile_interrupted_sends()

    assert _enrolment(db_path, contact_id) == [(1, "active")]
    assert _query(
        db_path, "SELECT last_sent_at IS NOT NULL FROM sequence_enrollments"
    ) == [(1,)], "the next step's delay would run from the wrong time"


def test_one_running_agent_holds_a_send_on_one_pass_and_settles_it_on_a_later_one(
    settings_factory, tmp_path
):
    """A process restarted right after a kill finds the row young; if it only
    settled at start-up, it would hold that contact until the next restart."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    server = _Server()
    a = _process(settings, server)
    passes = []
    held = []

    def after_each_pass(_delay):
        passes.append(1)
        if len(passes) == 1:
            held.append(_record(db_path, contact_id))
            _age_the_cut_off_send(db_path)
        else:
            a.stop()
        return 0.0

    a._next_loop_delay = after_each_pass
    a.run_continuous(dry_run=False, loop_delay=60)
    a.db.close_thread_conn()

    assert held == [(["preparing"], "pending")], "the first pass did not hold it"
    assert _record(db_path, contact_id) == (["sent_unconfirmed"], "emailed")
    assert server.accepted == []


def test_a_settled_contact_gets_its_next_follow_up_when_it_falls_due(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    _follow_up_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _one_pass(_process(settings, _Server()))
    _set_due(db_path)
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [PERSON], "the sequence stopped at the settled send"


def test_settling_the_last_follow_up_completes_the_sequence(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    server = _Server()
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON]
    _set_due(db_path)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _enrolment(db_path, contact_id) == [(1, "completed")]
    assert _query(
        db_path,
        "SELECT completed_at IS NOT NULL, last_sent_at IS NOT NULL "
        "FROM sequence_enrollments",
    ) == [(1, 1)]


def test_settling_after_the_sequence_completed_does_not_start_it_again(
    settings_factory, tmp_path
):
    """The last follow-up's result met a locked database; its process went on
    and completed the enrolment. The contact is 'emailed' with none active."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    server = _Server()
    _one_pass(_process(settings, server))
    _set_due(db_path)
    a = _process(settings, server)
    a.db.update_email_status = _locked
    _one_pass(a)
    assert _enrolment(db_path, contact_id) == [(1, "completed")]
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, server))

    assert _enrolment(db_path, contact_id) == [(1, "completed")], (
        "the whole sequence starts again for someone who has had all of it"
    )


@pytest.mark.parametrize("campaign", ["same-sequence", "switched-sequence"])
def test_settling_does_not_restart_a_sequence_they_finished(
    campaign, settings_factory, tmp_path
):
    """They finished the sequence, a later Direct Send failed ('failed'), and
    the operator's next one was cut off. Decision 8: no enrolment of any status,
    in any sequence, may stand behind a settled send that starts one. The
    operator may since have switched the campaign to a new sequence."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    finished = _update(
        db_path,
        "UPDATE sequence_enrollments SET status = 'completed', current_step = 2, "
        "completed_at = datetime('now', '-30 days'), "
        "last_sent_at = datetime('now', '-30 days')",
    )
    assert finished == 1
    refused = _Server(refuse=smtplib.SMTPDataError(451, b"try later"))
    ok, _message = _process(settings, refused).send_single_email(
        PERSON, organization="Brand GmbH", confirm_repeat=True
    )
    assert not ok
    assert _record(db_path, contact_id)[1] == "failed"
    _killed_at_acceptance(
        settings,
        tmp_path,
        run=lambda a: a.send_single_email(
            PERSON, organization="Brand GmbH", confirm_repeat=True
        ),
    )
    _age_the_cut_off_send(db_path)
    if campaign == "switched-sequence":
        settings = _settings(
            settings_factory,
            tmp_path,
            sequences_enabled=True,
            default_sequence_name="Autumn follow-ups",
        )

    _one_pass(_process(settings, _Server()))

    if campaign == "switched-sequence":
        assert _query(db_path, "SELECT COUNT(*) FROM sequences") == [(2,)], (
            "the switch made no second sequence, so this shows nothing"
        )
    assert _enrolment(db_path, contact_id) == [(2, "completed")], (
        "settling started a sequence for someone who had already had one"
    )


def test_a_settled_first_email_starts_the_follow_ups_as_a_sent_one_does(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == []
    assert _enrolment(db_path, contact_id) == [(0, "active")]


def test_a_settled_retry_with_sequences_on_starts_the_follow_ups(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _retry_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _enrolment(db_path, contact_id) == [(0, "active")]


def test_a_settled_direct_send_to_a_contact_it_created_starts_no_follow_ups(
    settings_factory, tmp_path
):
    """Whether the operator ticked "follow-ups" is recorded nowhere."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    _killed_at_acceptance(
        settings,
        tmp_path,
        run=lambda a: a.send_single_email(PERSON, organization="Brand GmbH"),
    )
    ((contact_id,),) = _query(
        db_path, "SELECT id FROM contacts WHERE email = ?", (PERSON,)
    )
    _age_the_cut_off_send(db_path)
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == []
    assert _record(db_path, contact_id) == (["sent_unconfirmed"], "emailed")
    assert _enrolment(db_path, contact_id) == []


def test_a_settled_direct_send_to_a_campaign_contact_starts_the_follow_ups(
    settings_factory, tmp_path
):
    """The agreed behaviour: settled, it is enrolled as the campaign's own send
    to that waiting contact would have been, although the same send completed
    without the tick starts nothing. The tick is recorded nowhere."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(
        settings,
        tmp_path,
        run=lambda a: a.send_single_email(PERSON, organization="Brand GmbH"),
    )
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _enrolment(db_path, contact_id) == [(0, "active")]


def test_a_settled_direct_send_to_someone_already_emailed_starts_no_follow_ups(
    settings_factory, tmp_path
):
    """Only a contact still waiting for its first email starts them. This one
    was emailed before sequences were switched on, and has no enrolment."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _update(
        db_path, "UPDATE contacts SET status = 'emailed' WHERE id = ?", (contact_id,)
    )
    _killed_at_acceptance(
        settings,
        tmp_path,
        run=lambda a: a.send_single_email(PERSON, organization="Brand GmbH"),
    )
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _enrolment(db_path, contact_id) == []


def test_a_send_whose_result_met_a_locked_database_is_settled_once(
    settings_factory, tmp_path
):
    """The same row without a kill: the result write fails, the process goes
    on, and moves the follow-up on itself. Settling must not move it again."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    server = _Server()
    a = _process(settings, server)
    a.db.update_email_status = _locked
    _one_pass(a)
    assert server.accepted == [PERSON], "the follow-up never went out"
    assert _record(db_path, contact_id) == (["sent", "preparing"], "emailed")
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, server))

    assert server.accepted == [PERSON]
    assert _record(db_path, contact_id) == (["sent", "sent_unconfirmed"], "emailed")
    assert _enrolment(db_path, contact_id) == [(1, "active")]


def test_a_locked_follow_up_of_someone_enrolled_long_ago_is_moved_once(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _update(
        db_path,
        "UPDATE sequence_enrollments SET enrolled_at = datetime('now', '-30 days')",
    )
    a = _process(settings, _Server())
    a.db.update_email_status = _locked
    _one_pass(a)
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _enrolment(db_path, contact_id) == [(1, "active")]


def test_settling_long_after_does_not_skip_the_step_that_fell_due_since(
    settings_factory, tmp_path
):
    """The follow-up's result met a locked database, its process moved the step
    on, and the agent was then off for five days."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    a = _process(settings, _Server())
    a.db.update_email_status = _locked
    _one_pass(a)
    assert _enrolment(db_path, contact_id) == [(1, "active")]
    _age_the_cut_off_send(db_path, timedelta(days=5))
    _update(
        db_path,
        "UPDATE sequence_enrollments SET "
        "last_sent_at = datetime('now', '-5 days', '+1 minute')",
    )
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [PERSON], "the last follow-up was skipped"


def test_settling_a_send_never_reopens_a_contact_closed_since(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    # Their reply was read before the send was settled.
    _update(
        db_path, "UPDATE contacts SET status = 'responded' WHERE id = ?", (contact_id,)
    )
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _record(db_path, contact_id) == (["sent_unconfirmed"], "responded")


def test_a_one_off_run_settles_a_send_cut_off_earlier(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    server = _Server()

    _process(settings, server).run_single(queries=["contract manufacturing"])

    assert server.accepted == []
    assert _record(db_path, contact_id) == (["sent_unconfirmed"], "emailed")
    assert len(_reported(db_path)) == 1


def test_a_send_that_cannot_be_settled_yet_holds_back_and_costs_the_pass_nothing(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    server = _Server()
    a = _process(settings, server)
    a.db.settle_interrupted_sends = _locked

    _one_pass(a)

    assert server.accepted == [BYSTANDER]


# ── Settled once, however many processes try ───────────────────────────────


def test_two_processes_settling_at_once_settle_and_report_once(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _retry_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    mine, theirs = Database(db_path), Database(db_path)
    settle = mine._settle_interrupted_send

    def they_get_there_first(*args, **kwargs):
        theirs.settle_interrupted_sends(timedelta(0))
        return settle(*args, **kwargs)

    mine._settle_interrupted_send = they_get_there_first

    assert mine.settle_interrupted_sends(timedelta(0)) == []
    assert len(_reported(db_path)) == 1
    assert _retries(db_path, contact_id) == [("succeeded", 1)]


def test_settling_is_one_transaction(settings_factory, tmp_path):
    """The report cannot be written: the whole settle rolls back, the row stays
    held, and the rest of the pass goes on."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _update(
        db_path,
        "CREATE TRIGGER no_report BEFORE INSERT ON activity_log "
        "WHEN NEW.action = 'EMAIL_INTERRUPTED' "
        "BEGIN SELECT RAISE(ABORT, 'disk full'); END",
    )
    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [BYSTANDER]
    assert _record(db_path, contact_id) == (["preparing"], "pending")


def test_every_stale_row_is_settled_in_one_pass(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    other = _lead(db_path, BYSTANDER, "Other Brand GmbH")
    Database(db_path).log_email(other, BYSTANDER, "Hello", delivery_status="preparing")
    then = clock.now().astimezone(timezone.utc) - timedelta(minutes=30)
    stamp = then.astimezone(clock.get_timezone()).strftime("%Y-%m-%d %H:%M:%S")
    _update(
        db_path,
        "UPDATE email_log SET timestamp = ? WHERE delivery_status = 'preparing'",
        (stamp,),
    )

    _one_pass(_process(settings, _Server()))

    assert _record(db_path, contact_id) == (["sent_unconfirmed"], "emailed")
    assert _record(db_path, other) == (["sent_unconfirmed"], "emailed")
    assert len(_reported(db_path)) == 2


def test_rows_settled_before_a_lock_are_still_returned(settings_factory, tmp_path):
    """What a pass settled before the database locked must still reach the
    caller: Direct Send refuses a click on what it finds among them."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    other = _lead(db_path, BYSTANDER, "Other Brand GmbH")
    db = Database(db_path)
    db.log_email(other, BYSTANDER, "Hello", delivery_status="preparing")
    real = db._settle_interrupted_send
    calls = []

    def locked_on_the_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    db._settle_interrupted_send = locked_on_the_second

    settled = db.settle_interrupted_sends(timedelta(0))

    assert [row["recipient_email"] for row in settled] == [PERSON]


# ── What the settled row and contact say ───────────────────────────────────


def test_the_settled_row_says_what_happened(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    ((response, error),) = _query(
        db_path, "SELECT smtp_response, error_message FROM email_log"
    )
    assert "Interrupted" in response and error is None


def test_a_settled_contact_keeps_no_stale_discard_reason(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _retry_due(settings)
    reason = "SELECT discard_reason FROM contacts WHERE id = ?"
    assert _query(db_path, reason, (contact_id,)) != [(None,)], (
        "the greylisted first email left no reason, so this shows nothing"
    )
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _query(db_path, reason, (contact_id,)) == [(None,)]


def test_settling_writes_the_warning_log_line(settings_factory, tmp_path, caplog):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)

    with caplog.at_level("WARNING"):
        _one_pass(_process(settings, _Server()))

    assert "[INTERRUPTED]" in caplog.text


# ── Reading the row's time ─────────────────────────────────────────────────


def test_a_repeated_hour_reads_as_the_later_instant():
    assert _instant("2026-10-25 02:30:00", ZoneInfo("Europe/Rome")) == datetime(
        2026, 10, 25, 1, 30, tzinfo=timezone.utc
    )


@pytest.fixture
def default_zone_again():
    yield
    clock.set_timezone(clock.DEFAULT_TIMEZONE)


def test_a_row_time_is_read_in_the_apps_zone_not_the_machines(
    settings_factory, tmp_path, default_zone_again
):
    """UTC-12 is behind every machine's zone, so read in the machine's zone a
    fresh row looks hours old on any machine, here or elsewhere."""
    settings = _settings(settings_factory, tmp_path, app_timezone="Etc/GMT+12")
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    assert clock.now().utcoffset() == timedelta(hours=-12)

    _one_pass(_process(settings, _Server()))

    assert _record(db_path, contact_id) == (["preparing"], "pending")


@pytest.mark.parametrize("stamp", ["garbage", "0001-01-01 00:00:00"])
def test_a_row_time_that_cannot_be_read_costs_the_pass_nothing(
    stamp, settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _update(
        db_path,
        "UPDATE email_log SET timestamp = ? WHERE delivery_status = 'preparing'",
        (stamp,),
    )
    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [BYSTANDER]
    assert _record(db_path, contact_id) == (["preparing"], "pending")
    # Nor a one-off run or a Direct Send, which settle outside the pass's guard.
    other = _Server()
    _process(settings, other).run_single(queries=["contract manufacturing"])
    ok, message = _process(settings, other).send_single_email(
        BYSTANDER, organization="Other Brand GmbH", confirm_repeat=True
    )
    assert ok, message


def test_a_step_time_out_of_range_costs_the_pass_nothing(settings_factory, tmp_path):
    """get_due_followups adds the delay to the stored time, which runs off the
    end of the calendar for a corrupt one."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    _follow_up_due(settings)
    _update(
        db_path, "UPDATE sequence_enrollments SET enrolled_at = '9999-12-31T23:59:59'"
    )
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == []


# ── Decision 1: the row carries the follow-up step it sent ─────────────────


def test_a_follow_up_row_carries_its_step_and_no_other_row_has_one(
    settings_factory, tmp_path
):
    settings = _settings(
        settings_factory, tmp_path, sequences_enabled=True, max_per_domain_per_day=10
    )
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    server = _Server()
    _one_pass(_process(settings, server))
    _set_due(db_path)
    _one_pass(_process(settings, server))
    ok, message = _process(settings, server).send_single_email(
        PERSON, organization="Brand GmbH", confirm_repeat=True
    )
    assert ok, message

    tags = _query(
        db_path,
        "SELECT sequence_step_id FROM email_log WHERE contact_id = ? ORDER BY id",
        (contact_id,),
    )

    first, second = _steps(db_path)
    assert tags == [(None,), (first,), (second,), (None,)]


def test_a_retry_row_carries_no_step(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _retry_due(settings)

    _one_pass(_process(settings, _Server()))

    assert _query(
        db_path,
        "SELECT sequence_step_id FROM email_log WHERE contact_id = ?",
        (contact_id,),
    ) == [(None,), (None,)]


# ── Decision 2: a step that may have gone out is moved on, not sent ────────

AFTER_THE_SEND = [
    ("db", "update_email_status"),
    ("db", "update_contact_status"),
    ("db", "log_activity"),
    ("db", "log_consent"),
    ("warmup", "record_send"),
    ("domain_throttle", "record_domain_send"),
]


def _die_after(a, owner_name, method, server, snapshot):
    owner = getattr(a, owner_name)
    assert owner is not None, owner_name
    real = getattr(owner, method)

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        if server.accepted:
            snapshot()
            raise _Killed
        return result

    setattr(owner, method, wrapper)


def _die_before_the_step_moves(a, server, snapshot):
    def wrapper(*_args, **_kwargs):
        assert server.accepted
        snapshot()
        raise _Killed

    a.sequences.advance_enrollment = wrapper


def _next_process_after_a_kill(settings, tmp_path, install):
    """A pass killed where ``install`` says, the database put back as committed
    then, and the next process: returns what that one sent."""
    db_path = settings.get_abs_db_path()
    at_the_kill = str(tmp_path / "at_the_kill.db")
    server = _Server()
    a = _process(settings, server)
    install(a, server, lambda: _copy_database(db_path, at_the_kill))
    try:
        _one_pass(a)
    except _Killed:
        _copy_database(at_the_kill, db_path)
    else:
        pytest.fail("the process was never killed")
    assert server.accepted == [PERSON]

    next_server = _Server()
    _one_pass(_process(settings, next_server))
    return next_server.accepted


@pytest.mark.parametrize("point", AFTER_THE_SEND, ids=lambda p: f"{p[0]}.{p[1]}")
@pytest.mark.parametrize(("send", "config", "make_due"), SENDS)
def test_a_kill_after_each_write_of_a_send_does_not_repeat_it(
    send, config, make_due, point, settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, **config)
    make_due(settings)

    accepted = _next_process_after_a_kill(
        settings,
        tmp_path,
        lambda a, server, snapshot: _die_after(a, *point, server, snapshot),
    )

    assert PERSON not in accepted, f"{send}: sent again after a kill at {point}"


def test_a_kill_just_before_the_step_moves_on_does_not_repeat_the_follow_up(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)

    accepted = _next_process_after_a_kill(
        settings, tmp_path, _die_before_the_step_moves
    )

    assert accepted == [], "the follow-up was sent again"
    assert _enrolment(db_path, contact_id) == [(1, "active")], (
        "the step that went out was not moved on"
    )


def test_a_step_that_could_not_be_moved_on_is_moved_on_by_the_next_pass(
    settings_factory, tmp_path, caplog
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    a = _process(settings, _Server())
    a.sequences.advance_enrollment = _locked

    _one_pass(a)

    assert "due again" not in caplog.text, "the log still says it will be sent again"
    server = _Server()
    _one_pass(_process(settings, server))
    assert server.accepted == [], "the follow-up was sent again"
    assert _enrolment(db_path, contact_id) == [(1, "active")]


@pytest.mark.parametrize(
    ("status", "sent_again"),
    [
        ("sent", False),
        ("sent_unconfirmed", False),
        ("failed", True),
        ("rejected", True),
        ("dry_run", True),
    ],
)
def test_only_a_row_that_may_have_gone_counts_as_the_step_having_gone(
    status, sent_again, settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _their_row(db_path, contact_id, status, step=_steps(db_path)[0])
    server = _Server()

    _one_pass(_process(settings, server))

    assert (PERSON in server.accepted) is sent_again, server.accepted
    assert _enrolment(db_path, contact_id) == [(1, "active")]


def test_a_later_enrolment_may_send_the_step_again(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _one_pass(_process(settings, _Server()))
    # They left the sequence, and an operator enrolled them again since.
    _update(
        db_path,
        "UPDATE sequence_enrollments SET status = 'cancelled', "
        "enrolled_at = datetime('now', '-20 days')",
    )
    _update(
        db_path,
        "UPDATE email_log SET timestamp = datetime('now', 'localtime', '-10 days') "
        "WHERE sequence_step_id IS NOT NULL",
    )
    _update(
        db_path,
        "INSERT INTO sequence_enrollments "
        "(contact_id, sequence_id, current_step, status, enrolled_at) "
        "SELECT contact_id, sequence_id, 0, 'active', datetime('now', '-4 days') "
        "FROM sequence_enrollments",
    )
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [PERSON], (
        "a row from before they were enrolled again stopped the step"
    )
    assert _enrolment(db_path, contact_id) == [(1, "cancelled"), (1, "active")]


def test_a_row_from_the_second_the_enrolment_began_counts(settings_factory, tmp_path):
    """The row's time is cut to the second, the enrolment's is not: compared
    as stored, a row from that same second would look older than the enrolment."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    then = clock.now().replace(microsecond=0) - timedelta(days=4)
    _their_row(db_path, contact_id, "sent", step=_steps(db_path)[0])
    _update(
        db_path,
        "UPDATE email_log SET timestamp = ? WHERE sequence_step_id IS NOT NULL",
        (then.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    _update(
        db_path,
        "UPDATE sequence_enrollments SET enrolled_at = ?",
        ((then + timedelta(milliseconds=500)).isoformat(),),
    )
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == []
    assert _enrolment(db_path, contact_id) == [(1, "active")]


def test_an_enrolment_time_without_an_offset_is_read_as_utc(
    settings_factory, tmp_path, default_zone_again
):
    """As get_due_followups reads it. The row went an hour after they were
    enrolled; read in this app's zone, twelve hours behind UTC, the enrolment
    would look like the later of the two, and the step would go again."""
    settings = _settings(
        settings_factory, tmp_path, sequences_enabled=True, app_timezone="Etc/GMT+12"
    )
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _their_row(db_path, contact_id, "sent", step=_steps(db_path)[0])
    then = clock.now() - timedelta(days=4) + timedelta(hours=1)
    _update(
        db_path,
        "UPDATE email_log SET timestamp = ? WHERE sequence_step_id IS NOT NULL",
        (then.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [], "the same follow-up went twice"
    assert _enrolment(db_path, contact_id) == [(1, "active")]


@pytest.mark.parametrize("stamp", ["garbage", "0001-01-01 00:00:00"])
def test_a_step_row_whose_time_cannot_be_read_counts_as_sent(
    stamp, settings_factory, tmp_path
):
    """Which can only err one way: never sent twice."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _their_row(db_path, contact_id, "sent", step=_steps(db_path)[0])
    _update(
        db_path,
        "UPDATE email_log SET timestamp = ? WHERE sequence_step_id IS NOT NULL",
        (stamp,),
    )
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == []
    assert _enrolment(db_path, contact_id) == [(1, "active")]


def test_a_dry_run_leaves_a_step_already_sent_where_it_is(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _their_row(db_path, contact_id, "sent", step=_steps(db_path)[0])
    rows = _record(db_path, contact_id)
    server = _Server()

    _dry_pass(_process(settings, server))

    assert server.accepted == []
    assert _enrolment(db_path, contact_id) == [(0, "active")]
    assert _record(db_path, contact_id) == rows


# ── Decision 3: settling moves on only the step on the row ─────────────────


def test_settling_does_not_mark_a_step_done_by_a_send_that_was_not_it(
    settings_factory, tmp_path
):
    """The operator's own Direct Send is cut off while step 0 is due. Moved on
    by settling, the follow-up would never go out."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _killed_at_acceptance(
        settings,
        tmp_path,
        run=lambda a: a.send_single_email(
            PERSON, organization="Brand GmbH", confirm_repeat=True
        ),
    )
    _age_the_cut_off_send(db_path)
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [PERSON], "the follow-up it was not was marked done"
    assert _enrolment(db_path, contact_id) == [(1, "active")]


def test_settling_leaves_a_stopped_sequence_stopped(settings_factory, tmp_path):
    """They replied while their last follow-up was held. The reply ended the
    sequence, and settling that follow-up must not turn it into 'completed'."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    server = _Server()
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON]
    _set_due(db_path)
    _killed_at_acceptance(settings, tmp_path)
    _update(
        db_path, "UPDATE contacts SET status = 'responded' WHERE id = ?", (contact_id,)
    )
    _one_pass(_process(settings, _Server()))
    assert _enrolment(db_path, contact_id) == [(1, "cancelled")]
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _record(db_path, contact_id) == (
        ["sent", "sent", "sent_unconfirmed"],
        "responded",
    )
    assert _enrolment(db_path, contact_id) == [(1, "cancelled")], (
        "settling rewrote a sequence their reply had ended"
    )


def test_a_process_that_outlives_the_bound_does_not_move_the_step_twice(
    settings_factory, tmp_path
):
    """A sleeping laptop: the row looks old, another process settles it, and
    this one wakes, finishes the send and moves on the same step."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)

    def another_process_wakes_first():
        _age_the_cut_off_send(db_path)
        _process(settings, _Server())._reconcile_interrupted_sends()

    server = _Server(kill=another_process_wakes_first)
    _one_pass(_process(settings, server))

    assert server.accepted == [PERSON]
    assert _enrolment(db_path, contact_id) == [(1, "active")], "a step was skipped"


def test_a_step_that_fell_due_in_the_second_its_row_was_written_moves_on(
    settings_factory, tmp_path
):
    """Due at T+0.5 s, its row written at T+0.7 s and stored as T: the row reads
    as older than the step it sent. The tag, not the time, says which it was."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    then = (clock.now() - timedelta(minutes=27)).replace(microsecond=0)
    _update(
        db_path,
        "UPDATE email_log SET timestamp = ? WHERE delivery_status = 'preparing'",
        (then.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    enrolled = then + timedelta(milliseconds=500) - timedelta(days=3)
    _update(
        db_path,
        "UPDATE sequence_enrollments SET enrolled_at = ?",
        (enrolled.isoformat(),),
    )
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [], "the settled follow-up was sent again"
    assert _enrolment(db_path, contact_id) == [(1, "active")]


def test_settling_is_not_fooled_by_a_step_time_it_cannot_read(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _update(db_path, "UPDATE sequence_enrollments SET enrolled_at = 'garbage'")

    _one_pass(_process(settings, _Server()))

    assert _enrolment(db_path, contact_id) == [(1, "active")]


@pytest.fixture
def engine_at_the_first_step(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    engine = SequenceEngine(db=db)
    contact_id = db.add_contact(db.log_search("q", source="test"), PERSON)
    sequence_id = engine.create_sequence(
        "followups",
        [
            {"delay_days": 3, "subject": "Following up", "template_name": "a"},
            {"delay_days": 7, "subject": "One more", "template_name": "b"},
        ],
    )
    return engine, engine.enroll_contact(contact_id, sequence_id)


def _step_and_status(engine, enrollment_id):
    return tuple(
        engine.db.conn.execute(
            "SELECT current_step, status FROM sequence_enrollments WHERE id = ?",
            (enrollment_id,),
        ).fetchone()
    )


def test_moving_a_step_on_is_compare_and_set(engine_at_the_first_step):
    """Moved on again from the step it has left, it stays, and the next step's
    delay is not run from now."""
    engine, enrollment = engine_at_the_first_step
    engine.advance_enrollment(enrollment, 0)
    long_ago = "2000-01-01T00:00:00+00:00"
    engine.db.conn.execute(
        "UPDATE sequence_enrollments SET last_sent_at = ?", (long_ago,)
    )
    engine.db.conn.commit()

    engine.advance_enrollment(enrollment, 0)

    assert _step_and_status(engine, enrollment) == (1, "active")
    assert (
        engine.db.conn.execute(
            "SELECT last_sent_at FROM sequence_enrollments"
        ).fetchone()[0]
        == long_ago
    )


def test_completing_a_sequence_is_compare_and_set(engine_at_the_first_step):
    engine, enrollment = engine_at_the_first_step

    engine.advance_enrollment(enrollment, 1)

    assert _step_and_status(engine, enrollment) == (0, "active"), (
        "the last step was finished by a caller that did not send it"
    )
    engine.advance_enrollment(enrollment, 0)
    engine.advance_enrollment(enrollment, 1)
    assert _step_and_status(engine, enrollment) == (1, "completed")


# ── Decision 4: a due list read earlier cannot re-send ─────────────────────


def _two_follow_ups_due(settings):
    """The bystander's follow-up first, then the person's."""
    db_path = settings.get_abs_db_path()
    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    _one_pass(_process(settings, _Server()))
    contact_id = _lead(db_path, PERSON, "Brand GmbH")
    _one_pass(_process(settings, _Server()))
    enrolled = _update(
        db_path,
        "UPDATE sequence_enrollments SET enrolled_at = datetime('now', '-4 days')",
    )
    assert enrolled == 2
    return contact_id


@pytest.mark.parametrize(
    "change",
    [
        pytest.param("status = 'cancelled'", id="left-the-sequence"),
        pytest.param("current_step = 1", id="moved-on"),
    ],
)
def test_a_due_list_older_than_the_enrolment_does_not_send(
    change, settings_factory, tmp_path
):
    """The batch is paced, so its list can be hours old by the time it reaches
    an item. Another process changes the enrolment, and leaves no row for it."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _two_follow_ups_due(settings)
    server = _Server()
    a = _process(settings, server)
    changed = []

    def paced(_seconds):
        if server.accepted and not changed:
            changed.append(
                _update(
                    db_path,
                    f"UPDATE sequence_enrollments SET {change} WHERE contact_id = ?",
                    (contact_id,),
                )
            )

    a._interruptible_sleep = paced

    _one_pass(a)

    assert changed == [1]
    assert server.accepted == [BYSTANDER]


def test_a_send_settled_during_the_batch_is_not_sent_again(settings_factory, tmp_path):
    """The kill, then a new process whose due list holds both follow-ups. In the
    send delay after the first, the operator's Direct Send settles the second."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _two_follow_ups_due(settings)
    _update(
        db_path,
        "UPDATE sequence_enrollments SET enrolled_at = datetime('now') "
        "WHERE contact_id != ?",
        (contact_id,),
    )
    killed = _killed_at_acceptance(settings, tmp_path)
    assert killed.accepted == [PERSON]
    _update(
        db_path,
        "UPDATE sequence_enrollments SET enrolled_at = datetime('now', '-4 days') "
        "WHERE contact_id != ?",
        (contact_id,),
    )
    server = _Server()
    a = _process(settings, server)
    settled = []

    def paced(_seconds):
        if server.accepted and not settled:
            settled.append(True)
            _age_the_cut_off_send(db_path)
            a._reconcile_interrupted_sends()

    a._interruptible_sleep = paced

    _one_pass(a)

    assert server.accepted == [BYSTANDER], "the same follow-up went twice"
    assert _enrolment(db_path, contact_id) == [(1, "active")], "a step was skipped"


# ── Decision 5: a send the server refused stays refused ────────────────────

REFUSALS = {
    "greylisted": (
        smtplib.SMTPRecipientsRefused({PERSON: (451, b"4.7.1 Try later")}),
        "failed",
        "failed",
        [("pending", 0)],
    ),
    "content-filter": (
        smtplib.SMTPDataError(554, b"5.7.1 Rejected by content filter"),
        "failed",
        "failed",
        [("pending", 0)],
    ),
    "login": (
        smtplib.SMTPAuthenticationError(535, b"5.7.3 Authentication failed"),
        "failed",
        "pending",
        [],
    ),
    "unknown-address": (
        smtplib.SMTPRecipientsRefused({PERSON: (550, b"5.1.1 No such user")}),
        "rejected",
        "bounced",
        [],
    ),
}


@pytest.mark.parametrize("why", REFUSALS)
def test_a_refused_send_whose_result_met_a_locked_database_stays_refused(
    why, settings_factory, tmp_path
):
    error, row, contact, retries = REFUSALS[why]
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    a = _process(settings, _Server(refuse=error))
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)

    _backlog_only(a)

    # Everything else it writes works as when the first write succeeds.
    assert _record(db_path, contact_id) == (["preparing"], contact)
    assert _retries(db_path, contact_id) == retries
    assert a.db.is_suppressed(PERSON) is (why == "unknown-address")

    # By now older than any live send could take: written, not settled.
    _age_the_cut_off_send(db_path)
    assert a._reconcile_interrupted_sends() == []

    assert _record(db_path, contact_id) == ([row], contact)
    ((response, reason),) = _query(
        db_path, "SELECT smtp_response, error_message FROM email_log"
    )
    assert response and response == reason, "the server's answer was lost"
    assert _reported(db_path) == []


@pytest.mark.parametrize("why", REFUSALS)
def test_a_real_lock_on_every_write_of_a_refused_send_costs_the_batch_nothing(
    why, settings_factory, tmp_path, monkeypatch, caplog
):
    error, row, _contact, _retries_left = REFUSALS[why]
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _rank(db_path, PERSON, 90)
    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    _rank(db_path, BYSTANDER, 10)
    _impatient(monkeypatch)
    lock = _WriteLock(db_path)
    server = _Server(refuse=error, on_refuse=lock.take, spared=(BYSTANDER,))
    a = _process(settings, server)
    a._interruptible_sleep = lambda _seconds: lock.release()

    try:
        _one_pass(a)
    finally:
        lock.release()

    # No LOOP_ERROR row can be written under the lock, so the log says it.
    assert "Loop #1 failed" not in caplog.text, "a write took the pass down"
    # A login failure halts the batch, as it does when the write succeeds.
    assert server.accepted == ([] if why == "login" else [BYSTANDER])
    assert _record(db_path, contact_id) == ([row], "pending")
    assert _reported(db_path) == []

    next_server = _Server()
    _one_pass(_process(settings, next_server))
    assert PERSON in next_server.accepted, "they never got their first email"


def test_an_email_that_could_not_be_built_stays_refused(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    server = _Server()
    a = _process(settings, server)

    def unbuildable(**_kwargs):
        raise ValueError("a header the email policy refuses")

    a.email_sender.create_email = unbuildable
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)

    _backlog_only(a)

    assert _record(db_path, contact_id) == (["preparing"], "failed")
    assert a._reconcile_interrupted_sends() == []
    assert _record(db_path, contact_id) == (["failed"], "failed")
    ((reason,),) = _query(db_path, "SELECT error_message FROM email_log")
    assert "Failed to create email" in reason


def test_a_real_lock_on_every_write_of_an_unbuildable_email_costs_the_batch_nothing(
    settings_factory, tmp_path, monkeypatch
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _rank(db_path, PERSON, 90)
    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    _rank(db_path, BYSTANDER, 10)
    _impatient(monkeypatch)
    lock = _WriteLock(db_path)
    server = _Server()
    a = _process(settings, server)
    build = a.email_sender.create_email

    def unbuildable_for_them(**kwargs):
        if kwargs["to_email"] == PERSON:
            lock.take()
            raise ValueError("a header the email policy refuses")
        return build(**kwargs)

    a.email_sender.create_email = unbuildable_for_them
    a._interruptible_sleep = lambda _seconds: lock.release()

    try:
        _one_pass(a)
    finally:
        lock.release()

    assert server.accepted == [BYSTANDER]
    assert _record(db_path, contact_id) == (["failed"], "pending")
    assert _reported(db_path) == []


def test_a_kept_result_is_written_when_a_one_off_run_finishes(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    error = REFUSALS["greylisted"][0]
    a = _process(settings, _Server(refuse=error))
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)

    a.run_single(queries=["contract manufacturing"])

    assert _record(db_path, contact_id) == (["failed"], "failed")


def test_a_kept_result_is_written_when_the_loop_stops(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    error = REFUSALS["greylisted"][0]
    a = _process(settings, _Server(refuse=error))
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)

    _one_pass(a)

    assert _record(db_path, contact_id) == (["failed"], "failed")
    assert _held_on_disk(tmp_path) == [], "the written result stayed held"


def test_a_kept_result_is_written_by_any_agent_in_the_process(
    settings_factory, tmp_path
):
    """The dashboard holds two: Direct Send's, and the running campaign's."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    error = REFUSALS["greylisted"][0]
    a = _process(settings, _Server(refuse=error))
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)
    _backlog_only(a)
    assert _record(db_path, contact_id)[0] == ["preparing"]

    _process(settings, _Server())._reconcile_interrupted_sends()

    assert _record(db_path, contact_id)[0] == ["failed"]


def test_a_kept_result_waits_out_a_lock_and_holds_the_contact_meanwhile(
    settings_factory, tmp_path, monkeypatch
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    error = REFUSALS["greylisted"][0]
    a = _process(settings, _Server(refuse=error))
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)
    _backlog_only(a)
    _impatient(monkeypatch)
    lock = _WriteLock(db_path)
    lock.take()

    try:
        assert a._reconcile_interrupted_sends() == []
        assert _record(db_path, contact_id)[0] == ["preparing"], "the lock lost it"
        assert a.db.send_in_doubt(contact_id)
    finally:
        lock.release()

    a._reconcile_interrupted_sends()
    assert _record(db_path, contact_id)[0] == ["failed"]


def test_a_row_whose_result_is_known_is_not_settled(settings_factory, tmp_path):
    """The write that would have told failed, and the lock ended just after."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    error = REFUSALS["greylisted"][0]
    a = _process(settings, _Server(refuse=error))
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)
    _backlog_only(a)
    _age_the_cut_off_send(db_path)
    a.db.write_kept_send_results = lambda: None

    assert a._reconcile_interrupted_sends() == []

    assert _record(db_path, contact_id) == (["preparing"], "failed")
    assert _reported(db_path) == []


def test_a_kept_result_does_not_overwrite_a_row_another_process_settled(
    settings_factory, tmp_path
):
    """A process that knows nothing of this memory, after the bound. Its contact
    and follow-ups already say emailed, so the row stays with them."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    error = REFUSALS["greylisted"][0]
    a = _process(settings, _Server(refuse=error))
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)
    _backlog_only(a)
    settled = _update(
        db_path,
        "UPDATE email_log SET delivery_status = 'sent_unconfirmed' "
        "WHERE delivery_status = 'preparing'",
    )
    assert settled == 1

    a._reconcile_interrupted_sends()

    assert _record(db_path, contact_id)[0] == ["sent_unconfirmed"]
    assert _held_on_disk(tmp_path) == [], "a result no longer needed stayed held"


def _held_on_disk(tmp_path):
    """What is held on disk beside the database about refused sends, as text."""
    folder = tmp_path / "spool" / "refused_sends"
    if not folder.is_dir():
        return []
    return [p.read_text(encoding="utf-8") for p in sorted(folder.iterdir())]


def _its_process_is_gone(monkeypatch):
    """The process that kept a result ended, or sits idle: another process has
    none of its memory, only what reached the disk."""
    monkeypatch.setattr(database_module, "_kept_send_results", {})


def _refused_under_a_lock(settings, how, why="greylisted"):
    """A send the server refused, whose result met a locked database."""
    a = _process(settings, _Server(refuse=REFUSALS[why][0]))
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)
    if how == "direct-send":
        ok, _message = a.send_single_email(PERSON, organization="Brand GmbH")
        assert not ok
    else:
        _backlog_only(a)
    return a


@pytest.mark.parametrize("how", ["direct-send", "first-email"])
def test_a_refused_send_whose_process_is_gone_is_not_settled_as_sent(
    how, settings_factory, tmp_path, monkeypatch
):
    """The dashboard that made the Direct Send sits idle after the lock clears,
    or the run that kept the result ended under it. Half an hour on, another
    process must not take the row for an email that may have gone."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _refused_under_a_lock(settings, how)
    assert _record(db_path, contact_id)[0] == ["preparing"]
    _its_process_is_gone(monkeypatch)
    _age_the_cut_off_send(db_path)

    _process(settings, _Server())._reconcile_interrupted_sends()

    assert _reported(db_path) == [], "a refused send was settled as possibly sent"
    statuses, contact = _record(db_path, contact_id)
    assert statuses == ["failed"]
    assert contact != "emailed"
    assert _enrolment(db_path, contact_id) == [], "follow-ups for an unsent email"
    assert _held_on_disk(tmp_path) == [], "the written result stayed held"


def test_a_refused_send_whose_process_is_gone_is_not_settled_under_a_lock(
    settings_factory, tmp_path, monkeypatch
):
    """The process left, and the database is locked again when another one
    looks: the row waits, unsettled, for a write to get through."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _refused_under_a_lock(settings, "first-email")
    _its_process_is_gone(monkeypatch)
    _age_the_cut_off_send(db_path)
    b = _process(settings, _Server())
    b.db.write_kept_send_results = lambda: None

    assert b._reconcile_interrupted_sends() == []

    assert _record(db_path, contact_id)[0] == ["preparing"]
    assert _reported(db_path) == []


def test_a_refused_send_held_on_disk_keeps_no_address(
    settings_factory, tmp_path, monkeypatch
):
    """What is held outside the database is out of an erasure's reach, so it
    says only that the send did not go out: the server's answer, which names
    the address, stays in this process's memory."""
    settings = _settings(settings_factory, tmp_path)
    _first_email_due(settings)
    _refused_under_a_lock(settings, "first-email", why="unknown-address")

    held = _held_on_disk(tmp_path)

    assert len(held) == 1, held
    assert "brand.de" not in held[0], held


def test_a_refused_follow_up_whose_result_met_a_lock_is_not_skipped(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    error = REFUSALS["greylisted"][0]
    a = _process(settings, _Server(refuse=error))
    a.db.update_email_status = _LockedOnce(a.db.update_email_status)
    _one_pass(a)
    assert _record(db_path, contact_id)[0] == ["sent", "failed"]
    assert _enrolment(db_path, contact_id) == [(0, "active")]
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [PERSON], "the refused step was skipped"


def test_a_refused_direct_send_still_reads_as_failed_with_the_database_locked(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    error = REFUSALS["greylisted"][0]
    a = _process(settings, _Server(refuse=error))
    a.db.update_email_status = _locked

    ok, message = a.send_single_email(PERSON, organization="Brand GmbH")

    assert not ok
    assert "Could not send" in message, message


def test_a_lost_preparing_log_line_does_not_stop_a_send_it_has_claimed(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    server = _Server()
    a = _process(settings, server)
    real = a.db.log_activity

    def locked_for_that_line(action, *args, **kwargs):
        if action == "EMAIL_PREPARING":
            raise sqlite3.OperationalError("database is locked")
        return real(action, *args, **kwargs)

    a.db.log_activity = locked_for_that_line

    _backlog_only(a)

    assert server.accepted == [PERSON]
    assert _record(db_path, contact_id) == (["sent"], "emailed")


# ── Decision 6: the row is the claim ───────────────────────────────────────


def _they_write_first(a, db_path, contact_id, status, step=None):
    """Another process passes every check too, and commits its row first."""
    real = a._process_contact
    written = []

    def just_after_the_check(contact, *args, **kwargs):
        if contact.get("id") == contact_id and not written:
            written.append(True)
            _their_row(db_path, contact_id, status, step=step)
        return real(contact, *args, **kwargs)

    a._process_contact = just_after_the_check


def _preparing_lines(db_path):
    return _query(
        db_path, "SELECT COUNT(*) FROM activity_log WHERE action = 'EMAIL_PREPARING'"
    )[0][0]


@pytest.mark.parametrize(("send", "config", "make_due"), SENDS)
def test_a_lost_claim_is_a_refusal_and_writes_nothing_about_the_contact(
    send, config, make_due, settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, **config)
    db_path = settings.get_abs_db_path()
    contact_id = make_due(settings)
    rows, status = _record(db_path, contact_id)
    enrolment, retries = HELD[send]
    lines = _preparing_lines(db_path)
    server = _Server()
    a = _process(settings, server)
    _they_write_first(a, db_path, contact_id, "preparing")

    _one_pass(a)

    assert server.accepted == [], f"the {send} went out beside theirs"
    assert _record(db_path, contact_id) == ([*rows, "preparing"], status)
    assert _enrolment(db_path, contact_id) == enrolment
    assert _retries(db_path, contact_id) == retries
    assert a._stats["emails_failed"] == 0, "a lost claim was counted as a failure"
    assert a._last_dispatch == 0, "a lost claim kept the send delay"
    assert _preparing_lines(db_path) == lines, "the activity log says it was sent"


def test_a_lost_claim_on_a_direct_send_says_so(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    server = _Server()
    a = _process(settings, server)
    _they_write_first(a, db_path, contact_id, "preparing")

    ok, message = a.send_single_email(PERSON, organization="Brand GmbH")

    assert (ok, server.accepted) == (False, []), message
    assert UNSETTLED in message and PERSON in message, message
    assert _record(db_path, contact_id) == (["preparing"], "pending")


@pytest.mark.parametrize(
    ("send", "config", "make_due"),
    [p for p in SENDS if p.id in ("first-email", "retry")],
)
@pytest.mark.parametrize("theirs", ["sent", "sent_unconfirmed"])
def test_a_first_email_or_retry_is_not_sent_over_a_row_that_says_sent(
    theirs, send, config, make_due, settings_factory, tmp_path
):
    """Theirs went out, or ended unconfirmed: both may have reached them."""
    settings = _settings(settings_factory, tmp_path, **config)
    db_path = settings.get_abs_db_path()
    contact_id = make_due(settings)
    enrolment, retries = HELD[send]
    server = _Server()
    a = _process(settings, server)
    _they_write_first(a, db_path, contact_id, theirs)

    _one_pass(a)

    assert server.accepted == [], f"the {send} went out twice"
    assert _retries(db_path, contact_id) == retries, "the lost claim spent the retry"
    assert _enrolment(db_path, contact_id) == enrolment


def test_a_follow_up_is_not_sent_over_a_row_for_its_step(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    server = _Server()
    a = _process(settings, server)
    _they_write_first(a, db_path, contact_id, "sent", step=_steps(db_path)[0])

    _one_pass(a)

    assert server.accepted == [], "the same follow-up went out twice"
    assert _enrolment(db_path, contact_id) == [(0, "active")]


def test_a_follow_up_goes_out_beside_a_row_for_another_step(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    server = _Server()
    a = _process(settings, server)
    _they_write_first(a, db_path, contact_id, "sent", step=_steps(db_path)[1])

    _one_pass(a)

    assert server.accepted == [PERSON], "another step's row stopped this one"


def test_every_contact_due_for_a_step_gets_it(settings_factory, tmp_path):
    """The step going to one person says nothing about the next one."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _two_follow_ups_due(settings)
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == [BYSTANDER, PERSON], (
        "the first person's follow-up stopped the second person's"
    )
    assert _enrolment(db_path, contact_id) == [(1, "active")]


@pytest.mark.parametrize("enroll_followup", [False, True])
def test_a_direct_send_may_follow_a_sent_email(
    enroll_followup, settings_factory, tmp_path
):
    """The operator's click never asks whether the first email already went:
    it refuses only a send that has not settled. With or without the follow-ups
    ticked, which is what a send's first email is otherwise told apart by."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    server = _Server()
    a = _process(settings, server)
    _they_write_first(a, db_path, contact_id, "sent")

    ok, message = a.send_single_email(
        PERSON, organization="Brand GmbH", enroll_followup=enroll_followup
    )

    assert ok, message
    assert server.accepted == [PERSON]


def test_a_dry_run_does_not_claim(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _their_row(db_path, contact_id, "preparing")
    sender = _process(settings, _Server()).email_sender

    result = sender.dispatch_to_contact(
        contact={"id": contact_id, "email": PERSON},
        subject="s",
        html_body="<p>h</p>",
        plain_body="h",
        dry_run=True,
    )

    assert result, "a dry run lost a claim it should never have made"
    assert _record(db_path, contact_id)[0] == ["preparing", "dry_run"]


def test_a_claim_reads_under_the_write_lock(db):
    """Their row is written and not yet committed when this claim starts. Read
    first and written later, the claim would see nothing and insert a second."""
    contact_id = db.add_contact(db.log_search("q"), PERSON)
    theirs = sqlite3.connect(db.db_path, timeout=30, check_same_thread=False)
    theirs.execute("BEGIN IMMEDIATE")
    theirs.execute(
        "INSERT INTO email_log (contact_id, recipient_email, subject, delivery_status) "
        "VALUES (?, ?, 'Theirs', 'preparing')",
        (contact_id, PERSON),
    )
    won = []
    claiming = threading.Event()
    real = db._transaction

    @contextmanager
    def noticed():
        with real() as conn:
            claiming.set()
            yield conn

    db._transaction = noticed

    def claim():
        won.append(db.claim_send(contact_id, PERSON, "Mine"))

    thread = threading.Thread(target=claim)
    thread.start()
    assert claiming.wait(timeout=30), "the claim never started"
    time.sleep(0.5)
    theirs.commit()
    theirs.close()
    thread.join()

    assert won == [None], "both claims were won"


class _TakesItsTime(_Server):
    """A server that holds the message until ``answer`` is set, as a real one
    keeps a send 'preparing' for as long as its exchanges take.

    The second request sets ``answer`` once it is refused. When it is held up
    before its claim until the first is done, nothing sets it, so the server
    gives up after 2 s: it waited 10, which cost the test 10 s per address.
    """

    answer: threading.Event

    def send_message(self, msg):
        self.answer.wait(timeout=2)
        return super().send_message(msg)


def test_two_direct_sends_for_one_address_send_it_once(settings_factory, tmp_path):
    """Two requests at once, and the first still at the server when the second
    reaches its claim, which is what a real server's seconds make of a double
    click. Without the claim, the second sends behind the first's lock."""
    settings = _settings(settings_factory, tmp_path, warmup_enabled=False)
    server = _TakesItsTime()
    a = _process(settings, server)
    results = []
    addresses = [f"lead{i}@brand{i}.de" for i in range(3)]
    for address in addresses:
        start = threading.Barrier(2)
        server.answer = threading.Event()

        def click(address=address, start=start, answer=server.answer):
            start.wait()
            results.append(a.send_single_email(address, organization="Brand GmbH"))
            a.db.close_thread_conn()
            answer.set()

        threads = [threading.Thread(target=click) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert sorted(server.accepted) == sorted(addresses), results
    assert [ok for ok, _message in results].count(True) == len(addresses), results


class _ClaimedAfterTheRead:
    """A connection on which another process claims the rows just after this
    one has read them."""

    def __init__(self, conn, db_path):
        self._conn = conn
        self._db_path = db_path

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        cursor = self._conn.execute(sql, *args)
        if sql.lstrip().startswith("SELECT"):
            rows = cursor.fetchall()
            _update(self._db_path, "UPDATE retry_queue SET status = 'in_flight'")
            return MagicMock(fetchall=lambda: rows)
        return cursor


def test_a_retry_another_process_claimed_first_is_not_handed_out(db):
    queue = RetryQueue(db)
    contact_id = db.add_contact(db.log_search("q"), PERSON)
    row = db.log_email(contact_id, PERSON, "s", delivery_status="failed")
    queue.add_to_retry(row, contact_id, "Initial send failed")
    _update(db.db_path, "UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    real = db._transaction

    @contextmanager
    def losing_the_race():
        with real() as conn:
            yield _ClaimedAfterTheRead(conn, db.db_path)

    db._transaction = losing_the_race

    assert queue.get_due_retries() == [], "a row another process won was handed out"


# ── Decision 7: Direct Send after the bound ────────────────────────────────


def test_a_direct_send_that_settles_its_own_cut_off_send_is_refused_and_says_so(
    settings_factory, tmp_path
):
    """The dashboard dies mid-send, and the operator retries half an hour later.
    Nothing has told them yet: this click is what settles the row."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _killed_at_acceptance(
        settings,
        tmp_path,
        run=lambda a: a.send_single_email(PERSON, organization="Brand GmbH"),
    )
    ((contact_id,),) = _query(
        db_path, "SELECT id FROM contacts WHERE email = ?", (PERSON,)
    )
    _age_the_cut_off_send(db_path)
    server = _Server()

    ok, message = _process(settings, server).send_single_email(
        PERSON, organization="Brand GmbH"
    )

    assert (ok, server.accepted) == (False, []), message
    assert "was cut off" in message and PERSON in message, message
    assert "has just been recorded as unconfirmed" in message, message
    assert _record(db_path, contact_id) == (["sent_unconfirmed"], "emailed")
    assert len(_reported(db_path)) == 1

    # The next click is a decision made knowing about it.
    ok, message = _process(settings, server).send_single_email(
        PERSON, organization="Brand GmbH", confirm_repeat=True
    )

    assert ok, message
    assert server.accepted == [PERSON]
    assert _record(db_path, contact_id) == (["sent_unconfirmed", "sent"], "emailed")
    assert len(_reported(db_path)) == 1


def test_settling_someone_else_does_not_refuse_this_click(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    server = _Server()

    ok, message = _process(settings, server).send_single_email(
        BYSTANDER, organization="Other Brand GmbH"
    )

    assert ok, message
    assert server.accepted == [BYSTANDER]
    assert len(_reported(db_path)) == 1


def test_a_direct_send_waits_for_a_send_that_has_not_settled(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    server = _Server()

    ok, message = _process(settings, server).send_single_email(
        PERSON, organization="Brand GmbH"
    )

    assert (ok, server.accepted) == (False, []), message
    assert UNSETTLED in message and PERSON in message, message
    assert _record(db_path, contact_id) == (["preparing"], "pending")


def test_a_preview_is_not_refused_while_a_send_is_held(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    server = _Server()

    ok, message = _process(settings, server).send_single_email(
        PERSON, organization="Brand GmbH", dry_run=True
    )

    assert ok, message
    assert server.accepted == []


def test_a_reply_ends_the_follow_ups_even_while_the_send_is_held(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _follow_up_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _update(
        db_path, "UPDATE contacts SET status = 'responded' WHERE id = ?", (contact_id,)
    )

    _one_pass(_process(settings, _Server()))

    ((_step, status),) = _enrolment(db_path, contact_id)
    assert status == "cancelled"


# ── Decision 9: a process that knows nothing went out says so ──────────────


def _stopped_while_connecting(a):
    def ctrl_c():
        raise KeyboardInterrupt

    for _key, _name, sender in a.senders:
        sender._new_connection = ctrl_c


@pytest.mark.parametrize(("send", "config", "make_due"), SENDS)
def test_a_stop_before_anything_reached_the_server_is_recorded_as_failed(
    send, config, make_due, settings_factory, tmp_path
):
    """Ctrl+C while connecting: the live process knows nothing was transmitted.
    The row says so, and the person is not recorded as emailed for good."""
    settings = _settings(settings_factory, tmp_path, **config)
    db_path = settings.get_abs_db_path()
    contact_id = make_due(settings)
    rows, status = _record(db_path, contact_id)
    a = _process(settings, _Server())
    _stopped_while_connecting(a)

    _one_pass(a)

    assert _record(db_path, contact_id) == ([*rows, "failed"], status)
    enrolment, retries = HELD[send]
    assert _enrolment(db_path, contact_id) == enrolment
    assert _retries(db_path, contact_id) == retries
    server = _Server()
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON], f"the {send} never went out"


@pytest.mark.parametrize(("send", "config", "make_due"), SENDS)
def test_a_stop_after_the_message_went_to_the_server_stays_in_doubt(
    send, config, make_due, settings_factory, tmp_path
):
    """Ctrl+C at acceptance. The process cannot know, so nothing is written, and
    nothing is sent again."""
    settings = _settings(settings_factory, tmp_path, **config)
    db_path = settings.get_abs_db_path()
    contact_id = make_due(settings)

    def ctrl_c():
        raise KeyboardInterrupt

    server = _Server(kill=ctrl_c)
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON]
    assert _record(db_path, contact_id)[0][-1] == "preparing"

    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    next_server = _Server()
    _one_pass(_process(settings, next_server))
    assert next_server.accepted == [BYSTANDER]

    _age_the_cut_off_send(db_path)
    last = _Server()
    _one_pass(_process(settings, last))
    assert last.accepted == []
    assert _record(db_path, contact_id)[0][-1] == "sent_unconfirmed"


def test_a_stop_leaves_a_row_another_process_has_settled(db):
    """It slept past the bound, and woke to Ctrl+C before it reached the server."""
    contact_id = db.add_contact(db.log_search("q"), PERSON)
    row = db.log_email(contact_id, PERSON, "s", delivery_status="sent_unconfirmed")
    db.set_email_message_id(row, "<one@suppleco.test>")

    db.record_stopped_send("<one@suppleco.test>")

    assert _record(db.db_path, contact_id)[0] == ["sent_unconfirmed"]


def test_a_stop_whose_write_meets_a_lock_still_stops(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    a = _process(settings, _Server())
    _stopped_while_connecting(a)
    a.db.record_stopped_send = _locked

    with pytest.raises(KeyboardInterrupt):
        a._running = True
        a._process_pending_contacts(False)

    a.db.close_thread_conn()
    assert _record(db_path, contact_id)[0] == ["preparing"]


# ── Decision 10: settling leaves what an unconfirmed send leaves ───────────

# Different on every run of the same send, in either way of ending.
VOLATILE = {
    "timestamp",
    "qualified_at",
    "tracking_id",
    "message_id",
    "enrolled_at",
    "last_sent_at",
    "completed_at",
    "next_retry_at",
    "created_at",
    "updated_at",
}
# An intended difference: the row's own note of how it was settled, in place of
# the server's last words. The other is the report, read apart by _activity:
# settling writes EMAIL_INTERRUPTED where the send's own process would have
# written EMAIL_UNCONFIRMED.
INTENDED_ON_THE_ROW = {"smtp_response"}


def _activity(db_path):
    """How many activity rows of each action, but for the one line a killed
    process never gets to: its request to stop."""
    return dict(
        _query(
            db_path,
            "SELECT action, COUNT(*) FROM activity_log "
            "WHERE action != 'AGENT_STOP_REQUESTED' GROUP BY action",
        )
    )


def _whole_database(db_path):
    """Every table but the activity log, row by row, as (column, value) pairs."""
    tables = _query(
        db_path,
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' AND name != 'activity_log' ORDER BY name",
    )
    dump = {}
    for (table,) in tables:
        columns = [c[1] for c in _query(db_path, f"PRAGMA table_info({table})")]
        dropped = VOLATILE | (INTENDED_ON_THE_ROW if table == "email_log" else set())
        dump[table] = sorted(
            repr([(c, v) for c, v in zip(columns, row) if c not in dropped])
            for row in _query(db_path, f"SELECT * FROM {table}")
        )
    return dump


@pytest.mark.parametrize(("send", "config", "make_due"), SENDS)
def test_a_settled_send_leaves_what_a_real_unconfirmed_send_leaves(
    send, config, make_due, settings_factory, tmp_path
):
    """Whole tables, not chosen fields. A real unconfirmed send is a server that
    takes the message and drops the connection. The same send, killed, settled."""
    (tmp_path / "unconfirmed").mkdir()
    (tmp_path / "settled").mkdir()
    config = {**config, "sequences_enabled": True, "OPEN_TRACKING_ENABLED": True}

    settings = _settings(settings_factory, tmp_path / "unconfirmed", **config)
    real_db = settings.get_abs_db_path()
    make_due(settings)
    _one_pass(_process(settings, _DropsAfterAccepting()))
    _one_pass(_process(settings, _Server()))

    settings = _settings(settings_factory, tmp_path / "settled", **config)
    settled_db = settings.get_abs_db_path()
    make_due(settings)
    _killed_at_acceptance(settings, tmp_path / "settled")
    _age_the_cut_off_send(settled_db)
    _one_pass(_process(settings, _Server()))

    assert _whole_database(settled_db) == _whole_database(real_db)
    real_activity, settled_activity = _activity(real_db), _activity(settled_db)
    assert real_activity.pop("EMAIL_UNCONFIRMED") == 1
    assert settled_activity.pop("EMAIL_INTERRUPTED") == 1
    assert settled_activity == real_activity


def test_opens_on_a_settled_send_are_recorded(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, OPEN_TRACKING_ENABLED=True)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _one_pass(_process(settings, _Server()))
    ((tracking_id,),) = _query(db_path, "SELECT tracking_id FROM email_log")
    assert tracking_id, "this send carried no pixel, so this shows nothing"

    Analytics(Database(db_path)).record_open(tracking_id)

    assert _query(db_path, "SELECT opened_at IS NOT NULL FROM email_log") == [(1,)]


def test_a_tracked_send_whose_result_met_a_lock_settles_with_its_one_tracking_row(
    settings_factory, tmp_path
):
    """Its own process went on and registered the tracking id. A second insert
    would fail the settle, and the row would stay held for good."""
    settings = _settings(settings_factory, tmp_path, OPEN_TRACKING_ENABLED=True)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    a = _process(settings, _Server())
    a.db.update_email_status = _locked
    _one_pass(a)
    assert _query(db_path, "SELECT COUNT(*) FROM open_tracking") == [(1,)]
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _record(db_path, contact_id) == (["sent_unconfirmed"], "emailed")
    assert _query(db_path, "SELECT COUNT(*) FROM open_tracking") == [(1,)]


def test_a_settled_send_is_counted_on_the_day_it_was_made(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path, timedelta(days=3))
    ((day,),) = _query(db_path, "SELECT substr(timestamp, 1, 10) FROM email_log")
    assert day != clock.today_str(), "this needs a row from another day"

    _one_pass(_process(settings, _Server()))

    assert _query(db_path, "SELECT date, count FROM daily_send_counts") == [(day, 1)]
    assert _query(db_path, "SELECT date, domain, count FROM domain_send_counts") == [
        (day, "brand.de", 1)
    ]


def test_a_settled_send_is_counted_against_the_mailbox_that_made_it(
    settings_factory, tmp_path
):
    settings = _settings(
        settings_factory,
        tmp_path,
        smtp_password="p",
        sender_accounts=json.dumps(
            [{"username": "export@suppleco.test", "password": "p2", "sender_name": "B"}]
        ),
    )
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _update(db_path, "UPDATE email_log SET sender_account = 'export@suppleco.test'")
    today = clock.today_str()

    _one_pass(_process(settings, _Server()))

    assert _query(db_path, "SELECT date, count FROM daily_send_counts") == [
        (f"export@suppleco.test::{today}", 1)
    ]


def test_settling_counts_nothing_for_a_warm_up_that_is_off(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, warmup_enabled=False)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _query(db_path, "SELECT COUNT(*) FROM daily_send_counts") == [(0,)]
    assert _query(db_path, "SELECT count FROM domain_send_counts") == [(1,)]


def test_a_settled_send_counts_against_the_domain_cap(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, max_per_domain_per_day=1)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _lead(db_path, COLLEAGUE, "Brand GmbH")
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == []


def test_a_settled_send_counts_against_the_warm_up(settings_factory, tmp_path):
    settings = _settings(
        settings_factory, tmp_path, warmup_start_daily=1, warmup_increment=0
    )
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _lead(db_path, BYSTANDER, "Other Brand GmbH")
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == []


def test_settling_records_a_basis_once(settings_factory, tmp_path):
    """The first email recorded it, and a follow-up relies on the same one."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    _follow_up_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)

    _one_pass(_process(settings, _Server()))

    assert _query(db_path, "SELECT COUNT(*) FROM consent_log") == [(1,)]


# ── Decisions 11 and 12: retention and erasure ─────────────────────────────


def test_retention_does_not_call_a_held_send_never_contacted(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _update(db_path, "UPDATE contacts SET timestamp = '2020-01-01 00:00:00'")

    counts = apply_retention(Database(db_path), settings)

    assert counts["erased_never_contacted"] == 0
    assert _record(db_path, contact_id) == (["preparing"], "pending")


def _surviving(db_path, address):
    """The columns that still hold ``address``, in any table."""
    raw = sqlite3.connect(db_path)
    hits = []
    tables = raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    for (table,) in tables.fetchall():
        columns = [c[1] for c in raw.execute(f"PRAGMA table_info({table})").fetchall()]
        for row in raw.execute(f"SELECT * FROM {table}").fetchall():
            for column, value in zip(columns, row):
                if isinstance(value, str) and address in value:
                    hits.append(f"{table}.{column}")
    raw.close()
    return hits


def test_a_settle_that_loses_a_race_with_an_erasure_writes_no_address_back(
    settings_factory, tmp_path
):
    """The settle read the row, and the erasure committed before it got the
    write lock. The report and the returned row carry the token."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    settler = Database(db_path)
    real = settler._settle_interrupted_send

    def erased_first(*args, **kwargs):
        DataExporter(Database(db_path)).gdpr_erase(PERSON)
        return real(*args, **kwargs)

    settler._settle_interrupted_send = erased_first

    settled = settler.settle_interrupted_sends(timedelta(0))

    assert [PERSON in row["recipient_email"] for row in settled] == [False], settled
    assert _surviving(db_path, PERSON) == []


def test_an_erasure_during_the_hold_stays_erased_and_records_no_basis(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    DataExporter(Database(db_path)).gdpr_erase(PERSON)
    _age_the_cut_off_send(db_path)
    server = _Server()

    _one_pass(_process(settings, server))

    assert server.accepted == []
    assert _record(db_path, contact_id) == (["sent_unconfirmed"], "erased")
    assert _enrolment(db_path, contact_id) == []
    assert _query(db_path, "SELECT COUNT(*) FROM consent_log") == [(0,)], (
        "erasure supersedes the basis, and settling wrote it back"
    )
    assert _query(db_path, "SELECT COUNT(*) FROM domain_send_counts") == [(0,)]
    assert _surviving(db_path, PERSON) == []


def test_an_erasure_after_settling_scrubs_the_report(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _one_pass(_process(settings, _Server()))
    assert _reported(db_path)

    DataExporter(Database(db_path)).gdpr_erase(PERSON)

    assert _surviving(db_path, PERSON) == []


# ── Decision 13: a dry run settles nothing ─────────────────────────────────


@pytest.mark.parametrize("how", ["run_single", "run_continuous", "preview"])
def test_a_dry_run_settles_nothing(how, settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    server = _Server()
    a = _process(settings, server)

    if how == "run_single":
        a.run_single(queries=["contract manufacturing"], dry_run=True)
    elif how == "run_continuous":
        _dry_pass(a)
    else:
        ok, message = a.send_single_email(
            "someone.else@third.de", organization="Third GmbH", dry_run=True
        )
        assert ok, message

    assert server.accepted == []
    assert _record(db_path, contact_id) == (["preparing"], "pending")
    assert _enrolment(db_path, contact_id) == []
    assert _reported(db_path) == []


def test_a_dry_run_never_leaves_a_send_in_progress_behind(settings_factory, tmp_path):
    """A row left 'preparing' is settled as an email that may have gone."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    a = _process(settings, _Server())
    a.db.update_email_status = _locked

    ok, message = a.send_single_email(PERSON, organization="Brand GmbH", dry_run=True)

    assert ok, message
    assert _record(db_path, contact_id) == (["dry_run"], "pending")


def test_a_dry_run_row_says_it_was_a_dry_run(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    _first_email_due(settings)

    ok, message = _process(settings, _Server()).send_single_email(
        PERSON, organization="Brand GmbH", dry_run=True
    )

    assert ok, message
    assert _query(db_path, "SELECT smtp_response FROM email_log") == [
        ("DRY RUN - email not actually sent",)
    ]


# ── Who else counts the row, and who does not ──────────────────────────────


@pytest.fixture
def held(db):
    """A contact whose send is in doubt, and a colleague at the same company."""
    sid = db.log_search("q", source="test")
    contact_id = db.add_contact(sid, PERSON, organization="Brand GmbH")
    db.add_contact(sid, COLLEAGUE, organization="Brand GmbH")
    db.log_email(contact_id, PERSON, "Hello", delivery_status="preparing")
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    return db, monitor


def test_an_opt_out_while_the_send_is_in_doubt_stops_the_company(held):
    db, monitor = held

    monitor.process_unsubscribes([{"email": PERSON}])

    assert db.is_blocked(COLLEAGUE)


def test_a_data_request_while_the_send_is_in_doubt_stops_the_company(held):
    db, monitor = held

    monitor.process_requests([{"email": PERSON, "request_kind": "access"}])

    assert db.is_blocked(COLLEAGUE)


def test_a_decline_while_the_send_is_in_doubt_is_an_objection(held):
    db, monitor = held

    monitor._record_declines(
        [
            {
                "email": PERSON,
                "subject": "Re: hello",
                "body": "No thanks, not interested.",
            }
        ]
    )

    assert db.is_blocked(PERSON)


def test_an_opt_out_held_on_disk_while_the_send_is_in_doubt_stops_the_company(held):
    db, _monitor = held
    spool_optout(db.db_path, PERSON, "Opted out by email", company="if_emailed")

    replay_optout_spool(db)

    assert db.is_blocked(COLLEAGUE)


def test_a_send_in_doubt_is_not_counted_as_delivered(held):
    db, _monitor = held

    assert db.get_stats()["emails_sent"] == 0


# ── Decision 15: the dashboard tells the truth ─────────────────────────────


def _send_now(settings, contact_id):
    client = main.create_dashboard_app(settings).test_client()
    return client.post(
        f"/api/contacts/{contact_id}/action", json={"action": "send_now"}
    )


def test_the_dashboard_does_not_requeue_a_send_that_has_not_settled(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _retry_due(settings)
    _killed_at_acceptance(settings, tmp_path)

    resp = _send_now(settings, contact_id)

    assert resp.status_code == 409, resp.get_json()
    assert UNSETTLED in resp.get_json()["error"], resp.get_json()
    assert _record(db_path, contact_id) == (["failed", "preparing"], "failed")


def test_the_dashboard_says_a_settled_first_email_may_already_have_gone(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _one_pass(_process(settings, _Server()))

    resp = _send_now(settings, contact_id)

    assert resp.status_code == 409, resp.get_json()
    assert "may already have gone" in resp.get_json()["error"], resp.get_json()


def test_the_dashboard_says_a_delivered_first_email_has_gone(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    contact_id = _first_email_due(settings)
    _one_pass(_process(settings, _Server()))

    resp = _send_now(settings, contact_id)

    assert resp.status_code == 409, resp.get_json()
    assert "has already gone" in resp.get_json()["error"], resp.get_json()


def test_the_why_panel_reports_a_send_that_has_not_settled(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)

    why = explain_contact(Database(db_path), contact_id)

    assert UNSETTLED in why["headline"], why["headline"]
    assert dict(why["facts"])["Unsettled send"], why["facts"]
    assert dict(why["facts"])["Emails delivered"] == "0"


def test_the_why_panel_keeps_a_closed_contacts_headline_with_a_send_unsettled(
    settings_factory, tmp_path
):
    """They replied: that is why nothing more goes, and it stays true whichever
    way the send settles. The unsettled send is still among the facts."""
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _update(
        db_path, "UPDATE contacts SET status = 'responded' WHERE id = ?", (contact_id,)
    )

    why = explain_contact(Database(db_path), contact_id)

    assert why["headline"].startswith("They replied"), why["headline"]
    assert dict(why["facts"])["Unsettled send"], why["facts"]


def test_the_why_panel_drops_the_unsettled_send_once_it_is_settled(
    settings_factory, tmp_path
):
    settings = _settings(settings_factory, tmp_path)
    db_path = settings.get_abs_db_path()
    contact_id = _first_email_due(settings)
    _killed_at_acceptance(settings, tmp_path)
    _age_the_cut_off_send(db_path)
    _one_pass(_process(settings, _Server()))

    why = explain_contact(Database(db_path), contact_id)

    assert why["headline"].startswith("Emailed"), why["headline"]
    assert "Unsettled send" not in dict(why["facts"])
