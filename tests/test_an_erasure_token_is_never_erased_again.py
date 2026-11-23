"""An erasure token is never erased again: the person behind it is, or no one.

Every erasure keeps the person's request register rows as their keyed token,
and so does the nightly retention run - which also leaves their objection on
the do-not-contact list in clear. `dsr erase` passed that token to
gdpr_erase, which took it for an address (the review of the GDPR work,
PRIV-01, PRIV-M1 and MISS-01):
- after the nightly run it erased the token, and her address and her
  company's domain stayed in clear in the do-not-contact list, the company
  block and the journal, while the register said "erased";
- for someone already erased - a second request, `gdpr-erase` first, a
  restore that reopened the request - it deleted her own token from the
  do-not-contact list. Once an operator lifted her company's block, she was
  collected and emailed again.
`dsr show` and `dsr export` worked from the token too: the operator could not
tell who had asked, and the access answer left out the address still held.

Now a token is refused wherever an address is erased, and `dsr show`, `dsr
export` and `dsr erase` act on the address the token stands for while the
do-not-contact list still holds it in clear. With nothing behind the token,
`dsr erase` reports her already erased and closes the request.
"""

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import main
from modules import backup, journal
from modules.database import Database, domain_token, erasure_token
from modules.export import DataExporter, anonymise_contact_rows, export_folder
from modules.imap_monitor import ImapMonitor
from modules.retention import apply_retention

ANNA = "anna@mariorossi.it"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "r.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


@pytest.fixture
def emailed(db):
    """Anna, whom the agent emailed. Returns her contact id."""
    cid = db.add_contact(
        db.log_search("q"),
        ANNA,
        name="Anna Rossi",
        organization="Mario Rossi Srl",
        source_url="https://www.mariorossi.it/contatti",
    )
    db.log_email(cid, ANNA, "Hello", delivery_status="sent")
    return cid


def _request(db, kind="erasure"):
    """Her reply as the inbox scan files it: an opt-out, her company blocked,
    and the request registered. Returns the request id."""
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    monitor.process_requests([{"email": ANNA, "request_kind": kind}])
    return max(r["id"] for r in db.list_dsrs())


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _rows(db, sql):
    raw = sqlite3.connect(db.db_path)
    try:
        return raw.execute(sql).fetchall()
    finally:
        raw.close()


def _still_erased(db, cid):
    """Her own token still stops her, even with her company's block lifted."""
    assert _rows(db, "SELECT email, reason FROM suppression_list") == [
        (erasure_token(ANNA), "gdpr_erasure")
    ]
    assert db.get_contact_by_id(cid)["email"] == erasure_token(ANNA)
    blocks = [d for (d,) in _rows(db, "SELECT domain FROM suppressed_domains")]
    assert domain_token("redacted.invalid") not in blocks
    # An operator may lift it: "the person's own address stays blocked".
    db.lift_domain_block("mariorossi.it")
    assert db.is_erased(ANNA)
    assert db.add_contact(db.log_search("q2"), ANNA) is None


# ── The register's token leads to her ──


def test_after_the_nightly_run_dsr_erase_erases_her_not_the_token(
    s, db, emailed, monkeypatch, capsys
):
    rid = _request(db)
    apply_retention(db, s)  # her objection ends the purpose at once (L1.16)
    assert db.get_dsr(rid)["email"] == erasure_token(ANNA), "setup: kept as the token"

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    assert _rows(db, "SELECT email, reason FROM suppression_list") == [
        (erasure_token(ANNA), "gdpr_erasure")
    ]
    assert _rows(db, "SELECT domain FROM suppressed_domains") == [
        (domain_token("mariorossi.it"),)
    ]
    text = journal.journal_path(db.db_path).read_text(encoding="utf-8")
    assert ANNA not in text and '"mariorossi.it"' not in text
    assert db.is_erased(ANNA)
    row = db.get_dsr(rid)
    assert (row["status"], row["outcome"]) == ("closed", "erased")
    assert ANNA not in out


