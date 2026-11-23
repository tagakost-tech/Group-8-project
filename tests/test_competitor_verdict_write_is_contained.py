"""A locked database while recording a competitor verdict does not escape.

Every other verdict write in the agent is wrapped in try/except
sqlite3.Error, because it runs mid-loop and the verdict itself - "do not
send" - is already decided. The competitor branch was the one left bare: a
locked database raised straight out of _process_contact and took down the
rest of the batch with it.
"""

import sqlite3

from agent import OutreachAgent
from modules.clients import ClientRegister


def test_a_failed_status_write_still_refuses_quietly(
    settings_factory, tmp_path, monkeypatch
):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    a.competitor_register = ClientRegister(["Naturacare"])
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="info@naturacare.com", organization="Naturacare")

    sent = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: sent.append(k) or 1
    )

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(a.db, "update_contact_status", locked)

    result = a._process_contact(
        {"id": cid, "email": "info@naturacare.com", "organization": "Naturacare"}
    )
    assert result is False
    assert sent == []
