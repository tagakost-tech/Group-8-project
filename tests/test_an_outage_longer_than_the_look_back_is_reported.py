"""An inbox outage longer than the look-back is reported, not skipped silently.

A read reaches back to the last complete read, but never more than 14 days
(ImapMonitor.MAX_LOOKBACK_HOURS). After a longer outage - the agent stopped,
or its inbox login failing - the first complete read searched the last 14
days only, moved the watermark to now and logged nothing. Opt-outs, data
requests and bounces received before that were never read, and nothing said
so (GDPR Art. 21(3), Art. 12(3)).

Now the read that moves a folder's watermark past such a stretch records it
once: an IMAP_GAP error in the activity log, which the dashboard shows,
naming the inbox, the folder and the dates to check by hand. doctor repeats
those rows for a month, and warns as soon as the inbox's last complete read
is further back than a read reaches, so an operator hears of it before
restarting a stopped agent. An outage the look-back covers raises nothing, and
while reads keep failing the stretch is not final yet, so the read that
completes records it.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

import main
from modules import clock
from modules.database import Database
from modules.imap_monitor import ImapMonitor, watermark_key
from tests.test_imap_inbox_scan import FakeIMAP, _raw, _searched_cutoff
from tests.test_the_junk_folder_is_read_too import FolderIMAP

INBOX = "me@corp.com"
OPT_OUT = _raw("anna@brand.it", "Re: hello", "Please remove me from your mailing list.")


@pytest.fixture(autouse=True)
def _a_day_far_from_a_clock_change(monkeypatch):
    fixed = datetime(2026, 6, 15, 12, 0, tzinfo=clock.get_timezone())
    monkeypatch.setattr(clock, "now", lambda: fixed)


def _monitor(tmp_path, fake):
    db = Database(str(tmp_path / "gap.db"))
    monitor = ImapMonitor(db, "imap.test", 993, INBOX, "pw")
    monitor._connect = lambda: fake
    return monitor, db


def _last_read(db, days_ago, key=None):
    when = clock.now() - timedelta(days=days_ago)
    db.set_state(key or watermark_key(INBOX), when.isoformat())
    return when


def _junk_key():
    return watermark_key(INBOX) + "::junk"


def _gaps(db):
    return [r for r in db.get_recent_activity(50) if r["action"] == "IMAP_GAP"]


# 14.03 days: 43 minutes past the look-back. SINCE is date-only and the server
# compares it in its own timezone, which the agent does not know, so that
# stretch may well have been read: the scan's window is all the agent can vouch
# for, and the report says the mail may be unread, to the minute.
@pytest.mark.parametrize("days", [14.03, 30, 400])
def test_a_read_after_a_long_outage_names_the_mail_it_skipped(tmp_path, days):
    fake = FakeIMAP([])
    monitor, db = _monitor(tmp_path, fake)
    last = _last_read(db, days)

    monitor.run_check()

    rows = _gaps(db)
    assert len(rows) == 1, "mail beyond the look-back went unread and nothing said so"
    assert rows[0]["status"] == "error"
    details = rows[0]["details"]
    assert INBOX in details and "INBOX" in details and "by hand" in details
    assert "may not have been read" in details
    assert last.strftime("%Y-%m-%d %H:%M") in details
    window_start = clock.now() - timedelta(hours=ImapMonitor.MAX_LOOKBACK_HOURS)
    assert window_start.strftime("%Y-%m-%d %H:%M") in details
    assert _searched_cutoff(fake).strftime("%Y-%m-%d") in details


def test_a_watermark_without_a_timezone_is_read_as_local_time(tmp_path):
    """_lookback_hours tolerates one written before the clock module owned it."""
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    naive = (clock.now() - timedelta(days=30)).replace(tzinfo=None)
    db.set_state(watermark_key(INBOX), naive.isoformat())

    summary = monitor.run_check()

    assert summary["ok"] is True
    rows = _gaps(db)
    assert len(rows) == 1 and naive.strftime("%Y-%m-%d %H:%M") in rows[0]["details"]


def test_the_mail_within_reach_is_still_acted_on(tmp_path):
    monitor, db = _monitor(tmp_path, FakeIMAP([OPT_OUT]))
    _last_read(db, 30)

    summary = monitor.run_check()

    assert summary["ok"] is True and db.is_suppressed("anna@brand.it")
    assert len(_gaps(db)) == 1


def test_the_dashboard_lists_the_gap_among_its_errors(settings_factory, tmp_path):
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, 30)
    monitor.run_check()

    s = settings_factory(db_path=str(tmp_path / "gap.db"))
    errors = main.create_dashboard_app(s).test_client().get("/api/errors").get_json()

    assert any(e["action"] == "IMAP_GAP" and INBOX in e["details"] for e in errors)


def test_the_gap_is_reported_once(tmp_path):
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, 30)

    for _ in range(3):
        monitor.run_check()

    assert len(_gaps(db)) == 1


# 13.97 days: with its slack the window is at its cap, yet reaches the last read.
@pytest.mark.parametrize("days", [13, 13.97])
def test_an_outage_the_look_back_covers_reports_nothing(tmp_path, days):
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, days)

    monitor.run_check()

    assert _gaps(db) == []


def test_a_first_read_reports_nothing(tmp_path):
    monitor, db = _monitor(tmp_path, FakeIMAP([]))

    monitor.run_check()

    assert _gaps(db) == []


def test_the_read_that_completes_records_the_gap(tmp_path):
    """While reads fail the watermark holds and the stretch keeps growing."""
    fake = FakeIMAP([], search_status="NO")
    monitor, db = _monitor(tmp_path, fake)
    _last_read(db, 30)

    monitor.run_check()
    assert _gaps(db) == []

    fake.search_status = "OK"
    monitor.run_check()
    assert len(_gaps(db)) == 1


def test_a_gap_in_the_junk_folder_is_reported_too(tmp_path):
    fake = FolderIMAP({"INBOX": [], "Junk Email": []}, junk_flag="Junk Email")
    monitor, db = _monitor(tmp_path, fake)
    _last_read(db, 0)
    _last_read(db, 30, key=_junk_key())

    monitor.run_check()

    rows = _gaps(db)
    assert len(rows) == 1 and "Junk Email" in rows[0]["details"]


def test_an_unreadable_junk_folder_holds_the_inbox_report_too(tmp_path):
    """INBOX's watermark waits for the whole account, and so does its report."""
    fake = FolderIMAP(
        {"INBOX": [], "Junk Email": []},
        junk_flag="Junk Email",
        select_fails={"Junk Email"},
    )
    monitor, db = _monitor(tmp_path, fake)
    _last_read(db, 30)

    monitor.run_check()
    monitor.run_check()
    assert _gaps(db) == []

    fake.select_fails.clear()
    monitor.run_check()
    assert len(_gaps(db)) == 1


