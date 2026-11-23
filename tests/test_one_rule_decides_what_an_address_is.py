"""One rule says what an email address is, and every path follows it.

Direct Send took anything with an "@" and a dot after it, and add_contact
anything with an "@". Erasure, the access export, the CLI and the restore
check took only [a-zA-Z0-9._%+-] before the "@". Between the two:

- d'amico@brand.it and o'brien@, which RFC 5322 allows and an operator types
  from a trade-fair card, were emailed and stored, and then could be neither
  erased, exported nor aged out. gdpr_erase refused the address, the access
  answer said nothing was held, `dsr erase` crashed on a request she emailed,
  and retention counted an erasure it never did, every night.
- 'anna@brand.es, bruno@brand.de' went to both, although Anna had opted out
  and Spain was not enabled: the opt-out and country checks read the whole
  string. 'Anna <anna@brand.es>' reached her as well.

is_address is now that rule: exactly one address, with an RFC 5322 dot-atom
before the "@". ASCII only, since a local part in another script needs
SMTPUTF8, which nothing confirms. Direct Send, add_contact and the send
chokepoint refuse anything else.

The inbox refuses nothing, because a refused opt-out fails open: it records
what it was sent and asks a person to look. So erasure and export reach every
address the database can hold, jürgen.müller@ included, and `dsr` says so when
a request is filed under something else. Retention counts only what it erased.
"""

import json
import logging
import sqlite3

import pytest
from smtp_sink import SmtpSink

import main
from agent import OutreachAgent
from modules import optout_spool
from modules.database import Database, erasure_token, is_address
from modules.email_sender import EmailSender
from modules.export import (
    DataExporter,
    export_folder,
    exports_dir,
    records_held_about,
    write_access_answer,
)
from modules.journal import held_addresses
from modules.retention import apply_retention
from tests.test_imap_inbox_scan import _monitor, _raw
from tests.test_send_path_against_real_smtp import trust_anything  # noqa: F401

APOS = "d'amico@brand.it"
ANNA = "anna@brand.es"
LIST = "anna@brand.es, bruno@brand.de"
NAMED = "Anna <anna@brand.es>"
UMLAUT = "jürgen.müller@brand.de"
LONG_AGO = "2000-01-01 00:00:00"


@pytest.fixture
def wire(monkeypatch):
    """Every message handed to SMTP. Nothing leaves the machine."""
    sent = []

    def _deliver(self, msg, blocked=None):
        sent.append(msg)
        return True, "250 2.0.0 Ok", False, False

    monkeypatch.setattr(EmailSender, "_deliver", _deliver)
    return sent


def _settings(settings_factory, tmp_path, **kw):
    return settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        attach_profile=False,
        **kw,
    )


def _settings_for(sink, settings_factory, tmp_path, **kw):
    return _settings(
        settings_factory,
        tmp_path,
        smtp_host="127.0.0.1",
        smtp_port=sink.port,
        smtp_username="agent@suppleco.test",
        smtp_password="not-a-real-password",
        **kw,
    )


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _stored_before_the_rule(db, text, **columns):
    """A contact row holding what is not one address, as the old Direct Send
    stored one."""
    row = {"search_id": db.log_search("q"), "email": text, **columns}
    cid = db.conn.execute(
        f"INSERT INTO contacts ({', '.join(row)}) "
        f"VALUES ({', '.join('?' for _ in row)})",
        tuple(row.values()),
    ).lastrowid
    db.conn.commit()
    return cid


def _outlived(db, text, count):
    """A contact row past its period, in the group apply_retention reports as
    ``count``."""
    status = {
        "erased_never_contacted": "pending",
        "erased_no_reply": "emailed",
        "erased_engaged": "responded",
        "erased_objection": "unsubscribed",
    }[count]
    cid = _stored_before_the_rule(
        db, text, name="Anna", status=status, timestamp=LONG_AGO
    )
    if count == "erased_no_reply":
        db.log_email(cid, text, "Hello", delivery_status="sent")
        db.conn.execute("UPDATE email_log SET timestamp = ?", (LONG_AGO,))
        db.conn.commit()
    return cid


