"""After a restore, a click on a link sent since the backup still opts out.

A restore takes the record of the links we made back to the backup, and the
journal holds no links. A click on a link sent since found no record, was told
"nothing was changed", and recorded nothing, though mailbox providers never
re-send a one-click. The restored database treats that person as never emailed,
so the next pass sent them the first email again (found by the analysis, probes
p3, p3b and p3c).

Now:
- replay-journal, and its rehearsal restore-check, record again the link of
  every contact the restored database holds: under the same UNSUBSCRIBE_SECRET
  it is the same token. Only contacts: no link went out to an address without
  one, and retention, which follows contacts, would never take that record away.
- a click still unmatched (a contact collected after the backup, whom the
  restored database does not hold) is set aside as <token>.unmatched, as a click
  held while the database was busy already was. The check before each send
  applies it.
- a click stays one lookup (step G20). The clicks set aside are capped: beyond
  the cap a new one is refused, none is evicted, the page says nothing was
  changed, and doctor reports it.
"""

import os
import shutil

import pytest

import main
from modules import backup, journal, optout_spool
from modules.database import Database
from modules.export import DataExporter
from modules.retention import apply_retention
from modules.unsubscribe import UnsubscribeHandler

SECRET = "s" * 40
ANNA = "anna.rossi@brandco.de"
BOB = "bob@brandco.de"
LUCA = "luca@other.de"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(
        db_path=str(tmp_path / "live" / "agent.db"), unsubscribe_secret=SECRET
    )


@pytest.fixture
def db(s):
    os.makedirs(os.path.dirname(s.get_abs_db_path()), exist_ok=True)
    return Database(s.get_abs_db_path())


def _contact(db, email=ANNA):
    return db.add_contact(db.log_search("q"), email, organization="Brandco")


def _send(db, cid, email=ANNA):
    """The email going out: its link is recorded and the send logged."""
    url = UnsubscribeHandler(db, SECRET).generate_unsubscribe_url(
        email, "https://outreach.example.test"
    )
    db.log_email(cid, email, "Hello", delivery_status="sent")
    return url.rsplit("/", 1)[1]


def _emailed_since_the_backup(db, email=ANNA):
    cid = _contact(db, email)
    backup.make_backup(db)
    return _send(db, cid, email)


def _restore(db):
    """What an operator does: put the newest backup in the database's place."""
    live = db.db_path
    newest = max(backup.backups_dir(live).glob("agent-*.db"))
    db.close_thread_conn()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(live + suffix):
            os.remove(live + suffix)
    shutil.copyfile(newest, live)
    return Database(live)


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _agent(s, monkeypatch):
    from agent import OutreachAgent

    a = OutreachAgent(s)
    a.sent = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: a.sent.append(k) or 1
    )
    return a


def _stray(i):
    """A link of ours the database has no record of: made-up ones are not kept."""
    return UnsubscribeHandler(None, SECRET).token_for(f"stray{i}@nowhere.test")


def _cap(monkeypatch, n):
    monkeypatch.setattr(optout_spool, "MAX_UNMATCHED_TOKENS", n, raising=False)


def _set_aside_text(db, token):
    path = optout_spool.spool_dir(db.db_path) / f"{token}.unmatched"
    return path.read_text(encoding="utf-8")


def _links(db):
    return sorted(r[0] for r in db.conn.execute("SELECT email FROM unsubscribe_tokens"))


# ── A click no record matches is kept, and the send check applies it ──


def test_a_click_on_a_link_the_restored_database_lost_is_set_aside(db):
    token = _emailed_since_the_backup(db)
    restored = _restore(db)
    journal.replay_journal(restored)
    assert restored.address_for_unsubscribe_token(token) is None, (
        "setup: the backup predates the link"
    )

    ok, _ = UnsubscribeHandler(restored, SECRET).process_unsubscribe(token)

    assert ok
    assert optout_spool.is_set_aside(restored.db_path, token)


def test_the_next_send_to_them_is_refused_and_applies_the_click(s, db, monkeypatch):
    token = _emailed_since_the_backup(db)
    restored = _restore(db)
    journal.replay_journal(restored)
    UnsubscribeHandler(restored, SECRET).process_unsubscribe(token)

    agent = _agent(s, monkeypatch)
    contact = agent.db.get_contact_by_email(ANNA)
    assert contact["status"] == "pending", "setup: the restored database never sent"

    assert agent._process_contact(contact) is False
    assert agent.sent == [], "they clicked, and got the first email again"
    assert agent.db.is_suppressed(ANNA)
    assert agent.db.get_contact_by_email(ANNA)["status"] == "unsubscribed"
    assert not agent.db.company_blocked(BOB), "a plain click stops only the address"
    assert not optout_spool.is_set_aside(agent.db.db_path, token)


