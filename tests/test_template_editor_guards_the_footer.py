"""The dashboard's template editor cannot save away the legal footer.

The editor writes whatever it is given over templates/email_template.html.
Deleting the {{legal_footer}} line - or the unsubscribe link - there produces
a template that is unlawful to send from, and nothing downstream would say so
until doctor ran. The save is refused instead, with the reason.
"""

import shutil
from pathlib import Path

import pytest

import main

REAL = Path(main.__file__).resolve().parent / "templates"


@pytest.fixture
def client(tmp_path, settings_factory, monkeypatch):
    """Points the editor at a COPY: a failing run must not overwrite the
    real template."""
    shutil.copytree(REAL, tmp_path / "templates")
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    s = settings_factory(db_path=str(tmp_path / "t.db"))
    c = main.create_dashboard_app(s).test_client()
    c.saved = tmp_path / "templates" / "email_template.html"
    return c


@pytest.mark.parametrize(
    "missing, content",
    [
        ("{{legal_footer}}", "<p>Hi</p><a href='{{unsubscribe_url}}'>unsubscribe</a>"),
        ("{{unsubscribe_url}}", "<p>Hi</p>{{legal_footer}}"),
    ],
)
def test_a_save_without_a_required_placeholder_is_refused(client, missing, content):
    before = client.saved.read_bytes()
    resp = client.post("/api/template", json={"content": content})
    assert resp.status_code == 400
    assert missing in resp.get_json()["error"]
    assert client.saved.read_bytes() == before, "the template was overwritten"


def test_a_complete_template_still_saves(client):
    """The control: the guard must not lock the editor."""
    content = "<p>Hi</p>{{legal_footer}}<a href='{{unsubscribe_url}}'>x</a>"
    resp = client.post("/api/template", json={"content": content})
    assert resp.status_code == 200
    assert client.saved.read_text(encoding="utf-8") == content


# -- The Italian texts -------------------------------------------------------
# Italian prospects read email_template_it.html and the two Italian
# follow-ups, and the editor could open only the English first email.

GOOD = "<p>Buongiorno</p>{{legal_footer}}<a href='{{unsubscribe_url}}'>x</a>"


def _file(client, name):
    return client.saved.parent / name


def test_the_italian_first_email_opens_by_name(client):
    resp = client.get("/api/template?name=email_template_it")
    assert resp.status_code == 200
    assert resp.get_json()["content"] == _file(
        client, "email_template_it.html"
    ).read_text(encoding="utf-8")


def test_without_a_name_the_english_first_email_is_still_edited(client):
    assert client.get("/api/template").get_json()["content"] == (
        client.saved.read_text(encoding="utf-8")
    )
    italian = _file(client, "email_template_it.html").read_bytes()
    assert client.post("/api/template", json={"content": GOOD}).status_code == 200
    assert client.saved.read_text(encoding="utf-8") == GOOD
    assert _file(client, "email_template_it.html").read_bytes() == italian


def test_an_italian_follow_up_without_the_opt_out_link_is_refused(client):
    path = _file(client, "followup_1_it.html")
    before = path.read_bytes()
    resp = client.post(
        "/api/template",
        json={"name": "followup_1_it", "content": "<p>Ciao</p>{{legal_footer}}"},
    )
    assert resp.status_code == 400
    assert "{{unsubscribe_url}}" in resp.get_json()["error"]
    assert path.read_bytes() == before


def test_a_good_save_writes_the_follow_up_and_keeps_a_backup(client):
    path = _file(client, "followup_2_it.html")
    before = path.read_text(encoding="utf-8")
    resp = client.post("/api/template", json={"name": "followup_2_it", "content": GOOD})
    assert resp.status_code == 200
    assert path.read_text(encoding="utf-8") == GOOD
    backup = _file(client, "followup_2_it.html.bak")
    assert backup.read_text(encoding="utf-8") == before
    assert client.saved.read_text(encoding="utf-8") != GOOD


@pytest.mark.parametrize(
    "name", ["../main", "email_template_b", "followup_3", "../../.env", 3]
)
def test_a_name_off_the_list_is_refused(client, name):
    before = {p.name: p.read_bytes() for p in client.saved.parent.iterdir()}
    query = name if isinstance(name, str) else str(name)
    assert client.get("/api/template", query_string={"name": query}).status_code == 400
    resp = client.post("/api/template", json={"name": name, "content": GOOD})
    assert resp.status_code == 400
    assert "Unknown template" in resp.get_json()["error"]
    after = {p.name: p.read_bytes() for p in client.saved.parent.iterdir()}
    assert after == before


def test_a_missing_italian_text_opens_empty_and_can_be_created(client):
    path = _file(client, "followup_1_it.html")
    path.unlink()
    body = client.get("/api/template?name=followup_1_it").get_json()
    assert (body["content"], body["missing"]) == ("", True)
    resp = client.post("/api/template", json={"name": "followup_1_it", "content": GOOD})
    assert resp.status_code == 200
    assert path.read_text(encoding="utf-8") == GOOD


# -- The English follow-ups --------------------------------------------------
# The editor could open the first emails and the Italian follow-ups, but
# refused followup_1 and followup_2 as unknown.


@pytest.mark.parametrize("name", ["followup_1", "followup_2"])
def test_an_english_follow_up_opens_and_saves_under_the_footer_guard(client, name):
    path = _file(client, f"{name}.html")
    body = client.get(f"/api/template?name={name}").get_json()
    assert body["content"] == path.read_text(encoding="utf-8")
    before = path.read_bytes()
    refused = client.post(
        "/api/template", json={"name": name, "content": "<p>Hi</p>{{legal_footer}}"}
    )
    assert refused.status_code == 400
    assert path.read_bytes() == before
    resp = client.post("/api/template", json={"name": name, "content": GOOD})
    assert resp.status_code == 200
    assert path.read_text(encoding="utf-8") == GOOD
