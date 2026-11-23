"""Block leaves a closed contact's status alone.

Block always set the status to 'blocked'. A closed status says why outreach
ended, and other rules read it: retention erases an objector at its next run
by the status 'unsubscribed' and counts a 'responded' contact as engaged, and
Unblock reopens only a 'blocked' one. So blocking an opted-out contact kept
their data up to a year longer, and Block then Undo turned a contact who had
replied, or a client, into a pending backlog entry - the reopening the unblock
guard was written to stop. Now Block on a closed contact keeps the status,
suppresses the address if it was not, and says the contact is already closed;
Unblock takes back only the suppression row that Block added. The contacts
list marks closed rows, so the dashboard can leave Block off them.
"""

import pytest

import main
from modules.database import CLOSED_CONTACT_STATUSES, Database
from modules.retention import apply_retention

ANNA = "anna@brand.de"


def _dashboard(tmp_path, settings_factory, status=None):
    s = settings_factory(db_path=str(tmp_path / "b.db"))
    client = main.create_dashboard_app(s).test_client()
    db = Database(str(tmp_path / "b.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, ANNA, organization="Brand")
    if status:
        db.update_contact_status(cid, status)
    return s, client, db, cid


def _act(client, cid, action):
    return client.post(f"/api/contacts/{cid}/action", json={"action": action})


def _status(db, cid):
    return db.get_contact_by_id(cid)["status"]


def _logged(db, action):
    return [
        r[0]
        for r in db.conn.execute(
            "SELECT details FROM activity_log WHERE action = ?", (action,)
        )
    ]


def test_blocking_an_objector_keeps_them_due_for_erasure(tmp_path, settings_factory):
    s, client, db, cid = _dashboard(tmp_path, settings_factory)
    db.record_opt_out(ANNA, "replied: please remove me")
    assert apply_retention(db, s, dry_run=True)["erased_objection"] == 1

    reply = _act(client, cid, "block")

    assert reply.status_code == 200
    assert _status(db, cid) == "unsubscribed"
    assert db.get_suppression_reason(ANNA) == "unsubscribe"
    assert apply_retention(db, s, dry_run=True)["erased_objection"] == 1


@pytest.mark.parametrize("status", sorted(CLOSED_CONTACT_STATUSES - {"blocked"}))
def test_block_keeps_every_closed_status_and_says_so(
    tmp_path, settings_factory, status
):
    _, client, db, cid = _dashboard(tmp_path, settings_factory, status)

    reply = _act(client, cid, "block")

    assert reply.status_code == 200
    assert _status(db, cid) == status
    assert db.is_suppressed(ANNA), "the address must still end up suppressed"
    body = reply.get_json()
    assert body["closed"] == status
    assert "already closed" in body["message"].lower()


@pytest.mark.parametrize("status", ["responded", "active_client"])
def test_block_then_undo_does_not_reopen_a_closed_contact(
    tmp_path, settings_factory, status
):
    _, client, db, cid = _dashboard(tmp_path, settings_factory, status)

    blocked = _act(client, cid, "block")
    assert blocked.get_json().get("undo") == "unblock"
    assert db.get_suppression_reason(ANNA) == "blocked_via_dashboard"

    undone = _act(client, cid, "unblock")

    assert undone.status_code == 200
    assert _status(db, cid) == status, "Undo put the contact back in the queue"
    assert not db.is_suppressed(ANNA)
    assert undone.get_json()["message"]
    assert _logged(db, "CONTACT_BLOCKED") == [
        f"{ANNA} blocked from the dashboard; kept as {status}"
    ]
    assert _logged(db, "CONTACT_UNBLOCKED") == [
        f"{ANNA} unblocked from the dashboard; kept as {status}"
    ]


def test_a_block_that_added_nothing_offers_no_undo(tmp_path, settings_factory):
    """The opt-out's row was already there, so there is nothing to take back,
    and Unblock still refuses to lift the objection."""
    _, client, db, cid = _dashboard(tmp_path, settings_factory)
    db.record_opt_out(ANNA, "one-click unsubscribe")

    blocked = _act(client, cid, "block")
    assert "undo" not in blocked.get_json()
    assert _logged(db, "CONTACT_BLOCKED") == [], "nothing changed, nothing to log"

    undone = _act(client, cid, "unblock")
    assert undone.status_code == 409
    assert db.get_suppression_reason(ANNA) == "unsubscribe"
    assert _status(db, cid) == "unsubscribed"


def test_unblock_still_refuses_a_closed_contact_that_was_never_blocked(
    tmp_path, settings_factory
):
    _, client, db, cid = _dashboard(tmp_path, settings_factory, "responded")

    reply = _act(client, cid, "unblock")

    assert reply.status_code == 409
    assert _status(db, cid) == "responded"


def test_an_open_contact_is_still_blocked_and_unblocked(tmp_path, settings_factory):
    """Control: the change is for closed contacts only."""
    _, client, db, cid = _dashboard(tmp_path, settings_factory)

    blocked = _act(client, cid, "block")
    assert blocked.get_json().get("undo") == "unblock"
    assert _status(db, cid) == "blocked"

    assert _act(client, cid, "unblock").status_code == 200
    assert _status(db, cid) == "pending"


def test_the_contacts_list_marks_closed_rows(tmp_path, settings_factory):
    _, client, db, closed_id = _dashboard(tmp_path, settings_factory, "responded")
    open_id = db.add_contact(db.log_search("q2", source="test"), "open@brand.de")

    rows = {r["id"]: r for r in client.get("/api/contacts").get_json()}

    assert rows[closed_id]["closed"] is True
    assert rows[open_id]["closed"] is False
