"""Personal and PEC mailboxes already in the database are not emailed either.

Review finding. The European personal-mailbox list (and the PEC screen) were
enforced when scraping and in Direct Send. Contacts stored BEFORE the list
grew - mario@libero.it harvested under the old ten-domain filter - still
went through the backlog, follow-ups and retries, none of which looked at
the address. The screen now sits at the send chokepoint every path shares.
"""

import pytest

from agent import OutreachAgent


@pytest.fixture
def agent(settings_factory, tmp_path, monkeypatch):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            sequences_enabled=True,
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    a.sent = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: a.sent.append(k) or 1
    )
    return a


def _stored(a, email, status="pending"):
    sid = a.db.log_search("q")
    cid = a.db.conn.execute(
        "INSERT INTO contacts (search_id, email, organization, status, basis) "
        "VALUES (?, ?, 'Shop', ?, 'legitimate_interest')",
        (sid, email, status),
    ).lastrowid
    a.db.conn.commit()
    return a.db.get_contact_by_id(cid)


@pytest.mark.parametrize("email", ["mario@libero.it", "ufficio@pec.shop.it"])
def test_a_stored_personal_or_pec_address_is_refused(agent, email):
    c = _stored(agent, email)
    assert agent._process_contact(dict(c), dry_run=False) is False
    assert agent.sent == []
    assert agent.db.get_contact_by_id(c["id"])["status"] == "irrelevant"


def test_its_follow_up_stops_too(agent, monkeypatch):
    c = _stored(agent, "hans@gmx.de", status="emailed")
    cancelled = []
    monkeypatch.setattr(
        agent.sequences,
        "get_due_followups",
        lambda: [{"enrollment_id": 1, "contact_id": c["id"], "subject": "s"}],
    )
    monkeypatch.setattr(
        agent.sequences, "cancel_enrollment", lambda eid, why: cancelled.append(why)
    )
    agent._process_sequences(dry_run=False)
    assert agent.sent == []
    assert cancelled


def test_a_business_address_still_goes(agent):
    c = _stored(agent, "info@brand.de")
    assert agent._process_contact(dict(c), dry_run=False)
    assert len(agent.sent) == 1
