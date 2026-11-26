"""The partner network: Omega routes work to specialist members by trade and
free capacity, each job moves only by the side allowed to move it, and a
member's login sees the network and nothing of Omega's own data."""

import base64

import pytest
from werkzeug.security import generate_password_hash

import main
from conftest import TEST_DASHBOARD_ORIGIN, anonymous
from modules import network
from modules.database import Database

MEMBER_PASSWORD = "member-password-123"


def _client(tmp_path, settings_factory, **extra):
    s = settings_factory(
        db_path=str(tmp_path / "net.db"), blacklist_check_enabled=False, **extra
    )
    return main.create_dashboard_app(s).test_client(), Database(s.db_path)


def _as(name, password):
    token = base64.b64encode(f"{name}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _member(db, name, specs, capacity, booked=0, rating=None, login=None):
    with db._transaction() as conn:
        return network._insert_member(
            conn,
            {
                "name": name,
                "city": "Lecco",
                "country": "IT",
                "specialisations": specs,
                "capacity_per_week": capacity,
                "booked": booked,
                "rating": rating,
                "contact_name": "Sara Mauri",
                "contact_email": "sara@member.example",
            },
            login=login,
            password_hash=generate_password_hash(MEMBER_PASSWORD) if login else None,
        )


@pytest.fixture
def net(tmp_path, settings_factory):
    """Omega, two cold formers, a turner with a login and a former with a
    login, and the test client signed in as Omega's administrator."""
    client, db = _client(
        tmp_path, settings_factory, dashboard_reviewers="maria:reviewer-pass-123"
    )
    omega = network.self_member(db)["id"]
    ids = {
        "omega": omega,
        "small": _member(db, "Small Former", ["cold_forming"], 1000, 0, 4.9),
        "big": _member(db, "Big Former", ["cold_forming"], 9000, 1000, 4.2,
                        login="stampaggio"),
        "busy": _member(db, "Busy Former", ["cold_forming"], 9000, 8900, 4.8),
        "turner": _member(db, "Turner", ["turning", "machining"], 3000, 0, 4.7,
                          login="tornitura"),
    }
    return client, db, ids


TURNER = _as("tornitura", MEMBER_PASSWORD)
FORMER = _as("stampaggio", MEMBER_PASSWORD)


def _job(client, to, quantity=500, operation="cold_forming", headers=None,
         approve=True, **extra):
    """Draft a job and, unless told not to, have Omega's administrator approve
    it so it reaches the recipient. Returns the drafting response."""
    body = {
        "to_member_id": to,
        "kind": "capacity" if operation == "cold_forming" else "operation",
        "operation": operation,
        "part": "M8x40 bolt",
        "quantity": quantity,
        **extra,
    }
    r = client.post("/api/network/jobs", json=body, headers=headers or {})
    if approve and r.status_code == 201:
        ok = client.post(f"/api/network/jobs/{r.get_json()['id']}/decision",
                         json={"decision": "approve"})
        assert ok.status_code == 200, ok.get_json()
    return r


def _decide(client, job_id, decision, headers=None):
    return client.post(f"/api/network/jobs/{job_id}/decision",
                       json={"decision": decision}, headers=headers or {})


def _booked(db, member_id):
    return network.get_member(db, member_id)["booked"]


# ── Matching ──


def test_matching_puts_those_with_room_first_then_free_capacity_then_rating(net):
    client, _db, ids = net
    found = client.get("/api/network/match?operation=cold_forming&quantity=2000").get_json()
    assert [(m["id"], m["fits"]) for m in found] == [
        (ids["big"], True),  # 8000 free
        (ids["small"], False),  # 1000 free
        (ids["busy"], False),  # 100 free
    ]
    assert ids["omega"] not in [m["id"] for m in found]
    assert found[0]["free"] == 8000 and "login" not in found[0]
    assert "password_hash" not in found[0]


def test_matching_breaks_a_tie_on_free_capacity_by_rating(net):
    client, db, ids = net
    twin = _member(db, "Twin Former", ["cold_forming"], 9000, 1000, 4.9)
    found = client.get("/api/network/match?operation=cold_forming&quantity=10").get_json()
    assert [m["id"] for m in found][:2] == [twin, ids["big"]]


def test_matching_refuses_an_unknown_operation_or_quantity(net):
    client, _db, _ids = net
    assert client.get("/api/network/match?operation=welding&quantity=5").status_code == 400
    assert client.get("/api/network/match?operation=turning&quantity=x").status_code == 400
    assert client.get("/api/network/match?operation=turning&quantity=0").status_code == 400


def test_me_says_who_is_asking(net):
    client, _db, ids = net
    me = client.get("/api/network/me").get_json()
    assert me["role"] == "orchestrator" and me["member"]["is_self"] is True
    assert me["member"]["id"] == ids["omega"] and me["name"] == "admin"
    them = client.get("/api/network/me", headers=TURNER).get_json()
    assert them["role"] == "member" and them["member"]["id"] == ids["turner"]
    assert them["name"] == "tornitura"
    members = client.get("/api/network/members").get_json()
    assert members[0]["is_self"] is True and len(members) == 5


# ── Creating a job ──


def test_a_job_goes_from_omega_to_the_member(net):
    client, _db, ids = net
    r = _job(client, ids["turner"], 1000, "turning", due_date="2026-12-18", notes="Groove")
    assert r.status_code == 201
    job = r.get_json()
    assert job["from_member"] == {"id": ids["omega"], "name": "Omega IFS"}
    assert job["to_member"]["id"] == ids["turner"]
    assert job["status"] == "draft" and job["direction"] == "out"
    assert job["created_by"] == "admin" and job["approved_by"] is None
    assert job["due_date"] == "2026-12-18"
    sent = client.get("/api/network/jobs").get_json()[0]
    assert sent["status"] == "requested" and sent["approved_by"] == "admin"
    assert sent["actions"] == ["cancel"]


# ── Approval: a named person at Omega sends every job ──


def test_a_draft_reaches_no_one_until_a_named_person_approves_it(net):
    client, _db, ids = net
    job = _job(client, ids["turner"], 1000, "turning", approve=False).get_json()
    assert job["status"] == "draft" and job["actions"] == ["approve", "reject", "cancel"]
    assert client.get("/api/network/jobs", headers=TURNER).get_json() == []
    path = f"/api/network/jobs/{job['id']}"
    assert client.get(f"{path}/messages", headers=TURNER).status_code == 404
    assert _decide(client, job["id"], "accept", headers=TURNER).status_code == 404

    r = _decide(client, job["id"], "approve")
    assert r.status_code == 200
    sent = r.get_json()
    assert sent["status"] == "requested" and sent["approved_by"] == "admin"
    assert sent["approved_at"] and sent["decided_by"] is None
    [seen] = client.get("/api/network/jobs", headers=TURNER).get_json()
    assert seen["id"] == job["id"] and "accept" in seen["actions"]
    assert _decide(client, job["id"], "approve").status_code == 409


def test_a_rejected_draft_is_never_sent(net):
    client, _db, ids = net
    job = _job(client, ids["turner"], 10, "turning", approve=False).get_json()
    r = _decide(client, job["id"], "reject")
    assert r.status_code == 200 and r.get_json()["status"] == "rejected"
    assert r.get_json()["approved_by"] == "admin"
    assert client.get("/api/network/jobs", headers=TURNER).get_json() == []
    assert _decide(client, job["id"], "approve").status_code == 409


def test_only_omegas_staff_approve_and_a_reviewer_may(net):
    client, _db, ids = net
    # A partner's own draft waits for Omega too; neither partner approves it.
    job = _job(client, ids["big"], 50, headers=TURNER, approve=False).get_json()
    assert job["status"] == "draft"
    assert client.get("/api/network/jobs", headers=FORMER).get_json() == []
    assert _decide(client, job["id"], "approve", headers=TURNER).status_code == 403
    assert _decide(client, job["id"], "approve", headers=FORMER).status_code == 404
    maria = _as("maria", "reviewer-pass-123")
    r = _decide(client, job["id"], "approve", headers=maria)
    assert r.status_code == 200 and r.get_json()["approved_by"] == "maria"


def test_an_approval_needs_a_name(net):
    client, db, ids = net
    job = _job(client, ids["turner"], 10, "turning", approve=False).get_json()
    nobody = network.Viewer(ids["omega"], "orchestrator", "")
    with pytest.raises(PermissionError):
        network.decide(db, nobody, job["id"], "approve")
    assert network.get_member(db, ids["turner"])["booked"] == 0


# ── Orders split across the network ──


def _order(**change):
    return {
        "part": "M10x60 flange bolt",
        "quantity": 1000,
        "self_quantity": 200,
        "due_date": "2026-12-18",
        "jobs": [
            {"to_member_id": None, "operation": "cold_forming", "quantity": 800},
            {"to_member_id": None, "operation": "turning", "quantity": 1000},
        ],
        **change,
    }


def _order_for(ids, **change):
    body = _order(**change)
    if "jobs" not in change:
        body["jobs"][0]["to_member_id"] = ids["big"]
        body["jobs"][1]["to_member_id"] = ids["turner"]
    return body


def test_an_order_splits_into_draft_jobs_that_add_up(net):
    client, db, ids = net
    r = client.post("/api/network/orders", json=_order_for(ids))
    assert r.status_code == 201, r.get_json()
    order = r.get_json()
    assert order["quantity"] == 1000 and order["self_quantity"] == 200
    forming, turning = order["jobs"]
    assert (forming["kind"], forming["quantity"], forming["to_member"]["id"]) == ("capacity", 800, ids["big"])
    assert (turning["kind"], turning["quantity"], turning["to_member"]["id"]) == ("operation", 1000, ids["turner"])
    assert {j["status"] for j in order["jobs"]} == {"draft"}
    assert forming["order"] == {"id": order["id"], "part": "M10x60 flange bolt",
                                "quantity": 1000, "self_quantity": 200}
    assert forming["part"] == "M10x60 flange bolt" and forming["due_date"] == "2026-12-18"
    # Nothing is booked or seen until approval.
    assert _booked(db, ids["big"]) == 1000
    assert client.get("/api/network/jobs", headers=FORMER).get_json() == []
    _decide(client, forming["id"], "approve")
    assert [j["id"] for j in client.get("/api/network/jobs", headers=FORMER).get_json()] == [forming["id"]]


@pytest.mark.parametrize(
    "change",
    [
        {"self_quantity": 300},  # 300 + 800 is not 1,000
        {"self_quantity": 1001},
        {"quantity": 0},
        {"part": ""},
        {"jobs": []},
        {"jobs": "all of it"},
        {"due_date": "tomorrow"},
    ],
)
def test_an_order_that_does_not_add_up_is_refused_whole(net, change):
    client, db, ids = net
    r = client.post("/api/network/orders", json=_order_for(ids, **change))
    assert r.status_code == 400 and r.get_json()["error"]
    assert db.conn.execute("SELECT COUNT(*) FROM network_jobs").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM network_orders").fetchone()[0] == 0


def test_an_order_line_that_breaks_a_rule_refuses_the_whole_order(net):
    client, db, ids = net
    body = _order_for(ids)
    body["jobs"][1]["to_member_id"] = ids["big"]  # the former does not turn
    assert client.post("/api/network/orders", json=body).status_code == 400
    body = _order_for(ids)
    body["jobs"][1]["quantity"] = 1001
    assert client.post("/api/network/orders", json=body).status_code == 400
    assert db.conn.execute("SELECT COUNT(*) FROM network_jobs").fetchone()[0] == 0


def test_a_member_cannot_split_an_order(net):
    client, _db, ids = net
    assert client.post("/api/network/orders", json=_order_for(ids), headers=TURNER).status_code == 403


# ── Changing a member's card ──


def test_omega_sets_its_own_capacity_and_a_member_only_its_own(net):
    client, db, ids = net
    assert client.get("/api/network/me").get_json()["manages_members"] is True
    maria = _as("maria", "reviewer-pass-123")
    assert client.get("/api/network/me", headers=maria).get_json()["manages_members"] is False
    assert client.get("/api/network/me", headers=TURNER).get_json()["manages_members"] is False
    r = client.patch(f"/api/network/members/{ids['omega']}", json={"capacity_per_week": 20000})
    assert r.status_code == 200 and r.get_json()["capacity_per_week"] == 20000
    r = client.patch(f"/api/network/members/{ids['turner']}", json={"capacity_per_week": 2500}, headers=TURNER)
    assert r.status_code == 200 and r.get_json()["free"] == 2500
    assert client.patch(f"/api/network/members/{ids['big']}", json={"capacity_per_week": 1}, headers=TURNER).status_code == 403
    assert client.patch(f"/api/network/members/{ids['turner']}", json={"certifications": "ISO 9001"}, headers=TURNER).status_code == 400
    maria = _as("maria", "reviewer-pass-123")
    assert client.patch(f"/api/network/members/{ids['turner']}", json={"capacity_per_week": 1}, headers=maria).status_code == 403
    assert client.patch(f"/api/network/members/{ids['turner']}", json={"capacity_per_week": -1}).status_code == 400
    assert client.patch("/api/network/members/999", json={"capacity_per_week": 1}).status_code == 404
    assert network.get_member(db, ids["turner"])["capacity_per_week"] == 2500


def test_a_new_installation_holds_only_omega_and_no_invented_figures(tmp_path):
    db = Database(str(tmp_path / "fresh.db"))
    [omega] = network.list_members(db)
    assert omega["name"] == "Omega IFS" and omega["is_self"]
    assert omega["capacity_per_week"] == 0 and omega["rating"] is None
    assert omega["contact_email"] == ""
    assert db.conn.execute("SELECT COUNT(*) FROM network_jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"to_member_id": 999},
        {"to_member_id": "2"},
        {"operation": "welding"},
        {"operation": "turning"},  # the former does not turn
        {"kind": "favour"},
        {"quantity": 0},
        {"quantity": 10_000_001},
        {"quantity": True},
        {"quantity": 1.5},
        {"part": ""},
        {"part": "x" * 121},
        {"notes": "x" * 1001},
        {"due_date": "18/12/2026"},
        {"due_date": "2026-02-30"},
    ],
)
def test_a_job_that_breaks_a_rule_is_refused(net, change):
    client, db, ids = net
    body = {
        "to_member_id": ids["big"],
        "kind": "capacity",
        "operation": "cold_forming",
        "part": "M8x40 bolt",
        "quantity": 800,
        **change,
    }
    r = client.post("/api/network/jobs", json=body)
    assert r.status_code == 400 and r.get_json()["error"]
    assert db.conn.execute("SELECT COUNT(*) FROM network_jobs").fetchone()[0] == 0


