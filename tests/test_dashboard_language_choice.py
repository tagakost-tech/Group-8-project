"""The operator chooses Italian, English or automatic for a contact.

Evidence about a company can be missing or wrong, and the operator often
knows better: a buyer at an Italian group who reads only English, a .de
distributor run from Bolzano. POST /api/contacts/<id>/language sets the
contact's language (or clears it, "auto"), behind the same login and
cross-origin check as every other write, JSON only. It is refused for an
erased contact and for a contact already emailed, which keeps the language
of its first email; the refusal says so in plain words.
"""

import base64

import pytest

import main
from conftest import anonymous
from modules.database import Database
from modules.export import DataExporter

COM = "acquisti@rossi-integratori.com"


@pytest.fixture
def app(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), COM, organization="Rossi Integratori")
    return client, db, cid


def _set(client, cid, body, **kw):
    return client.post(f"/api/contacts/{cid}/language", json=body, **kw)


def _row(client):
    return {c["email"]: c for c in client.get("/api/contacts").get_json()}[COM]


def test_choosing_italian_makes_the_contact_italian(app):
    client, _db, cid = app
    resp = _set(client, cid, {"language": "it"})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True,
        "language": "it",
        "reason": "Italian (set by you)",
    }
    row = _row(client)
    assert (row["language"], row["language_reason"]) == ("it", "Italian (set by you)")
    assert row["language_choice"] == "it"


def test_automatic_clears_the_choice(app):
    client, db, cid = app
    _set(client, cid, {"language": "it"})
    resp = _set(client, cid, {"language": "auto"})
    assert resp.status_code == 200
    assert db.get_contact_by_id(cid)["language_choice"] is None
    assert _row(client)["language_reason"] == "English"


@pytest.mark.parametrize("body", [{"language": "fr"}, {"language": 3}, {}, [1, 2]])
def test_anything_but_it_en_or_auto_is_refused(app, body):
    client, db, cid = app
    resp = _set(client, cid, body)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Choose it, en or auto."
    assert db.get_contact_by_id(cid)["language_choice"] is None


def test_only_json_is_taken(app):
    client, _db, cid = app
    resp = client.post(
        f"/api/contacts/{cid}/language",
        data='{"language": "it"}',
        content_type="text/plain",
    )
    assert resp.status_code == 415


def test_a_cross_origin_write_is_refused(app):
    client, db, cid = app
    resp = _set(
        client, cid, {"language": "it"}, headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    assert db.get_contact_by_id(cid)["language_choice"] is None


def test_it_needs_the_dashboard_login(tmp_path, settings_factory):
    s = settings_factory(
        db_path=str(tmp_path / "p.db"),
        DASHBOARD_USERNAME="op",
        DASHBOARD_PASSWORD="s3cret-passphrase",
    )
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), COM)
    assert _set(anonymous(client), cid, {"language": "it"}).status_code == 401
    token = base64.b64encode(b"op:s3cret-passphrase").decode()
    ok = _set(
        client, cid, {"language": "it"}, headers={"Authorization": f"Basic {token}"}
    )
    assert ok.status_code == 200


def test_an_unknown_contact_is_404(app):
    client, _db, _cid = app
    assert _set(client, 999, {"language": "it"}).status_code == 404


def test_an_erased_contact_is_refused(app):
    client, db, cid = app
    DataExporter(db).gdpr_erase(COM)
    resp = _set(client, cid, {"language": "it"})
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "An erased contact is never emailed."


@pytest.mark.parametrize(
    "status,version,said",
    [
        ("preparing", "notice-li-1-it", "Italian"),
        ("sent", "notice-li-1", "English"),
    ],
)
def test_a_contact_already_emailed_keeps_its_language(app, status, version, said):
    client, db, cid = app
    db.log_email(cid, COM, "s", delivery_status=status, notice_version=version)
    resp = _set(client, cid, {"language": "en" if said == "Italian" else "it"})
    assert resp.status_code == 409
    assert resp.get_json()["error"] == (
        f"Already emailed in {said}: a contact keeps the language of its first email."
    )
    assert db.get_contact_by_id(cid)["language_choice"] is None


def test_it_takes_no_get(app):
    client, _db, cid = app
    assert client.get(f"/api/contacts/{cid}/language").status_code == 405


def test_the_activity_log_names_no_address(app):
    client, db, cid = app
    _set(client, cid, {"language": "en"})
    rows = db.conn.execute(
        "SELECT action, details FROM activity_log WHERE action = 'LANGUAGE_SET'"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("LANGUAGE_SET", f"Contact {cid}: English (set by you)")
    ]
    assert "rossi" not in rows[0][1]
