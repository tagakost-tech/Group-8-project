"""One objection, erasure or one-click unsubscribe stops email to the company.

An opt-out stopped only the address it came from. The agent then found a
colleague's address on the same website and emailed them - after the company
had said no. Under art. 130 the company itself is a subscriber, and to the
person who objected a second email to their colleague is the same unwanted
contact (decision L1.6).

What blocks the company: the one-click Unsubscribe button (a deliberate act
by the recipient), an erasure, and an objection reply from someone we
emailed. Not a plain click on the footer link - mail filters open links by
themselves - and never a free-mail or certified-mail (PEC) domain, which is
shared by unrelated people. After an erasure the block is kept only as a
keyed token: a domain like mariorossi.it names a person.
"""

import sqlite3

import pytest

import main
from agent import OutreachAgent
from modules import optout_spool
from modules.company_block import block_company
from modules.database import Database
from modules.export import DataExporter
from modules.imap_monitor import ImapMonitor
from modules.unsubscribe import UnsubscribeHandler

SECRET = "s" * 40
OBJECTOR = "anna.rossi@brand.de"
COLLEAGUE = "info@brand.de"


def _raw_hits(db_path, needle):
    raw = sqlite3.connect(db_path)
    hits = []
    for (table,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [c[1] for c in raw.execute(f"PRAGMA table_info({table})")]
        for row in raw.execute(f"SELECT * FROM {table}"):
            for col, val in zip(cols, row):
                if isinstance(val, str) and needle in val.lower():
                    hits.append(f"{table}.{col}")
    raw.close()
    return hits


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "c.db"), unsubscribe_secret=SECRET)


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


def _with_colleague(db):
    sid = db.log_search("q", source="test")
    db.add_contact(sid, OBJECTOR, name="Anna", organization="Brand")
    db.add_contact(sid, COLLEAGUE, organization="Brand")


def _token(db, email):
    return UnsubscribeHandler(db, SECRET).generate_token(email)


def test_the_one_click_button_stops_the_whole_company(s, db):
    _with_colleague(db)
    client = main.create_dashboard_app(s).test_client()

    assert client.post(f"/unsubscribe/{_token(db, OBJECTOR)}").status_code == 200

    assert db.is_blocked(COLLEAGUE)
    assert [c["email"] for c in db.get_pending_contacts()] == []
    assert db.count_pending_contacts() == 0
    sid = db.log_search("q2", source="test")
    assert db.add_contact(sid, "sales@brand.de") is None


def test_a_click_on_the_footer_link_stops_only_that_address(s, db):
    _with_colleague(db)
    client = main.create_dashboard_app(s).test_client()

    client.get(f"/unsubscribe/{_token(db, OBJECTOR)}")

    assert db.is_blocked(OBJECTOR)
    assert not db.is_blocked(COLLEAGUE)


def test_a_one_click_held_by_a_locked_database_still_stops_the_company(db, monkeypatch):
    _with_colleague(db)
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.generate_token(OBJECTOR)
    with monkeypatch.context() as m:

        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        m.setattr(db, "record_opt_out", boom)
        handler.process_unsubscribe(token, one_click=True)

    optout_spool.replay_optout_spool(db)
    assert db.is_blocked(COLLEAGUE)


def _emailed(db, email):
    contact = db.get_contact_by_email(email)
    db.log_email(contact["id"], email, "Hello", delivery_status="sent")


def test_an_objection_reply_from_someone_we_emailed_stops_the_company(db):
    _with_colleague(db)
    _emailed(db, OBJECTOR)
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db

    monitor.process_unsubscribes([{"email": OBJECTOR}])

    assert db.is_blocked(COLLEAGUE)


def test_an_objection_from_an_address_we_never_emailed_stops_only_it(db):
    _with_colleague(db)
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db

    monitor.process_unsubscribes([{"email": "stranger@brand.de"}])

    assert db.is_blocked("stranger@brand.de")
    assert not db.is_blocked(COLLEAGUE)


@pytest.mark.parametrize(
    "address,neighbour",
    [
        ("mario@gmail.com", "luigi@gmail.com"),
        ("ufficio@pec.brand.it", "altro@pec.brand.it"),
    ],
)
def test_shared_mail_domains_are_never_blocked_whole(db, address, neighbour):
    assert block_company(db, address, "one-click unsubscribe") is False
    assert not db.is_blocked(neighbour)


def test_an_erasure_stops_the_company_without_keeping_its_domain(db):
    sid = db.log_search("q", source="test")
    db.add_contact(sid, "mario@mariorossi.it", name="Mario Rossi")
    block_company(db, "mario@mariorossi.it", "one-click unsubscribe")  # plain first

    DataExporter(db).gdpr_erase("mario@mariorossi.it")

    assert db.is_blocked("info@mariorossi.it")
    assert _raw_hits(db.db_path, "mariorossi") == []


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


