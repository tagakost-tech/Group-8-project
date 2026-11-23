"""A restore from a backup never makes the agent send an email twice.

The journal held everything that stops sending - opt-outs, blocks, bounces,
erasures - and nothing about what was sent. A backup is the database as it
was when it was taken, so after a restore the agent had forgotten every email
sent since: the contact was 'pending' again and got the first email a second
time, a follow-up step was still due and went out again, and the day's counts
per domain and per mailbox started from the backup's.

Now:
- every real send writes a journal line when it is recorded as sent, and so
  does one whose answer was lost, or that a later pass settled after a kill:
  who, which message, which follow-up step, when, from which mailbox. No
  subject and no text. A dry run and a refused send write none;
- an erasure rewrites the person's send lines as the token, like their
  other lines;
- replay_journal puts back each send the restored database does not know:
  the email_log row, the contact 'emailed', the follow-up step moved on, the
  basis, and today's counts. It enrols no one and reopens no one, and a
  second replay changes nothing;
- a send to an address the backup does not hold is kept under the address's
  keyed token. When that address is collected again it starts as 'emailed';
- the restore drill counts the sends since the backup and fails if one is
  missing after the replay.

Nothing here reaches a real server: every sender is stubbed, and a test that
forgets one meets _no_real_smtp.
"""

import json
import os
import shutil
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import main
from agent import OutreachAgent
from modules import backup, clock, journal
from modules.compliance import ERASURE_TREATMENTS
from modules.database import (
    RESTORED_SEND_NOTE,
    RESTORED_SEND_SUBJECT,
    Database,
    erasure_token,
)
from modules.dispatch_pipeline import AlreadyEmailed
from modules.email_sender import EmailSender
from modules.export import DataExporter

PERSON = "buyer@brand.de"
BYSTANDER = "purchasing@other-brand.de"
MAILBOX = "agent@suppleco.test"
EXTRA = "second@suppleco.test"


@pytest.fixture(autouse=True)
def _no_real_smtp(monkeypatch):
    """A test whose sender is not stubbed fails here, not at a real server."""

    def refused(*_a, **_k):
        raise AssertionError("a test reached for a real SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", refused)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refused)


class _Killed(BaseException):
    """The process dying: nothing after the raise runs as part of the send."""


class _Server:
    """A fake SMTP server that records every message it accepts. ``refuse``
    turns every message away; ``then`` runs once it holds a message."""

    def __init__(self, refuse=None, then=None):
        self.accepted = []
        self.messages = []
        self._refuse = refuse
        self._then = then

    def noop(self):
        return (250, b"2.0.0 OK")

    def send_message(self, msg):
        if self._refuse:
            raise self._refuse
        self.accepted.append(str(msg["To"]))
        self.messages.append(msg)
        if self._then:
            self._then()
        return {}

    def quit(self):
        pass

    def close(self):
        pass


class _DropsAfterAccepting(_Server):
    """The server takes the message and the connection dies before its
    answer: a real 'sent_unconfirmed'."""

    def send_message(self, msg):
        self.accepted.append(str(msg["To"]))
        self.messages.append(msg)
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


@pytest.fixture
def settings(settings_factory, tmp_path):
    return _settings(settings_factory, tmp_path)


