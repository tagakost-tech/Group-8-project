"""Retention runs on its own, and collection stops at what can be emailed.

Only a live run of the agent applied retention, once a day. While the agent
is not run - as now, until counsel signs the country table (L1.9) - nothing
was ever erased. And the agent collected up to 300 waiting contacts whatever
its sending rate, although at the pilot's 10-30 a day most could never be
emailed within the 30-day first-contact limit (L1.8).

Now `main.py retention` applies the periods, for a daily scheduled task, and
records the run as the agent does. `--dry-run` counts what would go and
changes nothing. And collection - discovery and searches already queued -
pauses while the backlog is at least what the mailboxes can email within the
limit: today's allowance across mailboxes times the sending days within it.
"""

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import main
from agent import OutreachAgent
from modules import clock
from modules.database import Database
from modules.export import write_access_answer

EMAIL = "anna.rossi@brandco.de"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "r.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _old_contact(db):
    cid = db.add_contact(db.log_search("q"), EMAIL, name="Anna", organization="Brand")
    db.conn.execute(
        "UPDATE contacts SET timestamp = datetime('now','localtime','-40 days') "
        "WHERE id = ?",
        (cid,),
    )
    db.conn.commit()  # log_activity writes through its own connection
    db.log_activity("SUPPRESSED", f"Skipped {EMAIL}")
    db.conn.execute(
        "UPDATE activity_log SET timestamp = datetime('now','localtime','-200 days')"
    )
    db.conn.commit()
    return cid


def test_the_command_applies_the_periods_and_records_the_run(
    s, db, monkeypatch, capsys
):
    cid = _old_contact(db)

    out = _cli(s, monkeypatch, capsys, "retention")

    assert db.get_contact_by_id(cid)["status"] == "erased"
    assert db.get_state("retention:last_run_date") == clock.today_str()
    assert db.conn.execute(
        "SELECT 1 FROM activity_log WHERE action = 'RETENTION_APPLIED'"
    ).fetchone()
    assert EMAIL not in out
    assert "erased never contacted" in out and "activity masked" in out


def test_a_dry_run_changes_nothing(s, db, monkeypatch, capsys):
    cid = _old_contact(db)
    answer = write_access_answer(db, EMAIL, controller="T S.r.l.", notice_url="")
    month_ago = time.time() - 31 * 86400
    for f in answer.iterdir():
        os.utime(f, (month_ago, month_ago))

    out = _cli(s, monkeypatch, capsys, "retention", "--dry-run")

    assert answer.exists()
    assert db.get_contact_by_id(cid)["status"] == "pending"
    assert (
        db.conn.execute(
            "SELECT details FROM activity_log WHERE action = 'SUPPRESSED'"
        ).fetchone()[0]
        == f"Skipped {EMAIL}"
    )
    assert db.get_state("retention:last_run_date") is None
    assert "Dry run" in out
    lines = {line.split()[-1] for line in out.splitlines() if "never contacted" in line}
    assert lines == {"1"}, out


def test_the_command_respects_the_switch(
    settings_factory, tmp_path, monkeypatch, capsys
):
    s = settings_factory(db_path=str(tmp_path / "off.db"), retention_enabled=False)
    db = Database(s.get_abs_db_path())
    cid = _old_contact(db)
    out = _cli(s, monkeypatch, capsys, "retention")
    assert db.get_contact_by_id(cid)["status"] == "pending"
    assert "RETENTION_ENABLED" in out


# ── Collection stops at what can be emailed within the limit ──


def _agent(settings_factory, tmp_path, **kw):
    kw.setdefault("warmup_start_daily", 2)
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            heuristic_discovery_enabled=False,
            target_queries="",
            **kw,
        )
    )


MONDAY = datetime(2026, 11, 2, 9, 0, tzinfo=ZoneInfo("Europe/Rome"))


def test_capacity_is_the_allowance_times_the_sending_days_within_the_limit(
    settings_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(clock, "now", lambda: MONDAY)
    weekdays = _agent(settings_factory, tmp_path, FIRST_CONTACT_MAX_AGE_DAYS=10)
    # Mon 2 Nov + 10 days: Mon-Fri, then Mon-Wed - 8 sending days, 2 a day.
    assert weekdays.loop_orchestrator.first_contact_capacity() == 16

    every_day = _agent(
        settings_factory,
        tmp_path,
        FIRST_CONTACT_MAX_AGE_DAYS=10,
        send_window_enabled=False,
    )
    assert every_day.loop_orchestrator.first_contact_capacity() == 20

    unlimited = _agent(settings_factory, tmp_path, warmup_enabled=False)
    assert unlimited.loop_orchestrator.first_contact_capacity() is None


@pytest.mark.parametrize("waiting,collects", [(4, True), (5, False)])
def test_collection_pauses_at_capacity(
    settings_factory, tmp_path, monkeypatch, waiting, collects
):
    a = _agent(
        settings_factory,
        tmp_path,
        FIRST_CONTACT_MAX_AGE_DAYS=5,
        send_window_enabled=False,
        warmup_start_daily=1,
    )
    assert a.loop_orchestrator.first_contact_capacity() == 5  # 1 a day, 5 days
    sid = a.db.log_search("q", source="test")
    for i in range(waiting):
        a.db.add_contact(sid, f"buyer{i}@brand{i}.de", organization=f"Brand {i}")
    a.db.log_search("queued query", source="manual", status="pending")

    searched = []
    a._process_pending_contacts = lambda *_a, **_k: None
    a.run_single = lambda queries, *_a, **_k: searched.append(queries)
    a._interruptible_sleep = lambda *_a, **_k: setattr(a, "_running", False)
    a.run_continuous(dry_run=True, loop_delay=60)

    assert bool(searched) is collects
