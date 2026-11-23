"""The unsubscribe link no longer carries the recipient's address.

The token was base64(address) + "." + signature: anyone who saw the link - a
link scanner, a forwarded email, a proxy or tunnel log, browser history -
could read the address back out of it. Now (step G20) the token is an
opaque keyed value, "u" and 32 hex digits of HMAC(UNSUBSCRIBE_SECRET,
address), which cannot be read back. A click is resolved by finding which
address we emailed has that token.

The old form is refused: no email carrying one was ever sent, and the
secret alone could make one for any address. A link whose address the
database no longer holds in clear (erased since) changes nothing and says
so plainly. When the database is too busy to resolve a
click, the token itself is held on disk; the send check refuses any
recipient whose token is held, and the replay resolves and applies it.
"""

import base64
import hashlib
import hmac
import re
import sqlite3

import pytest

import main
from modules import optout_spool
from modules.database import Database
from modules.unsubscribe import UnsubscribeHandler

SECRET = "s" * 40
EMAIL = "anna.rossi@brandco.de"


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "u.db"))


@pytest.fixture
def handler(db):
    return UnsubscribeHandler(db, SECRET)


def _emailed(db, email=EMAIL):
    cid = db.add_contact(db.log_search("q"), email, organization="Brandco")
    db.log_email(cid, email, "Hello", delivery_status="sent")
    return cid


def test_the_token_carries_no_address(handler):
    token = handler.generate_token(EMAIL)
    assert re.fullmatch(r"u[0-9a-f]{32}", token)
    assert EMAIL not in token
    encoded = base64.urlsafe_b64encode(EMAIL.encode()).decode().rstrip("=")
    assert encoded[:12] not in token
    assert handler.generate_token("Anna.Rossi@BrandCo.de") == token
    # Keyed: without the secret, nobody can make or test a token.
    assert (
        UnsubscribeHandler(handler.db, "another-secret" * 3).generate_token(EMAIL)
        != token
    )


def test_a_click_opts_out_the_address_we_emailed(db, handler):
    _emailed(db)
    ok, _ = handler.process_unsubscribe(handler.generate_token(EMAIL))
    assert ok and db.is_suppressed(EMAIL)


def test_a_link_in_the_old_form_is_refused(db, handler):
    _emailed(db)
    sig = hmac.new(SECRET.encode(), EMAIL.encode(), hashlib.sha256).digest()
    legacy = (
        base64.urlsafe_b64encode(EMAIL.encode()).decode()
        + "."
        + base64.urlsafe_b64encode(sig).decode()
    )
    ok, _ = handler.process_unsubscribe(legacy, one_click=True)
    assert ok is False
    assert not db.is_suppressed(EMAIL)
    assert optout_spool.pending_addresses(db.db_path) == set()


def test_the_unsubscribe_page_refuses_a_link_in_the_old_form(settings_factory, tmp_path):
    s = settings_factory(db_path=str(tmp_path / "o.db"), unsubscribe_secret=SECRET)
    db = Database(s.get_abs_db_path())
    _emailed(db)
    sig = hmac.new(SECRET.encode(), EMAIL.encode(), hashlib.sha256).digest()
    legacy = (
        base64.urlsafe_b64encode(EMAIL.encode()).decode()
        + "."
        + base64.urlsafe_b64encode(sig).decode()
    )
    client = main.create_dashboard_app(s).test_client()
    client.post(f"/unsubscribe/{legacy}")
    client.get(f"/unsubscribe/{legacy}")
    assert not db.is_suppressed(EMAIL)


def test_a_token_we_cannot_match_changes_nothing(db, handler):
    _emailed(db)
    ok, message = handler.process_unsubscribe(handler.token_for("nobody@nowhere.test"))
    assert ok and message == UnsubscribeHandler.NO_MATCH
    assert db.conn.execute("SELECT COUNT(*) FROM suppression_list").fetchone()[0] == 0
    ok, _ = handler.process_unsubscribe("not-a-token")
    assert ok is False


