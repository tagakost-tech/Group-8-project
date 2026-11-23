"""An emailed request is registered once, and a scan never undoes an erasure.

Every inbox scan reads the last day of mail again: the folder is opened
read-only, the window never shrinks below 24 hours and IMAP's SINCE counts
whole days, so a message is read on every scan for the rest of its day and
all of the next. The opt-outs, replies, declines and lead alerts a scan finds
change nothing the second time; a request was registered again on every
pass. With the default 30-minute interval one emailed request became dozens
of open entries, each with its own deadline and an activity row naming the
person, all listed as open or overdue by `dsr list` and doctor. After `dsr
erase`, the next scan registered her request again with her address in
clear, in the register and the activity log, and nothing tokenised it again
(the review of the GDPR work, PRIV-02).

Now a request is registered with the message it came in, by its Message-ID
(by a digest of the message when it has none), through the spool and its
replay too, and the register takes each message once. A new request from
someone erased is registered under her token.
"""

import sqlite3

import pytest

import main
from modules import optout_spool
from modules.database import Database, erasure_token
from modules.export import DataExporter
from modules.imap_monitor import ImapMonitor
from modules.retention import apply_retention
from tests.test_imap_inbox_scan import FakeIMAP, _raw

ANNA = "anna@brand.it"
MESSAGE_ID = "<CAH7wQ2p9Xk@mail.brand.it>"
LATER_ID = "<CAH7wQ2pZr4@mail.brand.it>"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "r.db"))


@pytest.fixture
def db(s):
    """Anna, whom the agent emailed."""
    db = Database(s.get_abs_db_path())
    cid = db.add_contact(
        db.log_search("q"), ANNA, name="Anna Rossi", organization="Brand"
    )
    db.log_email(cid, ANNA, "Hello", delivery_status="sent")
    return db


def _reply(body="Cancellatemi dal vostro database.", message_id=MESSAGE_ID):
    """Her reply as the mail server holds it."""
    headers = f"Message-ID: {message_id}\r\n" if message_id else ""
    return _raw(ANNA, "Re: hello", body, extra_headers=headers)


def _inbox(db, *messages):
    """The inbox monitor on a fake server that serves every message on every
    scan, as the real one does within the window. Returns it and the fake."""
    monitor = ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw")
    fake = FakeIMAP(list(messages))
    monitor._connect = lambda: fake
    return monitor, fake


def _rows(db, sql):
    raw = sqlite3.connect(db.db_path)
    try:
        return raw.execute(sql).fetchall()
    finally:
        raw.close()


def _received(db):
    """The DSR_RECEIVED activity rows, oldest first."""
    return [
        details
        for (details,) in _rows(
            db,
            "SELECT details FROM activity_log WHERE action = 'DSR_RECEIVED' "
            "ORDER BY id",
        )
    ]


