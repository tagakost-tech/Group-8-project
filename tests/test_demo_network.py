"""The demo's invented network: only on the demo branch, never in the product."""

from modules import network
from modules.database import Database


def test_the_demo_seeds_the_network(tmp_path, monkeypatch):
    from scripts import run_demo

    db = Database(str(tmp_path / "demo.db"))
    run_demo.seed_network(db, "demo-member-password")
    members = network.list_members(db)
    assert members[0]["name"] == "Omega IFS" and members[0]["is_self"]
    assert len(members) == 7
    assert all(m["contact_email"].endswith(".example") for m in members)
    statuses = sorted(
        r[0] for r in db.conn.execute("SELECT status FROM network_jobs").fetchall()
    )
    assert statuses == ["done", "draft", "draft", "in_progress", "requested"]
    [order] = db.conn.execute("SELECT quantity, self_quantity FROM network_orders").fetchall()
    assert tuple(order) == (1000, 200)
    turner = network.member_by_login(db, "tornitura")
    viewer = network.Viewer(turner["id"], "member", "tornitura")
    [job] = network.list_jobs(db, viewer)
    assert job["status"] == "requested" and job["unread"] == 1
    assert len(network.messages(db, viewer, job["id"])) == 3