def test_a_gap_that_cannot_be_recorded_waits_for_the_next_read(tmp_path, monkeypatch):
    """Once the watermark moves past the stretch, nothing else remembers it."""
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, 30)
    before = db.get_state(watermark_key(INBOX))

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(db, "log_activity", locked)
        summary = monitor.run_check()

    assert summary["ok"] is True
    assert db.get_state(watermark_key(INBOX)) == before

    monitor.run_check()
    assert len(_gaps(db)) == 1


def test_a_lock_while_looking_for_the_gap_does_not_stop_the_read(tmp_path, monkeypatch):
    """The window was worked out from the watermark; then the database locks.
    Nothing about the watermark may stop the scan (_lookback_hours): an opt-out
    in the window is honoured all the same, and the next read reports the gap.
    """
    monitor, db = _monitor(tmp_path, FakeIMAP([OPT_OUT]))
    _last_read(db, 30)
    real_get_state = db.get_state
    reads = []

    def locked_after_the_first_read(key, *args):
        if key == watermark_key(INBOX):
            reads.append(key)
            if len(reads) > 1:
                raise sqlite3.OperationalError("database is locked")
        return real_get_state(key, *args)

    with monkeypatch.context() as m:
        m.setattr(db, "get_state", locked_after_the_first_read)
        summary = monitor.run_check()

    assert summary["ok"] is True and db.is_suppressed("anna@brand.it")

    monitor.run_check()
    assert len(_gaps(db)) == 1


def _doctor(settings_factory, tmp_path, **overrides):
    overrides.setdefault("imap_enabled", True)
    settings = settings_factory(
        db_path=str(tmp_path / "gap.db"),
        smtp_username=INBOX,
        smtp_password="pw",
        **overrides,
    )
    checks = {c.label: c for c in main.run_doctor_checks(settings, network=False)}
    check = checks.get("Inbox gap")
    assert check is not None, "doctor says nothing about mail the agent skipped"
    return check


def test_doctor_warns_about_the_mail_the_agent_skipped(settings_factory, tmp_path):
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    last = _last_read(db, 30)
    monitor.run_check()

    check = _doctor(settings_factory, tmp_path)

    assert check.ok is False
    assert INBOX in check.detail and last.strftime("%Y-%m-%d") in check.detail
    # Live sends are not refused for it, so doctor must not say they are.
    assert not check.blocking


def test_doctor_warns_before_the_read_that_will_skip_the_mail(
    settings_factory, tmp_path
):
    """An operator runs doctor before restarting an agent that has been off."""
    _, db = _monitor(tmp_path, FakeIMAP([]))
    last = _last_read(db, 30)

    check = _doctor(settings_factory, tmp_path)

    assert check.ok is False
    assert INBOX in check.detail and last.strftime("%Y-%m-%d") in check.detail


