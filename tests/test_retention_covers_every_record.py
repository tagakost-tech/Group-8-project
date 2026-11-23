"""Retention reaches every record that holds personal data, not just contacts.

The daily job erased contacts and cleared open-tracking device data, and
nothing else ever aged out: addresses in the activity log, the reply text
and recipients of every lead alert, closed data-subject requests, access
answers written to disk, daily send counts per recipient domain, and bounced
addresses on the suppression list were all kept indefinitely.

Now, per the decision sheet's L5.1 (the developer's proposals, pending
counsel):
- activity-log lines are masked after 180 days (as log files are);
- a lead alert's reply excerpt and recipients are cleared after 30 days;
  the alert itself stays, so the lead is not alerted twice;
- a closed request is deleted after 1,095 days; an open one never is;
- an access answer is deleted 30 days after it was written;
- daily send counts per domain are deleted after 30 days;
- a bounced address is released after 24 months. An opt-out never is.
"""

import os
import time
from datetime import timedelta

import pytest

from modules import clock
from modules.database import Database
from modules.export import export_folder, write_access_answer
from modules.retention import apply_retention

EMAIL = "anna.rossi@brandco.de"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "r.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


def _age(db, table, column, days, where="1"):
    db.conn.execute(
        f"UPDATE {table} SET {column} = datetime('now','localtime',?) WHERE {where}",
        (f"-{days} days",),
    )
    db.conn.commit()


def test_old_activity_lines_are_masked(db, s):
    db.log_activity("SUPPRESSED", f"Skipped {EMAIL}: opted out")
    _age(db, "activity_log", "timestamp", 181)
    db.log_activity("SUPPRESSED", "Skipped marco@brandco.de: opted out")

    apply_retention(db, s)

    old, new = [
        r[0]
        for r in db.conn.execute(
            "SELECT details FROM activity_log WHERE action = 'SUPPRESSED' ORDER BY id"
        )
    ]
    assert EMAIL not in old and "a***@brandco.de" in old
    assert "marco@brandco.de" in new, "a line under 180 days old is kept as written"


def test_a_lead_alert_loses_its_text_after_30_days(db, s):
    db.record_lead_alert(EMAIL, notified_to="sales@co.it", reply_excerpt="Yes please")
    _age(db, "lead_alerts", "timestamp", 31)
    db.record_lead_alert(
        "marco@brandco.de", notified_to="sales@co.it", reply_excerpt="Ok"
    )

    apply_retention(db, s)

    rows = {
        r[0]: (r[1], r[2])
        for r in db.conn.execute(
            "SELECT email, notified_to, reply_excerpt FROM lead_alerts"
        )
    }
    assert rows[EMAIL] == (None, None)
    assert rows["marco@brandco.de"] == ("sales@co.it", "Ok")
    assert db.was_lead_alerted(EMAIL), "the alert stays, so it is not sent twice"


def test_a_closed_request_is_deleted_after_1095_days(db, s):
    old_closed = db.add_dsr(EMAIL, "access", "reply")
    db.close_dsr(old_closed, "answered")
    recent_closed = db.add_dsr("marco@brandco.de", "access", "reply")
    db.close_dsr(recent_closed, "answered")
    old_open = db.add_dsr("luca@brandco.de", "erasure", "reply")
    long_ago = (clock.now() - timedelta(days=1096)).isoformat()
    db.conn.execute(
        "UPDATE dsr_requests SET received_at = ?, closed_at = ? WHERE id = ?",
        (long_ago, long_ago, old_closed),
    )
    db.conn.execute(
        "UPDATE dsr_requests SET received_at = ? WHERE id = ?", (long_ago, old_open)
    )
    db.conn.commit()

    apply_retention(db, s)

    assert db.get_dsr(old_closed) is None
    assert db.get_dsr(recent_closed) is not None
    assert db.get_dsr(old_open) is not None, "an open request is never deleted"


def test_an_access_answer_is_deleted_after_30_days(db, s):
    def answer(email, days_old):
        folder = write_access_answer(
            db, email, controller="Test S.r.l.", notice_url="https://x.test/p"
        )
        then = time.time() - days_old * 86400
        for f in folder.iterdir():
            os.utime(f, (then, then))
        return folder

    old = answer(EMAIL, 31)
    fresh = answer("marco@brandco.de", 5)

    apply_retention(db, s)

    assert not old.exists()
    assert fresh.exists()
    assert export_folder(db.db_path, EMAIL).parent.exists()


def test_daily_send_counts_are_deleted_after_30_days(db, s):
    old_day = (clock.now() - timedelta(days=31)).strftime("%Y-%m-%d")
    db.conn.executemany(
        "INSERT INTO domain_send_counts (date, domain, count) VALUES (?, ?, 1)",
        [(old_day, "brandco.de"), (clock.today_str(), "brandco.de")],
    )
    db.conn.commit()

    apply_retention(db, s)

    assert [r[0] for r in db.conn.execute("SELECT date FROM domain_send_counts")] == [
        clock.today_str()
    ]


def test_a_bounce_is_released_after_24_months(db, s):
    db.add_to_suppression(EMAIL, "bounce")
    db.add_to_suppression("rejected@brandco.de", "smtp_rejected")
    db.add_to_suppression("out@brandco.de", "unsubscribe")
    _age(db, "suppression_list", "timestamp", 25 * 31)
    db.add_to_suppression("recent@brandco.de", "bounce")

    apply_retention(db, s)

    assert not db.is_suppressed(EMAIL)
    assert not db.is_suppressed("rejected@brandco.de")
    assert db.is_suppressed("out@brandco.de"), "an opt-out is kept"
    assert db.is_suppressed("recent@brandco.de")


def test_the_counts_say_what_was_done(db, s):
    db.log_activity("X", f"Skipped {EMAIL}")
    _age(db, "activity_log", "timestamp", 181)
    counts = apply_retention(db, s)
    assert counts["activity_masked"] == 1
    for key in (
        "lead_alerts_cleared",
        "requests_deleted",
        "exports_deleted",
        "send_counts_deleted",
        "bounces_released",
    ):
        assert counts[key] == 0, key
