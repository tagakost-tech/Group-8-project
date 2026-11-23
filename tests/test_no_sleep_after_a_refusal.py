"""The send delay is spent only after something was actually dispatched.

Each send loop slept the full send delay (two minutes by default) after every
contact it handed to the send, whatever happened there. When the send refused
before dispatch - an email template without the legal footer is the standing
case - nothing went out, yet each contact still cost two minutes, so a
misconfigured template stalled the whole backlog at that pace. The loops now
sleep only when the send reports a dispatch; a send that was attempted and
failed still keeps the gap, since the server saw it.
"""

import pytest

from agent import OutreachAgent


@pytest.fixture
def agent(settings_factory, tmp_path, monkeypatch):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            sequences_enabled=True,
        )
    )
    a._running = True
    a.sleeps = []
    a._interruptible_sleep = lambda s: a.sleeps.append(s)
    a.outcome = 1

    def _dispatch(**k):
        # As the real sender does: every attempt leaves an email_log row.
        c = k["contact"]
        status = "sent" if a.outcome else "failed"
        log_id = a.db.log_email(c["id"], c["email"], "s", delivery_status=status)
        return log_id if a.outcome else 0

    monkeypatch.setattr(a.email_sender, "dispatch_to_contact", _dispatch)
    a.sid = a.db.log_search("q")
    return a


def _contact(a, email, status="pending"):
    cid = a.db.add_contact(a.sid, email=email, organization="Brand")
    a.db.update_contact_status(cid, status)
    return a.db.get_contact_by_id(cid)


def test_a_template_refusal_costs_the_campaign_no_sleep(agent):
    a = agent
    a._email_template = "<p>{{greeting}}</p>"  # no {{legal_footer}}
    a._email_contacts([_contact(a, "one@brand.de"), _contact(a, "two@brand.de")])
    assert a.sleeps == []


def test_a_failed_dispatch_still_keeps_the_gap(agent):
    """The control: the server saw this attempt, so pacing applies."""
    a = agent
    a.outcome = 0
    a._email_contacts([_contact(a, "one@brand.de"), _contact(a, "two@brand.de")])
    assert len(a.sleeps) == 2


def test_a_template_refusal_costs_the_follow_ups_no_sleep(agent, monkeypatch):
    a = agent
    c = _contact(a, "buyer@brand.de", status="emailed")
    due = [{"enrollment_id": 1, "contact_id": c["id"], "contact": c, "subject": "s"}]
    monkeypatch.setattr(a.sequences, "get_due_followups", lambda: due)
    monkeypatch.setattr(a, "_load_followup_template", lambda name: "<p>no footer</p>")
    a._process_sequences(dry_run=False)
    assert a.sleeps == []


def test_a_template_refusal_costs_the_retries_no_sleep(agent):
    a = agent
    a._email_template = "<p>{{greeting}}</p>"
    c = _contact(a, "buyer@brand.de", status="failed")
    log_id = a.db.log_email(c["id"], c["email"], "s", delivery_status="failed")
    a.retry_queue.add_to_retry(log_id, c["id"], "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    a._process_retries()
    assert a.sleeps == []
