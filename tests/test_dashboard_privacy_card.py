"""The Overview shows what privacy work is waiting: open data requests with
their due dates, erasures to finish, held opt-outs, an inbox gap.

Data requests have a legal one-month deadline and nothing on the page showed
the register; the rest appeared only through doctor on the command line.
/api/data-requests lists the open requests with no address, token or
message reference, and /api/readiness carries the doctor's four privacy
checks from the same single run.
"""

import base64
import json
from datetime import datetime, timedelta

import main
from conftest import anonymous
from modules import clock, optout_spool
from modules.database import Database

PRIVACY = {"Erasures finished", "Held opt-outs", "Data-subject requests", "Inbox gap"}


def _setup(tmp_path, settings_factory, **kw):
    s = settings_factory(db_path=str(tmp_path / "p.db"), **kw)
    client = main.create_dashboard_app(s).test_client()
    return client, Database(s.get_abs_db_path()), s


def _ago(days):
    return (clock.now() - timedelta(days=days)).isoformat()


def test_open_requests_are_listed_by_due_date_with_days_left(
    tmp_path, settings_factory
):
    client, db, _s = _setup(tmp_path, settings_factory)
    soon = db.add_dsr("anna@brand.it", "access", "reply", received_at=_ago(29))
    late = db.add_dsr(
        "bruno@brand.de", "erasure", "manual", source_ref="<m1@x>", received_at=_ago(40)
    )
    closed = db.add_dsr("carla@brand.fr", "objection", "reply", received_at=_ago(5))
    db.close_dsr(closed, "answered")

    resp = client.get("/api/data-requests")
    assert resp.status_code == 200
    rows = resp.get_json()
    assert [r["id"] for r in rows] == [late, soon], "closed requests are left out"
    today = clock.now().date()
    for r in rows:
        assert set(r) == {
            "id",
            "kind",
            "channel",
            "received_at",
            "due_at",
            "days_left",
            "overdue",
        }
        assert (
            r["days_left"] == (datetime.fromisoformat(r["due_at"]).date() - today).days
        )
    assert rows[0]["overdue"] is True and rows[0]["days_left"] < 0
    assert rows[0]["kind"] == "erasure" and rows[0]["channel"] == "manual"
    assert rows[1]["overdue"] is False and rows[1]["days_left"] >= 0


def test_no_address_token_or_message_reference_is_returned(tmp_path, settings_factory):
    client, db, _s = _setup(tmp_path, settings_factory)
    db.add_dsr("anna@brand.it", "erasure", "reply", source_ref="<msg-77@brand.it>")
    text = client.get("/api/data-requests").get_data(as_text=True)
    assert "@" not in text
    for key in ("email", "token", "source_ref"):
        assert all(key not in r for r in json.loads(text))


def test_an_empty_register_is_an_empty_list(tmp_path, settings_factory):
    client, _db, _s = _setup(tmp_path, settings_factory)
    assert client.get("/api/data-requests").get_json() == []


def test_the_register_needs_the_login_and_is_read_only(tmp_path, settings_factory):
    client, _db, _s = _setup(
        tmp_path, settings_factory, DASHBOARD_PASSWORD="s3cret-passphrase"
    )
    assert anonymous(client).get("/api/data-requests").status_code == 401
    auth = {
        "Authorization": "Basic "
        + base64.b64encode(b"admin:s3cret-passphrase").decode()
    }
    assert client.get("/api/data-requests", headers=auth).status_code == 200
    assert client.post("/api/data-requests", headers=auth).status_code == 405


def test_a_locked_register_gives_a_plain_message(
    tmp_path, settings_factory, monkeypatch
):
    import sqlite3

    client, _db, _s = _setup(tmp_path, settings_factory)

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "list_dsrs", locked)
    resp = client.get("/api/data-requests")
    assert resp.status_code == 503
    assert "could not" in resp.get_json()["error"].lower()


def _privacy(body):
    rows = {c["label"]: c for c in body["privacy"]}
    assert set(rows) == PRIVACY
    return rows


def test_readiness_carries_the_four_privacy_checks(tmp_path, settings_factory):
    client, db, s = _setup(tmp_path, settings_factory)
    rows = _privacy(client.get("/api/readiness").get_json())
    assert rows["Erasures finished"]["ok"] is True
    assert rows["Held opt-outs"]["ok"] is True
    assert rows["Data-subject requests"]["ok"] is True
    assert rows["Inbox gap"]["ok"] is None, "IMAP is off in these settings"
    for c in rows.values():
        assert set(c) == {"label", "ok", "detail", "blocking"}

    # A held opt-out, written after start-up, which would have replayed it.
    optout_spool.spool_optout(s.get_abs_db_path(), "held@brand.de", "unsubscribe_link")
    db.add_dsr("late@brand.de", "erasure", "manual", received_at=_ago(40))
    body = client.get("/api/readiness").get_json()
    rows = _privacy(body)
    assert rows["Held opt-outs"]["ok"] is False
    assert "1 opt-out(s) held" in rows["Held opt-outs"]["detail"]
    assert rows["Data-subject requests"]["ok"] is False
    assert "overdue" in rows["Data-subject requests"]["detail"]
    assert "@" not in json.dumps(body["privacy"])
    assert not {c["label"] for c in body["checks"]} & PRIVACY