def _review_rows(db):
    return [r for r in db.get_recent_activity(50) if r["action"] == "SENDER_UNPARSED"]


# ── The rule ──


@pytest.mark.parametrize(
    "address",
    [
        APOS,
        "o'brien@brand.ie",
        "first.dell'orto@brand.it",
        "a&b@brand.it",
        "a=b@brand.it",
        "a#b@brand.it",
        "a!b@brand.it",
        "a/b@brand.it",
        "a$b@brand.it",
        "a*b@brand.it",
        "a?b@brand.it",
        "a^b@brand.it",
        "a`b@brand.it",
        "a{b}@brand.it",
        "a|b@brand.it",
        "a~b@brand.it",
    ],
)
def test_every_rfc_5322_dot_atom_before_the_at_is_an_address(address):
    assert is_address(address)
    assert main.build_parser().parse_args(["gdpr-erase", address]).email == address


@pytest.mark.parametrize(
    "text",
    [
        LIST,
        "anna@brand.es,bruno@brand.de",
        "anna@brand.es;bruno@brand.de",
        NAMED,
        "<anna@brand.es>",
        "anna @brand.es",
        '"anna"@brand.es',
        "a..b@brand.es",
        ".anna@brand.es",
        "anna.@brand.es",
        UMLAUT,
        "anna@brand",
        "",
        "@",
    ],
)
def test_a_list_a_display_name_or_a_broken_dot_atom_is_not_one_address(text):
    assert not is_address(text)


# ── Nothing is stored or emailed that is not one address ──


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("typed", [LIST, NAMED, UMLAUT])
def test_direct_send_refuses_anything_but_one_address_and_emails_nobody(
    settings_factory, tmp_path, wire, typed, dry_run
):
    """Anna opted out and Spain is not enabled, yet both forms reached her."""
    agent = OutreachAgent(_settings(settings_factory, tmp_path, ENABLED_COUNTRIES="DE"))
    agent.db.record_opt_out(ANNA, "Opted out via the unsubscribe link")

    ok, message = agent.send_single_email(
        typed, organization="Brand", channel="referral", dry_run=dry_run
    )

    assert (ok, wire) == (False, [])
    assert "valid email address" in message
    assert agent.db.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0


@pytest.mark.usefixtures("trust_anything")
def test_the_dashboard_answers_400_for_a_list_and_no_server_hears_of_it(
    settings_factory, tmp_path
):
    """Against a real SMTP conversation: both addresses got a RCPT TO."""
    with SmtpSink() as sink:
        s = _settings_for(sink, settings_factory, tmp_path, ENABLED_COUNTRIES="DE")
        Database(s.get_abs_db_path()).record_opt_out(ANNA, "Opted out by email")
        client = main.create_dashboard_app(s).test_client()

        r = client.post(
            "/api/send-manual",
            json={"email": LIST, "organization": "Brand", "channel": "referral"},
        )

    assert r.status_code == 400, r.get_json()
    assert r.get_json()["ok"] is False
    assert sink.received == []


@pytest.mark.usefixtures("trust_anything")
def test_the_dashboard_emails_one_apostrophe_address_and_only_it(
    settings_factory, tmp_path
):
    with SmtpSink() as sink:
        s = _settings_for(sink, settings_factory, tmp_path)
        client = main.create_dashboard_app(s).test_client()

        r = client.post(
            "/api/send-manual",
            json={"email": APOS, "organization": "Brand", "channel": "trade_fair"},
        )

    assert r.status_code == 200, r.get_json()
    assert [m["recipients"] for m in sink.received] == [[APOS]]


@pytest.mark.parametrize("text", [LIST, NAMED, UMLAUT, "a@brand", "a..b@brand.de"])
def test_add_contact_refuses_what_is_not_one_address(db, text):
    assert db.add_contact(db.log_search("q"), text) is None
    assert db.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0


