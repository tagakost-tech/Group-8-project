"""An access request is answered with the data and what Art. 15 requires.

gdpr-export printed raw JSON to the screen - the address and everything
held about it, in the terminal's scrollback - and nothing else: not why the
data is held, who receives it, how long it is kept, where it came from, the
rights the person has, or that they are scored (Art. 15(1)). A person erased
earlier who asked what was still held got "nothing", although their token
is still held.

Now the export writes a folder - named by the keyed token, so no address
appears in a file listing - with records.json and a cover letter covering
Art. 15(1), and prints only the folder's path. An erasure removes the
folder. Records held under the erasure token are found too.
"""

import json
import pathlib

import pytest

import main
from modules.database import Database, erasure_token
from modules.export import DataExporter, records_held_about

EMAIL = "anna.rossi@brandco.de"
SOURCE = "https://brandco.de/contatti"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "x.db"))


@pytest.fixture
def db(s):
    db = Database(s.get_abs_db_path())
    sid = db.log_search("q", source="test")
    cid = db.add_contact(
        sid, EMAIL, name="Anna Rossi", organization="Brandco", source_url=SOURCE
    )
    db.log_email(cid, EMAIL, "Hello", delivery_status="sent")
    return db


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _folder(out):
    (line,) = [l for l in out.splitlines() if "exports" in l]
    return pathlib.Path(line.split(": ", 1)[1].strip())


def test_the_command_prints_only_where_the_answer_is(s, db, monkeypatch, capsys):
    out = _cli(s, monkeypatch, capsys, "gdpr-export", EMAIL)

    assert EMAIL not in out
    folder = _folder(out)
    assert folder.is_dir() and EMAIL not in str(folder)
    records = json.loads((folder / "records.json").read_text(encoding="utf-8"))
    assert [c["email"] for c in records["contacts"]] == [EMAIL]


def test_the_cover_letter_answers_article_15(s, db, monkeypatch, capsys):
    folder = _folder(_cli(s, monkeypatch, capsys, "gdpr-export", EMAIL))
    cover = (folder / "cover.md").read_text(encoding="utf-8")
    for required in (
        "Purposes",
        "Data held",
        "Recipients",
        "How long",
        SOURCE,  # where the data came from (Art. 15(1)(g))
        "rectification",
        "erasure",
        "object",
        "complaint",
        "scor",  # profiling (Art. 15(1)(h))
        s.legal_entity_name,
    ):
        assert required in cover, required


def test_a_registered_request_is_exported_by_its_id(s, db, monkeypatch, capsys):
    rid = db.add_dsr(EMAIL, "access", "reply")
    out = _cli(s, monkeypatch, capsys, "dsr", "export", str(rid))
    assert EMAIL not in out
    assert (_folder(out) / "cover.md").exists()
    assert f"request #{rid}" in (_folder(out) / "cover.md").read_text(encoding="utf-8")
    assert db.get_dsr(rid)["status"] == "open"  # closed once the answer is sent


def test_an_erasure_removes_the_export(s, db, monkeypatch, capsys):
    folder = _folder(_cli(s, monkeypatch, capsys, "gdpr-export", EMAIL))
    DataExporter(db).gdpr_erase(EMAIL)
    assert not folder.exists()


def test_records_held_under_the_erasure_token_are_found(db):
    DataExporter(db).gdpr_erase(EMAIL)
    held = records_held_about(db, EMAIL)
    assert [c["email"] for c in held["contacts"]] == [erasure_token(EMAIL)]
    assert [r["email"] for r in held["suppression_list"]] == [erasure_token(EMAIL)]
