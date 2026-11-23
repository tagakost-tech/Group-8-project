"""A second Direct Send to an address asks first.

The operator who pressed Send twice, or did not know the campaign had already
written to an address, sent the same company a second cold email with no word
from the page. Now the route refuses with 409 and says when the last email went
and in which language, unless the request confirms; a preview is never held
back, and every refusal about who may be emailed still comes first.
"""

import sqlite3

import pytest

import main
from modules.database import POSSIBLY_SENT_STATUSES_SQL


@pytest.fixture
def app_db(settings_factory, tmp_path, monkeypatch):
    settings = settings_factory(
        db_path=str(tmp_path / "direct.db"),
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        ai_humanize_emails=False,
        dry_run=False,
    )
    app = main.create_dashboard_app(settings)
    dispatched = []
    from modules.email_sender import EmailSender

    def _dispatch(self, **k):
        dispatched.append(k["contact"]["email"])
        return 1

    monkeypatch.setattr(EmailSender, "dispatch_to_contact", _dispatch)
    db = main.Database(settings.get_abs_db_path())
    return app.test_client(), db, dispatched


def _emailed(db, address, status="sent", version="2026-11-it"):
    cid = db.add_contact(db.log_search("q"), address, organization="Rossi Srl")
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "INSERT INTO email_log (timestamp, contact_id, recipient_email, "
            "subject, delivery_status, notice_version) VALUES (?,?,?,?,?,?)",
            ("2026-11-10 14:05:00", cid, address, "Ciao", status, version),
        )
    return cid


def _post(client, **body):
    body = {"organization": "Rossi Srl", "channel": "inbound_request", **body}
    return client.post("/api/send-manual", json=body)


@pytest.mark.parametrize("status", ["sent", "sent_unconfirmed"])
def test_a_second_send_is_refused_with_when_and_in_which_language(app_db, status):
    assert f"'{status}'" in POSSIBLY_SENT_STATUSES_SQL
    client, db, dispatched = app_db
    _emailed(db, "acquisti@rossi.it", status=status)

    res = _post(client, email="acquisti@rossi.it")

    assert res.status_code == 409, res.get_json()
    data = res.get_json()
    assert data["ok"] is False and data["already_emailed"] is True
    assert data["last_sent_on"] == "10 November 2026"
    assert "10 November 2026" in data["message"], data
    assert "Italian" in data["message"], data
    assert dispatched == []


def test_the_language_said_is_the_one_the_email_went_in(app_db):
    client, db, _dispatched = app_db
    _emailed(db, "buyer@brand.de", version="2026-10")

    res = _post(client, email="buyer@brand.de")
    data = res.get_json()

    assert res.status_code == 409, data
    assert "English" in data["message"] and "Italian" not in data["message"], data


def test_a_confirmed_second_send_goes(app_db):
    client, db, dispatched = app_db
    _emailed(db, "acquisti@rossi.it")

    res = _post(client, email="acquisti@rossi.it", confirm_repeat=True)

    assert res.status_code == 200, res.get_json()
    assert dispatched == ["acquisti@rossi.it"]


def test_a_preview_is_never_held_back(app_db):
    client, db, _dispatched = app_db
    _emailed(db, "acquisti@rossi.it")

    res = _post(client, email="acquisti@rossi.it", dry_run=True)

    assert res.status_code == 200, res.get_json()
    assert "already_emailed" not in res.get_json()


def test_a_failed_email_is_not_an_earlier_one(app_db):
    """The control: nothing that may have reached them is on record."""
    client, db, dispatched = app_db
    _emailed(db, "acquisti@rossi.it", status="failed")

    res = _post(client, email="acquisti@rossi.it")

    assert res.status_code == 200, res.get_json()
    assert dispatched == ["acquisti@rossi.it"]


def test_an_unsubscribed_address_is_refused_as_before_not_asked(app_db):
    """The opt-out refusal stands ahead of the question: confirming a repeat
    must never be the way past it."""
    client, db, dispatched = app_db
    _emailed(db, "acquisti@rossi.it")
    db.add_to_suppression("acquisti@rossi.it", "unsubscribed")

    for confirm in (False, True):
        res = _post(client, email="acquisti@rossi.it", confirm_repeat=confirm)
        assert res.status_code == 400, res.get_json()
        assert "already_emailed" not in res.get_json()
    assert dispatched == []