@pytest.mark.parametrize("stored", [LIST, NAMED, UMLAUT])
def test_a_contact_stored_under_anything_but_one_address_is_never_emailed(
    settings_factory, tmp_path, wire, stored
):
    """The campaign, follow-ups and retries all pass the send chokepoint."""
    agent = OutreachAgent(_settings(settings_factory, tmp_path))
    agent.db.record_opt_out(ANNA, "Opted out via the unsubscribe link")
    cid = _stored_before_the_rule(
        agent.db, stored, organization="Brand", basis="legitimate_interest"
    )

    sent = agent._process_contact(agent.db.get_contact_by_id(cid), dry_run=False)

    assert (sent, wire) == (False, [])
    assert agent.db.get_contact_by_id(cid)["status"] == "irrelevant"


# ── An apostrophe address works everywhere ──


def test_add_contact_stores_an_apostrophe_address(db):
    cid = db.add_contact(db.log_search("q"), "  D'Amico@Brand.IT ")
    assert db.get_contact_by_id(cid)["email"] == APOS


def test_an_apostrophe_address_sent_by_direct_send_can_be_erased(
    settings_factory, tmp_path, wire
):
    agent = OutreachAgent(_settings(settings_factory, tmp_path))
    ok, _ = agent.send_single_email(
        APOS, name="Marco D'Amico", organization="Brand", channel="trade_fair"
    )
    assert ok and [m["To"] for m in wire] == [APOS]
    cid = agent.db.get_contact_by_email(APOS)["id"]

    DataExporter(agent.db).gdpr_erase(APOS)

    row = agent.db.get_contact_by_id(cid)
    assert (row["email"], row["name"]) == (erasure_token(APOS), None)
    assert agent.db.is_suppressed(APOS)
    assert APOS not in held_addresses(agent.db)


def test_the_access_answer_lists_what_is_held_about_an_apostrophe_address(db):
    db.add_contact(db.log_search("q"), APOS, name="Marco D'Amico")

    folder = write_access_answer(
        db, APOS, controller="SUPPLECO", notice_url="https://example.test/privacy"
    )

    held = json.loads((folder / "records.json").read_text(encoding="utf-8"))
    assert [r["email"] for r in held.get("contacts", [])] == [APOS]


@pytest.mark.parametrize(
    "argv",
    [
        ["gdpr-erase", "D'Amico@brand.it"],
        ["gdpr-export", APOS],
        ["dsr", "add", APOS, "--kind", "erasure"],
    ],
)
def test_the_cli_takes_an_apostrophe_address(argv):
    assert main.build_parser().parse_args(argv).email == APOS


def test_the_restore_check_sees_an_apostrophe_address(db):
    db.add_contact(db.log_search("q"), APOS)
    assert APOS in held_addresses(db)


def test_a_request_emailed_from_an_apostrophe_address_can_be_erased(
    settings_factory, tmp_path, monkeypatch, capsys
):
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    monitor, _ = _monitor(
        tmp_path, [_raw(APOS, "Re: hello", "Vi chiedo di cancellare i miei dati.")]
    )
    monitor.db = db = Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), APOS, name="Marco D'Amico")
    db.log_email(cid, APOS, "Hello", delivery_status="sent")
    monitor.run_check()
    (request,) = db.list_dsrs()
    monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")

    _cli(s, monkeypatch, capsys, "dsr", "erase", str(request["id"]))

    assert db.get_dsr(request["id"])["status"] == "closed"
    row = db.get_contact_by_id(cid)
    assert (row["email"], row["name"]) == (erasure_token(APOS), None)


def test_an_opt_out_from_an_apostrophe_address_is_filed_under_it(tmp_path):
    monitor, _ = _monitor(
        tmp_path,
        [_raw("Marco D'Amico <d'amico@brand.it>", "Re: hello", "Please remove me.")],
    )

    monitor.run_check()

    assert monitor.db.is_suppressed(APOS)
    assert _review_rows(monitor.db) == []


# ── The inbox refuses nothing, and asks a person to look ──


@pytest.mark.parametrize("sender", [UMLAUT, "anna@brand.it (anna rossi)", "vendite"])
def test_an_opt_out_is_recorded_whatever_the_sender_looks_like(tmp_path, sender):
    monitor, _ = _monitor(tmp_path, [])

    monitor.process_unsubscribes([{"email": sender}])
    monitor.process_unsubscribes([{"email": sender}])

    assert monitor.db.is_suppressed(sender)
    (row,) = _review_rows(monitor.db)
    assert row["status"] == "error"
    assert sender in row["details"] and "review manually" in row["details"]