def test_a_job_cannot_be_sent_to_your_own_company(net):
    client, _db, ids = net
    r = _job(client, ids["omega"])
    assert r.status_code == 400 and "own company" in r.get_json()["error"]
    r = _job(client, ids["turner"], 10, "turning", headers=TURNER)
    assert r.status_code == 400


def test_the_limits_themselves_are_allowed(net):
    client, _db, ids = net
    assert _job(client, ids["big"], 10_000_000, part="x" * 120, notes="n" * 1000).status_code == 201
    assert _job(client, ids["big"], 1).status_code == 201


# ── Decisions ──


def test_accepting_books_the_capacity_and_finishing_releases_it(net):
    client, db, ids = net
    job = _job(client, ids["big"], 800).get_json()
    assert _booked(db, ids["big"]) == 1000

    r = client.post(f"/api/network/jobs/{job['id']}/decision", json={"decision": "accept"}, headers=FORMER)
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert r.get_json()["decided_by"] == "stampaggio"
    assert _booked(db, ids["big"]) == 1800

    r = client.post(f"/api/network/jobs/{job['id']}/decision", json={"decision": "start"}, headers=FORMER)
    assert r.get_json()["status"] == "in_progress" and r.get_json()["actions"] == ["done"]
    assert _booked(db, ids["big"]) == 1800

    r = client.post(f"/api/network/jobs/{job['id']}/decision", json={"decision": "done"}, headers=FORMER)
    assert r.get_json()["status"] == "done" and r.get_json()["actions"] == []
    assert _booked(db, ids["big"]) == 1000


