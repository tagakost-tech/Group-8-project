"""A restore keeps the request register: registered and closed requests survive.

The journal held opt-outs, blocks, lifts and erasures, but not the request
register. A request registered by hand (post, phone: `dsr add`) after the
newest backup vanished on a restore, deadline included, and one answered
since was open again - an erasure request erased with `dsr erase` among
them. Now registering and closing a request are journalled, keyed by the
request itself (its message, or whose it is, what and when it arrived),
never by the row id, and replay-journal applies them, skipping a request the
restored database already holds. An erased person's lines carry only the
token, and the free-text outcome of their requests is not kept.
"""

import json
import os
import shutil

import pytest

from modules import backup, journal
from modules.database import Database, erasure_token
from modules.export import DataExporter

ANNA = "anna.rossi@brandco.de"
LUCA = "luca@other.de"


@pytest.fixture
def db(settings_factory, tmp_path):
    s = settings_factory(db_path=str(tmp_path / "live" / "agent.db"))
    os.makedirs(os.path.dirname(s.get_abs_db_path()), exist_ok=True)
    return Database(s.get_abs_db_path())


def _restore(db):
    """What an operator does: put the newest backup in the database's place."""
    live = db.db_path
    newest = max(backup.backups_dir(live).glob("agent-*.db"))
    db.conn.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(live + suffix):
            os.remove(live + suffix)
    shutil.copyfile(newest, live)
    return Database(live)


def _register(db):
    return sorted(
        (r["email"], r["kind"], r["status"], r["outcome"]) for r in db.list_dsrs()
    )


def test_a_request_registered_by_hand_since_the_backup_comes_back(db):
    db.add_dsr(LUCA, "access", "manual")
    backup.make_backup(db)
    db.add_dsr(ANNA, "access", "manual", received_at="2026-11-02T10:00:00+01:00")
    before = {r["email"]: r for r in db.list_dsrs()}[ANNA]

    restored = _restore(db)
    counts = journal.replay_journal(restored)

    after = {r["email"]: r for r in restored.list_dsrs()}
    assert set(after) == {LUCA, ANNA}
    assert (after[ANNA]["received_at"], after[ANNA]["due_at"]) == (
        before["received_at"],
        before["due_at"],
    )
    assert after[ANNA]["status"] == "open"
    assert counts["requests"] == 1


def test_a_request_answered_since_the_backup_stays_closed(db):
    luca = db.add_dsr(LUCA, "access", "manual")
    backup.make_backup(db)
    db.close_dsr(luca, "answered by letter")

    restored = _restore(db)
    journal.replay_journal(restored)

    assert _register(restored) == [(LUCA, "access", "closed", "answered by letter")]


def test_replaying_onto_a_backup_that_holds_them_registers_nothing_twice(db):
    anna = db.add_dsr(ANNA, "access", "manual")
    db.add_dsr(LUCA, "objection", "reply", source_ref="<m1@other.de>")
    db.close_dsr(anna, "answered")
    backup.make_backup(db)

    restored = _restore(db)
    journal.replay_journal(restored)
    journal.replay_journal(restored)

    assert _register(restored) == [
        (ANNA, "access", "closed", "answered"),
        (LUCA, "objection", "open", None),
    ]


def test_an_emailed_erasure_request_erased_since_the_backup_stays_closed(db):
    """`dsr erase`: the erasure, then the close, both after the backup."""
    rid = db.add_dsr(ANNA, "erasure", "reply", source_ref="<m2@brandco.de>")
    backup.make_backup(db)
    DataExporter(db).gdpr_erase(ANNA)
    db.close_dsr(rid, "erased")

    restored = _restore(db)
    journal.replay_journal(restored)

    assert _register(restored) == [(erasure_token(ANNA), "erasure", "closed", "erased")]
    assert ANNA not in journal.held_addresses(restored)


def test_a_request_for_someone_erased_since_the_backup_is_matched_by_the_token(db):
    """Registered by hand before the backup, closed and erased after it: the
    journal's lines now carry her token while the backup holds her address."""
    rid = db.add_dsr(ANNA, "erasure", "post")
    backup.make_backup(db)
    DataExporter(db).gdpr_erase(ANNA)
    db.close_dsr(rid, "erased")

    restored = _restore(db)
    journal.replay_journal(restored)

    assert _register(restored) == [(erasure_token(ANNA), "erasure", "closed", "erased")]


def test_the_journal_keeps_an_erased_persons_requests_only_as_tokens(db):
    rid = db.add_dsr(ANNA, "access", "manual")
    db.close_dsr(rid, "Answered Anna Rossi by letter")

    DataExporter(db).gdpr_erase(ANNA)

    text = journal.journal_path(db.db_path).read_text(encoding="utf-8").lower()
    assert "anna" not in text and "rossi" not in text
    kinds = [json.loads(line)["kind"] for line in text.splitlines()]
    assert "dsr" in kinds and "dsr_close" in kinds


def test_a_request_from_someone_already_erased_is_journalled_as_the_token(db):
    DataExporter(db).gdpr_erase(ANNA)

    db.add_dsr(ANNA, "access", "manual")

    (line,) = [
        json.loads(raw)
        for raw in journal.journal_path(db.db_path).read_text(encoding="utf-8").splitlines()
        if json.loads(raw)["kind"] == "dsr"
    ]
    assert line.get("token") == erasure_token(ANNA) and "email" not in line
