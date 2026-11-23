"""An opt-out that meets a busy database does not write an erased address back.

When the database could not take an opt-out, the address was written to the
holding folder in clear until the next replay - also for someone erased on
request, whose address the erasure had just removed from that folder (found
by the review of step G20). A write lock does not stop a read, so an address
erased on request (suppressed for good by its keyed token), with its company
already blocked when this opt-out would block it, has nothing left to hold.
Any other suppression can be lifted and is still held, and a database that
cannot even be read still holds it.
"""

import sqlite3

import pytest

from modules import optout_spool
from modules.database import Database
from modules.export import DataExporter
from modules.imap_monitor import ImapMonitor
from modules.unsubscribe import UnsubscribeHandler

SECRET = "s" * 40
ERASED = "mario@mariorossi.it"


def _link(db, email):
    """A link whose click resolves to this address, erased or not.

    These tests used a link in the old form, which carried the address
    itself, so a click resolved to it even after an erasure had removed the
    link records. That form is refused now (no email carrying one was ever
    sent). A record of an opaque link that names the address - the record a
    restore from a backup made before the erasure brings back - reaches the
    same code."""
    token = UnsubscribeHandler(db, SECRET).token_for(email)
    db.register_unsubscribe_token(token, email)
    return token


def _busy(*a, **k):
    raise sqlite3.OperationalError("database is locked")


def _held_text(db):
    folder = optout_spool.spool_dir(db.db_path)
    if not folder.is_dir():
        return ""
    return "".join(p.read_text(encoding="utf-8") for p in folder.iterdir())


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "e.db"))
    db.add_contact(db.log_search("q"), ERASED, name="Mario Rossi")
    DataExporter(db).gdpr_erase(ERASED)
    return db


def test_an_old_link_click_on_a_busy_database_writes_nothing_back(db, monkeypatch):
    monkeypatch.setattr(Database, "record_opt_out", _busy)
    ok, _ = UnsubscribeHandler(db, SECRET).process_unsubscribe(
        _link(db, ERASED), one_click=True
    )
    assert ok
    assert "mariorossi" not in _held_text(db)
    assert db.is_suppressed(ERASED)


def test_a_company_block_that_meets_a_busy_database_writes_nothing_back(
    db, monkeypatch
):
    monkeypatch.setattr("modules.unsubscribe.block_company", _busy)
    UnsubscribeHandler(db, SECRET).process_unsubscribe(_link(db, ERASED), one_click=True)
    assert "mariorossi" not in _held_text(db)


def test_an_opt_out_by_email_on_a_busy_database_writes_nothing_back(db, monkeypatch):
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    monkeypatch.setattr(Database, "record_opt_out", _busy)
    monitor.process_unsubscribes([{"email": ERASED}])
    assert "mariorossi" not in _held_text(db)


def test_a_database_that_cannot_even_be_read_still_holds_it(db, monkeypatch):
    monkeypatch.setattr(Database, "record_opt_out", _busy)
    monkeypatch.setattr(Database, "is_erased", _busy)
    UnsubscribeHandler(db, SECRET).process_unsubscribe(_link(db, ERASED))
    assert optout_spool.pending_addresses(db.db_path) == {ERASED}


def test_someone_not_yet_suppressed_is_still_held(db, monkeypatch):
    monkeypatch.setattr(Database, "record_opt_out", _busy)
    UnsubscribeHandler(db, SECRET).process_unsubscribe(_link(db, "anna@brandco.de"))
    assert optout_spool.pending_addresses(db.db_path) == {"anna@brandco.de"}


@pytest.mark.parametrize("lifted_later", ["operator_block", "bounce"])
def test_a_suppression_that_can_be_lifted_still_holds_the_opt_out(
    tmp_path, monkeypatch, lifted_later
):
    """Found by the security review of the first version of this fix, which
    skipped every suppressed address: an operator's block (Unblock lifts it)
    and a bounce (retention releases it) were skipped too, so the opt-out
    that would have made them permanent was lost."""
    db = Database(str(tmp_path / "l.db"))
    if lifted_later == "operator_block":
        db.block_address("anna@brandco.de")
    else:
        db.add_to_suppression("anna@brandco.de", "bounce")
    with monkeypatch.context() as m:
        m.setattr(Database, "record_opt_out", _busy)
        UnsubscribeHandler(db, SECRET).process_unsubscribe(_link(db, "anna@brandco.de"))
    assert optout_spool.pending_addresses(db.db_path) == {"anna@brandco.de"}
    optout_spool.replay_optout_spool(db)
    assert db.get_suppression_reason("anna@brandco.de") == "unsubscribe"


def _plain_domains(db):
    return [r[0] for r in db.conn.execute("SELECT domain FROM suppressed_domains")]


def test_a_one_click_after_the_erasures_block_was_lifted_reblocks_it_keyed(db):
    """An operator lifted the company block the erasure left. The erased
    person's one-click on an old link blocks the company again - and wrote
    the domain back in clear, naming them (found while testing this file)."""
    db.lift_domain_block("mariorossi.it")
    UnsubscribeHandler(db, SECRET).process_unsubscribe(_link(db, ERASED), one_click=True)
    assert db.company_blocked("sales@mariorossi.it")
    assert "mariorossi.it" not in _plain_domains(db)


def test_that_one_click_meeting_a_busy_database_is_held_and_reblocks(db, monkeypatch):
    db.lift_domain_block("mariorossi.it")
    with monkeypatch.context() as m:
        m.setattr("modules.unsubscribe.block_company", _busy)
        UnsubscribeHandler(db, SECRET).process_unsubscribe(
            _link(db, ERASED), one_click=True
        )
    optout_spool.replay_optout_spool(db)
    assert db.company_blocked("sales@mariorossi.it")
    assert "mariorossi.it" not in _plain_domains(db)