def test_declining_books_nothing(net):
    client, db, ids = net
    job = _job(client, ids["big"], 800).get_json()
    r = client.post(f"/api/network/jobs/{job['id']}/decision", json={"decision": "decline"}, headers=FORMER)
    assert r.get_json()["status"] == "declined"
    assert _booked(db, ids["big"]) == 1000


def test_cancelling_an_accepted_job_releases_its_capacity(net):
    client, db, ids = net
    job = _job(client, ids["big"], 800).get_json()
    client.post(f"/api/network/jobs/{job['id']}/decision", json={"decision": "accept"}, headers=FORMER)
    assert _booked(db, ids["big"]) == 1800
    r = client.post(f"/api/network/jobs/{job['id']}/decision", json={"decision": "cancel"})
    assert r.status_code == 200 and r.get_json()["status"] == "cancelled"
    assert _booked(db, ids["big"]) == 1000


def test_only_the_recipient_accepts_and_only_the_sender_cancels(net):
    client, _db, ids = net
    job = _job(client, ids["big"], 800).get_json()
    path = f"/api/network/jobs/{job['id']}/decision"
    for decision in ("accept", "decline", "start", "done"):
        assert client.post(path, json={"decision": decision}).status_code == 403, decision
    assert client.post(path, json={"decision": "cancel"}, headers=FORMER).status_code == 403
    # Neither side of someone else's job.
    assert client.post(path, json={"decision": "accept"}, headers=TURNER).status_code == 403
    assert client.post(path, json={"decision": "maybe"}, headers=FORMER).status_code == 400
    assert client.post("/api/network/jobs/999/decision", json={"decision": "accept"}).status_code == 404


