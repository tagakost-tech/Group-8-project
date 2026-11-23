"""Releasing a bounce never releases an objection or an operator's block.

Found by the security review of the bounce release. Retention releases a
bounced address after 24 months (L5.1). An address has one suppression row,
and the first reason written wins, so a row that read 'bounce' could hide a
later objection (record_opt_out kept bounce reasons) or an operator's block
(the bounce handler turned a block into a bounce, and a block on a bounced
address changed nothing). Releasing the bounce released those too.

Now an objection replaces a bounce, as it replaces a block. A bounce on a
blocked address, in either order, is 'bounce_blocked': it counts in the
bounce rate, Unblock cannot remove it, and retention never releases it.
"""

import pytest

import main
from modules.database import Database
from modules.imap_monitor import ImapMonitor
from modules.retention import apply_retention

EMAIL = "buyer@brand.de"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "d.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


@pytest.fixture
def client(s):
    return main.create_dashboard_app(s).test_client()


def _emailed(db):
    cid = db.add_contact(db.log_search("q"), EMAIL, organization="Brand")
    db.log_email(cid, EMAIL, "s", delivery_status="sent")
    db.update_contact_status(cid, "emailed")
    return cid


def _block(client, cid):
    resp = client.post(f"/api/contacts/{cid}/action", json={"action": "block"})
    assert resp.status_code == 200


def _bounce(db):
    ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw").process_bounces(
        [{"email": "Buyer@Brand.de", "subject": "Undeliverable"}]
    )


def _two_years_on(db, s):
    db.conn.execute(
        "UPDATE suppression_list SET timestamp = datetime('now','localtime','-800 days')"
    )
    db.conn.commit()
    apply_retention(db, s)


def test_an_objection_after_a_bounce_replaces_it(db, s):
    db.add_to_suppression(EMAIL, "bounce")
    assert db.record_opt_out(EMAIL, "Opted out by email")["new"]
    assert db.get_suppression_reason(EMAIL) == "unsubscribe"
    _two_years_on(db, s)
    assert db.is_suppressed(EMAIL)


def test_a_block_then_a_bounce_is_never_released(db, s, client):
    cid = _emailed(db)
    _block(client, cid)
    _bounce(db)
    assert db.get_suppression_reason(EMAIL) == "bounce_blocked"
    _two_years_on(db, s)
    assert db.is_suppressed(EMAIL)


def test_a_bounce_then_a_block_is_never_released(db, s, client):
    cid = _emailed(db)
    _bounce(db)
    _block(client, cid)
    assert db.get_suppression_reason(EMAIL) == "bounce_blocked"
    _two_years_on(db, s)
    assert db.is_suppressed(EMAIL)


def test_a_plain_bounce_is_still_released(db, s):
    _emailed(db)
    _bounce(db)
    assert db.get_suppression_reason(EMAIL) == "bounce"
    _two_years_on(db, s)
    assert not db.is_suppressed(EMAIL)


def test_unblock_cannot_remove_a_blocked_bounce(db, client):
    cid = _emailed(db)
    _bounce(db)
    _block(client, cid)
    resp = client.post(f"/api/contacts/{cid}/action", json={"action": "unblock"})
    assert resp.status_code in (200, 409)
    assert db.is_suppressed(EMAIL)
