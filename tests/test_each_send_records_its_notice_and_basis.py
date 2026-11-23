"""Each live email records the notice it carried and the basis it relied on.

The basis record (consent_log) was written when a contact was qualified -
dry runs included, so a preview left a record saying someone had been
contacted - and before a Direct Send, even one that then failed. It held
neither the contact, the channel nor the rules version, and nothing recorded
which version of the footer's notice an email carried.

Now:
- email_log.notice_version records the notice each email carried;
- the basis record is written after a live send succeeds, with the contact,
  the channel and the rules version, and never by a dry run;
- a Direct Send relies on the operator's channel, not on how the contact was
  first collected;
- a contact with no recorded basis is never emailed live.
"""

import pytest

from agent import OutreachAgent
from modules.compliance import RULE_VERSION, notice_version
from modules.database import Database
from modules.email_sender import EmailSender

EMAIL = "anna@brand.de"


def _agent(settings_factory, tmp_path, monkeypatch):
    agent = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            ai_humanize_emails=False,
        )
    )
    agent.dispatched = []
    monkeypatch.setattr(
        agent.email_sender,
        "dispatch_to_contact",
        lambda **k: agent.dispatched.append(k) or 1,
    )
    return agent


def _stored(agent, channel="third_party"):
    cid = agent.db.add_contact(
        agent.db.log_search("q"),
        EMAIL,
        organization="Brand",
        source_url="https://brand.de/kontakt",
        source_channel=channel,
    )
    return agent.db.get_contact_by_id(cid)


def _consent(db):
    return [
        dict(r)
        for r in db.conn.execute(
            "SELECT email, legal_basis, contact_id, channel, rule_version "
            "FROM consent_log"
        )
    ]


def test_a_send_carries_the_notice_version(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    assert agent._process_contact(_stored(agent)) is True
    assert agent.dispatched[0]["notice_version"] == notice_version(
        "legitimate_interest"
    )


def test_the_email_log_records_the_notice_version(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    cid = db.add_contact(db.log_search("q"), EMAIL)
    sender = EmailSender(db, "smtp.test", 587, "me@co.it", "pw")
    sender.dispatch_to_contact(
        contact={"id": cid, "email": EMAIL},
        subject="Hello",
        html_body="<p>Hi</p>",
        plain_body="Hi",
        dry_run=True,
        notice_version="notice-test-7",
    )
    row = db.conn.execute("SELECT notice_version FROM email_log").fetchone()
    assert row[0] == "notice-test-7"


def test_a_live_send_records_its_basis(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    contact = _stored(agent)
    agent._process_contact(contact)
    assert _consent(agent.db) == [
        {
            "email": EMAIL,
            "legal_basis": "legitimate_interest",
            "contact_id": contact["id"],
            "channel": "third_party",
            "rule_version": RULE_VERSION,
        }
    ]


def test_a_dry_run_records_no_basis(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    contact = _stored(agent)
    agent._process_pending_contacts(dry_run=True)  # qualification included
    assert agent._process_contact(contact, dry_run=True) is True
    assert agent.dispatched, "setup: the dry run must reach the dispatch"
    assert _consent(agent.db) == []


def test_a_failed_send_records_no_basis(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    monkeypatch.setattr(agent.email_sender, "dispatch_to_contact", lambda **k: False)
    agent.send_single_email(EMAIL, channel="trade_fair")
    assert _consent(agent.db) == []


def test_a_direct_send_relies_on_the_operators_channel(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    _stored(agent, channel="third_party")
    ok, _ = agent.send_single_email(EMAIL, channel="inbound_request")
    assert ok is True
    [record] = _consent(agent.db)
    assert (record["channel"], record["legal_basis"]) == ("inbound_request", "consent")


def test_a_partial_contact_uses_its_stored_basis(
    settings_factory, tmp_path, monkeypatch
):
    """Follow-ups and retries hand over an id and an address, not the row."""
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    contact = _stored(agent)
    assert agent._process_contact({"id": contact["id"], "email": EMAIL}) is True
    [record] = _consent(agent.db)
    assert (record["legal_basis"], record["channel"]) == (
        "legitimate_interest",
        "third_party",
    )


@pytest.mark.parametrize("dry_run,sent", [(False, False), (True, True)])
def test_a_contact_without_a_basis_is_never_emailed_live(
    settings_factory, tmp_path, monkeypatch, dry_run, sent
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    contact = _stored(agent)
    agent.db.conn.execute("UPDATE contacts SET basis = NULL")
    agent.db.conn.commit()
    assert (
        agent._process_contact(
            agent.db.get_contact_by_id(contact["id"]), dry_run=dry_run
        )
        is sent
    )