def _naming(db, text):
    """The register and activity rows that hold `text` in clear."""
    rows = _rows(db, "SELECT * FROM dsr_requests") + _rows(
        db, "SELECT action, details FROM activity_log"
    )
    return [r for r in rows if any(text in str(v) for v in r)]


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _locked(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


# ── One message, one request ──


def test_a_request_read_again_on_every_scan_is_registered_once(db):
    monitor, _ = _inbox(db, _reply())
    for _ in range(3):  # three 30-minute scans of the same day's mail
        monitor.run_check()

    assert [(r["email"], r["kind"], r["status"]) for r in db.list_dsrs()] == [
        (ANNA, "erasure", "open")
    ]
    assert len(_received(db)) == 1


def test_a_message_without_a_message_id_is_registered_once_too(db):
    """Mail systems add one, but nothing guarantees it: without one, the
    message is told apart by a digest of what the server serves, which is
    the same on every read."""
    monitor, _ = _inbox(db, _reply(message_id=None))
    monitor.run_check()
    monitor.run_check()

    assert len(db.list_dsrs()) == 1
    assert len(_received(db)) == 1


@pytest.mark.parametrize(
    "first,second",
    [(MESSAGE_ID, LATER_ID), (None, None)],
    ids=["two Message-IDs", "no Message-ID"],
)
def test_two_messages_are_two_requests(db, first, second):
    monitor, fake = _inbox(db, _reply(message_id=first))
    monitor.run_check()
    fake.messages.append(_reply("Where did you get my email address?", second))

    monitor.run_check()

    assert sorted(r["kind"] for r in db.list_dsrs()) == ["access", "erasure"]
    assert len(_received(db)) == 2


def test_the_message_is_kept_only_as_its_token(db):
    """A Message-ID can name the sender's mail server (mail.brand.it), and
    the register needs only to recognise the message."""
    monitor, _ = _inbox(db, _reply())
    monitor.run_check()

    (row,) = db.list_dsrs()
    assert row["source_ref"]
    assert "CAH7wQ2p9Xk" not in str(row) and "mail.brand.it" not in str(row)


# ── A scan after the erasure ──


def test_a_scan_after_her_erasure_writes_nothing_back(s, db, monkeypatch, capsys):
    monitor, _ = _inbox(db, _reply())
    monitor.run_check()
    (request,) = db.list_dsrs()
    _cli(s, monkeypatch, capsys, "dsr", "erase", str(request["id"]))

    monitor.run_check()  # 30 minutes later, the same window

    assert _naming(db, ANNA) == []
    assert [(r["email"], r["status"]) for r in db.list_dsrs()] == [
        (erasure_token(ANNA), "closed")
    ]
    assert len(_received(db)) == 1
    assert optout_spool.pending_addresses(db.db_path) == set()
    assert _naming(db, "brand.it") == []


def test_after_dsr_erase_no_copy_of_her_request_is_left_open(
    s, db, monkeypatch, capsys
):
    """The copies stayed open under her token, and invited a second erasure
    of someone already erased (PRIV-M1)."""
    monitor, _ = _inbox(db, _reply("Please delete my personal data."))
    monitor.run_check()
    monitor.run_check()  # 30 minutes later
    first = min(r["id"] for r in db.list_dsrs())
    _cli(s, monkeypatch, capsys, "dsr", "erase", str(first))

    assert _cli(s, monkeypatch, capsys, "dsr", "list").strip() == "No open requests."


def test_after_the_nightly_run_a_scan_registers_nothing_again(s, db):
    """Retention keeps her request as her token and her objection in clear
    on the do-not-contact list, so she is not erased; the message is still
    recognised."""
    monitor, _ = _inbox(db, _reply())
    monitor.run_check()
    apply_retention(db, s)  # her objection ends the purpose at once (L1.16)

    monitor.run_check()

    assert [r["email"] for r in db.list_dsrs()] == [erasure_token(ANNA)]
    assert len(_received(db)) == 1


def test_a_new_request_from_someone_erased_is_registered_under_her_token(
    s, db, monkeypatch, capsys
):
    """A new message still needs its deadline; her address must not come back."""
    monitor, fake = _inbox(db, _reply())
    monitor.run_check()
    (first,) = db.list_dsrs()
    _cli(s, monkeypatch, capsys, "dsr", "erase", str(first["id"]))
    fake.messages.append(_reply("Where did you get my email address?", LATER_ID))

    monitor.run_check()

    assert [(r["email"], r["kind"]) for r in db.list_dsrs(open_only=True)] == [
        (erasure_token(ANNA), "access")
    ]
    assert _naming(db, ANNA) == []
    assert _received(db)[-1].startswith(f"access request from {erasure_token(ANNA)}")


# ── Held while the database was busy ──


@pytest.mark.parametrize(
    "replayed_first", [True, False], ids=["replay, then scan", "scan, then replay"]
)
def test_a_request_held_while_the_database_was_busy_is_registered_once(
    db, monkeypatch, replayed_first
):
    monitor, _ = _inbox(db, _reply())
    with monkeypatch.context() as m:
        m.setattr(db, "record_opt_out", _locked)
        monitor.run_check()
    assert db.list_dsrs() == [], "setup: held on disk"

    if replayed_first:
        optout_spool.replay_optout_spool(db)
        monitor.run_check()
    else:
        monitor.run_check()
        optout_spool.replay_optout_spool(db)

    assert [(r["email"], r["kind"]) for r in db.list_dsrs()] == [(ANNA, "erasure")]
    assert len(_received(db)) == 1
    assert optout_spool.pending_addresses(db.db_path) == set()


def test_a_request_held_for_someone_erased_is_registered_under_her_token(
    db, monkeypatch
):
    DataExporter(db).gdpr_erase(ANNA)
    monitor, _ = _inbox(db, _reply("Where did you get my email address?"))
    with monkeypatch.context() as m:
        m.setattr(db, "record_opt_out", _locked)
        monitor.run_check()

    optout_spool.replay_optout_spool(db)

    assert [(r["email"], r["kind"], r["status"]) for r in db.list_dsrs()] == [
        (erasure_token(ANNA), "access", "open")
    ]
    assert _naming(db, ANNA) == []
    assert optout_spool.pending_addresses(db.db_path) == set()


def test_a_request_added_by_hand_for_someone_erased_is_kept_as_her_token(
    s, db, monkeypatch, capsys
):
    """The register is written in one place, so a letter from her is kept
    as her token too."""
    DataExporter(db).gdpr_erase(ANNA)

    _cli(s, monkeypatch, capsys, "dsr", "add", ANNA, "--kind", "access")

    assert [r["email"] for r in db.list_dsrs()] == [erasure_token(ANNA)]
