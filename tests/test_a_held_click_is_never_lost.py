"""An unsubscribe click on a link we sent is never lost (review of step G20).

Found by an independent review and reproduced before fixing:
- After UNSUBSCRIBE_SECRET changed, a link sent earlier matched nobody: the
  click was answered and recorded nothing, a click held on disk was deleted
  by the replay, and the person kept being emailed. The record of the links
  we sent is what makes a token ours, so it alone decides.
- A click held while the database was broken, followed by a restore from a
  backup older than the link, was deleted by the replay (the restored
  database has no record of the link) and the person was emailed again. An
  unmatched click is now set aside under its own token for a year: the
  recipient whose link it is is refused, and the opt-out applied the moment
  we are about to email them. A send looks up that one file, so stray tokens
  cost it nothing and cannot push a real click out.
- While a one-click (company-wide) opt-out was held, colleagues at the same
  company were still emailed.
- The agent applied held clicks only at its first send, not when it started.
"""

import os
import sqlite3

import pytest

from modules import clock, optout_spool
from modules.database import Database
from modules.unsubscribe import UnsubscribeHandler

SECRET = "s" * 40
EARLIER_SECRET = "e" * 40
EMAIL = "anna.rossi@brandco.de"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "h.db"))


def _emailed(db, email=EMAIL):
    cid = db.add_contact(db.log_search("q"), email, organization="Brandco")
    db.log_email(cid, email, "Hello", delivery_status="sent")
    return cid


@pytest.fixture
def agent(settings_factory, tmp_path, monkeypatch):
    from agent import OutreachAgent

    a = OutreachAgent(
        settings_factory(db_path=str(tmp_path / "a.db"), unsubscribe_secret=SECRET)
    )
    a.sent = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: a.sent.append(k) or 1
    )
    return a


def test_a_link_sent_before_a_key_change_still_opts_out(db):
    _emailed(db)
    token = UnsubscribeHandler(db, EARLIER_SECRET).generate_token(EMAIL)
    ok, _ = UnsubscribeHandler(db, SECRET).process_unsubscribe(token)
    assert ok and db.is_suppressed(EMAIL)


def test_a_held_click_survives_a_key_change(db):
    _emailed(db)
    token = UnsubscribeHandler(db, EARLIER_SECRET).generate_token(EMAIL)
    optout_spool.spool_optout(db.db_path, None, "link", token=token)
    optout_spool.replay_optout_spool(
        db, resolve=UnsubscribeHandler(db, SECRET).verify_token
    )
    assert db.is_suppressed(EMAIL)
    assert optout_spool.pending_tokens(db.db_path) == set()


def test_a_held_click_a_restore_cannot_match_is_applied_when_its_recipient_comes_up(
    agent,
):
    cid = _emailed(agent.db)
    # The link went out, but the restored database never saw it recorded.
    token = agent.unsubscribe_handler.token_for(EMAIL)
    optout_spool.spool_optout(
        agent.db.db_path, None, "one-click", company=True, token=token
    )

    optout_spool.replay_optout_spool(
        agent.db, resolve=agent.unsubscribe_handler.verify_token
    )
    assert optout_spool.unmatched_tokens(agent.db.db_path) == {token}, "not dropped"

    assert agent._process_contact(agent.db.get_contact_by_id(cid)) is False
    assert agent.sent == []
    assert agent.db.is_suppressed(EMAIL)
    assert agent.db.company_blocked("someone@brandco.de")
    assert optout_spool.unmatched_tokens(agent.db.db_path) == set()


def test_retention_drops_a_click_set_aside_for_a_year(db, settings_factory):
    from modules.retention import apply_retention

    stray = "u" + "1" * 32
    optout_spool.spool_optout(db.db_path, None, "link", token=stray)
    optout_spool.replay_optout_spool(
        db, resolve=UnsubscribeHandler(db, SECRET).verify_token
    )
    path = optout_spool.spool_dir(db.db_path) / f"{stray}.unmatched"
    day = 86400
    year_ago = clock.now().timestamp() - optout_spool.UNMATCHED_TOKEN_DAYS * day
    s = settings_factory(db_path=db.db_path)

    os.utime(path, (year_ago + day, year_ago + day))
    assert apply_retention(db, s)["held_clicks_expired"] == 0
    os.utime(path, (year_ago - day, year_ago - day))
    assert apply_retention(db, s, dry_run=True)["held_clicks_expired"] == 1
    assert path.exists()
    assert apply_retention(db, s)["held_clicks_expired"] == 1
    assert not path.exists()