@pytest.mark.parametrize(
    "steps, decision",
    [
        ([], "start"),
        ([], "done"),
        (["accept"], "accept"),
        (["accept"], "decline"),
        (["accept"], "done"),
        (["accept", "start"], "start"),
        (["decline"], "accept"),
    ],
)
def test_a_decision_in_the_wrong_state_is_refused_and_books_nothing(net, steps, decision):
    client, db, ids = net
    job = _job(client, ids["big"], 800).get_json()
    path = f"/api/network/jobs/{job['id']}/decision"
    for step in steps:
        assert client.post(path, json={"decision": step}, headers=FORMER).status_code == 200
    booked = _booked(db, ids["big"])
    r = client.post(path, json={"decision": decision}, headers=FORMER)
    assert r.status_code == 409 and r.get_json()["error"]
    assert _booked(db, ids["big"]) == booked


@pytest.mark.parametrize("steps", [["accept", "start"], ["accept", "start", "done"], ["decline"]])
def test_the_sender_cannot_cancel_once_work_has_started_or_ended(net, steps):
    client, _db, ids = net
    job = _job(client, ids["big"], 800).get_json()
    path = f"/api/network/jobs/{job['id']}/decision"
    for step in steps:
        client.post(path, json={"decision": step}, headers=FORMER)
    assert client.post(path, json={"decision": "cancel"}).status_code == 409