def test_a_one_click_set_aside_stops_the_company_when_its_recipient_comes_up(
    s, db, monkeypatch
):
    token = _emailed_since_the_backup(db)
    restored = _restore(db)
    journal.replay_journal(restored)
    UnsubscribeHandler(restored, SECRET).process_unsubscribe(token, one_click=True)

    agent = _agent(s, monkeypatch)
    assert agent._process_contact(agent.db.get_contact_by_email(ANNA)) is False
    assert agent.sent == []
    assert agent.db.is_suppressed(ANNA)
    assert agent.db.company_blocked(BOB)


def test_a_contact_collected_after_the_backup_is_refused_when_found_again(
    s, db, monkeypatch, capsys
):
    """The case re-recording links cannot reach: the restored database does not
    hold the address, so only the click kept on disk can stop the next email."""
    backup.make_backup(db)
    token = _send(db, _contact(db))
    restored = _restore(db)
    restored.close_thread_conn()
    _cli(s, monkeypatch, capsys, "replay-journal")

    after = Database(s.get_abs_db_path())
    assert after.get_contact_by_email(ANNA) is None, "setup: collected after the backup"
    UnsubscribeHandler(after, SECRET).process_unsubscribe(token, one_click=True)

    agent = _agent(s, monkeypatch)
    _contact(agent.db)
    assert agent._process_contact(agent.db.get_contact_by_email(ANNA)) is False
    assert agent.sent == []
    assert agent.db.is_suppressed(ANNA)
    assert agent.db.company_blocked(BOB)


def test_the_route_keeps_a_one_click_and_says_so_to_a_plain_click(s, db):
    client = main.create_dashboard_app(s).test_client()
    one_click, plain = _stray(1), _stray(2)

    assert client.post(f"/unsubscribe/{one_click}").status_code == 200
    assert optout_spool.is_set_aside(db.db_path, one_click)

    body = client.get(f"/unsubscribe/{plain}").get_data(as_text=True)
    assert optout_spool.is_set_aside(db.db_path, plain)
    assert "kept your request for a year" in body
    assert "nothing was changed" not in body, "the click is kept now"
    assert "will not email you again" not in body
    assert "unsubscribe link in it" in body


def test_an_erased_persons_click_writes_nothing_back_when_she_comes_up(
    s, db, monkeypatch
):
    token = _send(db, _contact(db))
    DataExporter(db).gdpr_erase(ANNA)

    UnsubscribeHandler(db, SECRET).process_unsubscribe(token, one_click=True)
    assert optout_spool.is_set_aside(db.db_path, token)
    assert ANNA not in _set_aside_text(db, token)

    agent = _agent(s, monkeypatch)
    assert agent._process_contact({"id": None, "email": ANNA}) is False
    assert agent.sent == []
    held = [r[0] for r in db.conn.execute("SELECT email FROM suppression_list")]
    domains = [r[0] for r in db.conn.execute("SELECT domain FROM suppressed_domains")]
    assert ANNA not in held and "brandco.de" not in domains
    assert not optout_spool.is_set_aside(db.db_path, token)


@pytest.mark.parametrize("token", ["not-a-token", "../../evil", "U" + "0" * 32])
def test_a_token_not_in_our_form_is_refused_and_writes_nothing(db, token):
    ok, _ = UnsubscribeHandler(db, SECRET).process_unsubscribe(token)
    assert ok is False
    assert not optout_spool.spool_dir(db.db_path).exists()


# ── replay-journal records the links again ──


def test_after_replay_journal_a_click_opts_out_at_once(s, db, monkeypatch, capsys):
    token = _emailed_since_the_backup(db)
    restored = _restore(db)
    restored.close_thread_conn()

    out = _cli(s, monkeypatch, capsys, "replay-journal")

    after = Database(s.get_abs_db_path())
    ok, message = UnsubscribeHandler(after, SECRET).process_unsubscribe(token)
    assert ok and message != UnsubscribeHandler.NO_MATCH
    assert after.is_suppressed(ANNA)
    assert after.get_contact_by_email(ANNA)["status"] == "unsubscribed"
    assert not optout_spool.is_set_aside(after.db_path, token)
    assert "links" in out


def test_after_replay_journal_a_one_click_stops_colleagues_at_once(
    s, db, monkeypatch, capsys
):
    cid = _contact(db, ANNA)
    _contact(db, BOB)
    backup.make_backup(db)
    token = _send(db, cid)
    restored = _restore(db)
    restored.close_thread_conn()
    _cli(s, monkeypatch, capsys, "replay-journal")

    after = Database(s.get_abs_db_path())
    UnsubscribeHandler(after, SECRET).process_unsubscribe(token, one_click=True)

    agent = _agent(s, monkeypatch)
    assert agent._process_contact(agent.db.get_contact_by_email(BOB)) is False
    assert agent.sent == [], "Anna's company said no, and her colleague got the email"
    assert agent.db.company_blocked(BOB)


