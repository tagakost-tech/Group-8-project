"""The whole life of a contact, end to end, with the bytes checked at the end.

Each GDPR step has its own tests; this one runs them as one story, through
the real entry points, and then reads the files themselves (step G19):

1. contacts are collected, each with where it came from;
2. a live email goes to two of them, and a backup is taken;
3. one recipient's one-click opt-out meets a locked database, is held on
   disk, and is applied before the next send - so a colleague at the same
   company is refused;
4. another asks in Italian for her data to be deleted: the request is
   registered and her company blocked;
5. her access answer is exported; then she is erased, from the register;
6. the database file, its log, the spool, the exports, the journal and the
   log files are read as bytes: her address and name are nowhere;
7. a contact never emailed within the first-contact limit is erased by
   retention;
8. the backup from step 2 is restored and the journal replayed: she is gone
   again, and the files are clean again.
"""

import logging
import os
import shutil

import pytest

import main
from agent import OutreachAgent
from modules import backup, optout_spool, reply_intent
from modules.database import Database
from modules.export import exports_dir
from modules.imap_monitor import ImapMonitor
from modules.journal import journal_path, replay_journal
from modules.retention import apply_retention
from modules.search import DEFAULT_BLOCKED_PREFIXES, SearchModule
from modules.unsubscribe import UnsubscribeHandler

GIULIA = "giulia.bianchi@bianchi-integratori.it"
GIULIA_NAME = "Giulia Bianchi"
ANNA = "anna@brandco.de"
LUCA = "luca@brandco.de"
OLD = "info@oldco.de"


@pytest.fixture
def logs(tmp_path):
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    main.setup_logging(
        False, name="lifecycle", retention_days=7, log_dir=tmp_path / "logs"
    )
    yield tmp_path / "logs"
    for handler in root.handlers:
        handler.close()
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    logging.getLogger("werkzeug").setLevel(logging.NOTSET)


@pytest.fixture
def s(settings_factory, tmp_path):
    (tmp_path / "data").mkdir()
    return settings_factory(
        db_path=str(tmp_path / "data" / "agent.db"),
        verify_emails=False,
        imap_enabled=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        ai_humanize_emails=False,
    )


def _agent(s, monkeypatch):
    agent = OutreachAgent(s)
    agent.sent = []

    # Logs the send as the real sender does, minus the SMTP conversation.
    def send(**k):
        agent.sent.append(k["contact"]["email"])
        return agent.db.log_email(
            k["contact"]["id"],
            k["contact"]["email"],
            k["subject"],
            body_preview=k["plain_body"][:200],
            delivery_status="sent",
            notice_version=k["notice_version"],
        )

    monkeypatch.setattr(agent.email_sender, "dispatch_to_contact", send)
    return agent


def _collect(db):
    sm = SearchModule.__new__(SearchModule)
    sm.db = db
    sm.rate_limit = 0.0
    sm.max_results = 10
    sm.blocked_prefixes = DEFAULT_BLOCKED_PREFIXES
    sm._robots_cache = {}
    sm._ddg_search = lambda q: ["https://www.bianchi-integratori.it/contatti"]
    sm.scrape_site = lambda url: [
        {
            "email": GIULIA,
            "name": GIULIA_NAME,
            "organization": "Bianchi Integratori",
            "source_url": url,
            "source_channel": "contact_page",
        },
        {
            "email": ANNA,
            "organization": "Brandco",
            "source_url": "https://brandco.de/kontakt",
            "source_channel": "contact_page",
        },
        {
            "email": LUCA,
            "organization": "Brandco",
            "source_url": "https://brandco.de/team",
            "source_channel": "own_website",
        },
    ]
    return {c["email"]: c["id"] for c in sm.search_and_extract("integratori")}


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _files(s, logs):
    """Every file the proof reads, as bytes. The backups are left out: they
    hold her until they expire, and replay-journal erases her on restore."""
    data = os.path.dirname(s.get_abs_db_path())
    found = []
    for folder, _dirs, names in os.walk(data):
        if os.path.basename(folder) == "backups":
            continue
        found += [os.path.join(folder, n) for n in names]
    found += [os.path.join(logs, n) for n in os.listdir(logs)]
    return found


