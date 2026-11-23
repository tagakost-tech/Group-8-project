"""The dashboard's "send now" cannot re-queue someone who is closed.

send_now set any contact back to 'pending' - including one who replied, is a
client, unsubscribed, bounced or was erased on request. The next loop then
emailed them. Its own comment called it a stand-in. The operator may still
override a FILTER verdict (irrelevant, low score): that is a judgement call
this button exists for. A closed contact is not a judgement call.
"""

import pytest

import main


@pytest.fixture
def app(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    client = main.create_dashboard_app(s).test_client()
    db = main.Database(s.get_abs_db_path())
    return client, db


def _contact(db, status, email="buyer@brand.de"):
    sid = db.log_search("q")
    cid = db.add_contact(sid, email=email, organization="Brand")
    db.update_contact_status(cid, status)
    return cid


def _send_now(client, cid):
    return client.post(f"/api/contacts/{cid}/action", json={"action": "send_now"})


@pytest.mark.parametrize(
    "status",
    ["responded", "active_client", "unsubscribed", "erased", "bounced", "blocked"],
)
def test_a_closed_contact_is_not_requeued(app, status):
    client, db = app
    cid = _contact(db, status)
    resp = _send_now(client, cid)
    assert resp.status_code == 409
    assert db.get_contact_by_id(cid)["status"] == status


def test_a_suppressed_address_is_not_requeued(app):
    """Suppressed with a status that looks harmless - the list is what counts."""
    client, db = app
    cid = _contact(db, "emailed")
    db.add_to_suppression("buyer@brand.de", "unsubscribe")
    assert _send_now(client, cid).status_code == 409


@pytest.mark.parametrize("status", ["failed", "irrelevant", "low_score"])
def test_the_operator_can_still_override_a_filter(app, status):
    client, db = app
    cid = _contact(db, status)
    assert _send_now(client, cid).status_code == 200
    assert db.get_contact_by_id(cid)["status"] == "pending"


def test_an_unknown_contact_is_404(app):
    client, _db = app
    assert _send_now(client, 999).status_code == 404