def test_replay_journal_records_no_link_for_someone_erased_since_the_backup(
    s, db, monkeypatch, capsys
):
    _contact(db, ANNA)
    _contact(db, LUCA)
    backup.make_backup(db)
    DataExporter(db).gdpr_erase(LUCA)
    restored = _restore(db)
    restored.close_thread_conn()

    _cli(s, monkeypatch, capsys, "replay-journal")

    assert _links(Database(s.get_abs_db_path())) == [ANNA]


def test_replay_journal_records_no_link_for_a_contact_retention_erases(
    s, db, monkeypatch, capsys
):
    cid = _contact(db, LUCA)
    db.conn.execute(
        "UPDATE contacts SET timestamp = datetime('now','localtime','-31 days') "
        "WHERE id = ?",
        (cid,),
    )
    db.conn.commit()
    backup.make_backup(db)
    restored = _restore(db)
    restored.close_thread_conn()

    _cli(s, monkeypatch, capsys, "replay-journal")

    after = Database(s.get_abs_db_path())
    assert after.get_contact_by_email(LUCA) is None
    assert _links(after) == []


def test_a_link_recorded_again_never_outlives_what_retention_can_reach(
    s, db, monkeypatch, capsys
):
    """No link went out to an address only the suppression list holds, and
    retention follows contacts: recorded by the replay, a released bounce would
    stay in clear for good, as a previewed address once did."""
    db.add_to_suppression(LUCA, "bounce")
    backup.make_backup(db)
    restored = _restore(db)
    restored.close_thread_conn()
    _cli(s, monkeypatch, capsys, "replay-journal")

    after = Database(s.get_abs_db_path())
    after.conn.execute("UPDATE suppression_list SET timestamp = '2020-01-01 00:00:00'")
    after.conn.commit()
    apply_retention(after, s)

    assert LUCA not in journal.held_addresses(after)


def test_replay_journal_can_be_repeated_and_counts_only_the_missing_links(
    s, db, monkeypatch, capsys
):
    _contact(db)

    first = _cli(s, monkeypatch, capsys, "replay-journal")
    second = _cli(s, monkeypatch, capsys, "replay-journal")

    assert "links recorded again: 1" in first
    assert "links recorded again: 0" in second


def test_restore_check_rehearses_it_and_leaves_the_live_database_alone(
    s, db, monkeypatch, capsys
):
    _contact(db)
    backup.make_backup(db)
    token = UnsubscribeHandler(db, SECRET).token_for(ANNA)

    out = _cli(s, monkeypatch, capsys, "restore-check")

    assert "passed" in out.lower()
    assert "1 link(s)" in out
    assert db.address_for_unsubscribe_token(token) is None


# ── A click stays one lookup, and the clicks kept are capped ──


def test_a_stray_click_costs_one_lookup_whatever_is_held(db, monkeypatch):
    """Step G20: matching a click against every address held made each
    unauthenticated request cost one HMAC per address. Nor may it read every
    click set aside."""
    for i in range(50):
        _contact(db, f"buyer{i}@brand{i}.de")
    handler = UnsubscribeHandler(db, SECRET)
    for i in range(20):
        handler.process_unsubscribe(_stray(i))
    assert len(optout_spool.unmatched_tokens(db.db_path)) == 20
    late = _stray(99)

    hmacs, reads = [], []
    token_for = UnsubscribeHandler.token_for
    read = optout_spool._read
    monkeypatch.setattr(
        UnsubscribeHandler,
        "token_for",
        lambda self, email: hmacs.append(email) or token_for(self, email),
    )
    monkeypatch.setattr(optout_spool, "_read", lambda p: reads.append(p) or read(p))

    handler.process_unsubscribe(late)

    assert optout_spool.is_set_aside(db.db_path, late)
    assert hmacs == [], "a click made an HMAC of an address"
    assert reads == [], "a click read the clicks set aside"


def test_beyond_the_cap_a_new_click_is_refused_and_none_is_evicted(db, monkeypatch):
    _cap(monkeypatch, 3)
    handler = UnsubscribeHandler(db, SECRET)
    kept = {_stray(i) for i in range(3)}
    for token in sorted(kept):
        handler.process_unsubscribe(token)
    assert optout_spool.unmatched_tokens(db.db_path) == kept

    ok, message = handler.process_unsubscribe(_stray(3))

    assert ok and message == UnsubscribeHandler.NOT_KEPT
    assert optout_spool.unmatched_tokens(db.db_path) == kept


def test_opt_outs_held_while_busy_do_not_use_up_the_cap(db, monkeypatch):
    _cap(monkeypatch, 1)
    optout_spool.spool_optout(db.db_path, "someone@brand.de", "link")

    UnsubscribeHandler(db, SECRET).process_unsubscribe(_stray(1))

    assert optout_spool.is_set_aside(db.db_path, _stray(1))


