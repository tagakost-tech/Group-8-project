"""GET /api/contacts/<id>/why serves the explainer to the dashboard.

Read-only, behind the same login as every other dashboard route (the
timeline names the contact and quotes the agent's log about them), and 404
for a contact that does not exist.
"""

import base64

import pytest

import main
from conftest import anonymous
from modules.database import Database


def _app(tmp_path, settings_factory, **kw):
    s = settings_factory(db_path=str(tmp_path / "d.db"), **kw)
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), "buyer@brand.de", organization="Brand")
    db.update_contact_status(cid, "low_score", discard_reason="Score 12 below 40")
    return client, cid


def test_the_explanation_is_served(tmp_path, settings_factory):
    client, cid = _app(tmp_path, settings_factory)
    resp = client.get(f"/api/contacts/{cid}/why")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "lead score" in body["headline"].lower()
    assert ["Reason", "Score 12 below 40"] in body["facts"]
    assert isinstance(body["timeline"], list)


def test_an_unknown_contact_is_404(tmp_path, settings_factory):
    client, _cid = _app(tmp_path, settings_factory)
    assert client.get("/api/contacts/999/why").status_code == 404


def test_it_needs_the_dashboard_login(tmp_path, settings_factory):
    client, cid = _app(
        tmp_path,
        settings_factory,
        DASHBOARD_USERNAME="op",
        DASHBOARD_PASSWORD="s3cret-passphrase",
    )
    assert anonymous(client).get(f"/api/contacts/{cid}/why").status_code == 401
    token = base64.b64encode(b"op:s3cret-passphrase").decode()
    resp = client.get(
        f"/api/contacts/{cid}/why", headers={"Authorization": f"Basic {token}"}
    )
    assert resp.status_code == 200


def _why_facts(tmp_path, settings_factory, email):
    s = settings_factory(db_path=str(tmp_path / "lang.db"), ITALIAN_COUNTRIES="IT")
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), email, organization="Brand")
    return dict(client.get(f"/api/contacts/{cid}/why").get_json()["facts"])


def test_the_explanation_says_the_language_of_the_emails(tmp_path, settings_factory):
    facts = _why_facts(tmp_path, settings_factory, "acquisti@rossi-integratori.it")
    assert facts["Language"] == "Italian (its address ends in .it)"
    facts = _why_facts(tmp_path / "en", settings_factory, "einkauf@brand.de")
    assert facts["Language"] == "English"


def test_the_language_fact_says_when_the_italian_text_is_missing(
    tmp_path, settings_factory, monkeypatch
):
    import shutil
    from pathlib import Path

    from modules.file_handler import FileHandler

    root = tmp_path / "product"
    shutil.copytree(
        Path(main.__file__).parent / "templates",
        root / "templates",
        ignore=shutil.ignore_patterns("email_template_it.html"),
    )
    monkeypatch.setattr(main, "FileHandler", lambda: FileHandler(str(root)))
    facts = _why_facts(tmp_path, settings_factory, "acquisti@rossi-integratori.it")
    assert facts["Language"] == (
        "Italian, but templates/email_template_it.html is missing, so the "
        "English first email goes"
    )


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_it_only_reads(tmp_path, settings_factory, method):
    client, cid = _app(tmp_path, settings_factory)
    assert getattr(client, method)(f"/api/contacts/{cid}/why").status_code == 405