def _settings(settings_factory, tmp_path, **config):
    os.makedirs(tmp_path / "live", exist_ok=True)
    return settings_factory(
        db_path=str(tmp_path / "live" / "agent.db"),
        smtp_username=MAILBOX,
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        ai_humanize_emails=False,
        heuristic_discovery_enabled=False,
        min_lead_score_to_email=0,
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


def _process(settings, server):
    """A new agent process on the database, sending through ``server``."""
    a = OutreachAgent(settings)
    a.search.search_and_extract = lambda *_a, **_k: []
    for _key, _name, sender in a.senders:
        sender._new_connection = lambda: server
    return a


def _one_pass(a, dry_run=False):
    """What a started agent does: one pass of the continuous loop."""

    def stop_after_the_pass(_loop_delay):
        a.stop()
        return 0.0

    a._next_loop_delay = stop_after_the_pass
    try:
        a.run_continuous(dry_run=dry_run, loop_delay=60)
    finally:
        a.db.close_thread_conn()
    crashed = _query(
        a.db.db_path, "SELECT details FROM activity_log WHERE action = 'LOOP_ERROR'"
    )
    assert not crashed, f"the pass failed, so it shows nothing: {crashed}"


def _sends_to(settings):
    """One pass of a new process, and who its server accepted mail for."""
    server = _Server()
    _one_pass(_process(settings, server))
    return server.accepted


def _lead(settings, email, organization="Brand GmbH"):
    """A lead a search stored: pending, never emailed."""
    db = Database(settings.get_abs_db_path())
    try:
        sid = db.log_search("contract manufacturing", source="test")
        return db.add_contact(sid, email, organization=organization)
    finally:
        db.close_thread_conn()


def _copy_database(src_path, dst_path):
    src, dst = sqlite3.connect(src_path), sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def _backup(settings) -> Path:
    db = Database(settings.get_abs_db_path())
    try:
        return backup.make_backup(db)
    finally:
        db.close_thread_conn()


def _restore(settings, backup_file) -> None:
    """The database put back as the backup holds it. The journal stays where
    it is, beside the database, as in a real restore."""
    _copy_database(str(backup_file), settings.get_abs_db_path())


def _cli(settings, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _failing(settings, monkeypatch, capsys, *argv):
    with pytest.raises(SystemExit) as stopped:
        _cli(settings, monkeypatch, capsys, *argv)
    assert stopped.value.code == 1
    return capsys.readouterr().out


def _replay(settings, monkeypatch, capsys) -> str:
    """`main.py replay-journal`, as the operator runs it after a restore."""
    return _cli(settings, monkeypatch, capsys, "replay-journal")


def _replayed_count(out, key="sends"):
    line = next(x for x in out.splitlines() if x.strip().startswith(key))
    return int(line.split()[-1])


def _journal(settings):
    return journal.journal_path(settings.get_abs_db_path())


def _send_lines(settings):
    return [e for e in journal.read(_journal(settings)) if e.get("kind") == "send"]


def _write_lines(settings, *lines):
    """Journal lines as given, each on its own line, after what is there."""
    with open(_journal(settings), "a", encoding="utf-8") as f:
        f.writelines(
            (line if isinstance(line, str) else json.dumps(line)) + "\n"
            for line in lines
        )


def _line(email=PERSON, message="<m1@suppleco.test>", at=None, **fields):
    line = {"at": at or clock.now_iso(), "kind": "send", "email": email}
    if message:
        line["id"] = message
    return {**line, "mailbox": MAILBOX, **fields}


def _rows(settings, email=PERSON):
    """The address's email_log rows: status, step and message id, oldest
    first."""
    return _query(
        settings.get_abs_db_path(),
        "SELECT delivery_status, sequence_step_id, message_id FROM email_log "
        "WHERE LOWER(recipient_email) = ? ORDER BY id",
        (email,),
    )


def _status(settings, email=PERSON):
    rows = _query(
        settings.get_abs_db_path(),
        "SELECT status FROM contacts WHERE LOWER(email) = ?",
        (email,),
    )
    return rows[0][0] if rows else None


def _counts(settings):
    """Today's send counts, by domain and by warm-up counter."""
    db_path, today = settings.get_abs_db_path(), clock.today_str()
    domains = dict(
        _query(
            db_path,
            "SELECT domain, count FROM domain_send_counts WHERE date = ?",
            (today,),
        )
    )
    mailboxes = dict(
        _query(
            db_path,
            "SELECT date, count FROM daily_send_counts WHERE date LIKE ?",
            (f"%{today}",),
        )
    )
    return domains, mailboxes


def _enrolments(settings, contact_id):
    return _query(
        settings.get_abs_db_path(),
        "SELECT current_step, status FROM sequence_enrollments "
        "WHERE contact_id = ? ORDER BY id",
        (contact_id,),
    )


def _follow_up_due(settings):
    """Emailed four days ago: the first follow-up is due."""
    contact_id = _lead(settings, PERSON)
    assert _sends_to(settings) == [PERSON], "the first email was never sent"
    enrolled = _update(
        settings.get_abs_db_path(),
        "UPDATE sequence_enrollments SET enrolled_at = datetime('now', '-4 days')",
    )
    assert enrolled == 1, "the first email started no follow-ups"
    return contact_id


def _sender(db, server=None, mailbox=MAILBOX):
    sender = EmailSender(
        db=db,
        smtp_host="smtp.test",
        smtp_port=587,
        smtp_username=mailbox,
        smtp_password="pw",
        sender_name="Agent",
        company_name="SUPPLECO",
    )
    server = server or _Server()
    sender._new_connection = lambda: server
    return sender


def _send(db, email=PERSON, **sender):
    """A real send to a stored contact, through the send path's own
    recording: claimed, delivered, recorded and journalled."""
    contact = db.get_contact_by_email(email)
    result = _sender(db, **sender).dispatch_to_contact(
        contact=contact,
        subject="A subject no journal line holds",
        html_body="<p>Body text no journal line holds</p>",
        plain_body="Body text no journal line holds",
        notice_version="2026-11",
    )
    assert result, "the send was refused"
    return result


@pytest.fixture
def db(settings):
    return Database(settings.get_abs_db_path())


def _contact(db, email=PERSON):
    return db.add_contact(db.log_search("q"), email, organization="Brand GmbH")


def _all(db, sql):
    """Every row of a query on this Database's own connection, as tuples."""
    return [tuple(row) for row in db.conn.execute(sql)]


def _restored_copy(db, tmp_path, source, **replay):
    """The backup at source in a folder of its own, replayed with the live
    journal."""
    folder = tmp_path / "restored"
    folder.mkdir()
    shutil.copyfile(source, folder / "agent.db")
    copy = Database(str(folder / "agent.db"))
    counts = journal.replay_journal(copy, journal.journal_path(db.db_path), **replay)
    return copy, counts


# ── A send is journalled when it is recorded as sent ──


def test_a_real_send_is_journalled_with_what_a_replay_needs(settings):
    _lead(settings, PERSON)
    server = _Server()
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON]

    (line,) = _send_lines(settings)
    ((notice,),) = _query(
        settings.get_abs_db_path(), "SELECT notice_version FROM email_log"
    )
    assert line["email"] == PERSON
    assert line["id"] == str(server.messages[0]["Message-ID"])
    assert line["mailbox"] == MAILBOX
    assert line["notice"] == notice
    assert "step" not in line and "unconfirmed" not in line
    assert clock.now() - datetime.fromisoformat(line["at"]) < timedelta(minutes=5)


def test_the_line_holds_no_subject_and_no_text(settings):
    _lead(settings, PERSON, organization="Unmistakable Brand GmbH")
    server = _Server()
    _one_pass(_process(settings, server))

    raw = _journal(settings).read_text(encoding="utf-8")
    assert str(server.messages[0]["Subject"]) not in raw
    assert "Unmistakable" not in raw
    (line,) = _send_lines(settings)
    assert set(line) <= {"at", "kind", *journal.SEND_LINE_FIELDS}


def test_a_follow_up_names_its_step(settings_factory, tmp_path):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    _follow_up_due(settings)
    assert _sends_to(settings) == [PERSON], "the follow-up was never sent"

    first, follow_up = _send_lines(settings)
    ((step_id,),) = _query(
        settings.get_abs_db_path(),
        "SELECT id FROM sequence_steps WHERE step_number = 0",
    )
    assert "step" not in first
    assert follow_up["step"] == step_id


def test_a_send_whose_answer_was_lost_is_journalled_as_unconfirmed(settings):
    _lead(settings, PERSON)
    server = _DropsAfterAccepting()
    _one_pass(_process(settings, server))
    assert _rows(settings)[0][0] == "sent_unconfirmed"

    (line,) = _send_lines(settings)
    assert line["unconfirmed"] is True
    assert line["id"] == str(server.messages[0]["Message-ID"])


def test_a_dry_run_is_not_journalled(settings):
    _lead(settings, PERSON)
    _one_pass(_process(settings, _Server()), dry_run=True)

    assert _rows(settings)[0][0] == "dry_run", "the dry run never reached the send"
    assert _send_lines(settings) == []


@pytest.mark.parametrize(
    "refusal",
    [
        smtplib.SMTPRecipientsRefused({PERSON: (451, b"4.7.1 Try later")}),
        smtplib.SMTPRecipientsRefused({PERSON: (550, b"5.1.1 No such user")}),
        smtplib.SMTPDataError(554, b"5.7.1 Refused"),
    ],
    ids=["try-later", "no-such-user", "refused-at-data"],
)
def test_a_refused_send_is_not_journalled(settings, refusal):
    _lead(settings, PERSON)
    _one_pass(_process(settings, _Server(refuse=refusal)))

    assert _rows(settings)[0][0] in ("failed", "rejected")
    assert _send_lines(settings) == []


def test_a_send_the_database_could_not_record_is_journalled_all_the_same(
    settings, monkeypatch, capsys
):
    """The message is out. With the database locked the row stays
    'preparing', and the journal is the only record that outlives a restore."""
    _lead(settings, PERSON)
    backed_up = _backup(settings)
    real = Database.update_email_status

    def locked_for_the_result(self, email_id, status, *a, **k):
        if status == "sent":
            raise sqlite3.OperationalError("database is locked")
        return real(self, email_id, status, *a, **k)

    monkeypatch.setattr(Database, "update_email_status", locked_for_the_result)
    server = _Server()
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON]
    assert _rows(settings)[0][0] == "preparing", "setup: the result was not written"
    monkeypatch.undo()

    (line,) = _send_lines(settings)
    assert line["email"] == PERSON and "unconfirmed" not in line

    _restore(settings, backed_up)
    _replay(settings, monkeypatch, capsys)
    _lead(settings, BYSTANDER, "Other Brand GmbH")
    assert _sends_to(settings) == [BYSTANDER]