def test_after_the_nightly_run_show_names_who_asked(
    s, db, emailed, monkeypatch, capsys
):
    rid = _request(db)
    apply_retention(db, s)

    assert ANNA in _cli(s, monkeypatch, capsys, "dsr", "show", str(rid))


def test_after_the_nightly_run_the_access_answer_includes_what_is_still_held(
    s, db, emailed, monkeypatch, capsys
):
    """Her objection is still on the do-not-contact list, in clear; the answer
    worked from the token and left it out (Art. 15(1))."""
    rid = _request(db, "access")
    apply_retention(db, s)

    out = _cli(s, monkeypatch, capsys, "dsr", "export", str(rid))

    folder = Path(out.split("written to: ", 1)[1].splitlines()[0].strip())
    held = json.loads((folder / "records.json").read_text(encoding="utf-8"))
    assert [r["email"] for r in held.get("suppression_list", [])] == [ANNA]
    # The folder her erasure removes, as it removes any answer prepared for her.
    assert folder == export_folder(db.db_path, ANNA)


# ── Someone already erased stays erased ──


def test_a_second_request_from_someone_erased_leaves_her_erased(
    s, db, emailed, monkeypatch, capsys
):
    """She wrote twice. The first request's erasure kept the second as her
    token, and erasing that deleted her own do-not-contact entry."""
    first, second = _request(db), _request(db)
    _cli(s, monkeypatch, capsys, "dsr", "erase", str(first))

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(second))

    _still_erased(db, emailed)
    assert "already erased" in out.lower()
    row = db.get_dsr(second)
    assert (row["status"], row["outcome"]) == ("closed", "already erased")


def test_dsr_erase_after_gdpr_erase_leaves_her_erased(
    s, db, emailed, monkeypatch, capsys
):
    rid = _request(db)
    _cli(s, monkeypatch, capsys, "gdpr-erase", ANNA)

    out = _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    _still_erased(db, emailed)
    assert "already erased" in out.lower()
    assert db.get_dsr(rid)["status"] == "closed"


def test_dsr_erase_after_a_restore_leaves_her_erased(
    s, db, emailed, monkeypatch, capsys
):
    """The backup holds the request open; the replay erases her again and
    keeps the reopened request as her token."""
    rid = _request(db)
    backup.make_backup(db)
    DataExporter(db).gdpr_erase(ANNA)
    db.close_dsr(rid, "erased")
    live = db.db_path
    db.conn.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(live + suffix):
            os.remove(live + suffix)
    shutil.copyfile(max(backup.backups_dir(live).glob("agent-*.db")), live)
    restored = Database(live)
    journal.replay_journal(restored)

    _cli(s, monkeypatch, capsys, "dsr", "erase", str(rid))

    _still_erased(restored, emailed)


# ── A token is refused wherever an address is erased ──


@pytest.fixture
def erased(db, emailed):
    DataExporter(db).gdpr_erase(ANNA)
    return erasure_token(ANNA)


def test_gdpr_erase_refuses_a_token_and_changes_nothing(db, emailed, erased):
    with pytest.raises(ValueError):
        DataExporter(db).gdpr_erase(f"  {erased.upper()} ")
    _still_erased(db, emailed)


def test_suppress_erased_refuses_a_token(db, emailed, erased):
    """Its DELETE of the plain entry it replaces took her own token row."""
    with pytest.raises(ValueError):
        db.suppress_erased(erased)
    _still_erased(db, emailed)


def test_the_shared_anonymiser_leaves_a_token_alone(db, emailed, erased):
    """Retention never passes one; it re-keyed her rows under a token of it,
    which no check for her address matches."""
    result = anonymise_contact_rows(db, erased)
    db.conn.commit()
    assert result == {"contact_ids": [], "actions": []}
    assert db.get_contact_by_id(emailed)["email"] == erased


def test_the_erase_command_refuses_a_token_before_opening_the_database(capsys):
    with pytest.raises(SystemExit) as exc:
        main.build_parser().parse_args(["gdpr-erase", erasure_token(ANNA)])
    assert exc.value.code == 2
    assert "erasure token" in capsys.readouterr().err
