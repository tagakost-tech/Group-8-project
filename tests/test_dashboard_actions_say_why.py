"""The dashboard's contact actions record what the operator did, and Unblock
only unblocks.

Block, Unblock and Send changed a contact's status with its reason cleared and
no activity row, so afterwards nothing said that a person - not the agent -
had made the change. Unblock also reset ANY contact without a suppression row
to 'pending': only the page hid the button, and the API turned a contact who
had replied, a client, or a discard verdict back into a backlog entry. Block
answered "ok" for a contact that does not exist.
"""

import pytest

import main
from modules.database import Database


@pytest.fixture
def dash(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())
    db.cid = db.add_contact(db.log_search("q"), "buyer@brand.de", organization="Brand")
    return client, db


def _act(client, cid, action):
    return client.post(f"/api/contacts/{cid}/action", json={"action": action})


def _logged(db, action):
    return [r for r in db.get_recent_activity(limit=50) if r["action"] == action]


def test_a_block_records_who_and_why(dash):
    client, db = dash
    assert _act(client, db.cid, "block").status_code == 200
    row = db.get_contact_by_id(db.cid)
    assert row["status"] == "blocked"
    assert "dashboard" in (row["discard_reason"] or "").lower()
    rows = _logged(db, "CONTACT_BLOCKED")
    assert rows and "buyer@brand.de" in rows[0]["details"]


def test_an_unblock_records_it(dash):
    client, db = dash
    _act(client, db.cid, "block")
    assert _act(client, db.cid, "unblock").status_code == 200
    assert db.get_contact_by_id(db.cid)["status"] == "pending"
    assert _logged(db, "CONTACT_UNBLOCKED")


@pytest.mark.parametrize("status", ["responded", "active_client", "low_score"])
def test_unblock_leaves_a_contact_that_is_not_blocked_alone(dash, status):
    client, db = dash
    db.update_contact_status(db.cid, status)
    assert _act(client, db.cid, "unblock").status_code == 409
    assert db.get_contact_by_id(db.cid)["status"] == status


def test_a_requeue_records_it(dash):
    client, db = dash
    db.update_contact_status(db.cid, "low_score", discard_reason="Score 10")
    assert _act(client, db.cid, "send_now").status_code == 200
    row = db.get_contact_by_id(db.cid)
    assert "dashboard" in (row["discard_reason"] or "").lower()
    assert _logged(db, "CONTACT_REQUEUED")


def test_blocking_an_unknown_contact_is_404(dash):
    client, _db = dash
    assert _act(client, 999, "block").status_code == 404


def test_marking_a_client_names_the_contact_it_came_from(tmp_path, settings_factory):
    from openpyxl import Workbook

    xlsx = tmp_path / "clients.xlsx"
    wb = Workbook()
    wb.active.append(["Some Other Company S.r.l."])
    wb.save(xlsx)
    s = settings_factory(db_path=str(tmp_path / "d.db"), client_list_path=str(xlsx))
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), "buyer@brand.de", organization="Brand")
    assert _act(client, cid, "mark_client").status_code == 200
    rows = _logged(db, "CLIENT_ADDED")
    assert rows and "buyer@brand.de" in rows[0]["details"]
