"""Replies are answered by a person, with the agent's help.

At AUTONOMY_REPLY=approve a reply from a company the agent emailed becomes a
proposal: the prospect's own words, the agent's reading of them (interested
or not) and a suggested answer. Nothing is answered until a person approves
the text, and the answer that goes is exactly that text.
"""

from conftest import sent_to
from modules.review_queue import ReviewError, ReviewQueue


def _agent(settings_factory, tmp_path, monkeypatch, level="approve"):
    from agent import OutreachAgent

    monkeypatch.setenv("AUTONOMY_REPLY", level)
    s = settings_factory(
        db_path=str(tmp_path / "agent.db"),
        verify_emails=False,
        dry_run=True,
        send_window_enabled=False,
    )
    agent = OutreachAgent(s)
    agent._running = True
    return agent


def _emailed(agent, email="lena.koch@doorlocks.example"):
    sid = agent.db.log_search("q")
    cid = agent.db.add_contact(sid, email, name="Lena Koch", organization="Door Locks GmbH")
    sent_to(agent.db, email)
    return cid


REPLY = {
    "email": "lena.koch@doorlocks.example",
    "subject": "Re: Special cold-formed parts to your drawing",
    "body": "Interesting - can you send a price for 200k pins a year?\n\n> our email",
}


def test_a_reply_waits_for_a_person_with_its_reading(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _emailed(agent)

    agent.loop_orchestrator.handle_interested_replies([REPLY])

    item = ReviewQueue(agent.db).item_for(cid, "reply")
    assert item and item["status"] == "pending"
    ev = item["evidence"]
    assert "200k pins" in ev["their_words"] and "our email" not in ev["their_words"]
    assert ev["reads_as_interested"] is True
    assert item["proposed_subject"].startswith("Re: ")
    assert "{{legal_footer}}" in item["proposed_body"]


def test_the_approved_answer_is_what_goes(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _emailed(agent)
    agent.loop_orchestrator.handle_interested_replies([REPLY])
    q = ReviewQueue(agent.db)
    item = q.item_for(cid, "reply")

    q.open(item["id"], "maria")
    q.approve(
        item["id"],
        "maria",
        body="<p>Dear Ms Koch,</p><p>Yes: send the drawing and we quote within 8 hours.</p>"
        "{{legal_footer}}",
    )
    ok, _message = agent.send_reply_answer(item["id"], dry_run=True)

    assert ok
    row = agent.db.conn.execute(
        "SELECT subject, body_preview, delivery_status FROM email_log "
        "WHERE contact_id = ? ORDER BY id DESC",
        (cid,),
    ).fetchone()
    assert row["delivery_status"] == "dry_run"
    assert "quote within 8 hours" in row["body_preview"]


def test_an_unapproved_answer_is_never_sent(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _emailed(agent)
    agent.loop_orchestrator.handle_interested_replies([REPLY])
    item = ReviewQueue(agent.db).item_for(cid, "reply")

    ok, message = agent.send_reply_answer(item["id"], dry_run=True)

    assert not ok and "approved" in message


def test_at_auto_no_reply_is_queued(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch, level="auto")
    cid = _emailed(agent)
    agent.loop_orchestrator.handle_interested_replies([REPLY])
    assert ReviewQueue(agent.db).item_for(cid, "reply") is None


def test_a_reply_from_someone_never_emailed_is_not_queued(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    agent.loop_orchestrator.handle_interested_replies(
        [{**REPLY, "email": "stranger@elsewhere.example"}]
    )
    assert agent.db.conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0


def test_an_answer_cannot_drop_the_footer(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _emailed(agent)
    agent.loop_orchestrator.handle_interested_replies([REPLY])
    q = ReviewQueue(agent.db)
    item = q.item_for(cid, "reply")
    q.open(item["id"], "maria")
    try:
        q.approve(item["id"], "maria", body="<p>no footer</p>")
    except ReviewError as e:
        assert "legal_footer" in str(e)
    else:
        raise AssertionError("an answer without the footer was approved")


def test_a_note_left_for_the_reviewer_cannot_be_approved(
    settings_factory, tmp_path, monkeypatch
):
    """The suggested answer holds "[Answer anything else they asked here.]".
    Approved as it is, the prospect would read that note."""
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _emailed(agent)
    agent.loop_orchestrator.handle_interested_replies([REPLY])
    q = ReviewQueue(agent.db)
    item = q.item_for(cid, "reply")
    q.open(item["id"], "maria")
    try:
        q.approve(item["id"], "maria")
    except ReviewError as e:
        assert "fill in or delete" in str(e)
    else:
        raise AssertionError("a text with a note for the reviewer was approved")
