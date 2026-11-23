"""A made-up unsubscribe link is never kept, and a repeat click never moves
the date a kept click expires by.

A click on a link the database has no record of is set aside, so that a link a
restore lost still opts out (PRIV-03). Two holes in that, found by review:
- anyone could post 2000 made-up tokens: each was kept, the cap was reached,
  and a genuine one-click after them was told "nothing was changed";
- a repeat one-click rewrote the entry, so its date and the file's mtime moved
  to now on every repeat and it never expired.

Now a token carries a tag only our key makes. A token the database does not
record is kept only when its tag is ours: a kept click applies only through
token_for(recipient) under the current key, so nothing else could ever have
been applied. The database lookup comes first and is unchanged: a link it
records works whatever its form or the key that made it. A click on an entry
already kept never moves its date or its mtime, on the route or the replay.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid

import pytest

from modules import optout_spool
from modules import unsubscribe as unsubscribe_module
from modules.database import Database
from modules.unsubscribe import OPAQUE_TOKEN, TOKEN_TAG_LABEL, UnsubscribeHandler
from tests.test_a_click_after_a_restore_still_opts_out import (
    ANNA,
    BOB,
    SECRET,
    _agent,
    _contact,
    _emailed_since_the_backup,
    _restore,
)

DAY = 86400


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(
        db_path=str(tmp_path / "live" / "agent.db"), unsubscribe_secret=SECRET
    )


@pytest.fixture
def db(s):
    os.makedirs(os.path.dirname(s.get_abs_db_path()), exist_ok=True)
    return Database(s.get_abs_db_path())


def _made_up():
    return "u" + uuid.uuid4().hex


def _path(db, token):
    return optout_spool.spool_dir(db.db_path) / f"{token}.unmatched"


def _entry(db, token):
    return json.loads(_path(db, token).read_text(encoding="utf-8"))


def _age(db, token, days):
    """Make the kept click look as if it was kept this many days ago."""
    path = _path(db, token)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["at"] = f"first click, {days} days ago"
    path.write_text(json.dumps(entry), encoding="utf-8")
    then = time.time() - days * DAY
    os.utime(path, (then, then))
    return entry["at"], path.stat().st_mtime


def _flip(token, i):
    return token[:i] + ("0" if token[i] != "0" else "1") + token[i + 1 :]


# ── A made-up token is never kept ──


def test_two_thousand_made_up_tokens_keep_nothing_and_a_genuine_click_still_counts(
    s, db, monkeypatch
):
    handler = UnsubscribeHandler(db, SECRET)
    for _ in range(optout_spool.MAX_UNMATCHED_TOKENS):
        handler.process_unsubscribe(_made_up())
    assert optout_spool.unmatched_tokens(db.db_path) == set()

    genuine = handler.token_for(ANNA)
    ok, message = handler.process_unsubscribe(genuine, one_click=True)

    assert ok and message == UnsubscribeHandler.NO_MATCH
    assert optout_spool.is_set_aside(db.db_path, genuine)
    agent = _agent(s, monkeypatch)
    _contact(agent.db, ANNA)
    assert agent._process_contact(agent.db.get_contact_by_email(ANNA)) is False
    assert agent.sent == [], "the genuine click was crowded out by made-up ones"
    assert agent.db.is_suppressed(ANNA)
    assert agent.db.company_blocked(BOB)


def test_a_made_up_token_is_told_nothing_was_changed(db):
    ok, message = UnsubscribeHandler(db, SECRET).process_unsubscribe(_made_up())

    assert ok and message == UnsubscribeHandler.NOT_KEPT


def test_a_made_up_token_is_logged_without_the_token(db, caplog):
    token = _made_up()
    with caplog.at_level("WARNING", logger="modules.unsubscribe"):
        UnsubscribeHandler(db, SECRET).process_unsubscribe(token)

    assert caplog.records, "a refused made-up token left no warning"
    assert all(r.levelname == "WARNING" for r in caplog.records)
    assert token not in caplog.text


def test_the_route_keeps_no_made_up_token(s, db):
    import main

    client = main.create_dashboard_app(s).test_client()
    token = _made_up()

    body = client.get(f"/unsubscribe/{token}").get_data(as_text=True)

    assert "nothing was changed" in body
    assert "kept your request" not in body
    assert client.post(f"/unsubscribe/{token}").status_code == 200
    assert not optout_spool.is_set_aside(db.db_path, token)


def test_a_genuine_token_the_database_does_not_record_is_kept(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)

    ok, message = handler.process_unsubscribe(token)

    assert ok and message == UnsubscribeHandler.NO_MATCH
    assert optout_spool.is_set_aside(db.db_path, token)


def test_a_genuine_link_a_restore_lost_is_still_kept(db):
    token = _emailed_since_the_backup(db)
    restored = _restore(db)

    ok, message = UnsubscribeHandler(restored, SECRET).process_unsubscribe(token)

    assert ok and message == UnsubscribeHandler.NO_MATCH
    assert optout_spool.is_set_aside(restored.db_path, token)


# ── The database lookup comes first and is unchanged ──


def test_a_recorded_link_in_the_earlier_form_still_opts_out(db):
    earlier = (
        "u" + hmac.new(SECRET.encode(), ANNA.encode(), hashlib.sha256).hexdigest()[:32]
    )
    _contact(db, ANNA)
    db.register_unsubscribe_token(earlier, ANNA)
    handler = UnsubscribeHandler(db, SECRET)
    assert not handler.is_our_token(earlier), "setup: the earlier form fails the check"

    ok, message = handler.process_unsubscribe(earlier)

    assert ok and message not in (
        UnsubscribeHandler.NOT_KEPT,
        UnsubscribeHandler.NO_MATCH,
    )
    assert db.is_suppressed(ANNA)


def test_a_recorded_link_made_under_another_secret_still_opts_out(db):
    _contact(db, ANNA)
    old = UnsubscribeHandler(db, "an earlier secret " * 3).generate_token(ANNA)
    handler = UnsubscribeHandler(db, SECRET)
    assert not handler.is_our_token(old), "setup: another key made it"

    ok, message = handler.process_unsubscribe(old, one_click=True)

    assert ok and message not in (
        UnsubscribeHandler.NOT_KEPT,
        UnsubscribeHandler.NO_MATCH,
    )
    assert db.is_suppressed(ANNA)
    assert db.company_blocked(BOB)


@pytest.mark.parametrize("one_click", [True, False])
def test_a_busy_database_still_holds_a_recorded_link_made_under_another_secret(
    db, monkeypatch, one_click
):
    """A click held while the database is busy was told it is recorded, so it
    is never refused: the check that the token is ours must not run there,
    for a one-click or a plain click."""
    _contact(db, ANNA)
    old = UnsubscribeHandler(db, "an earlier secret " * 3).generate_token(ANNA)
    handler = UnsubscribeHandler(db, SECRET)
    assert not handler.is_our_token(old), "setup: another key made it"

    def busy(token):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(handler, "verify_token", busy)
    ok, message = handler.process_unsubscribe(old, one_click=one_click)

    assert ok and message == "Your request is recorded."
    assert old in optout_spool.pending_tokens(db.db_path)

    monkeypatch.undo()
    optout_spool.replay_optout_spool(db, resolve=handler.verify_token)
    assert db.is_suppressed(ANNA)


# ── The token verifies itself ──


def test_the_tag_is_an_hmac_of_the_fixed_label_and_the_head(db):
    key = SECRET.encode()
    head = hmac.new(key, ANNA.encode(), hashlib.sha256).hexdigest()[:16]
    tag = hmac.new(key, (TOKEN_TAG_LABEL + head).encode(), hashlib.sha256).hexdigest()[
        :16
    ]

    assert TOKEN_TAG_LABEL
    assert UnsubscribeHandler(db, SECRET).token_for(ANNA) == "u" + head + tag


def test_the_check_compares_the_tag_with_compare_digest(db, monkeypatch):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)
    forged = _flip(token, 32)
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(
        unsubscribe_module.hmac,
        "compare_digest",
        lambda a, b: calls.append((a, b)) or real(a, b),
    )

    assert handler.is_our_token(token)
    assert not handler.is_our_token(forged)

    assert (token[17:], token[17:]) in calls
    assert any(a == forged[17:] for a, _ in calls)


def test_a_token_is_the_same_shape_and_deterministic(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)

    assert len(token) == 33
    assert OPAQUE_TOKEN.fullmatch(token)
    assert token == handler.token_for(ANNA)
    assert token == handler.token_for("  Anna.Rossi@Brandco.de ")
    assert token != handler.token_for(BOB)
    assert token != UnsubscribeHandler(db, "another secret " * 3).token_for(ANNA)
    assert ANNA not in token


def test_the_check_accepts_our_tokens_and_no_one_elses(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)

    assert handler.is_our_token(token)
    assert not UnsubscribeHandler(db, "another secret " * 3).is_our_token(token)
    assert not handler.is_our_token(_made_up())
    assert not handler.is_our_token("not-a-token")
    assert not handler.is_our_token("")


def test_one_changed_digit_in_either_half_is_refused(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)

    for i in (1, 16, 17, 32):
        assert not handler.is_our_token(_flip(token, i)), f"digit {i} changed"


def test_a_token_with_one_changed_digit_is_not_kept(db):
    handler = UnsubscribeHandler(db, SECRET)
    forged = _flip(handler.token_for(ANNA), 32)

    ok, message = handler.process_unsubscribe(forged)

    assert ok and message == UnsubscribeHandler.NOT_KEPT
    assert not optout_spool.is_set_aside(db.db_path, forged)


# ── A repeat click never moves the date a kept click expires by ──


def test_a_repeat_one_click_leaves_the_date_and_mtime_alone_twice(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)
    handler.process_unsubscribe(token, one_click=True)
    at, mtime = _age(db, token, 300)

    for _ in range(2):
        ok, message = handler.process_unsubscribe(token, one_click=True)
        assert ok and message == UnsubscribeHandler.NO_MATCH
        assert _entry(db, token)["at"] == at
        assert _path(db, token).stat().st_mtime == mtime
        assert _entry(db, token)["company"] is True


@pytest.mark.parametrize("seconds", [0, 60])
def test_a_repeat_one_click_within_the_hour_leaves_the_date_and_mtime_alone(
    db, seconds
):
    """A one-click repeated every few hours must not keep a young entry young,
    however young: a rewrite held back only past some age would pass an entry
    aged a full hour."""
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)
    handler.process_unsubscribe(token, one_click=True)
    at, mtime = _age(db, token, seconds / DAY)

    ok, message = handler.process_unsubscribe(token, one_click=True)

    assert ok and message == UnsubscribeHandler.NO_MATCH
    assert _entry(db, token)["at"] == at
    assert _path(db, token).stat().st_mtime == mtime


def test_a_repeat_plain_click_changes_nothing(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)
    handler.process_unsubscribe(token)
    at, mtime = _age(db, token, 300)

    ok, message = handler.process_unsubscribe(token)

    assert ok and message == UnsubscribeHandler.NO_MATCH
    assert _entry(db, token)["company"] is False
    assert _entry(db, token)["at"] == at
    assert _path(db, token).stat().st_mtime == mtime


def test_a_one_click_after_a_plain_click_adds_the_company_stop_and_keeps_the_date(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)
    handler.process_unsubscribe(token)
    at, mtime = _age(db, token, 300)
    assert _entry(db, token)["company"] is False

    ok, message = handler.process_unsubscribe(token, one_click=True)

    assert ok and message == UnsubscribeHandler.NO_MATCH
    assert _entry(db, token)["company"] is True
    assert _entry(db, token)["at"] == at
    assert _path(db, token).stat().st_mtime == mtime


def test_an_unreadable_kept_click_is_rewritten_keeping_its_mtime(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)
    handler.process_unsubscribe(token)
    path = _path(db, token)
    path.write_text("{not json", encoding="utf-8")
    then = time.time() - 300 * DAY
    os.utime(path, (then, then))

    handler.process_unsubscribe(token, one_click=True)

    assert _entry(db, token)["company"] is True
    assert path.stat().st_mtime == then


def test_a_held_one_click_over_a_kept_click_keeps_its_date(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)
    handler.process_unsubscribe(token)
    at, mtime = _age(db, token, 300)

    for _ in range(2):
        optout_spool.spool_optout(
            db.db_path, None, "one-click", company=True, token=token
        )
        optout_spool.replay_optout_spool(db, resolve=handler.verify_token)

        assert _entry(db, token)["company"] is True
        assert _entry(db, token)["at"] == at
        assert _path(db, token).stat().st_mtime == mtime
        assert optout_spool.pending_tokens(db.db_path) == set()


def test_a_click_kept_over_a_year_ago_and_clicked_again_still_expires(db):
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.token_for(ANNA)
    handler.process_unsubscribe(token)
    _age(db, token, 400)

    handler.process_unsubscribe(token, one_click=True)
    handler.process_unsubscribe(token, one_click=True)
    optout_spool.spool_optout(db.db_path, None, "one-click", company=True, token=token)
    optout_spool.replay_optout_spool(db, resolve=handler.verify_token)

    assert optout_spool.expire_unmatched(db.db_path) == 1
    assert not optout_spool.is_set_aside(db.db_path, token)
