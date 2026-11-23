"""A template that has lost the legal footer is refused when it is used.

Review finding. doctor marks a template without {{legal_footer}} as a
blocking [FAIL] and prints "live sending is refused" - but nothing at
runtime looked at the template. The dashboard editor guards the one file it
edits; an A/B variant, a follow-up template or a file edited on disk went
out with no legal identity and no notice. The chokepoint now refuses a live
send whose chosen template has no footer. Dry runs still render, so the
operator can see what is wrong.
"""

import pytest

from agent import OutreachAgent

NO_FOOTER = "<p>{{greeting}}</p>{{body}}<a href='{{unsubscribe_url}}'>unsubscribe</a>"


@pytest.fixture
def agent(settings_factory, tmp_path, monkeypatch):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    a.sent = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: a.sent.append(k) or 1
    )
    sid = a.db.log_search("q")
    a.cid = a.db.add_contact(sid, email="info@brand.de", organization="Brand")
    return a


def _send(a, **kw):
    return a._process_contact({"id": a.cid, "email": "info@brand.de"}, **kw)


def test_a_first_email_template_without_the_footer_is_refused(agent):
    agent._email_template = NO_FOOTER
    assert _send(agent, dry_run=False) is False
    assert agent.sent == []
    rows = [
        r
        for r in agent.db.get_recent_activity(20)
        if r["action"] == "LEGAL_FOOTER_MISSING"
    ]
    assert len(rows) == 1


def test_a_follow_up_template_without_the_footer_is_refused(agent):
    assert (
        _send(agent, dry_run=False, template_override=NO_FOOTER, is_followup=True)
        is False
    )
    assert agent.sent == []


def test_a_dry_run_still_renders_it(agent):
    agent._email_template = NO_FOOTER
    assert _send(agent, dry_run=True)
    assert len(agent.sent) == 1


def test_the_real_templates_still_send(agent):
    assert _send(agent, dry_run=False)
    assert len(agent.sent) == 1
