"""After a restore, nothing is started or sent until the journal is replayed.

A backup is the database as it was when it was taken. The emails sent and the
opt-outs, blocks and bounces recorded since are only in the journal, until
`main.py replay-journal` applies them. An operator who restored an older
backup and pressed Start first had the agent email again everyone emailed
since the backup, and email the people who had opted out since.

Now every journal line carries a number that the database records before the
line is written. A database that does not know a number its journal holds was
restored from an older backup, or lost writes, and:
- `main.py run`, the dashboard's Start and Direct Send refuse, naming
  replay-journal, and the refusal is in the log and the activity log;
- doctor reports it as a blocking check, and so does the "Ready to send" card;
- replay-journal clears it, and a replay that stops on an error does not;
- new lines written while it is behind do not hide the gap;
- the restore drill shows it before its replay, and fails if its replay
  leaves it.
A healthy install, a fresh one, a journal from before the numbers and one the
backups trimmed are never refused.
"""

import json
import logging
import os
import shutil
import sqlite3
import sys
import threading

import pytest

import main
from agent import OutreachAgent
from modules import backup, journal
from modules.database import Database

ANNA = "anna.rossi@brandco.de"
LUCA = "luca@other.de"
MARIA = "maria@third.de"


@pytest.fixture
def s(settings_factory, tmp_path):
    os.makedirs(tmp_path / "live", exist_ok=True)
    return settings_factory(
        db_path=str(tmp_path / "live" / "agent.db"), blacklist_check_enabled=False
    )


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


@pytest.fixture
def built(monkeypatch):
    """Every agent the code under test builds, none of which runs a pass."""
    agents = []
    real = OutreachAgent.__init__

    def init(self, settings):
        real(self, settings)
        agents.append(self)

    monkeypatch.setattr(OutreachAgent, "__init__", init)
    monkeypatch.setattr(OutreachAgent, "run_single", lambda self, **_kw: None)
    monkeypatch.setattr(OutreachAgent, "run_continuous", lambda self, **_kw: None)
    return agents


def _lines(db_path):
    return journal.read(journal.journal_path(db_path))


def _unnumbered(db_path, events):
    """Lines as a journal from before the numbers holds them."""
    with open(journal.journal_path(db_path), "a", encoding="utf-8") as f:
        f.writelines(
            json.dumps({"at": "2026-11-01T10:00:00+01:00", **e}) + "\n" for e in events
        )


def _restore(db, backup_file):
    """What an operator does: the backup put in the database's place. The
    journal stays where it is."""
    path = db.db_path
    db.close_thread_conn()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    shutil.copyfile(backup_file, path)
    return Database(path)


def _restored_after_two_opt_outs(db):
    """A backup, two opt-outs after it, and the backup restored."""
    db.record_opt_out(ANNA, "Opted out by email")
    taken = backup.make_backup(db)
    db.record_opt_out(LUCA, "Opted out by email")
    db.record_opt_out(MARIA, "Opted out by email")
    restored = _restore(db, taken)
    assert not restored.is_suppressed(LUCA), "setup: the backup predates them"
    return restored


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _refused_run(s, monkeypatch, capsys, *argv):
    with pytest.raises(SystemExit) as stopped:
        _cli(s, monkeypatch, capsys, "run", *argv)
    assert stopped.value.code == 1
    return capsys.readouterr().out


def _doctor_row(s):
    (row,) = main.run_doctor_checks(s, network=False, only=("Journal replayed",))
    return row


def _refusals(db):
    return [r for r in db.get_recent_errors() if r["action"] == "JOURNAL_NOT_REPLAYED"]


# ── Each line is numbered, and the database knows the number first ──


def test_every_line_is_numbered_in_order_and_the_database_knows_the_last(db):
    db.record_opt_out(ANNA, "Opted out by email")
    db.block_domain("third.de", "objection")
    db.add_to_suppression(LUCA, "bounce")

    numbers = [e["seq"] for e in _lines(db.db_path)]
    assert numbers == [1, 2, 3]
    assert db.get_state(journal.LAST_LINE) == "3"
    assert journal.unreplayed(db.db_path) == 0