@pytest.mark.parametrize("sender", [UMLAUT, "anna@brand.it (anna rossi)", "vendite"])
def test_a_request_is_recorded_whatever_the_sender_looks_like(tmp_path, sender):
    monitor, _ = _monitor(tmp_path, [])

    monitor.process_requests([{"email": sender, "request_kind": "erasure"}])

    assert monitor.db.is_suppressed(sender)
    assert [r["email"] for r in monitor.db.list_dsrs()] == [sender]
    (row,) = _review_rows(monitor.db)
    assert row["status"] == "error"
    assert sender in row["details"] and "review manually" in row["details"]


@pytest.mark.parametrize("held", ["unsubscribe", "request"])
@pytest.mark.parametrize("sender", ["anna@brand.it (anna rossi)", "vendite"])
def test_what_waited_for_a_locked_database_is_reviewed_as_well(
    tmp_path, monkeypatch, sender, held
):
    """Held in the spool and applied later, it was filed with no review row."""
    monitor, _ = _monitor(tmp_path, [])
    with monkeypatch.context() as m:

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        m.setattr(monitor.db, "record_opt_out", locked)
        if held == "unsubscribe":
            monitor.process_unsubscribes([{"email": sender}])
        else:
            monitor.process_requests([{"email": sender, "request_kind": "erasure"}])
    assert optout_spool.pending_addresses(monitor.db.db_path) == {sender}
    assert _review_rows(monitor.db) == []

    assert optout_spool.replay_optout_spool(monitor.db) == 1

    assert monitor.db.is_suppressed(sender)
    (row,) = _review_rows(monitor.db)
    assert row["status"] == "error"
    assert sender in row["details"] and "review manually" in row["details"]


def test_one_address_held_for_a_locked_database_needs_no_review(tmp_path, monkeypatch):
    monitor, _ = _monitor(tmp_path, [])
    with monkeypatch.context() as m:

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        m.setattr(monitor.db, "record_opt_out", locked)
        monitor.process_requests([{"email": APOS, "request_kind": "access"}])

    assert optout_spool.replay_optout_spool(monitor.db) == 1

    assert [r["email"] for r in monitor.db.list_dsrs()] == [APOS]
    assert _review_rows(monitor.db) == []


def test_one_address_needs_no_review(tmp_path):
    monitor, _ = _monitor(tmp_path, [])

    monitor.process_unsubscribes([{"email": APOS}])
    monitor.process_requests([{"email": "o'brien@brand.ie", "request_kind": "access"}])

    assert _review_rows(monitor.db) == []


# ── Erasure and export reach every address the database can hold ──


@pytest.mark.parametrize(
    "address",
    [
        UMLAUT,
        "anna@münchen.de",
        "anna@firma.xn--90ae",
        "a..b@brand.de",
        ".anna@brand.de",
        "anna.@brand.de",
    ],
)
def test_erasure_and_export_reach_an_address_stored_before_the_rule(db, address):
    cid = _stored_before_the_rule(db, address, name="Jürgen Müller")
    assert [r["email"] for r in records_held_about(db, address)["contacts"]] == [
        address
    ]
    assert address in held_addresses(db)
    assert main.build_parser().parse_args(["gdpr-erase", address]).email == address

    DataExporter(db).gdpr_erase(address)

    row = db.get_contact_by_id(cid)
    assert (row["email"], row["name"]) == (erasure_token(address), None)
    assert db.is_suppressed(address)
    assert address not in held_addresses(db)


