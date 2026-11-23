"""An unsubscribe that meets a locked database is held, never lost.

The one-click route answers 200 whatever happens - mailbox providers do not
re-send a failed one-click unsubscribe, and an error counts against the
sending domain. So when record_opt_out hit "database is locked" (a retention
run or a send pass holding the write lock), the opt-out was logged and gone,
and the person kept receiving mail (GDPR Art. 21(3)).
"""

import os
import sqlite3
import time

import pytest

import main
from agent import OutreachAgent
from modules import optout_spool
from modules.database import Database
from modules.unsubscribe import UnsubscribeHandler

EMAIL = "objector@brand.de"
SECRET = "s" * 40


def _locked(monkeypatch, db):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "record_opt_out", boom)


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "u.db"))
    # A link only ever goes to someone we emailed (step G20 resolves it so).
    cid = db.add_contact(db.log_search("q"), EMAIL)
    db.log_email(cid, EMAIL, "Hello", delivery_status="sent")
    return db


def test_a_locked_database_holds_the_opt_out_on_disk(db, monkeypatch):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.generate_token(EMAIL)
    _locked(monkeypatch, db)

    ok, _ = handler.process_unsubscribe(token)

    assert ok
    assert optout_spool.pending_addresses(db.db_path) == {EMAIL}


def test_the_held_opt_out_is_applied_once_the_database_is_free(db, monkeypatch):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.generate_token(EMAIL)
    with monkeypatch.context() as m:
        _locked(m, db)
        handler.process_unsubscribe(token)

    assert optout_spool.replay_optout_spool(db) == 1
    assert db.is_suppressed(EMAIL)
    assert optout_spool.pending_addresses(db.db_path) == set()


def test_a_still_locked_database_keeps_the_file_for_next_time(db, monkeypatch):
    optout_spool.spool_optout(db.db_path, EMAIL, "one-click unsubscribe")
    with monkeypatch.context() as m:
        _locked(m, db)
        assert optout_spool.replay_optout_spool(db) == 0
        assert optout_spool.pending_addresses(db.db_path) == {EMAIL}

    # Handed back, not left claimed: the very next pass applies it, rather
    # than waiting out the stale-claim period with every send refused.
    assert optout_spool.replay_optout_spool(db) == 1
    assert db.is_suppressed(EMAIL)


def test_a_file_claimed_by_the_other_process_is_left_alone(db):
    held = optout_spool.spool_optout(db.db_path, EMAIL, "one-click unsubscribe")
    os.replace(held, held.with_suffix(".claimed"))

    assert optout_spool.replay_optout_spool(db) == 0
    assert not db.is_suppressed(EMAIL)
    assert optout_spool.pending_addresses(db.db_path) == {EMAIL}


def test_a_claim_abandoned_by_a_dead_process_is_taken_back(db):
    held = optout_spool.spool_optout(db.db_path, EMAIL, "one-click unsubscribe")
    claimed = held.with_suffix(".claimed")
    os.replace(held, claimed)
    old = time.time() - optout_spool.STALE_CLAIM_SECONDS - 60
    os.utime(claimed, (old, old))

    assert optout_spool.replay_optout_spool(db) == 1
    assert db.is_suppressed(EMAIL)


@pytest.mark.parametrize("method", ["post", "get"])
def test_both_link_routes_hold_the_opt_out(
    tmp_path, settings_factory, monkeypatch, method
):
    s = settings_factory(db_path=str(tmp_path / "w.db"), unsubscribe_secret=SECRET)
    held = Database(s.get_abs_db_path())
    # A link only ever goes to someone we emailed (step G20 resolves it so).
    cid = held.add_contact(held.log_search("q"), EMAIL)
    held.log_email(cid, EMAIL, "Hello", delivery_status="sent")
    token = UnsubscribeHandler(held, SECRET).generate_token(EMAIL)

    def boom(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "record_opt_out", boom)
    client = main.create_dashboard_app(s).test_client()

    response = getattr(client, method)(f"/unsubscribe/{token}")

    assert response.status_code == 200
    assert optout_spool.pending_addresses(s.get_abs_db_path()) == {EMAIL}