def test_a_line_the_database_cannot_number_is_still_written(tmp_path, caplog):
    """The event is already committed: an opt-out line must never be lost
    because its number could not be recorded."""
    missing = tmp_path / "nowhere" / "agent.db"
    missing.parent.mkdir()

    with caplog.at_level(logging.ERROR, logger="modules.journal"):
        journal.append(missing, "opt_out", email=ANNA)

    (line,) = _lines(missing)
    assert line["email"] == ANNA and "seq" not in line
    assert "the restore guard cannot see it" in caplog.text
    assert not missing.exists(), "numbering a line made a database of its own"


def test_two_writers_at_once_never_look_like_a_restore(db):
    """The agent's thread and the dashboard's write lines at the same time."""
    other = Database(db.db_path)

    def opt_outs(who, n):
        for i in range(n):
            who.record_opt_out(f"person{i}@{id(who)}.example", "Opted out by email")
        who.close_thread_conn()

    threads = [
        threading.Thread(target=opt_outs, args=(db, 25)),
        threading.Thread(target=opt_outs, args=(other, 25)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    numbers = sorted(e["seq"] for e in _lines(db.db_path))
    assert numbers == list(range(1, 51))
    assert journal.unreplayed(db.db_path) == 0
    assert db.get_state(journal.BEHIND_SINCE) is None


# ── Never refused: healthy, fresh, before the numbers, trimmed ──


def test_a_healthy_install_starts(s, db):
    db.record_opt_out(ANNA, "Opted out by email")
    backup.make_backup(db)
    db.record_opt_out(LUCA, "Opted out by email")

    assert journal.unreplayed(db.db_path) == 0
    assert main.unreplayed_journal(s) is None
    row = _doctor_row(s)
    assert row.ok is True and row.blocking is True
    main.refuse_a_start_before_the_replay(s)


def test_a_fresh_install_with_no_database_and_no_journal_starts(s):
    assert not os.path.exists(s.get_abs_db_path())

    assert journal.unreplayed(s.get_abs_db_path()) == 0
    assert main.unreplayed_journal(s) is None
    assert _doctor_row(s).ok is True


def test_a_journal_from_before_the_numbers_is_not_a_restore(s, db):
    """An install updated with a journal already written: none of its lines
    is numbered, and the next one numbers on from 1."""
    _unnumbered(
        db.db_path,
        [{"kind": "opt_out", "email": who} for who in (ANNA, LUCA)],
    )

    assert journal.unreplayed(db.db_path) == 0
    db.record_opt_out(MARIA, "Opted out by email")
    assert _lines(db.db_path)[-1]["seq"] == 1
    assert journal.unreplayed(db.db_path) == 0
    assert db.get_state(journal.BEHIND_SINCE) is None


def test_a_journal_the_backups_trimmed_is_not_a_restore(s, db):
    db.record_opt_out(ANNA, "Opted out by email")
    db.record_opt_out(LUCA, "Opted out by email")
    path = journal.journal_path(db.db_path)
    # Time passes: both lines are now older than every backup.
    aged = [{**e, "at": "2020-01-01T10:00:00+01:00"} for e in _lines(db.db_path)]
    path.write_text("".join(json.dumps(e) + "\n" for e in aged), encoding="utf-8")

    backup.make_backup(db)

    assert _lines(db.db_path) == [], "setup: the backup routine trimmed them"
    assert journal.unreplayed(db.db_path) == 0
    db.record_opt_out(MARIA, "Opted out by email")
    assert _lines(db.db_path)[-1]["seq"] == 3
    assert journal.unreplayed(db.db_path) == 0
    assert main.unreplayed_journal(s) is None


def test_a_long_journal_is_numbered_from_its_end(db, monkeypatch):
    """Only the end of the journal is read to number the next line; a journal
    whose end holds no numbered line is read whole."""
    monkeypatch.setattr(journal, "TAIL_BYTES", 300)
    db.record_opt_out(ANNA, "Opted out by email")
    _unnumbered(db.db_path, [{"kind": "dsr", "email": f"p{i}@x.de"} for i in range(10)])
    # The database lost its record of line 1.
    db.set_state(journal.LAST_LINE, "0")

    db.record_opt_out(LUCA, "Opted out by email")

    assert _lines(db.db_path)[-1]["seq"] == 2
    assert db.get_state(journal.BEHIND_SINCE) == "0"
    assert journal.unreplayed(db.db_path) == 2


# ── A restore: refused everywhere until the replay ──


def test_a_restored_database_is_behind_by_the_lines_since_the_backup(db):
    restored = _restored_after_two_opt_outs(db)

    assert journal.unreplayed(restored.db_path) == 2


def test_run_refuses_and_names_the_replay(s, db, built, monkeypatch, capsys, caplog):
    restored = _restored_after_two_opt_outs(db)

    with caplog.at_level(logging.ERROR):
        out = _refused_run(s, monkeypatch, capsys)

    assert "Refusing to start - the database is behind its journal by 2" in out
    assert "main.py replay-journal" in out
    assert built == [], "an agent was built before the replay"
    assert "Refusing to start - the database is behind its journal" in caplog.text
    assert len(_refusals(restored)) == 1
    assert "replay-journal" in _refusals(restored)[0]["details"]


@pytest.mark.parametrize("argv", [("--dry-run",), ("--continuous",)])
def test_every_kind_of_run_is_refused(s, db, built, monkeypatch, capsys, argv):
    _restored_after_two_opt_outs(db)

    out = _refused_run(s, monkeypatch, capsys, *argv)

    assert "replay-journal" in out
    assert built == []


def test_doctor_fails_it_as_blocking(s, db, monkeypatch, capsys):
    _restored_after_two_opt_outs(db)

    row = _doctor_row(s)
    assert row.ok is False and row.blocking is True
    assert "behind its journal by 2" in row.detail
    assert "main.py replay-journal" in row.detail
    readiness = main.readiness_report(s)
    assert readiness["can_send_live"] is False
    card = [c for c in readiness["checks"] if c["label"] == "Journal replayed"]
    assert card and card[0]["ok"] is False and card[0]["blocking"] is True


@pytest.mark.parametrize("mode", ["single", "continuous", "dry_run"])
def test_the_dashboards_start_refuses(s, db, built, mode):
    restored = _restored_after_two_opt_outs(db)
    client = main.create_dashboard_app(s).test_client()

    resp = client.post("/api/agent/start", json={"mode": mode})

    assert resp.status_code == 409
    error = resp.get_json()["error"]
    assert "Nothing was started or sent" in error
    assert "behind its journal" in error and "main.py replay-journal" in error
    assert built == []
    assert client.get("/api/agent/status").get_json()["running"] is False
    assert len(_refusals(restored)) == 1


def test_the_dashboards_direct_send_refuses(s, db, built):
    restored = _restored_after_two_opt_outs(db)
    client = main.create_dashboard_app(s).test_client()

    resp = client.post("/api/send-manual", json={"email": LUCA, "name": "Luca"})

    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert "Nothing was started or sent" in body["message"]
    assert "main.py replay-journal" in body["message"]
    assert built == []
    assert restored.get_contact_by_email(LUCA) is None
    assert len(_refusals(restored)) == 1


def test_replay_journal_clears_it(s, db, built, monkeypatch, capsys):
    restored = _restored_after_two_opt_outs(db)
    client = main.create_dashboard_app(s).test_client()
    assert client.post("/api/agent/start", json={"mode": "dry_run"}).status_code == 409

    out = _cli(s, monkeypatch, capsys, "replay-journal")

    assert "The agent may start." in out
    assert restored.is_suppressed(LUCA) and restored.is_suppressed(MARIA)
    assert journal.unreplayed(restored.db_path) == 0
    assert _doctor_row(s).ok is True
    assert client.post("/api/agent/start", json={"mode": "dry_run"}).status_code == 200
    _cli(s, monkeypatch, capsys, "run", "--dry-run")
    assert len(built) == 2


def test_a_replay_that_stops_on_an_error_leaves_it_refused(s, db, monkeypatch):
    restored = _restored_after_two_opt_outs(db)

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(journal, "_redact_companies", broken)
    with pytest.raises(sqlite3.OperationalError):
        journal.replay_journal(restored)

    assert journal.unreplayed(restored.db_path) == 2
    assert main.unreplayed_journal(s) is not None


def test_new_lines_after_a_restore_do_not_hide_the_gap(s, db):
    """Blocks and opt-outs recorded on the restored database before the
    replay, more of them than the lines it missed, number on past the gap."""
    restored = _restored_after_two_opt_outs(db)

    for domain in ("one.de", "two.de", "three.de", "four.de"):
        restored.block_domain(domain, "Blocked from the dashboard")

    assert journal.unreplayed(restored.db_path) >= 2
    assert main.unreplayed_journal(s) is not None
    journal.replay_journal(restored)
    assert journal.unreplayed(restored.db_path) == 0
    assert restored.get_state(journal.BEHIND_SINCE) is None


def test_a_gap_once_seen_stays_until_the_replay_though_its_lines_are_gone(s, db):
    restored = _restored_after_two_opt_outs(db)
    restored.block_domain("one.de", "Blocked from the dashboard")
    journal.journal_path(restored.db_path).write_text("", encoding="utf-8")

    assert journal.unreplayed(restored.db_path) == 1
    assert main.unreplayed_journal(s) is not None


def _aged(db_path):
    """Time passes: every line is older than any backup made from now on."""
    path = journal.journal_path(db_path)
    aged = [{**e, "at": "2020-01-01T10:00:00+01:00"} for e in _lines(db_path)]
    path.write_text("".join(json.dumps(e) + "\n" for e in aged), encoding="utf-8")


def test_a_backup_before_the_replay_keeps_the_lines_the_replay_needs(
    s, db, monkeypatch, caplog
):
    """The nightly backup runs between the restore and the replay, and the
    restored copy is no longer among the copies kept. Its trim dropped the
    two opt-outs since the backup: the guard then let the agent start, and
    the replay had nothing to put back."""
    restored = _restored_after_two_opt_outs(db)
    _aged(restored.db_path)
    monkeypatch.setattr(backup, "BACKUP_COPIES", 1)

    with caplog.at_level(logging.ERROR, logger="modules.backup"):
        backup.make_backup(restored)

    assert journal.unreplayed(restored.db_path) == 2
    assert main.unreplayed_journal(s) is not None
    assert "Not trimming the journal" in caplog.text
    assert "replay-journal" in caplog.text
    journal.replay_journal(restored)
    assert restored.is_suppressed(LUCA) and restored.is_suppressed(MARIA)
    assert journal.unreplayed(restored.db_path) == 0
    backup.make_backup(restored)
    assert _lines(restored.db_path) == [], "once replayed, the next backup trims"


def test_a_backup_that_cannot_tell_keeps_the_journal(db, monkeypatch, caplog):
    db.record_opt_out(ANNA, "Opted out by email")
    _aged(db.db_path)

    def unreadable(_path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(journal, "unreplayed", unreadable)
    with caplog.at_level(logging.ERROR, logger="modules.backup"):
        backup.make_backup(db)

    assert [e["email"] for e in _lines(db.db_path)] == [ANNA]
    assert "could not be checked" in caplog.text


def test_an_erasure_line_the_rewrite_adds_is_numbered_too(db, monkeypatch):
    """The journal refused the erasure's own line, and the rewrite after the
    commit adds it: it counts for the guard like any other line."""
    from modules.database import erasure_token
    from modules.export import DataExporter

    db.add_contact(db.log_search("q"), ANNA, organization="Brand")
    db.record_opt_out(LUCA, "Opted out by email")
    taken = backup.make_backup(db)
    real = journal.append

    def refuses_the_erasure(db_path, kind, **fields):
        if kind != "erasure":
            real(db_path, kind, **fields)

    monkeypatch.setattr(journal, "append", refuses_the_erasure)
    DataExporter(db).gdpr_erase(ANNA)

    (line,) = [e for e in _lines(db.db_path) if e["kind"] == "erasure"]
    assert line["token"] == erasure_token(ANNA)
    # After the company's block, which the erasure keeps as its token.
    assert line["seq"] == 3 == int(db.get_state(journal.LAST_LINE))
    assert journal.unreplayed(db.db_path) == 0
    restored = _restore(db, taken)
    assert journal.unreplayed(restored.db_path) == 2


def test_a_lost_database_beside_its_journal_is_refused(s, db):
    db.record_opt_out(ANNA, "Opted out by email")
    path = db.db_path
    db.close_thread_conn()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)

    assert journal.unreplayed(path) == 1
    assert "behind its journal by 1" in main.unreplayed_journal(s)
    fresh = Database(path)
    journal.replay_journal(fresh)
    assert fresh.is_suppressed(ANNA)
    assert journal.unreplayed(path) == 0


def test_a_check_that_cannot_be_made_refuses(s, db, monkeypatch):
    def unreadable(_path):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(journal, "unreplayed", unreadable)

    refusal = main.unreplayed_journal(s)
    assert refusal and "could not be checked" in refusal
    with pytest.raises(SystemExit):
        main.refuse_a_start_before_the_replay(s)


def test_a_journal_that_is_not_utf8_is_refused_in_words(
    s, db, built, monkeypatch, capsys
):
    """A damaged byte in the journal: run stopped on a traceback, and the
    dashboard's Start answered with a server error."""
    db.record_opt_out(ANNA, "Opted out by email")
    with open(journal.journal_path(db.db_path), "ab") as f:
        f.write(b"\xff\xfe not utf-8\n")

    out = _refused_run(s, monkeypatch, capsys)
    assert "Refusing to start - whether the database is behind its journal" in out
    client = main.create_dashboard_app(s).test_client()
    resp = client.post("/api/agent/start", json={"mode": "single"})
    assert resp.status_code == 409
    assert "could not be checked" in resp.get_json()["error"]
    assert built == []


# ── The restore drill ──


def test_the_drill_shows_the_guard_before_its_replay_and_clears_it(
    s, db, monkeypatch, capsys
):
    db.record_opt_out(ANNA, "Opted out by email")
    backup.make_backup(db)
    db.record_opt_out(LUCA, "Opted out by email")
    db.close_thread_conn()

    out = _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill PASSED" in out
    assert (
        "Start guard:   1 journal line(s) newer than the backup before the "
        "replay, 0 after it"
    ) in out
    assert journal.unreplayed(s.get_abs_db_path()) == 0, "the live database moved"


def test_a_fresh_backup_drill_finds_nothing_newer(s, db, monkeypatch, capsys):
    db.record_opt_out(ANNA, "Opted out by email")
    db.close_thread_conn()

    out = _cli(s, monkeypatch, capsys, "restore-drill")

    assert "Restore drill PASSED" in out
    assert "0 journal line(s) newer than the backup before the replay, 0" in out


def test_the_drill_fails_when_its_replay_leaves_the_guard(s, db, monkeypatch, capsys):
    db.record_opt_out(ANNA, "Opted out by email")
    backup.make_backup(db)
    db.record_opt_out(LUCA, "Opted out by email")
    db.close_thread_conn()
    monkeypatch.setattr(journal, "_mark_replayed", lambda *_a, **_k: None)

    with pytest.raises(SystemExit) as stopped:
        _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "the restored copy would still refuse to start" in out
    assert "Restore drill FAILED" in out