# ── Messages ──


def test_messages_are_for_the_two_parties_and_count_as_unread(net):
    client, _db, ids = net
    job = _job(client, ids["turner"], 1000, "turning").get_json()
    path = f"/api/network/jobs/{job['id']}/messages"

    r = client.post(path, json={"body": "Groove at 12 mm, please."})
    assert r.status_code == 201
    assert r.get_json()["from_member"]["id"] == ids["omega"] and r.get_json()["author"] == "admin"

    seen = client.get("/api/network/jobs", headers=TURNER).get_json()
    assert seen[0]["unread"] == 1 and seen[0]["direction"] == "in"
    thread = client.get(path, headers=TURNER).get_json()
    assert [m["body"] for m in thread] == ["Groove at 12 mm, please."]
    assert client.get("/api/network/jobs", headers=TURNER).get_json()[0]["unread"] == 0

    assert client.post(path, json={"body": "OK, from the 9th."}, headers=TURNER).status_code == 201
    assert client.get("/api/network/jobs").get_json()[0]["unread"] == 1

    # The former is not a party to the turning job.
    assert client.get(path, headers=FORMER).status_code == 403
    assert client.post(path, json={"body": "Hi"}, headers=FORMER).status_code == 403
    assert client.post(path, json={"body": ""}).status_code == 400
    assert client.post(path, json={"body": "x" * 2001}).status_code == 400
    assert client.get("/api/network/jobs/999/messages").status_code == 404