def test_a_held_company_opt_out_refuses_colleagues(agent, monkeypatch):
    sid = agent.db.log_search("q")
    colleague = agent.db.add_contact(sid, "bob@brandco.de", organization="Brandco")
    other = agent.db.add_contact(sid, "carla@othercorp.de", organization="Othercorp")
    optout_spool.spool_optout(agent.db.db_path, EMAIL, "one-click", company=True)

    def busy(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "record_opt_out", busy)
    assert agent._process_contact(agent.db.get_contact_by_id(colleague)) is False
    assert agent.sent == []
    assert agent._process_contact(agent.db.get_contact_by_id(other)) is True


def test_a_held_address_only_opt_out_leaves_colleagues_alone(agent, monkeypatch):
    colleague = agent.db.add_contact(
        agent.db.log_search("q"), "bob@brandco.de", organization="Brandco"
    )
    optout_spool.spool_optout(agent.db.db_path, EMAIL, "link", company=False)

    def busy(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "record_opt_out", busy)
    assert agent._process_contact(agent.db.get_contact_by_id(colleague)) is True


def test_the_agent_applies_held_clicks_when_it_starts(settings_factory, tmp_path):
    from agent import OutreachAgent

    s = settings_factory(db_path=str(tmp_path / "s.db"), unsubscribe_secret=SECRET)
    db = Database(s.get_abs_db_path())
    _emailed(db)
    token = UnsubscribeHandler(db, SECRET).generate_token(EMAIL)
    optout_spool.spool_optout(db.db_path, None, "link", token=token)

    OutreachAgent(s)
    assert db.is_suppressed(EMAIL)
    assert optout_spool.pending_tokens(db.db_path) == set()


def test_stray_clicks_cost_a_send_nothing_and_push_out_nothing(agent, monkeypatch):
    """Found by two security reviews: unmatched clicks kept were rescanned
    before every send, and a cap on them let random tokens push real ones
    out. Each is set aside under its own token, so a send looks up one file."""
    cid = _emailed(agent.db)
    real = agent.unsubscribe_handler.token_for(EMAIL)
    for i in range(300):
        optout_spool.spool_optout(agent.db.db_path, None, "link", token=f"u{i:032x}")
    optout_spool.spool_optout(agent.db.db_path, None, "link", token=real)
    optout_spool.replay_optout_spool(
        agent.db, resolve=agent.unsubscribe_handler.verify_token
    )
    assert len(optout_spool.unmatched_tokens(agent.db.db_path)) == 301

    other = agent.db.add_contact(
        agent.db.log_search("q"), "carla@othercorp.de", organization="Othercorp"
    )
    reads = []
    read = optout_spool._read
    monkeypatch.setattr(optout_spool, "_read", lambda p: reads.append(p) or read(p))
    assert agent._process_contact(agent.db.get_contact_by_id(other)) is True
    assert reads == [], "a send read the clicks set aside"
    assert agent._process_contact(agent.db.get_contact_by_id(cid)) is False
    assert agent.db.is_suppressed(EMAIL)


def test_a_set_aside_click_the_database_cannot_take_yet_still_refuses(
    agent, monkeypatch
):
    cid = _emailed(agent.db)
    token = agent.unsubscribe_handler.token_for(EMAIL)
    optout_spool.spool_optout(agent.db.db_path, None, "link", token=token)
    optout_spool.replay_optout_spool(
        agent.db, resolve=agent.unsubscribe_handler.verify_token
    )

    def busy(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "record_opt_out", busy)
    assert agent._process_contact(agent.db.get_contact_by_id(cid)) is False
    assert agent.sent == []
    assert optout_spool.unmatched_tokens(agent.db.db_path) == {token}
