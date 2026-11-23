"""An erasure, or any block, that lands while a send is prepared stops it.

process_contact checks the suppression list once, before the email is
personalised, and that takes seconds with AI personalisation. An erasure
that landed in between (`dsr erase`, run from another process) found nothing
sent and cleaned every table; the email then went out anyway, wrote her
address and name back into email_log, consent_log and the activity log, and
flipped her erased contact to 'emailed' (bugs-privacy PRIV-09).

Now dispatch_to_contact checks the address again before it writes anything
about the person, and _deliver again once it holds a working connection, just
before the message goes to the server: waiting for another send to finish, or
reconnecting after the pooled connection idled out, is time an erasure can
land in too.
"""

import sqlite3

import pytest

import modules.dispatch_pipeline as dp
from agent import OutreachAgent
from modules.database import Database
from modules.export import DataExporter

ANNA = "anna@brand.it"
BRUNO = "bruno@brand.it"


class _Server:
    """An SMTP connection that accepts everything and records who it went to."""

    def __init__(self, delivered):
        self.delivered = delivered

    def noop(self):
        return (250, b"OK")

    def send_message(self, msg):
        self.delivered.append(msg["To"])

    def quit(self):
        pass

    def close(self):
        pass


def _agent(settings_factory, tmp_path, monkeypatch):
    a = OutreachAgent(
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
    a.delivered = []
    monkeypatch.setattr(a.email_sender, "_new_connection", lambda: _Server(a.delivered))
    return a


def _stored(a, email=ANNA, name="Anna Rossi"):
    cid = a.db.add_contact(a.db.log_search("q"), email, name=name, organization="Brand")
    return a.db.get_contact_by_id(cid)


def _erase(a, email=ANNA):
    """The operator's `dsr erase`: another process, with its own connection."""
    DataExporter(Database(a.db.db_path)).gdpr_erase(email)


def _block(a, contact):
    """The dashboard's Block button, in the dashboard's process: the address
    only, unlike an erasure or an opt-out, which block the company too."""
    db = Database(a.db.db_path)
    db.block_address(contact["email"])
    db.update_contact_status(
        contact["id"], "blocked", discard_reason="Blocked from the dashboard"
    )


def _while_the_email_is_built(monkeypatch, block):
    real = dp.EmailSender.render_template

    def render(*args, **kwargs):
        block()
        return real(*args, **kwargs)

    monkeypatch.setattr(dp.EmailSender, "render_template", render)


def _while_the_message_is_assembled(monkeypatch, a, block):
    """After the attempt is logged, before delivery: reading a large PDF."""
    real = a.email_sender.create_email

    def create(*args, **kwargs):
        block()
        return real(*args, **kwargs)

    monkeypatch.setattr(a.email_sender, "create_email", create)


def _while_the_server_is_reached(monkeypatch, a, block):
    """Inside _deliver: reconnecting after the pooled connection idled out."""
    sender = a.email_sender

    def connect():
        block()
        return _Server(a.delivered)

    monkeypatch.setattr(sender, "_new_connection", connect)


def _while_another_send_holds_the_connection(monkeypatch, a, block):
    """Inside _deliver: waiting for a send on another thread to finish."""

    class Busy:
        def __enter__(self):
            block()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(a.email_sender, "_send_lock", Busy())


def _naming(db, needles=(ANNA, "Anna", "Rossi", "Brand", "brand.it")):
    """Every column of every table that names her, her company or its domain."""
    raw = sqlite3.connect(db.db_path)
    hits = []
    for (table,) in raw.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [c[1] for c in raw.execute(f"PRAGMA table_info({table})")]
        for row in raw.execute(f"SELECT * FROM {table}").fetchall():
            for col, val in zip(cols, row):
                if isinstance(val, str) and any(n in val for n in needles):
                    hits.append(f"{table}.{col}")
    raw.close()
    return sorted(set(hits))


def _status(a, contact):
    return a.db.conn.execute(
        "SELECT status FROM contacts WHERE id = ?", (contact["id"],)
    ).fetchone()[0]


def _stops(a):
    """The dashboard's record of each stopped send."""
    return [
        r["details"]
        for r in a.db.get_recent_activity(50)
        if r["action"] == "EMAIL_STOPPED"
    ]


def test_an_erasure_while_the_email_is_built_stops_the_send(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    anna = _stored(a)
    _while_the_email_is_built(monkeypatch, lambda: _erase(a))

    assert a._process_contact(anna) is False
    assert a.delivered == []
    assert _naming(a.db) == []
    assert _status(a, anna) == "erased"
    assert len(_stops(a)) == 1


def test_an_erasure_just_before_delivery_stops_the_send(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    anna = _stored(a)
    _while_the_message_is_assembled(monkeypatch, a, lambda: _erase(a))

    assert a._process_contact(anna) is False
    assert a.delivered == []
    assert _naming(a.db) == []
    assert _status(a, anna) == "erased"
    assert len(_stops(a)) == 1
    # The attempt was logged before the erasure: it is closed as not sent,
    # not left looking like a send in progress.
    [(status, error)] = a.db.conn.execute(
        "SELECT delivery_status, error_message FROM email_log"
    ).fetchall()
    assert status == "failed"
    assert "blocked" in error


def test_an_erasure_stops_the_email_to_a_colleague_at_her_company(
    settings_factory, tmp_path, monkeypatch
):
    """An erasure blocks the company too (decision L1.6)."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    _stored(a)
    bruno = _stored(a, BRUNO, name="Bruno Bianchi")
    _while_the_email_is_built(monkeypatch, lambda: _erase(a))

    assert a._process_contact(bruno) is False
    assert a.delivered == []


def test_an_erasure_just_before_delivery_stops_the_email_to_a_colleague(
    settings_factory, tmp_path, monkeypatch
):
    """Only the company block stops this one: Bruno's own address is free."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    _stored(a)
    bruno = _stored(a, BRUNO, name="Bruno Bianchi")
    _while_the_message_is_assembled(monkeypatch, a, lambda: _erase(a))

    assert a._process_contact(bruno) is False
    assert a.delivered == []
    assert _status(a, bruno) == "pending"


def test_an_erasure_while_the_server_is_reconnected_stops_the_send(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    anna = _stored(a)
    _while_the_server_is_reached(monkeypatch, a, lambda: _erase(a))

    assert a._process_contact(anna) is False
    assert a.delivered == []
    assert _naming(a.db) == []
    assert _status(a, anna) == "erased"
    assert len(_stops(a)) == 1


def test_an_erasure_while_another_send_finishes_stops_the_send(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    anna = _stored(a)
    _while_another_send_holds_the_connection(monkeypatch, a, lambda: _erase(a))

    assert a._process_contact(anna) is False
    assert a.delivered == []
    assert _naming(a.db) == []
    assert _status(a, anna) == "erased"


def test_a_preview_of_an_address_erased_meanwhile_names_nobody(
    settings_factory, tmp_path, monkeypatch
):
    """Direct Send's preview of an address the agent does not hold: no contact
    id, and the rows it logs would name the address."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    _while_the_email_is_built(monkeypatch, lambda: _erase(a))

    ok, _message = a.send_single_email(ANNA, dry_run=True)

    assert ok is False
    assert _naming(a.db) == []


def test_a_block_while_the_email_is_built_stops_the_send(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    anna = _stored(a)
    _while_the_email_is_built(monkeypatch, lambda: _block(a, anna))

    assert a._process_contact(anna) is False
    assert a.delivered == []
    assert _status(a, anna) == "blocked"


def test_a_stopped_send_is_not_queued_to_go_out_later(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    assert a.retry_queue
    anna = _stored(a)
    _while_the_message_is_assembled(monkeypatch, a, lambda: _block(a, anna))

    assert a._process_contact(anna) is False
    assert a.delivered == []
    assert a.db.conn.execute("SELECT COUNT(*) FROM retry_queue").fetchone()[0] == 0


def test_an_erasure_during_a_dry_run_writes_nothing_back(
    settings_factory, tmp_path, monkeypatch
):
    """A preview sends nothing, but it logs the same rows a send does."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    anna = _stored(a)
    _while_the_email_is_built(monkeypatch, lambda: _erase(a))

    assert a._process_contact(anna, dry_run=True) is False
    assert _naming(a.db) == []


def test_a_check_that_cannot_be_read_sends_nothing(
    settings_factory, tmp_path, monkeypatch
):
    """A locked database: nobody can say the address is still free to email."""
    a = _agent(settings_factory, tmp_path, monkeypatch)
    anna = _stored(a)

    def locked(_email):
        raise sqlite3.OperationalError("database is locked")

    _while_the_message_is_assembled(
        monkeypatch, a, lambda: monkeypatch.setattr(a.db, "is_blocked", locked)
    )

    with pytest.raises(sqlite3.OperationalError):
        a._process_contact(anna)
    assert a.delivered == []
    # Not passed off as a failed send to her, which would drop her lead.
    assert _status(a, anna) == "pending"


def test_a_send_nobody_blocked_still_goes_out(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    anna = _stored(a)
    _while_the_message_is_assembled(monkeypatch, a, lambda: None)

    assert a._process_contact(anna) is True
    assert a.delivered == [ANNA]
    assert _status(a, anna) == "emailed"
    assert _stops(a) == []


def test_erasing_someone_at_another_company_stops_nothing(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, monkeypatch)
    anna = _stored(a)
    _while_the_message_is_assembled(
        monkeypatch, a, lambda: _erase(a, "carla@other-brand.de")
    )

    assert a._process_contact(anna) is True
    assert a.delivered == [ANNA]