def test_the_send_check_refuses_a_colleague(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path)
    _with_colleague(agent.db)
    colleague = next(c for c in agent.db.get_all_contacts() if c["email"] == COLLEAGUE)
    block_company(agent.db, OBJECTOR, "one-click unsubscribe")

    assert agent._contact_is_closed(colleague) == "company blocked"
    assert agent._process_contact(colleague, dry_run=True) is False
    # Refused for the block, not by a later check that would also say no.
    raw = sqlite3.connect(agent.db.db_path)
    logged = raw.execute(
        "SELECT details FROM activity_log WHERE action = 'COMPANY_BLOCKED'"
    ).fetchall()
    raw.close()
    assert logged == [(f"Skipped {COLLEAGUE}: company blocked",)]


def test_sales_can_lift_a_block_with_a_reason(s, db, monkeypatch, capsys):
    block_company(db, OBJECTOR, "one-click unsubscribe")
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)

    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "suppress-domain",
            "lift",
            "brand.de",
            "--reason",
            "they asked us back",
        ],
    )
    main.main()

    assert not db.is_blocked(COLLEAGUE)
    logged = db.conn.execute(
        "SELECT details FROM activity_log WHERE action = 'DOMAIN_UNBLOCKED'"
    ).fetchone()
    assert logged and "they asked us back" in logged[0]


def test_lifting_needs_a_reason(s, monkeypatch):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr("sys.argv", ["main.py", "suppress-domain", "lift", "brand.de"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2


def test_the_list_shows_plain_blocks_and_counts_the_keyed_ones(
    s, db, monkeypatch, capsys
):
    block_company(db, OBJECTOR, "one-click unsubscribe")
    block_company(db, "mario@mariorossi.it", "gdpr_erasure", keyed=True)
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", "suppress-domain", "list"])

    main.main()

    out = capsys.readouterr().out
    assert "brand.de" in out
    assert "mariorossi" not in out
    assert "1 more" in out


def test_an_objection_from_a_contact_never_emailed_stops_only_it(db):
    """Stored but never emailed: we never contacted them, so nothing tells us
    their company was asked."""
    _with_colleague(db)
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db

    monitor.process_unsubscribes([{"email": OBJECTOR}])

    assert db.is_blocked(OBJECTOR)
    assert not db.is_blocked(COLLEAGUE)


def test_lifting_a_block_left_by_an_erasure_does_not_name_the_domain(
    s, db, monkeypatch
):
    block_company(db, "mario@mariorossi.it", "gdpr_erasure", keyed=True)
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "suppress-domain",
            "lift",
            "mariorossi.it",
            "--reason",
            "asked back",
        ],
    )
    main.main()

    assert not db.is_blocked("info@mariorossi.it")
    assert _raw_hits(db.db_path, "mariorossi") == []


def _locked_block(m, db):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    m.setattr(db, "block_domain", boom)


def test_a_one_click_company_block_that_meets_a_locked_database_is_held(
    db, monkeypatch
):
    """The opt-out is written, then the company block hits the lock: the block
    must not be lost (found by the security review of the company block)."""
    _with_colleague(db)
    handler = UnsubscribeHandler(db, SECRET)
    token = handler.generate_token(OBJECTOR)
    with monkeypatch.context() as m:
        _locked_block(m, db)
        ok, _ = handler.process_unsubscribe(token, one_click=True)

    assert ok and db.is_suppressed(OBJECTOR)
    assert not db.is_blocked(COLLEAGUE)
    optout_spool.replay_optout_spool(db)
    assert db.is_blocked(COLLEAGUE)


def test_a_reply_company_block_that_meets_a_locked_database_is_held(db, monkeypatch):
    _with_colleague(db)
    _emailed(db, OBJECTOR)
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    with monkeypatch.context() as m:
        _locked_block(m, db)
        monitor.process_unsubscribes([{"email": OBJECTOR}])

    optout_spool.replay_optout_spool(db)
    assert db.is_blocked(COLLEAGUE)


@pytest.mark.parametrize("who", ["mario@mariorossi.it", "info@mariorossi.it"])
def test_a_later_one_click_never_writes_an_erased_domain_back(db, who):
    """After an erasure the company block is a keyed token. A later one-click
    from an old email - the erased person's, or a colleague's - used to add
    the domain again in clear beside it (found by the final document check)."""
    sid = db.log_search("q", source="test")
    db.add_contact(sid, "mario@mariorossi.it", name="Mario Rossi")
    handler = UnsubscribeHandler(db, SECRET)
    old_link = handler.generate_token(who)
    DataExporter(db).gdpr_erase("mario@mariorossi.it")

    handler.process_unsubscribe(old_link, one_click=True)

    assert db.is_blocked("sales@mariorossi.it")
    domains = [r[0] for r in db.conn.execute("SELECT domain FROM suppressed_domains")]
    assert "mariorossi.it" not in domains
    if who.startswith("mario@"):
        assert _raw_hits(db.db_path, "mariorossi") == []


def test_a_held_one_click_replayed_after_an_erasure_keeps_the_domain_keyed(db):
    sid = db.log_search("q", source="test")
    db.add_contact(sid, "mario@mariorossi.it", name="Mario Rossi")
    optout_spool.spool_optout(
        db.db_path, "mario@mariorossi.it", "one-click unsubscribe", company=True
    )
    DataExporter(db).gdpr_erase("mario@mariorossi.it")

    optout_spool.replay_optout_spool(db)

    assert _raw_hits(db.db_path, "mariorossi") == []
