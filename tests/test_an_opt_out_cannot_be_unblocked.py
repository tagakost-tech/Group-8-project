"""An opt-out stands, even for a contact the operator had blocked.

Each address keeps one suppression row, and the first reason written wins.
Both opt-out paths only added a row when there was none, so a person the
operator had already blocked ('blocked_via_dashboard') who then wrote "remove
me", or used the unsubscribe link, left the row saying "blocked" - and the
dashboard's Unblock, which may remove exactly that reason, deleted it. The
person became mailable again and no record of their opt-out remained: a
right-to-object failure (GDPR Art. 21). An opt-out now turns an operator's
block into an opt-out, which Unblock refuses.

An opt-out by email also never marked the contact: it stayed 'emailed' (or
'blocked') and kept its active follow-ups until the next pass happened to
notice the suppression. It is now recorded exactly as the link records it:
'unsubscribed', with the reason, and the follow-ups end at once.
"""

import pytest

import main
from modules.database import Database
from modules.imap_monitor import ImapMonitor
from modules.unsubscribe import UnsubscribeHandler


@pytest.fixture
def dash(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), "buyer@brand.de", organization="Brand")
    db.update_contact_status(cid, "emailed")
    return client, db, cid


def _action(client, cid, action):
    return client.post(f"/api/contacts/{cid}/action", json={"action": action})


def _opt_out_by_email(db):
    ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw").process_unsubscribes(
        [{"email": "Buyer@Brand.de"}]
    )


def _opt_out_by_link(db):
    h = UnsubscribeHandler(db, "secret")
    ok, _ = h.process_unsubscribe(h.generate_token("buyer@brand.de"))
    assert ok


@pytest.mark.parametrize("opt_out", [_opt_out_by_email, _opt_out_by_link])
def test_an_opt_out_after_a_block_survives_unblock(dash, opt_out):
    client, db, cid = dash
    assert _action(client, cid, "block").status_code == 200
    opt_out(db)
    assert _action(client, cid, "unblock").status_code == 409
    assert db.is_suppressed("buyer@brand.de")
    assert db.get_suppression_reason("buyer@brand.de") == "unsubscribe"
    assert db.get_contact_by_id(cid)["status"] == "unsubscribed"


def test_an_opt_out_by_email_marks_the_contact(dash):
    _client, db, cid = dash
    _opt_out_by_email(db)
    row = db.get_contact_by_id(cid)
    assert row["status"] == "unsubscribed"
    assert row["discard_reason"], "the contact does not say why it is closed"


def test_an_opt_out_by_email_ends_the_follow_ups_at_once(dash):
    _client, db, cid = dash
    db.conn.execute("INSERT INTO sequences (id, name) VALUES (1, 's')")
    db.conn.execute(
        "INSERT INTO sequence_enrollments (contact_id, sequence_id, status) "
        "VALUES (?, 1, 'active')",
        (cid,),
    )
    db.conn.commit()
    _opt_out_by_email(db)
    status = db.conn.execute("SELECT status FROM sequence_enrollments").fetchone()[0]
    assert status == "cancelled"


def test_a_block_alone_can_still_be_undone(dash):
    """The control: the operator's own block stays theirs to take back."""
    client, db, cid = dash
    _action(client, cid, "block")
    assert _action(client, cid, "unblock").status_code == 200
    assert not db.is_suppressed("buyer@brand.de")
