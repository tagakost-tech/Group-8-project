"""The no-reply retention clock runs from the last email actually sent.

It ran from the newest email_log row of any status. A dry run, or a send
that failed, wrote a fresh row, so previewing a campaign or a mailbox outage
restarted the 365-day clock for everyone it touched, and a contact emailed
once, years ago, could be kept indefinitely (GDPR Art. 5(1)(e)).
"""

import pytest

from modules.database import Database
from modules.retention import apply_retention


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "r.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


def _emailed_long_ago(db, email, later_status, later_days_ago):
    sid = db.log_search("q")
    cid = db.add_contact(sid, email, name="Anna", organization="Brand")
    db.update_contact_status(cid, "emailed")
    db.conn.execute(
        "UPDATE contacts SET timestamp = datetime('now','localtime','-500 days') "
        "WHERE id = ?",
        (cid,),
    )
    db.conn.commit()
    for status, days_ago in (("sent", 400), (later_status, later_days_ago)):
        log_id = db.log_email(cid, email, "Hello", delivery_status=status)
        db.conn.execute(
            "UPDATE email_log SET timestamp = datetime('now','localtime',?) "
            "WHERE id = ?",
            (f"-{days_ago} days", log_id),
        )
        db.conn.commit()  # log_email writes through its own connection
    return cid


@pytest.mark.parametrize("later_status", ["dry_run", "failed"])
def test_a_later_unsent_row_does_not_restart_the_clock(db, s, later_status):
    cid = _emailed_long_ago(db, "quiet@brand.de", later_status, later_days_ago=10)
    apply_retention(db, s)
    assert db.get_contact_by_id(cid)["status"] == "erased"


def test_a_recent_real_send_still_keeps_the_contact(db, s):
    cid = _emailed_long_ago(db, "recent@brand.de", "sent", later_days_ago=10)
    apply_retention(db, s)
    assert db.get_contact_by_id(cid)["status"] == "emailed"