def test_a_busy_database_holds_the_token_and_the_send_check_refuses_it(
    settings_factory, tmp_path, monkeypatch
):
    from agent import OutreachAgent

    agent = OutreachAgent(
        settings_factory(db_path=str(tmp_path / "a.db"), unsubscribe_secret=SECRET)
    )
    sent = []
    monkeypatch.setattr(
        agent.email_sender, "dispatch_to_contact", lambda **k: sent.append(1) or 1
    )
    cid = _emailed(agent.db)
    token = agent.unsubscribe_handler.generate_token(EMAIL)

    with monkeypatch.context() as m:

        def busy(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        m.setattr(Database, "address_for_unsubscribe_token", busy)
        m.setattr("modules.dispatch_pipeline.replay_optout_spool", lambda *a, **k: 0)
        ok, _ = agent.unsubscribe_handler.process_unsubscribe(token, one_click=True)
        assert ok
        assert optout_spool.pending_tokens(agent.db.db_path) == {token}
        assert agent._process_contact(agent.db.get_contact_by_id(cid)) is False
        assert sent == [], "a recipient whose opt-out is held is never emailed"

    optout_spool.replay_optout_spool(
        agent.db, resolve=agent.unsubscribe_handler.verify_token
    )
    assert agent.db.is_suppressed(EMAIL)
    assert agent.db.company_blocked("someone@brandco.de"), "one-click stops the company"
    assert optout_spool.pending_tokens(agent.db.db_path) == set()


def test_the_email_carries_the_opaque_link(settings_factory, tmp_path, monkeypatch):
    from agent import OutreachAgent

    agent = OutreachAgent(
        settings_factory(db_path=str(tmp_path / "e.db"), unsubscribe_secret=SECRET)
    )
    sent = []
    monkeypatch.setattr(
        agent.email_sender, "dispatch_to_contact", lambda **k: sent.append(k) or 1
    )
    cid = agent.db.add_contact(agent.db.log_search("q"), EMAIL, organization="Brandco")
    agent._process_contact(agent.db.get_contact_by_id(cid))
    url = sent[0]["unsubscribe_url"]
    assert url.endswith(
        "/unsubscribe/" + agent.unsubscribe_handler.generate_token(EMAIL)
    )
    encoded = base64.urlsafe_b64encode(EMAIL.encode()).decode().rstrip("=")
    assert encoded[:12] not in sent[0]["html_body"]


def test_the_one_click_route_takes_the_opaque_token(settings_factory, tmp_path):
    s = settings_factory(db_path=str(tmp_path / "w.db"), unsubscribe_secret=SECRET)
    db = Database(s.get_abs_db_path())
    _emailed(db)
    token = UnsubscribeHandler(db, SECRET).generate_token(EMAIL)
    client = main.create_dashboard_app(s).test_client()
    assert client.post(f"/unsubscribe/{token}").status_code == 200
    assert db.is_suppressed(EMAIL)


def test_a_replay_that_cannot_resolve_keeps_the_token_held(db, handler):
    _emailed(db)
    token = handler.generate_token(EMAIL)
    optout_spool.spool_optout(db.db_path, None, "link", token=token)
    assert optout_spool.replay_optout_spool(db) == 0  # no resolver: it waits
    assert optout_spool.pending_tokens(db.db_path) == {token}
    assert not db.is_suppressed(EMAIL)


def test_the_dashboard_resolves_held_tokens_when_it_starts(settings_factory, tmp_path):
    s = settings_factory(db_path=str(tmp_path / "d.db"), unsubscribe_secret=SECRET)
    db = Database(s.get_abs_db_path())
    _emailed(db)
    token = UnsubscribeHandler(db, SECRET).generate_token(EMAIL)
    optout_spool.spool_optout(db.db_path, None, "link", token=token)
    main.create_dashboard_app(s)
    assert db.is_suppressed(EMAIL)


def test_a_click_is_one_lookup_not_a_scan(db, handler, monkeypatch):
    """Found by the security review: matching a click against every address
    held made each unauthenticated request cost one HMAC per address."""
    for i in range(50):
        _emailed(db, f"buyer{i}@brand{i}.de")
    calls = []
    real = UnsubscribeHandler.token_for
    monkeypatch.setattr(
        UnsubscribeHandler,
        "token_for",
        lambda self, email: calls.append(email) or real(self, email),
    )
    assert handler.verify_token("u" + "0" * 32) is None
    assert len(calls) <= 1


def test_an_erasure_takes_the_link_with_it(db, handler):
    from modules.export import DataExporter

    _emailed(db)
    token = handler.generate_token(EMAIL)
    DataExporter(db).gdpr_erase(EMAIL)

    ok, message = handler.process_unsubscribe(token)
    assert ok and message == UnsubscribeHandler.NO_MATCH
    held = [r[0] for r in db.conn.execute("SELECT email FROM suppression_list")]
    assert EMAIL not in held, "an old link wrote the erased address back"
    assert db.conn.execute("SELECT COUNT(*) FROM unsubscribe_tokens").fetchone()[0] == 0


def test_retention_takes_the_link_with_it(db, handler, settings_factory):
    from modules.retention import apply_retention

    _emailed(db)
    handler.generate_token(EMAIL)
    db.record_opt_out(EMAIL, "Opted out by email")  # erased at the next run
    apply_retention(db, settings_factory(db_path=db.db_path))
    assert db.conn.execute("SELECT COUNT(*) FROM unsubscribe_tokens").fetchone()[0] == 0


def test_a_link_made_from_a_mixed_case_address_goes_with_the_erasure(db, handler):
    from modules.export import DataExporter

    _emailed(db)
    token = handler.generate_token("Anna.Rossi@BrandCo.de")
    DataExporter(db).gdpr_erase(EMAIL)
    assert handler.verify_token(token) is None


def test_a_preview_makes_the_same_link_but_records_nothing(
    settings_factory, tmp_path, monkeypatch
):
    """A dry run recorded the link, so a previewed address the agent does not
    otherwise hold stayed in unsubscribe_tokens beyond retention's reach."""
    from agent import OutreachAgent

    agent = OutreachAgent(
        settings_factory(db_path=str(tmp_path / "p.db"), unsubscribe_secret=SECRET)
    )
    sent = []
    monkeypatch.setattr(
        agent.email_sender, "dispatch_to_contact", lambda **k: sent.append(k) or 1
    )
    cid = agent.db.add_contact(agent.db.log_search("q"), EMAIL, organization="Brandco")
    agent._process_contact(agent.db.get_contact_by_id(cid), dry_run=True)
    token = agent.unsubscribe_handler.token_for(EMAIL)
    assert sent and sent[0]["unsubscribe_url"].endswith("/unsubscribe/" + token)
    assert db_count(agent.db, "unsubscribe_tokens") == 0


def test_held_addresses_reads_a_database_made_before_the_links_table(tmp_path):
    from modules import journal

    path = tmp_path / "old.db"
    db = Database(str(path))
    _emailed(db)
    db.conn.execute("DROP TABLE unsubscribe_tokens")
    db.conn.commit()
    assert journal.held_addresses_at(path) == {EMAIL}


def db_count(db, table):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
