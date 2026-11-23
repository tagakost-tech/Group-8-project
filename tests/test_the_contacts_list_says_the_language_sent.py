"""The contacts list names the language its emails actually go in.

With templates/email_template_it.html missing or empty, process_contact sends
an Italian contact the English first email, and the follow-ups after it go in
English too. The contact panel said so; the list's badge still read IT,
"Written in Italian". With an Italian follow-up missing, backlog_draining
sends that follow-up in English, and neither the list nor the panel said so.
"""

import shutil
from pathlib import Path

import pytest

import main
from modules.database import Database
from modules.file_handler import FileHandler

ITALIAN = "acquisti@rossi-integratori.it"


def _app(tmp_path, settings_factory, monkeypatch, drop=(), blank=(), space=""):
    root = tmp_path / "product"
    shutil.copytree(
        Path(main.__file__).parent / "templates",
        root / "templates",
        ignore=shutil.ignore_patterns(*drop),
    )
    for name in blank:
        (root / "templates" / name).write_text(space, encoding="utf-8")
    monkeypatch.setattr(main, "FileHandler", lambda: FileHandler(str(root)))
    s = settings_factory(db_path=str(tmp_path / "lang.db"), ITALIAN_COUNTRIES="IT")
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), ITALIAN, organization="Rossi")
    return client, cid


def _listed(client, cid):
    return next(c for c in client.get("/api/contacts").get_json() if c["id"] == cid)


def _panel(client, cid):
    return dict(client.get(f"/api/contacts/{cid}/why").get_json()["facts"])[
        "Language"
    ]


FIRST_MISSING = (
    "Italian, but templates/email_template_it.html is missing, so the English "
    "first email goes"
)


@pytest.mark.parametrize(
    "drop, blank, space",
    [
        (("email_template_it.html",), (), ""),
        ((), ("email_template_it.html",), ""),
        ((), ("email_template_it.html",), " \n\t\n"),
    ],
    ids=["missing", "empty", "white-space"],
)
def test_no_italian_first_email_lists_english(
    tmp_path, settings_factory, monkeypatch, drop, blank, space
):
    client, cid = _app(tmp_path, settings_factory, monkeypatch, drop, blank, space)
    row = _listed(client, cid)
    assert row["language"] == "en"
    assert row["language_reason"].startswith(FIRST_MISSING), row["language_reason"]
    assert _panel(client, cid) == row["language_reason"]


def test_setting_italian_answers_with_the_language_sent(
    tmp_path, settings_factory, monkeypatch
):
    """The toast after a language change read "Italian (set by you)" while the
    English first email would go: the route skipped the list's rule."""
    client, cid = _app(
        tmp_path, settings_factory, monkeypatch, drop=("email_template_it.html",)
    )
    answer = client.post(
        f"/api/contacts/{cid}/language", json={"language": "it"}
    ).get_json()
    row = _listed(client, cid)
    assert answer["language"] == "en"
    assert answer["reason"].startswith(FIRST_MISSING), answer["reason"]
    assert (answer["language"], answer["reason"]) == (
        row["language"],
        row["language_reason"],
    )


def test_with_every_italian_text_the_list_says_italian(
    tmp_path, settings_factory, monkeypatch
):
    client, cid = _app(tmp_path, settings_factory, monkeypatch)
    row = _listed(client, cid)
    assert row["language"] == "it"
    assert row["language_reason"] == "Italian (its address ends in .it)"
    assert _panel(client, cid) == row["language_reason"]


@pytest.mark.parametrize(
    "drop, said",
    [
        (
            ("followup_1_it.html",),
            (
                ", but templates/followup_1_it.html is missing, so follow-up 1 "
                "goes in English"
            ),
        ),
        (
            ("followup_2_it.html",),
            (
                ", but templates/followup_2_it.html is missing, so follow-up 2 "
                "goes in English"
            ),
        ),
        (
            ("followup_1_it.html", "followup_2_it.html"),
            (
                ", but templates/followup_1_it.html and followup_2_it.html are "
                "missing, so follow-ups 1 and 2 go in English"
            ),
        ),
    ],
    ids=["first", "second", "both"],
)
def test_a_missing_italian_follow_up_is_said_to_go_in_english(
    tmp_path, settings_factory, monkeypatch, drop, said
):
    client, cid = _app(tmp_path, settings_factory, monkeypatch, drop)
    row = _listed(client, cid)
    assert row["language"] == "it"
    assert row["language_reason"] == "Italian (its address ends in .it)" + said
    assert _panel(client, cid) == row["language_reason"]


@pytest.mark.parametrize("space", ["", " \n\t\n"], ids=["empty", "white-space"])
def test_an_empty_italian_follow_up_counts_as_missing(
    tmp_path, settings_factory, monkeypatch, space
):
    client, cid = _app(
        tmp_path,
        settings_factory,
        monkeypatch,
        blank=("followup_2_it.html",),
        space=space,
    )
    assert _listed(client, cid)["language_reason"].endswith(
        "so follow-up 2 goes in English"
    )


def test_after_an_italian_first_email_its_follow_ups_go_in_english(
    tmp_path, settings_factory, monkeypatch
):
    """backlog_draining sends every follow-up in English while the Italian
    first email is missing, even to a contact first written to in Italian."""
    client, cid = _app(
        tmp_path, settings_factory, monkeypatch, drop=("email_template_it.html",)
    )
    db = Database(str(tmp_path / "lang.db"))
    db.log_email(
        cid, ITALIAN, "s", delivery_status="sent", notice_version="notice-li-1-it"
    )
    row = _listed(client, cid)
    assert row["language"] == "it"
    assert row["language_reason"] == (
        "Italian (the language of its first email), but "
        "templates/email_template_it.html is missing, so its follow-ups go in English"
    )
    assert _panel(client, cid) == row["language_reason"]