def test_doctor_looks_at_every_inbox(settings_factory, tmp_path):
    _, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, 0)
    _last_read(db, 30, key=watermark_key("two@corp.com"))

    check = _doctor(
        settings_factory,
        tmp_path,
        sender_2_email="two@corp.com",
        sender_2_password="pw",
    )

    assert check.ok is False and "two@corp.com" in check.detail
    assert INBOX not in check.detail


def test_doctor_warns_about_a_gap_in_the_junk_folder(settings_factory, tmp_path):
    fake = FolderIMAP({"INBOX": [], "Junk Email": []}, junk_flag="Junk Email")
    monitor, db = _monitor(tmp_path, fake)
    _last_read(db, 0)
    _last_read(db, 30, key=_junk_key())
    monitor.run_check()

    check = _doctor(settings_factory, tmp_path)

    assert check.ok is False and "Junk Email" in check.detail


def test_doctor_lists_every_gap_found_this_month(settings_factory, tmp_path):
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    first = _last_read(db, 30)
    monitor.run_check()
    second = _last_read(db, 20)
    monitor.run_check()

    detail = _doctor(settings_factory, tmp_path).detail

    assert first.strftime("%Y-%m-%d") in detail
    assert second.strftime("%Y-%m-%d") in detail


def test_doctor_finds_no_gap_when_there_was_none(settings_factory, tmp_path):
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, 13)
    monitor.run_check()

    assert _doctor(settings_factory, tmp_path).ok is True


def test_doctor_reads_a_watermark_without_a_timezone_as_local_time(
    settings_factory, tmp_path
):
    _, db = _monitor(tmp_path, FakeIMAP([]))
    naive = (clock.now() - timedelta(days=30)).replace(tzinfo=None)
    db.set_state(watermark_key(INBOX), naive.isoformat())

    check = _doctor(settings_factory, tmp_path)

    assert check.ok is False and naive.strftime("%Y-%m-%d") in check.detail


def test_doctor_counts_only_the_gaps_among_the_month_s_activity(
    settings_factory, tmp_path
):
    _, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, 0)
    db.log_activity("EMAIL_SENT", "Sent to anna@brand.it")
    db.log_activity("IMAP_ERROR", "Login failed for me@corp.com", status="error")

    check = _doctor(settings_factory, tmp_path)

    assert check.ok is True
    assert "anna@brand.it" not in check.detail


def test_doctor_leaves_an_inbox_never_read_to_the_inbox_read_check(
    settings_factory, tmp_path
):
    assert _doctor(settings_factory, tmp_path).ok is True


def test_doctor_leaves_a_corrupt_watermark_to_the_inbox_read_check(
    settings_factory, tmp_path
):
    _, db = _monitor(tmp_path, FakeIMAP([]))
    db.set_state(watermark_key(INBOX), "not-a-date")

    assert _doctor(settings_factory, tmp_path).ok is True


def test_doctor_does_not_warn_for_ever_about_a_junk_folder_since_gone(
    settings_factory, tmp_path
):
    """Its watermark stands still for good; the read that finds it reports it."""
    _, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, 0)
    _last_read(db, 30, key=_junk_key())

    assert _doctor(settings_factory, tmp_path).ok is True


def test_doctor_stops_warning_a_month_after_the_gap_was_found(
    settings_factory, tmp_path, monkeypatch
):
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, 30)
    monitor.run_check()

    a_month_on = clock.add_months(clock.now(), 1)
    monkeypatch.setattr(clock, "now", lambda: a_month_on)
    _last_read(db, 0)  # the reads went on
    assert _doctor(settings_factory, tmp_path).ok is False

    a_moment_later = a_month_on + timedelta(seconds=1)
    monkeypatch.setattr(clock, "now", lambda: a_moment_later)
    _last_read(db, 0)
    assert _doctor(settings_factory, tmp_path).ok is True


def test_doctor_warns_for_a_calendar_month_not_30_days(
    settings_factory, tmp_path, monkeypatch
):
    """Found on 15 July, the gap is still due a look on 15 August: July has 31 days."""
    mid_july = datetime(2026, 7, 15, 12, 0, tzinfo=clock.get_timezone())
    monkeypatch.setattr(clock, "now", lambda: mid_july)
    monitor, db = _monitor(tmp_path, FakeIMAP([]))
    _last_read(db, 30)
    monitor.run_check()

    thirty_and_a_half_days_on = mid_july + timedelta(days=30, hours=12)
    monkeypatch.setattr(clock, "now", lambda: thirty_and_a_half_days_on)
    _last_read(db, 0)

    assert _doctor(settings_factory, tmp_path).ok is False


def test_doctor_skips_it_with_imap_off(settings_factory, tmp_path):
    assert _doctor(settings_factory, tmp_path, imap_enabled=False).ok is None