def _killed_at_acceptance(settings, tmp_path):
    """A process whose first accepted message is the last thing it does. The
    database is left as its death left it."""
    db_path = settings.get_abs_db_path()
    at_acceptance = str(tmp_path / "committed_at_acceptance.db")

    def kill():
        _copy_database(db_path, at_acceptance)
        raise _Killed

    server = _Server(then=kill)
    with pytest.raises(_Killed):
        _one_pass(_process(settings, server))
    _copy_database(at_acceptance, db_path)
    return server


def _age_the_cut_off_send(settings, age=timedelta(minutes=27)):
    then = clock.now().astimezone(clock.get_timezone()) - age
    aged = _update(
        settings.get_abs_db_path(),
        "UPDATE email_log SET timestamp = ? WHERE delivery_status = 'preparing'",
        (then.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    assert aged == 1, "the killed send left no 'preparing' row"
    return then.strftime("%Y-%m-%d %H:%M:%S")


def test_a_send_settled_after_a_kill_is_journalled_when_it_is_settled(
    settings, tmp_path, monkeypatch, capsys
):
    _lead(settings, PERSON)
    backed_up = _backup(settings)
    server = _killed_at_acceptance(settings, tmp_path)
    assert _send_lines(settings) == [], "the killed process journalled nothing"
    _age_the_cut_off_send(settings)

    assert _sends_to(settings) == [], "the next pass settles it and sends nothing"
    assert _rows(settings)[0][0] == "sent_unconfirmed"
    (line,) = _send_lines(settings)
    assert line["email"] == PERSON
    assert line["id"] == str(server.messages[0]["Message-ID"])
    assert line["unconfirmed"] is True and line["mailbox"] == MAILBOX

    _restore(settings, backed_up)
    _replay(settings, monkeypatch, capsys)
    _lead(settings, BYSTANDER, "Other Brand GmbH")
    assert _sends_to(settings) == [BYSTANDER]


def test_a_settled_send_with_no_message_id_is_known_by_its_time(
    settings, tmp_path, monkeypatch, capsys
):
    """The Message-ID is written after the claim. A row settled without one
    is journalled with the row's own time, and is put back at that time."""
    _lead(settings, PERSON)
    backed_up = _backup(settings)
    _killed_at_acceptance(settings, tmp_path)
    started = _age_the_cut_off_send(settings)
    _update(settings.get_abs_db_path(), "UPDATE email_log SET message_id = NULL")
    assert _sends_to(settings) == []

    (line,) = _send_lines(settings)
    assert "id" not in line and line["sent"] == started

    _restore(settings, backed_up)
    assert _replayed_count(_replay(settings, monkeypatch, capsys)) == 1
    assert _replayed_count(_replay(settings, monkeypatch, capsys)) == 0
    assert _query(
        settings.get_abs_db_path(),
        "SELECT delivery_status, timestamp FROM email_log",
    ) == [("sent_unconfirmed", started)]
    _lead(settings, BYSTANDER, "Other Brand GmbH")
    assert _sends_to(settings) == [BYSTANDER]


def test_a_send_settled_on_a_later_day_is_put_back_on_the_day_it_went(
    settings, tmp_path, monkeypatch, capsys
):
    """A send cut off at night and settled the next day. Its line carries the
    time the send started beside its Message-ID, so the replay puts the row
    back at that time and counts it on that day, as the settling did: not at
    the time of the line, and not against today's caps."""
    _lead(settings, PERSON)
    backed_up = _backup(settings)
    _killed_at_acceptance(settings, tmp_path)
    started = _age_the_cut_off_send(settings, timedelta(days=1))
    assert _sends_to(settings) == []
    settled = _counts(settings)

    (line,) = _send_lines(settings)
    assert line["id"] and line["sent"] == started

    _restore(settings, backed_up)
    assert _replayed_count(_replay(settings, monkeypatch, capsys)) == 1
    assert _query(
        settings.get_abs_db_path(),
        "SELECT delivery_status, timestamp FROM email_log",
    ) == [("sent_unconfirmed", started)]
    assert _counts(settings) == settled
    _lead(settings, BYSTANDER, "Other Brand GmbH")
    assert _sends_to(settings) == [BYSTANDER]


def test_with_journalling_off_a_send_writes_no_line(db):
    _contact(db)
    db.journaling = False
    _send(db)
    assert journal.read(journal.journal_path(db.db_path)) == []


# ── An erasure covers the send lines ──


def test_an_erasure_rewrites_the_send_lines_as_the_token(db):
    _contact(db)
    _send(db)
    (before,) = [e for e in journal.read(journal.journal_path(db.db_path))]
    assert before["email"] == PERSON

    DataExporter(db).gdpr_erase(PERSON)

    raw = journal.journal_path(db.db_path).read_text(encoding="utf-8")
    assert PERSON not in raw and "brand.de" not in raw
    (line,) = [
        e for e in journal.read(journal.journal_path(db.db_path)) if e["kind"] == "send"
    ]
    assert "email" not in line
    assert line["token"] == erasure_token(PERSON)
    assert line["id"] == before["id"], "the message is still known after it"


def test_someone_erased_while_the_message_went_out_is_named_by_the_token(db):
    """The erasure rewrote the journal before the send's line was in it, so
    the line itself must not put the address back."""
    _contact(db)

    def erased_meanwhile():
        other = Database(db.db_path)
        DataExporter(other).gdpr_erase(PERSON)
        other.close_thread_conn()

    _send(db, server=_Server(then=erased_meanwhile))

    raw = journal.journal_path(db.db_path).read_text(encoding="utf-8")
    assert PERSON not in raw
    (line,) = [
        e for e in journal.read(journal.journal_path(db.db_path)) if e["kind"] == "send"
    ]
    assert line.get("token") == erasure_token(PERSON) and "email" not in line


def test_a_database_that_cannot_say_who_was_erased_names_the_token(
    db, tmp_path, monkeypatch
):
    """A locked database must not decide that the address goes in clear. The
    token is enough: the replay finds the contact by it."""
    _contact(db)
    backed_up = backup.make_backup(db)

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(db, "_transaction", locked)
        db.journal_send(PERSON, message_id="<m1@suppleco.test>", mailbox=MAILBOX)
    (line,) = journal.read(journal.journal_path(db.db_path))
    assert line.get("token") == erasure_token(PERSON) and "email" not in line

    copy, counts = _restored_copy(db, tmp_path, backed_up)
    assert counts["sends"] == 1
    held = copy.get_contact_by_email(PERSON)
    assert held["status"] == "emailed" and copy.has_been_emailed(held["id"])
    assert copy.last_possibly_sent(PERSON), "the row is under their own address"


def test_every_field_of_a_send_line_says_what_an_erasure_does_with_it(db):
    """As PERSONAL_COLUMNS does for the database: a field added to the line
    without an entry fails here."""
    assert set(journal.SEND_LINE_FIELDS.values()) <= set(ERASURE_TREATMENTS)
    assert journal.SEND_LINE_FIELDS["email"] == "token"
    db.journal_send(
        PERSON,
        message_id="<m1@suppleco.test>",
        step_id=3,
        mailbox=MAILBOX,
        notice="2026-11-it",
        unconfirmed=True,
    )
    db.journal_send(PERSON, sent="2026-11-17 10:00:00", mailbox=MAILBOX)
    db.journal_send(erasure_token(PERSON), message_id="<m2@suppleco.test>")
    written = set()
    for line in journal.read(journal.journal_path(db.db_path)):
        written |= set(line)
    assert written - {"at", "kind"} == set(journal.SEND_LINE_FIELDS)


# ── After a restore and a replay, nothing is sent twice ──


def test_without_the_replay_a_restored_agent_would_send_it_again(settings):
    """The control: this is what the journal line is for."""
    _lead(settings, PERSON)
    backed_up = _backup(settings)
    assert _sends_to(settings) == [PERSON]

    _restore(settings, backed_up)

    assert _status(settings) == "pending"
    assert _sends_to(settings) == [PERSON]


@pytest.mark.parametrize(
    ("server", "status"),
    [(_Server, "sent"), (_DropsAfterAccepting, "sent_unconfirmed")],
    ids=["delivered", "answer-lost"],
)
def test_a_first_email_sent_since_the_backup_is_not_sent_again(
    settings, monkeypatch, capsys, server, status
):
    _lead(settings, PERSON)
    backed_up = _backup(settings)
    first = server()
    _one_pass(_process(settings, first))
    assert first.accepted == [PERSON]
    sent_at = _send_lines(settings)[0]["at"]

    _restore(settings, backed_up)
    assert _status(settings) == "pending", "setup: the backup is older than the send"
    out = _replay(settings, monkeypatch, capsys)

    assert _replayed_count(out) == 1
    assert _status(settings) == "emailed"
    assert _rows(settings) == [(status, None, str(first.messages[0]["Message-ID"]))]
    ((subject, note, mailbox, stamp, attached),) = _query(
        settings.get_abs_db_path(),
        "SELECT subject, smtp_response, sender_account, timestamp, has_attachment "
        "FROM email_log",
    )
    assert (subject, note, mailbox) == (
        RESTORED_SEND_SUBJECT,
        RESTORED_SEND_NOTE,
        MAILBOX,
    )
    assert attached == 0
    written = datetime.fromisoformat(sent_at).astimezone(clock.get_timezone())
    assert stamp == written.strftime("%Y-%m-%d %H:%M:%S"), "at the time it went"
    _lead(settings, BYSTANDER, "Other Brand GmbH")
    assert _sends_to(settings) == [BYSTANDER]
    assert _sends_to(settings) == []


def test_a_retry_that_went_since_the_backup_is_not_sent_again(
    settings, monkeypatch, capsys
):
    """Greylisted before the backup, delivered by its retry after it. The
    restored queue still holds the retry, due."""
    db_path = settings.get_abs_db_path()
    _lead(settings, PERSON)
    greylisted = _Server(
        refuse=smtplib.SMTPRecipientsRefused({PERSON: (451, b"4.7.1 Try later")})
    )
    _one_pass(_process(settings, greylisted))
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    queued = _update(
        db_path,
        "UPDATE retry_queue SET next_retry_at = ? WHERE status = 'pending'",
        (two_hours_ago.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    assert queued == 1, "the greylisted first email was not queued for a retry"
    backed_up = _backup(settings)
    assert _sends_to(settings) == [PERSON], "the retry never went"

    _restore(settings, backed_up)
    assert _query(db_path, "SELECT status FROM retry_queue") == [("pending",)]
    assert _replayed_count(_replay(settings, monkeypatch, capsys)) == 1

    _lead(settings, BYSTANDER, "Other Brand GmbH")
    assert _sends_to(settings) == [BYSTANDER]
    assert _query(db_path, "SELECT status FROM retry_queue") == [("cancelled",)]
    assert _status(settings) == "emailed"


def test_a_follow_up_sent_since_the_backup_is_not_sent_again(
    settings_factory, tmp_path, monkeypatch, capsys
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    contact_id = _follow_up_due(settings)
    backed_up = _backup(settings)
    assert _sends_to(settings) == [PERSON], "the follow-up was never sent"
    assert _enrolments(settings, contact_id) == [(1, "active")]
    ((moved_at,),) = _query(
        settings.get_abs_db_path(), "SELECT last_sent_at FROM sequence_enrollments"
    )

    _restore(settings, backed_up)
    assert _enrolments(settings, contact_id) == [(0, "active")], "setup: still due"
    assert _replayed_count(_replay(settings, monkeypatch, capsys)) == 1

    assert _enrolments(settings, contact_id) == [(1, "active")]
    ((restored_at,),) = _query(
        settings.get_abs_db_path(), "SELECT last_sent_at FROM sequence_enrollments"
    )
    assert abs(
        datetime.fromisoformat(restored_at) - datetime.fromisoformat(moved_at)
    ) < timedelta(seconds=5), (
        "the next step waits from when this one went, not from the replay"
    )
    _lead(settings, BYSTANDER, "Other Brand GmbH")
    assert _sends_to(settings) == [BYSTANDER]


def test_the_step_after_it_still_goes_once_when_it_falls_due(
    settings_factory, tmp_path, monkeypatch, capsys
):
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    _follow_up_due(settings)
    backed_up = _backup(settings)
    assert _sends_to(settings) == [PERSON]
    _restore(settings, backed_up)
    _replay(settings, monkeypatch, capsys)
    assert _sends_to(settings) == []

    _update(
        settings.get_abs_db_path(),
        "UPDATE sequence_enrollments SET last_sent_at = datetime('now', '-30 days')",
    )

    assert _sends_to(settings) == [PERSON], "the second follow-up is due now"
    assert _sends_to(settings) == []
    steps = [step for _status_, step, _id in _rows(settings)]
    assert len(steps) == 3 and steps[0] is None and steps[1] != steps[2]


def test_a_follow_up_the_backup_already_moved_past_moves_nothing(db, settings):
    """A line older than the backup: the enrolment is past that step, and
    must not be pushed a step further."""
    from modules.sequences import SequenceEngine

    cid = _contact(db)
    engine = SequenceEngine(db)
    sequence = engine.create_sequence(
        "Default",
        [
            {"delay_days": 3, "subject": "One", "template_name": "f1"},
            {"delay_days": 5, "subject": "Two", "template_name": "f2"},
            {"delay_days": 7, "subject": "Three", "template_name": "f3"},
        ],
    )
    enrolment = engine.enroll_contact(cid, sequence)
    engine.advance_enrollment(enrolment)
    (first_step,) = db.conn.execute(
        "SELECT id FROM sequence_steps WHERE step_number = 0"
    ).fetchone()
    _write_lines(settings, _line(step=first_step))

    assert journal.replay_journal(db)["sends"] == 1

    assert _enrolments(settings, cid) == [(1, "active")]


def test_the_last_step_restored_completes_the_sequence(db, settings):
    from modules.sequences import SequenceEngine

    cid = _contact(db)
    engine = SequenceEngine(db)
    sequence = engine.create_sequence(
        "Default", [{"delay_days": 3, "subject": "One", "template_name": "f1"}]
    )
    engine.enroll_contact(cid, sequence)
    (only_step,) = db.conn.execute("SELECT id FROM sequence_steps").fetchone()
    yesterday = (clock.now() - timedelta(days=1)).isoformat()
    _write_lines(settings, _line(step=only_step, at=yesterday))

    journal.replay_journal(db)

    assert _enrolments(settings, cid) == [(0, "completed")]
    assert _all(db, "SELECT last_sent_at, completed_at FROM sequence_enrollments") == [
        (yesterday, yesterday)
    ], "ended when the step went, not when the journal was replayed"


def test_a_step_moved_on_waits_from_when_it_went(db, settings):
    from modules.sequences import SequenceEngine

    cid = _contact(db)
    engine = SequenceEngine(db)
    sequence = engine.create_sequence(
        "Default",
        [
            {"delay_days": 3, "subject": "One", "template_name": "f1"},
            {"delay_days": 5, "subject": "Two", "template_name": "f2"},
        ],
    )
    engine.enroll_contact(cid, sequence)
    (first_step,) = db.conn.execute(
        "SELECT id FROM sequence_steps WHERE step_number = 0"
    ).fetchone()
    yesterday = (clock.now() - timedelta(days=1)).isoformat()
    _write_lines(settings, _line(step=first_step, at=yesterday))

    journal.replay_journal(db)

    assert _enrolments(settings, cid) == [(1, "active")]
    assert _all(db, "SELECT last_sent_at FROM sequence_enrollments") == [(yesterday,)]


def test_a_stopped_sequence_stays_stopped(db, settings):
    from modules.sequences import SequenceEngine

    cid = _contact(db)
    engine = SequenceEngine(db)
    sequence = engine.create_sequence(
        "Default",
        [
            {"delay_days": 3, "subject": "One", "template_name": "f1"},
            {"delay_days": 5, "subject": "Two", "template_name": "f2"},
        ],
    )
    engine.cancel_enrollment(engine.enroll_contact(cid, sequence), "replied")
    (first_step,) = db.conn.execute(
        "SELECT id FROM sequence_steps WHERE step_number = 0"
    ).fetchone()
    _write_lines(settings, _line(step=first_step))

    journal.replay_journal(db)

    assert _enrolments(settings, cid) == [(0, "cancelled")]


def test_the_replay_enrols_no_one(settings_factory, tmp_path, monkeypatch, capsys):
    """Whether a follow-up was still wanted is not in the journal: a reply or
    a stop since the backup is lost with it. So none goes out on a guess."""
    settings = _settings(settings_factory, tmp_path, sequences_enabled=True)
    contact_id = _lead(settings, PERSON)
    backed_up = _backup(settings)
    assert _sends_to(settings) == [PERSON]
    assert _enrolments(settings, contact_id) == [(0, "active")], "setup: enrolled live"

    _restore(settings, backed_up)
    _replay(settings, monkeypatch, capsys)

    assert _enrolments(settings, contact_id) == []
    assert _sends_to(settings) == []


def test_a_closed_contact_is_not_reopened(db, settings):
    cid = _contact(db)
    db.update_contact_status(cid, "responded")
    _write_lines(settings, _line())

    assert journal.replay_journal(db)["sends"] == 1

    assert _status(settings) == "responded"
    assert db.has_been_emailed(cid), "the send itself is still on record"


def test_a_contact_a_filter_had_set_aside_counts_as_emailed(db, settings):
    cid = _contact(db)
    db.update_contact_status(cid, "low_score", discard_reason="Score 3 below 15")
    _write_lines(settings, _line())

    journal.replay_journal(db)

    assert db.get_contact_by_id(cid)["status"] == "emailed"
    assert db.get_contact_by_id(cid)["discard_reason"] is None


def test_the_basis_the_send_relied_on_is_recorded_again(db, settings):
    cid = _contact(db)
    basis = db.get_contact_by_id(cid)["basis"]
    assert basis, "setup: the contact was collected on a basis"
    _write_lines(settings, _line())

    journal.replay_journal(db)

    assert _all(db, "SELECT email, legal_basis, contact_id FROM consent_log") == [
        (PERSON, basis, cid)
    ]


def test_a_second_replay_changes_nothing(settings, monkeypatch, capsys):
    _lead(settings, PERSON)
    backed_up = _backup(settings)
    assert _sends_to(settings) == [PERSON]
    _restore(settings, backed_up)
    assert _replayed_count(_replay(settings, monkeypatch, capsys)) == 1
    once = (_rows(settings), _status(settings), _counts(settings))

    assert _replayed_count(_replay(settings, monkeypatch, capsys)) == 0

    assert (_rows(settings), _status(settings), _counts(settings)) == once
    assert len(_send_lines(settings)) == 1, "a replay journals nothing again"


def test_a_send_the_backup_already_holds_is_left_alone(settings, monkeypatch, capsys):
    _lead(settings, PERSON)
    assert _sends_to(settings) == [PERSON]
    backed_up = _backup(settings)
    assert len(_send_lines(settings)) == 1, "setup: the line is still in the journal"
    before = (_rows(settings), _counts(settings))

    _restore(settings, backed_up)

    assert _replayed_count(_replay(settings, monkeypatch, capsys)) == 0
    assert (_rows(settings), _counts(settings)) == before


# ── Today's counts ──


def test_todays_counts_are_what_they_were_before_the_restore(
    settings, monkeypatch, capsys
):
    _lead(settings, PERSON)
    backed_up = _backup(settings)
    assert _sends_to(settings) == [PERSON]
    live = _counts(settings)
    assert live == ({"brand.de": 1}, {clock.today_str(): 1}), "setup"

    _restore(settings, backed_up)
    assert _counts(settings) == ({}, {}), "setup: the backup is from before it"
    _replay(settings, monkeypatch, capsys)

    assert _counts(settings) == live


def test_a_send_of_another_day_adds_nothing_to_todays_counts(db, settings):
    _contact(db)
    yesterday = (clock.now() - timedelta(days=1)).isoformat()
    _write_lines(settings, _line(at=yesterday))

    assert journal.replay_journal(db, primary_mailbox=MAILBOX)["sends"] == 1

    went = datetime.fromisoformat(yesterday).astimezone(clock.get_timezone())
    assert _all(db, "SELECT timestamp FROM email_log") == [
        (went.strftime("%Y-%m-%d %H:%M:%S"),)
    ], "the row is dated when the email went, not when the journal was replayed"
    assert _counts(settings) == ({}, {})
    assert db.conn.execute("SELECT COUNT(*) FROM domain_send_counts").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM daily_send_counts").fetchone()[0] == 0


def test_an_extra_mailbox_counts_on_its_own_counter(db, settings):
    _contact(db)
    _contact(db, BYSTANDER)
    today = clock.today_str()
    _write_lines(
        settings,
        _line(mailbox=EXTRA),
        _line(BYSTANDER, "<m2@suppleco.test>", mailbox=MAILBOX.upper()),
    )

    journal.replay_journal(db, primary_mailbox=MAILBOX)

    assert _counts(settings)[1] == {f"{EXTRA}::{today}": 1, today: 1}


def test_a_mailbox_not_known_to_be_the_primary_counts_on_both(db, settings):
    """restore-check and the drill replay without the settings. Over the
    cap's side, never under it."""
    _contact(db)
    today = clock.today_str()
    _write_lines(settings, _line(mailbox=EXTRA))

    journal.replay_journal(db)

    assert _counts(settings)[1] == {f"{EXTRA}::{today}": 1, today: 1}


def test_the_restored_count_holds_the_domain_to_its_daily_cap(
    settings_factory, tmp_path, monkeypatch, capsys
):
    """What the count is for: a colleague at a company that had its one email
    today waits for tomorrow, as before the restore."""
    settings = _settings(settings_factory, tmp_path, max_per_domain_per_day=1)
    _lead(settings, "colleague@brand.de")
    _update(settings.get_abs_db_path(), "UPDATE contacts SET lead_score = 1")
    _lead(settings, PERSON)
    _update(
        settings.get_abs_db_path(),
        "UPDATE contacts SET lead_score = 90 WHERE email = ?",
        (PERSON,),
    )
    backed_up = _backup(settings)
    assert _sends_to(settings) == [PERSON], "setup: one email a day to brand.de"

    _restore(settings, backed_up)
    _replay(settings, monkeypatch, capsys)

    assert _sends_to(settings) == []
    assert _status(settings, "colleague@brand.de") == "pending"


# ── An address the backup does not hold ──


def _emailed_then_restored_to_before_it_was_found(settings, monkeypatch, capsys):
    backed_up = _backup(settings)
    _lead(settings, PERSON)
    server = _Server()
    _one_pass(_process(settings, server))
    assert server.accepted == [PERSON]
    _restore(settings, backed_up)
    assert _status(settings) is None, "setup: the backup does not hold the contact"
    out = _replay(settings, monkeypatch, capsys)
    return str(server.messages[0]["Message-ID"]), out


def test_a_send_to_an_address_the_backup_does_not_hold_is_kept_by_its_token(
    settings, monkeypatch, capsys
):
    message_id, out = _emailed_then_restored_to_before_it_was_found(
        settings, monkeypatch, capsys
    )

    assert _replayed_count(out) == 1
    assert _query(
        settings.get_abs_db_path(),
        "SELECT token, message_id, delivery_status, sender_account FROM prior_sends",
    ) == [(erasure_token(PERSON), message_id, "sent", MAILBOX)]
    db = Database(settings.get_abs_db_path())
    assert journal.held_addresses(db) == set(), "the address itself is not stored"
    assert _status(settings) is None and _rows(settings) == []
    assert _counts(settings) == ({"brand.de": 1}, {clock.today_str(): 1})
    db.close_thread_conn()
    assert _replayed_count(_replay(settings, monkeypatch, capsys)) == 0
    assert len(_query(settings.get_abs_db_path(), "SELECT 1 FROM prior_sends")) == 1


def test_found_again_that_address_is_not_emailed_again(settings, monkeypatch, capsys):
    message_id, _out = _emailed_then_restored_to_before_it_was_found(
        settings, monkeypatch, capsys
    )

    contact_id = _lead(settings, PERSON)

    assert contact_id, "a live prospect is stored again, not turned away"
    assert _status(settings) == "emailed"
    assert _rows(settings) == [("sent", None, message_id)]
    assert _query(settings.get_abs_db_path(), "SELECT 1 FROM prior_sends") == []
    _lead(settings, BYSTANDER, "Other Brand GmbH")
    assert _sends_to(settings) == [BYSTANDER]
    assert _enrolments(settings, contact_id) == []


def test_only_that_address_starts_as_emailed(settings, monkeypatch, capsys):
    _emailed_then_restored_to_before_it_was_found(settings, monkeypatch, capsys)

    _lead(settings, "colleague@brand.de")

    assert _status(settings, "colleague@brand.de") == "pending"
    assert len(_query(settings.get_abs_db_path(), "SELECT 1 FROM prior_sends")) == 1


def test_direct_send_asks_before_writing_to_that_address_again(
    settings, monkeypatch, capsys
):
    _emailed_then_restored_to_before_it_was_found(settings, monkeypatch, capsys)
    server = _Server()
    a = _process(settings, server)

    ok, message = a.send_single_email(
        PERSON, organization="Brand GmbH", channel="inbound_request"
    )
    a.db.close_thread_conn()

    assert isinstance(ok, AlreadyEmailed), message
    assert server.accepted == []


def test_the_access_answer_holds_a_send_kept_by_the_token(db, settings):
    _write_lines(settings, _line())
    journal.replay_journal(db)

    held = DataExporter(db).gdpr_export(PERSON)

    assert [r["message_id"] for r in held["prior_sends"]] == ["<m1@suppleco.test>"]
    assert "prior_sends" not in DataExporter(db).gdpr_export(BYSTANDER)


# ── Someone erased since the backup ──


def test_someone_erased_since_keeps_their_send_only_under_the_token(db, tmp_path):
    _contact(db)
    backed_up = backup.make_backup(db)
    _send(db)
    (message_id,) = db.conn.execute("SELECT message_id FROM email_log").fetchone()
    DataExporter(db).gdpr_erase(PERSON)

    copy, counts = _restored_copy(db, tmp_path, backed_up, primary_mailbox=MAILBOX)

    assert counts["sends"] == 1
    assert journal.held_addresses(copy) == set()
    assert _all(
        copy, "SELECT recipient_email, message_id, delivery_status FROM email_log"
    ) == [(erasure_token(PERSON), message_id, "sent")]
    assert _all(copy, "SELECT status FROM contacts") == [("erased",)]
    assert _all(copy, "SELECT * FROM consent_log") == []
    assert _all(copy, "SELECT domain FROM domain_send_counts") == []
    assert _all(copy, "SELECT date, count FROM daily_send_counts") == [
        (clock.today_str(), 1)
    ], "the mailbox still sent one today"


def test_an_erased_person_the_backup_does_not_hold_leaves_only_tokens(db, tmp_path):
    backed_up = backup.make_backup(db)
    _contact(db)
    _send(db)
    DataExporter(db).gdpr_erase(PERSON)

    copy, counts = _restored_copy(db, tmp_path, backed_up)

    assert counts["sends"] == 1
    assert journal.held_addresses(copy) == set()
    assert _all(copy, "SELECT token FROM prior_sends") == [(erasure_token(PERSON),)]
    assert _all(copy, "SELECT domain FROM domain_send_counts") == []
    assert copy.add_contact(copy.log_search("q"), PERSON) is None, "still erased"


def test_a_line_left_in_clear_for_someone_erased_counts_no_domain(db, settings):
    """An erasure whose rewrite of the journal is still pending: the replay
    reads the line as the rewrite will leave it."""
    db.suppress_erased(PERSON)
    _write_lines(settings, _line(), {"kind": "erasure", "token": erasure_token(PERSON)})

    assert journal.replay_journal(db, primary_mailbox=MAILBOX)["sends"] == 1

    assert _counts(settings) == ({}, {clock.today_str(): 1})
    assert journal.held_addresses(db) == set()


# ── Lines the replay cannot use, and journals from before ──


@pytest.mark.parametrize(
    "damaged",
    [
        {"at": "2026-11-17T10:00:00+01:00", "kind": "send", "email": PERSON},
        {"kind": "send", "id": "<m9@suppleco.test>"},
        _line(message="<m9@suppleco.test>", step="2"),
        _line(message="<m9@suppleco.test>", step=True),
        _line(message="<m9@suppleco.test>", mailbox=7),
        _line(message="<m9@suppleco.test>", notice=["it"]),
        _line(message=None, sent="yesterday afternoon"),
        _line(message="<m9@suppleco.test>", sent="yesterday afternoon"),
        '{"kind": "send", "email": "buyer@brand.de", "id": 12}',
    ],
    ids=[
        "no-message",
        "no-one",
        "step-as-text",
        "step-as-a-flag",
        "mailbox-not-text",
        "notice-not-text",
        "time-unreadable",
        "time-beside-an-id-unreadable",
        "id-not-text",
    ],
)
def test_a_send_line_that_cannot_be_read_is_skipped(db, settings, damaged):
    _contact(db)
    _contact(db, BYSTANDER)
    _write_lines(settings, damaged, _line(BYSTANDER, "<m2@suppleco.test>"))

    counts = journal.replay_journal(db)

    assert counts["sends"] == 1, "the line after it is still applied"
    assert _rows(settings) == [] and _status(settings) == "pending"
    assert _status(settings, BYSTANDER) == "emailed"


def test_a_journal_from_before_send_lines_still_replays(db, settings):
    _contact(db)
    _write_lines(
        settings,
        {"at": clock.now_iso(), "kind": "opt_out", "email": BYSTANDER},
        {"at": clock.now_iso(), "kind": "block", "domain": "other-brand.de"},
    )

    counts = journal.replay_journal(db)

    assert (counts["opt_outs"], counts["blocks"], counts["sends"]) == (1, 1, 0)
    assert db.is_suppressed(BYSTANDER) and _status(settings) == "pending"


# ── The restore drill ──


def _row(out, label):
    line = next(x for x in out.splitlines() if x.strip().startswith(label))
    checked, missing = line.split()[-2:]
    return int(checked), int(missing)


def test_the_drill_counts_the_sends_since_the_backup(settings, db, monkeypatch, capsys):
    _contact(db)
    _contact(db, BYSTANDER)
    _send(db)
    backup.make_backup(db)
    _send(db, BYSTANDER)
    db.close_thread_conn()

    out = _cli(settings, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill PASSED" in out
    assert "1 send(s) since the backup, all restored" in out
    assert _row(out, "sends") == (2, 0)
    assert PERSON not in out and BYSTANDER not in out


def test_the_drill_fails_when_a_send_is_missing_after_the_replay(
    settings, db, monkeypatch, capsys
):
    _contact(db)
    backup.make_backup(db)
    _send(db)
    db.close_thread_conn()
    monkeypatch.setattr(Database, "restore_send", lambda *a, **k: False)

    out = _failing(settings, monkeypatch, capsys, "restore-drill", "--newest")

    assert "1 send(s) missing from the restored copy" in out
    assert "1 send(s) since the backup, 1 missing after the replay" in out
    assert _row(out, "sends") == (1, 1)
    assert _row(out, "opt-outs") == (0, 0)


def test_the_drill_fails_when_the_journal_lost_a_send(
    settings, db, monkeypatch, capsys
):
    """The live database knows the email went; the journal refused its line
    (a full disk). A restore would send it again, and the drill says so."""
    _contact(db)
    backup.make_backup(db)
    db.journaling = False
    _send(db)
    db.close_thread_conn()

    out = _failing(settings, monkeypatch, capsys, "restore-drill", "--newest")

    assert "1 send(s) missing from the restored copy" in out
    assert _row(out, "sends") == (1, 1)


def test_with_the_live_database_gone_the_journal_names_the_sends(
    settings, db, tmp_path, monkeypatch, capsys
):
    _contact(db)
    off_machine = tmp_path / "copy.db"
    off_machine.write_bytes(backup.make_backup(db).read_bytes())
    _send(db)
    db.close_thread_conn()
    for suffix in ("", "-wal", "-shm"):
        Path(db.db_path + suffix).unlink(missing_ok=True)

    out = _cli(
        settings, monkeypatch, capsys, "restore-drill", "--backup", str(off_machine)
    )

    assert "1 send(s) since the backup, all restored" in out
    assert _row(out, "sends") == (1, 0)

    monkeypatch.setattr(Database, "restore_send", lambda *a, **k: False)
    out = _failing(
        settings, monkeypatch, capsys, "restore-drill", "--backup", str(off_machine)
    )
    assert _row(out, "sends") == (1, 1)


def test_the_drill_counts_a_send_kept_by_its_token_as_restored(
    settings, db, monkeypatch, capsys
):
    backup.make_backup(db)
    _contact(db)
    _send(db)
    db.close_thread_conn()

    out = _cli(settings, monkeypatch, capsys, "restore-drill", "--newest")

    assert "1 send(s) since the backup, all restored" in out
    assert _row(out, "sends") == (1, 0)


def test_the_drill_knows_a_send_with_no_message_id_by_its_time(
    settings, db, monkeypatch, capsys
):
    _contact(db)
    backup.make_backup(db)
    db.journal_send(PERSON, sent="2026-11-16 09:30:00", mailbox=MAILBOX)
    db.close_thread_conn()

    out = _cli(settings, monkeypatch, capsys, "restore-drill", "--newest")
    assert _row(out, "sends") == (1, 0)

    monkeypatch.setattr(Database, "restore_send", lambda *a, **k: False)
    out = _failing(settings, monkeypatch, capsys, "restore-drill", "--newest")
    assert _row(out, "sends") == (1, 1)


def test_the_drill_knows_a_token_kept_send_with_no_message_id(
    settings, db, monkeypatch, capsys
):
    backup.make_backup(db)
    db.journal_send(PERSON, sent="2026-11-16 09:30:00", mailbox=MAILBOX)
    db.close_thread_conn()

    out = _cli(settings, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill PASSED" in out
    assert _row(out, "sends") == (1, 0)


def test_the_drill_writes_the_send_count_to_the_activity_log(
    settings, db, monkeypatch, capsys
):
    _contact(db)
    backup.make_backup(db)
    _send(db)
    db.close_thread_conn()

    _cli(settings, monkeypatch, capsys, "restore-drill", "--newest")

    ((details,),) = _query(
        settings.get_abs_db_path(),
        "SELECT details FROM activity_log WHERE action = 'RESTORE_DRILL'",
    )
    assert details.startswith("PASS") and "1 sends" in details
    assert PERSON not in details


def test_the_drill_reads_a_live_database_from_before_message_ids(
    settings, db, monkeypatch, capsys
):
    """A database no version has opened since the column was added: its
    sends are in every backup, and the drill still reads the rest of it."""
    _contact(db)
    db.record_opt_out(BYSTANDER, "asked by email")
    backup.make_backup(db)
    db.close_thread_conn()
    raw = sqlite3.connect(db.db_path)
    raw.execute("DROP INDEX idx_email_log_message_id")
    raw.execute("ALTER TABLE email_log DROP COLUMN message_id")
    raw.commit()
    raw.close()

    out = _cli(settings, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Live database: read" in out
    assert _row(out, "opt-outs") == (1, 0) and _row(out, "sends") == (0, 0)