def test_omega_staff_read_a_job_between_two_partners(net):
    client, _db, ids = net
    job = _job(client, ids["big"], 100, headers=TURNER).get_json()
    path = f"/api/network/jobs/{job['id']}/messages"
    client.post(path, json={"body": "Can you form these?"}, headers=TURNER)
    staff_view = client.get("/api/network/jobs").get_json()[0]
    assert staff_view["direction"] == "between" and staff_view["actions"] == []
    assert staff_view["approved_by"] == "admin"
    assert [m["body"] for m in client.get(path).get_json()] == ["Can you form these?"]


# ── What a member's login reaches ──


def test_a_member_sees_only_its_own_jobs(net):
    client, _db, ids = net
    to_turner = _job(client, ids["turner"], 10, "turning").get_json()
    _job(client, ids["big"], 10)
    _job(client, ids["small"], 10)
    mine = client.get("/api/network/jobs", headers=TURNER).get_json()
    assert [j["id"] for j in mine] == [to_turner["id"]]
    assert len(client.get("/api/network/jobs").get_json()) == 3


@pytest.mark.parametrize(
    "path",
    [
        "/api/contacts",
        "/api/review",
        "/api/funnel",
        "/api/setup",
        "/api/stats",
        "/api/activity",
        "/api/config",
        "/classic",
    ],
)
def test_a_member_cannot_read_omegas_own_data(net, path):
    client, _db, _ids = net
    r = client.get(path, headers=TURNER)
    assert r.status_code == 403, path
    if path.startswith("/api/"):
        assert r.get_json()["error"]


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/agent/start", {"mode": "dry_run"}),
        ("/api/network/members", {"name": "Mine"}),
        ("/api/send-manual", {"email": "x@y.example"}),
    ],
)
def test_a_member_cannot_control_the_agent_or_admit_members(net, path, body):
    client, db, _ids = net
    before = db.conn.execute("SELECT COUNT(*) FROM network_members").fetchone()[0]
    assert client.post(path, json=body, headers=TURNER).status_code == 403
    assert db.conn.execute("SELECT COUNT(*) FROM network_members").fetchone()[0] == before


def test_a_member_still_opens_the_app_shell_and_its_files(net):
    client, _db, _ids = net
    assert client.get("/", headers=TURNER).status_code == 200
    assert client.get("/static/app/app.js", headers=TURNER).status_code == 200
    assert client.get("/api/network/members", headers=TURNER).status_code == 200


