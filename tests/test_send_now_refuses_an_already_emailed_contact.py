"""The dashboard's Send button says so when the first email already went out.

Send re-queues a contact as 'pending' for the next campaign pass. For a
contact whose first email was already delivered, the campaign now refuses
the duplicate and puts the status back - so the button answered "ok", nothing
was sent, and the contact quietly returned to 'emailed'. It now answers 409
with the reason instead. A contact whose earlier attempt failed is still the
operator's to re-queue.
"""

import pytest

import main


@pytest.fixture
def app(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    client = main.create_dashboard_app(s).test_client()
    db = main.Database(s.get_abs_db_path())
    return client, db


def _contact(db, status, delivered):
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de", organization="Brand")
    db.update_contact_status(cid, status)
    db.log_email(cid, "buyer@brand.de", "s", delivery_status=delivered)
    return cid


def _send_now(client, cid):
    return client.post(f"/api/contacts/{cid}/action", json={"action": "send_now"})


@pytest.mark.parametrize("status", ["emailed", "pending"])
def test_an_already_emailed_contact_is_not_requeued(app, status):
    client, db = app
    cid = _contact(db, status, delivered="sent")
    resp = _send_now(client, cid)
    assert resp.status_code == 409
    assert "already" in resp.get_json()["error"]
    assert db.get_contact_by_id(cid)["status"] == status


def test_a_failed_attempt_can_still_be_requeued(app):
    client, db = app
    cid = _contact(db, "failed", delivered="failed")
    assert _send_now(client, cid).status_code == 200
    assert db.get_contact_by_id(cid)["status"] == "pending"