def test_the_dashboard_applies_held_opt_outs_when_it_starts(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "w.db"))
    Database(s.get_abs_db_path())
    optout_spool.spool_optout(s.get_abs_db_path(), EMAIL, "one-click unsubscribe")

    main.create_dashboard_app(s)

    assert Database(s.get_abs_db_path()).is_suppressed(EMAIL)


def _agent(settings_factory, tmp_path):
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )


def test_the_agent_applies_held_opt_outs_when_it_starts(tmp_path, settings_factory):
    path = str(tmp_path / "a.db")
    Database(path)
    optout_spool.spool_optout(path, EMAIL, "one-click unsubscribe")

    agent = _agent(settings_factory, tmp_path)

    assert agent.db.is_suppressed(EMAIL)


def _emails_logged(agent):
    return agent.db.conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0]


def _contact(agent):
    sid = agent.db.log_search("q", source="test")
    cid = agent.db.add_contact(sid, EMAIL, name="Anna", organization="Brand")
    return agent.db.get_contact_by_id(cid)


# Dry runs: they go through the same per-email checks, without the live-send
# blocker (blank legal identity) refusing everything first.


def test_control_the_dry_run_goes_through_when_nothing_is_held(
    tmp_path, settings_factory
):
    agent = _agent(settings_factory, tmp_path)
    assert agent._process_contact(_contact(agent), dry_run=True) is True
    assert _emails_logged(agent) == 1


def test_no_email_goes_to_an_address_whose_opt_out_is_held(
    tmp_path, settings_factory, monkeypatch
):
    agent = _agent(settings_factory, tmp_path)
    contact = _contact(agent)
    optout_spool.spool_optout(agent.db.db_path, EMAIL, "one-click unsubscribe")
    _locked(monkeypatch, agent.db)

    assert agent._process_contact(contact, dry_run=True) is False
    assert _emails_logged(agent) == 0


def test_the_send_check_applies_held_opt_outs_first(tmp_path, settings_factory):
    agent = _agent(settings_factory, tmp_path)
    contact = _contact(agent)
    optout_spool.spool_optout(agent.db.db_path, EMAIL, "one-click unsubscribe")

    assert agent._process_contact(contact, dry_run=True) is False
    assert agent.db.is_suppressed(EMAIL)
    assert _emails_logged(agent) == 0


def _monitor(db):
    from modules.imap_monitor import ImapMonitor

    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    return monitor


def test_an_opt_out_by_email_that_meets_a_locked_database_is_held(db, monkeypatch):
    """Before, the error escaped process_unsubscribes: the watermark held, so
    the next inbox check would retry - but until then the address was not
    suppressed, and every later opt-out in the same batch waited too."""
    with monkeypatch.context() as m:
        real = db.record_opt_out

        def locked_for_one(email, how):
            if email == EMAIL:
                raise sqlite3.OperationalError("database is locked")
            return real(email, how)

        m.setattr(db, "record_opt_out", locked_for_one)
        _monitor(db).process_unsubscribes(
            [{"email": EMAIL}, {"email": "second@brand.de"}]
        )

    assert db.is_suppressed("second@brand.de"), "one failure stopped the batch"
    assert optout_spool.pending_addresses(db.db_path) == {EMAIL}
    optout_spool.replay_optout_spool(db)
    assert db.is_suppressed(EMAIL)


def test_a_held_email_opt_out_from_someone_we_emailed_still_stops_their_company(
    db, monkeypatch
):
    # The fixture has emailed EMAIL already; a colleague at the same company.
    db.add_contact(db.log_search("q", source="test"), "info@brand.de")
    with monkeypatch.context() as m:
        _locked(m, db)
        _monitor(db).process_unsubscribes([{"email": EMAIL}])

    optout_spool.replay_optout_spool(db)
    assert db.is_blocked("info@brand.de")
