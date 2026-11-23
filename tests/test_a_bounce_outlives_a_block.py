"""A hard bounce for an address the operator had blocked cannot be unblocked.

Found by the review round - the sibling of the opt-out fix. Each address has
one suppression row and the first reason wins. The bounce handler only wrote
a row when there was none, so an emailed contact that the operator blocked
before its DSN was read kept the reason 'blocked_via_dashboard': the bounce
was neither recorded nor counted in the bounce rate, and the dashboard's
Unblock - which may remove exactly that reason - put the dead address back in
the backlog, its follow-ups still enrolled.

A hard bounce now replaces an operator's block, as an opt-out does. It is
recorded as 'bounce_blocked', which retention never releases
(test_a_released_bounce_keeps_every_objection).
"""

import pytest

import main
from modules.database import Database
from modules.imap_monitor import ImapMonitor


@pytest.fixture
def blocked_then_bounced(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), "buyer@brand.de", organization="Brand")
    db.log_email(cid, "buyer@brand.de", "s", delivery_status="sent")
    db.update_contact_status(cid, "emailed")
    assert (
        client.post(f"/api/contacts/{cid}/action", json={"action": "block"}).status_code
        == 200
    )
    ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw").process_bounces(
        [{"email": "Buyer@Brand.de", "subject": "Undeliverable"}]
    )
    return client, db, cid


def test_the_bounce_replaces_the_block(blocked_then_bounced):
    _client, db, cid = blocked_then_bounced
    assert db.get_suppression_reason("buyer@brand.de") == "bounce_blocked"
    assert db.get_contact_by_id(cid)["status"] == "bounced"


def test_unblock_cannot_bring_the_dead_address_back(blocked_then_bounced):
    client, db, cid = blocked_then_bounced
    resp = client.post(f"/api/contacts/{cid}/action", json={"action": "unblock"})
    assert resp.status_code == 409
    assert db.is_suppressed("buyer@brand.de")


def test_the_bounce_counts_in_the_bounce_rate(blocked_then_bounced):
    _client, db, _cid = blocked_then_bounced
    assert db.get_deliverability_metrics()["recent_bounced"] == 1