def test_a_member_signs_in_on_the_page_and_its_session_ends_with_a_new_password(net):
    client, db, ids = net
    browser = anonymous(client)
    r = browser.post(
        "/login",
        data={"username": "tornitura", "password": MEMBER_PASSWORD, "next": "/classic"},
        headers={"Origin": TEST_DASHBOARD_ORIGIN},
    )
    assert r.status_code == 302 and r.headers["Location"] == "/"
    me = browser.get("/api/network/me").get_json()
    assert me["role"] == "member" and me["member"]["id"] == ids["turner"]
    assert browser.get("/api/contacts").status_code == 403
    ok = browser.post(
        "/api/network/jobs",
        json={"to_member_id": ids["big"], "kind": "capacity", "operation": "cold_forming",
              "part": "Pin", "quantity": 5},
        headers={"Origin": TEST_DASHBOARD_ORIGIN},
    )
    assert ok.status_code == 201 and ok.get_json()["from_member"]["id"] == ids["turner"]
    assert ok.get_json()["status"] == "draft"

    network.set_member_password(db, ids["turner"], generate_password_hash("another-password-456"))
    assert browser.get("/api/network/me").status_code == 401


def test_a_wrong_member_password_is_refused_and_counts_toward_the_lock_out(net):
    client, _db, _ids = net
    stranger = anonymous(client)
    assert stranger.get("/api/network/me", headers=_as("tornitura", "wrong-password-1")).status_code == 401
    page = stranger.post(
        "/login",
        data={"username": "tornitura", "password": "wrong-password-2", "next": "/"},
        headers={"Origin": TEST_DASHBOARD_ORIGIN},
    )
    assert page.status_code == 401
    for _ in range(main.LOGIN_FAILURES_ALLOWED):
        stranger.get("/api/network/me", headers=_as("tornitura", "wrong-password-3"))
    # Locked: even the right password waits.
    assert stranger.get("/api/network/me", headers=TURNER).status_code == 429


def test_a_member_login_never_takes_a_staff_name(net):
    client, _db, _ids = net
    r = client.post("/api/network/members", json=_new_member(login="maria"))
    assert r.status_code == 400 and "taken" in r.get_json()["error"]
    r = client.post("/api/network/members", json=_new_member(login="tornitura"))
    assert r.status_code == 400


# ── Staff ──


def test_a_reviewer_can_send_a_job_but_not_admit_a_member(net):
    client, _db, ids = net
    maria = _as("maria", "reviewer-pass-123")
    r = _job(client, ids["turner"], 1000, "turning", headers=maria)
    assert r.status_code == 201
    job = r.get_json()
    assert job["from_member"]["id"] == ids["omega"]
    assert client.post(
        f"/api/network/jobs/{job['id']}/messages", json={"body": "Drawing attached."}, headers=maria
    ).get_json()["author"] == "maria"
    assert client.post("/api/network/members", json=_new_member(), headers=maria).status_code == 403


def _new_member(**change):
    return {
        "name": "Zincatura Lambro S.r.l.",
        "city": "Monza",
        "country": "IT",
        "specialisations": ["plating"],
        "certifications": "ISO 9001",
        "capacity_per_week": 30000,
        "contact_name": "Elena Fumagalli",
        "contact_email": "linea@zincatura.example",
        "login": "zincatura",
        "password": "plating-password-1",
        **change,
    }


def test_the_administrator_admits_a_member_who_can_then_sign_in(net):
    client, db, _ids = net
    r = client.post("/api/network/members", json=_new_member())
    assert r.status_code == 201
    added = r.get_json()
    assert added["specialisations"] == ["plating"] and added["free"] == 30000
    assert "password" not in added and "password_hash" not in added
    stored = db.conn.execute(
        "SELECT password_hash FROM network_members WHERE login = 'zincatura'"
    ).fetchone()[0]
    assert "plating-password-1" not in stored

    me = anonymous(client).get("/api/network/me", headers=_as("zincatura", "plating-password-1"))
    assert me.status_code == 200 and me.get_json()["member"]["id"] == added["id"]


@pytest.mark.parametrize(
    "change",
    [
        {"password": "short"},
        {"specialisations": []},
        {"specialisations": ["welding"]},
        {"login": "a b"},
        {"name": ""},
        {"capacity_per_week": -1},
        {"contact_email": "not an address"},
    ],
)
def test_a_member_that_breaks_a_rule_is_not_admitted(net, change):
    client, _db, _ids = net
    r = client.post("/api/network/members", json=_new_member(**change))
    assert r.status_code == 400 and r.get_json()["error"]