def _scan(s, logs, *needles):
    leaks = []
    for path in _files(s, logs):
        with open(path, "rb") as f:
            raw = f.read()
        leaks += [
            (os.path.basename(path), n) for n in needles if n.encode("utf-8") in raw
        ]
    return leaks


def test_a_contact_lives_and_is_erased_without_a_trace(s, logs, monkeypatch, capsys):
    agent = _agent(s, monkeypatch)
    db = agent.db

    # 1. Collected, each with where it came from.
    ids = _collect(db)
    assert db.get_contact_by_id(ids[GIULIA])["source_channel"] == "contact_page"

    # 2. A live email to two of them; a backup.
    assert agent._process_contact(db.get_contact_by_id(ids[GIULIA])) is True
    assert agent._process_contact(db.get_contact_by_id(ids[ANNA])) is True
    backup.make_backup(db)

    # 3. A one-click opt-out meets a locked database and is held.
    token = UnsubscribeHandler(db, s.unsubscribe_secret).generate_token(ANNA)
    with monkeypatch.context() as m:

        def locked(self, *a, **k):
            import sqlite3

            raise sqlite3.OperationalError("database is locked")

        m.setattr(Database, "record_opt_out", locked)
        response = (
            main.create_dashboard_app(s).test_client().post(f"/unsubscribe/{token}")
        )
        assert response.status_code == 200
    assert optout_spool.pending_addresses(db.db_path) == {ANNA}
    assert agent._process_contact(db.get_contact_by_id(ids[LUCA])) is False
    assert db.is_suppressed(ANNA) and db.company_blocked(LUCA)
    assert LUCA not in agent.sent

    # 4. An erasure request, in Italian.
    subject, body = (
        "Re: Partnership",
        "Vi chiedo di cancellare i miei dati dal vostro database.",
    )
    assert reply_intent.dsr_kind(subject, reply_intent.strip_quoted(body)) == "erasure"
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    monitor.process_requests(
        [{"email": GIULIA, "subject": subject, "body": body, "request_kind": "erasure"}]
    )
    [request] = db.list_dsrs(open_only=True)
    assert db.company_blocked("someone@bianchi-integratori.it")

    # 5. Her access answer, then her erasure - both from the register.
    out = _cli(s, monkeypatch, capsys, "dsr", "export", str(request["id"]))
    assert exports_dir(db.db_path).exists() and GIULIA not in out
    monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")
    _cli(s, monkeypatch, capsys, "dsr", "erase", str(request["id"]))
    assert db.get_dsr(request["id"])["status"] == "closed"

    # 6. The bytes: her address and name are nowhere.
    wal = s.get_abs_db_path() + "-wal"
    assert not os.path.exists(wal) or os.path.getsize(wal) == 0
    assert _scan(s, logs, GIULIA, GIULIA_NAME) == []

    # 7. A contact never emailed within the limit is erased by retention.
    old = db.add_contact(db.log_search("q"), OLD, source_channel="own_website")
    db.conn.execute(
        "UPDATE contacts SET timestamp = datetime('now','localtime','-31 days') "
        "WHERE id = ?",
        (old,),
    )
    db.conn.commit()
    apply_retention(db, s)
    assert db.get_contact_by_id(old)["status"] == "erased"
    assert _scan(s, logs, OLD) == []

    # 8. Restore the backup from step 2, replay: she is gone again. Into a
    #    folder of its own - Windows will not delete a file this process
    #    still holds - with the live journal, as an operator's restore uses.
    live = s.get_abs_db_path()
    newest = max(backup.backups_dir(live).glob("agent-*.db"))
    restored_dir = os.path.join(os.path.dirname(os.path.dirname(live)), "restored")
    os.makedirs(restored_dir)
    restored_path = os.path.join(restored_dir, "agent.db")
    shutil.copyfile(newest, restored_path)
    restored = Database(restored_path)
    assert restored.get_contact_by_email(GIULIA), (
        "setup: the backup predates her erasure"
    )
    replay_journal(restored, journal_path(live))
    assert restored.get_contact_by_email(GIULIA) is None
    assert restored.is_suppressed(GIULIA) and restored.is_suppressed(ANNA)
    restored.close_thread_conn()
    for name in os.listdir(restored_dir):
        with open(os.path.join(restored_dir, name), "rb") as f:
            raw = f.read()
        assert GIULIA.encode() not in raw and GIULIA_NAME.encode() not in raw, name