def test_a_request_from_a_non_ascii_sender_is_answered_and_erased(
    settings_factory, tmp_path, monkeypatch, capsys
):
    """The access answer said nothing was held and `dsr erase` crashed."""
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    monitor, _ = _monitor(tmp_path, [])
    monitor.db = db = Database(s.get_abs_db_path())
    monitor.process_requests([{"email": UMLAUT, "request_kind": "erasure"}])
    (request,) = db.list_dsrs()

    _cli(s, monkeypatch, capsys, "dsr", "export", str(request["id"]))

    held = json.loads(
        (export_folder(db.db_path, UMLAUT) / "records.json").read_text(encoding="utf-8")
    )
    assert [r["email"] for r in held["dsr_requests"]] == [UMLAUT]
    assert [r["email"] for r in held["suppression_list"]] == [UMLAUT]

    monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")
    _cli(s, monkeypatch, capsys, "dsr", "erase", str(request["id"]))

    row = db.get_dsr(request["id"])
    assert (row["status"], row["email"]) == ("closed", erasure_token(UMLAUT))
    assert db.is_suppressed(UMLAUT)
    assert UMLAUT not in held_addresses(db)
    details = [r["details"] for r in db.get_recent_activity(50)]
    assert not [d for d in details if UMLAUT in (d or "")], details


@pytest.mark.parametrize(
    "text",
    [
        LIST,
        NAMED,
        "vendite",
        "anna@brand.it (anna rossi)",
        '"a b"@brand.de',
        "@brand.de",
        "anna@",
        "brand.de",
        "%@%.it",
    ],
)
def test_erasure_and_export_still_refuse_what_is_not_one_address(db, text):
    """A partial input matches everyone's free text."""
    with pytest.raises(ValueError):
        DataExporter(db).gdpr_erase(text)
    with pytest.raises(ValueError):
        DataExporter(db).gdpr_export(text)
    assert records_held_about(db, text) == {}
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["gdpr-erase", text])


@pytest.mark.parametrize("action", ["erase", "export"])
@pytest.mark.parametrize("filed", ["vendite", "anna@brand.it (anna rossi)", LIST])
def test_a_request_filed_under_anything_else_is_left_to_a_person(
    settings_factory, tmp_path, monkeypatch, capsys, action, filed
):
    """`dsr erase` crashed, and `dsr export` wrote an answer saying nothing was
    held, while the register and the suppression list held the sender."""
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    db = Database(s.get_abs_db_path())
    rid = db.add_dsr(filed, "erasure", "reply")
    monkeypatch.setattr("builtins.input", lambda prompt="": "ERASE")

    out = _cli(s, monkeypatch, capsys, "dsr", action, str(rid))

    assert "not filed under one email address" in out
    row = db.get_dsr(rid)
    assert (row["status"], row["email"]) == ("open", filed)
    assert not exports_dir(db.db_path).exists()


# ── Retention counts only what it erased ──


@pytest.mark.parametrize("address", [APOS, UMLAUT])
def test_retention_erases_an_old_address_of_any_form(
    settings_factory, tmp_path, address
):
    s = settings_factory(db_path=str(tmp_path / "r.db"))
    db = Database(s.get_abs_db_path())
    cid = _outlived(db, address, "erased_never_contacted")

    assert apply_retention(db, s, dry_run=True)["erased_never_contacted"] == 1
    counts = apply_retention(db, s)

    assert counts["erased_never_contacted"] == 1
    row = db.get_contact_by_id(cid)
    assert (row["email"], row["name"], row["status"]) == (
        erasure_token(address),
        None,
        "erased",
    )


@pytest.mark.parametrize("left", [LIST, NAMED, " padded@brand.de "])
@pytest.mark.parametrize(
    "count",
    [
        "erased_never_contacted",
        "erased_no_reply",
        "erased_engaged",
        "erased_objection",
    ],
)
def test_retention_counts_only_what_it_erased_and_logs_each_contact_it_left(
    settings_factory, tmp_path, caplog, count, left
):
    s = settings_factory(db_path=str(tmp_path / "r.db"))
    db = Database(s.get_abs_db_path())
    old = _outlived(db, "old@brand.de", count)
    kept = _outlived(db, left, count)

    assert apply_retention(db, s, dry_run=True)[count] == 1
    assert db.get_contact_by_id(old)["email"] == "old@brand.de"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="modules.retention"):
        counts = apply_retention(db, s)

    assert counts[count] == 1
    assert db.get_contact_by_id(old)["status"] == "erased"
    assert db.get_contact_by_id(kept)["email"] == left
    lines = [r.getMessage() for r in caplog.records if r.name == "modules.retention"]
    named = [line for line in lines if f"contact {kept}:" in line]
    assert len(named) == 1, lines
    assert "@" not in named[0]