def test_beyond_the_cap_a_click_on_a_kept_link_still_counts(db, monkeypatch):
    _cap(monkeypatch, 1)
    handler = UnsubscribeHandler(db, SECRET)
    handler.process_unsubscribe(_stray(1))
    assert optout_spool.unmatched_tokens(db.db_path) == {_stray(1)}

    ok, message = handler.process_unsubscribe(_stray(1), one_click=True)

    assert ok and message == UnsubscribeHandler.NO_MATCH
    assert optout_spool.unmatched_tokens(db.db_path) == {_stray(1)}
    assert '"company": true' in _set_aside_text(db, _stray(1))


def test_a_plain_click_after_a_one_click_keeps_the_company_stop(db):
    """A mail filter opening the link after the recipient pressed the button
    must not undo it."""
    handler = UnsubscribeHandler(db, SECRET)
    handler.process_unsubscribe(_stray(1), one_click=True)
    assert optout_spool.is_set_aside(db.db_path, _stray(1))
    assert '"company": true' in _set_aside_text(db, _stray(1))

    handler.process_unsubscribe(_stray(1))

    assert '"company": true' in _set_aside_text(db, _stray(1))


def test_a_plain_click_held_while_busy_keeps_the_company_stop(db):
    handler = UnsubscribeHandler(db, SECRET)
    handler.process_unsubscribe(_stray(1), one_click=True)
    optout_spool.spool_optout(db.db_path, None, "link", token=_stray(1))

    optout_spool.replay_optout_spool(db, resolve=handler.verify_token)

    assert '"company": true' in _set_aside_text(db, _stray(1))
    assert optout_spool.pending_tokens(db.db_path) == set()


def test_a_one_click_held_while_busy_adds_the_company_stop(db):
    handler = UnsubscribeHandler(db, SECRET)
    handler.process_unsubscribe(_stray(1))
    optout_spool.spool_optout(
        db.db_path, None, "one-click", company=True, token=_stray(1)
    )

    optout_spool.replay_optout_spool(db, resolve=handler.verify_token)

    assert '"company": true' in _set_aside_text(db, _stray(1))


def test_a_click_held_while_busy_is_set_aside_even_beyond_the_cap(db, monkeypatch):
    """That click was told "Your request is recorded". The cap is on made-up
    clicks arriving at the route; it never drops one already promised."""
    _cap(monkeypatch, 1)
    handler = UnsubscribeHandler(db, SECRET)
    handler.process_unsubscribe(_stray(1))
    optout_spool.spool_optout(db.db_path, None, "link", token=_stray(2))

    optout_spool.replay_optout_spool(db, resolve=handler.verify_token)

    assert optout_spool.unmatched_tokens(db.db_path) == {_stray(1), _stray(2)}


def test_beyond_the_cap_the_page_promises_nothing_and_a_provider_still_gets_200(
    s, db, monkeypatch
):
    _cap(monkeypatch, 1)
    client = main.create_dashboard_app(s).test_client()
    client.get(f"/unsubscribe/{_stray(1)}")
    assert optout_spool.is_set_aside(db.db_path, _stray(1))

    body = client.get(f"/unsubscribe/{_stray(2)}").get_data(as_text=True)

    assert not optout_spool.is_set_aside(db.db_path, _stray(2))
    assert "nothing was changed" in body
    assert "kept your request" not in body
    assert "will not email you again" not in body
    assert client.post(f"/unsubscribe/{_stray(3)}").status_code == 200
    assert not optout_spool.is_set_aside(db.db_path, _stray(3))


def test_a_click_that_cannot_be_written_promises_nothing(s, db, monkeypatch):
    def disk_full(*a, **k):
        raise OSError("No space left on device")

    monkeypatch.setattr(optout_spool, "set_aside_click", disk_full, raising=False)
    client = main.create_dashboard_app(s).test_client()

    response = client.get(f"/unsubscribe/{_stray(1)}")

    assert response.status_code == 500
    assert "kept your request" not in response.get_data(as_text=True)
    assert client.post(f"/unsubscribe/{_stray(1)}").status_code == 200


def test_doctor_reports_the_cap_reached(s, db, monkeypatch):
    def held_opt_outs():
        checks = main.run_doctor_checks(s, network=False)
        return next(c for c in checks if c.label == "Held opt-outs")

    _cap(monkeypatch, 2)
    handler = UnsubscribeHandler(db, SECRET)
    handler.process_unsubscribe(_stray(1))
    assert held_opt_outs().ok is True

    handler.process_unsubscribe(_stray(2))

    check = held_opt_outs()
    assert check.ok is False and "refused" in check.detail
