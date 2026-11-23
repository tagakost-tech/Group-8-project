"""The agent proposes, a person decides (modules/review_queue.py).

With a stage at ``approve`` - the default - nothing the agent writes reaches a
mailbox until a named person has opened it and approved it, and what goes out
is the text they approved, byte for byte. These tests drive the real send path
in dry run, so a "send" is a dry_run row in the email log.
"""

import pytest

from conftest import _dry_run_logged
from modules.review_queue import ReviewError, ReviewQueue, text_hash


def _agent(settings_factory, tmp_path, monkeypatch, level="approve", **overrides):
    from agent import OutreachAgent

    monkeypatch.setenv("AUTONOMY_FIRST_EMAIL", level)
    s = settings_factory(
        db_path=str(tmp_path / "agent.db"),
        verify_emails=False,
        dry_run=True,
        delay_between_emails=0.0,
        send_window_enabled=False,
        min_lead_score_to_email=0,
        **overrides,
    )
    agent = OutreachAgent(s)
    agent._running = True
    return agent


def _contact(agent, email="anna.weber@doorlocks.example", org="Door Locks GmbH"):
    sid = agent.db.log_search("door lock manufacturer Germany")
    return agent.db.add_contact(
        sid,
        email,
        name="Anna Weber",
        role="Head of Purchasing",
        organization=org,
        qualification_rationale="Hersteller von Schließsystemen für Fahrzeuge",
    )


def _queue(agent):
    return ReviewQueue(agent.db)


def test_nothing_is_sent_before_a_person_approves(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _contact(agent)

    agent._process_pending_contacts(dry_run=True)
    agent._process_pending_contacts(dry_run=True)

    assert not _dry_run_logged(agent.db, cid), (
        "an email went out with nobody's approval"
    )
    item = _queue(agent).item_for(cid, "first_email")
    assert item and item["status"] == "pending"
    # The proposal carries what it rests on, for the reviewer to judge first.
    assert item["evidence"]["organization"] == "Door Locks GmbH"
    assert "Schließsystemen" in item["evidence"]["found_on_their_site"]
    # Two passes, one proposal: the reviewer's work is never doubled.
    rows = agent.db.conn.execute(
        "SELECT COUNT(*) FROM review_queue WHERE contact_id = ?", (cid,)
    ).fetchone()[0]
    assert rows == 1
    assert agent.db.get_contact_by_id(cid)["status"] == "pending"


def test_the_approved_text_is_what_goes_out(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _contact(agent)
    agent._process_pending_contacts(dry_run=True)
    q = _queue(agent)
    item = q.item_for(cid, "first_email")

    edited = item["proposed_body"].replace(
        item["proposed_body"].split("</p>", 1)[0] + "</p>",
        item["proposed_body"].split("</p>", 1)[0]
        + "</p><p>We saw your new door module line at IAA.</p>",
        1,
    )
    q.open(item["id"], "taga")
    q.approve(item["id"], "taga", subject="Parts for your door modules", body=edited)
    agent._process_pending_contacts(dry_run=True)

    assert _dry_run_logged(agent.db, cid)
    log = agent.db.conn.execute(
        "SELECT subject, body_preview FROM email_log WHERE contact_id = ?", (cid,)
    ).fetchone()
    assert log["subject"] == "Parts for your door modules"
    assert "door module line at IAA" in log["body_preview"]
    assert q.get(item["id"])["status"] in ("approved", "sent")


def test_a_rejection_closes_the_contact_with_its_reason(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _contact(agent)
    agent._process_pending_contacts(dry_run=True)
    q = _queue(agent)
    item = q.item_for(cid, "first_email")

    q.open(item["id"], "taga")
    with pytest.raises(ReviewError, match="reason"):
        q.reject(item["id"], "taga", "")
    q.reject(item["id"], "taga", "they make the locks in China, not in Europe")
    agent._process_pending_contacts(dry_run=True)

    contact = agent.db.get_contact_by_id(cid)
    assert contact["status"] == "rejected_in_review"
    assert "rejected in review" in contact["discard_reason"]
    assert not _dry_run_logged(agent.db, cid)


def test_an_unopened_proposal_cannot_be_approved(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _contact(agent)
    agent._process_pending_contacts(dry_run=True)
    q = _queue(agent)
    item = q.item_for(cid, "first_email")

    with pytest.raises(ReviewError, match="open"):
        q.approve(item["id"], "taga")
    with pytest.raises(ReviewError, match="named reviewer"):
        q.open(item["id"], "taga")
        q.approve(item["id"], "  ")


def test_the_footer_and_opt_out_cannot_be_edited_away(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _contact(agent)
    agent._process_pending_contacts(dry_run=True)
    q = _queue(agent)
    item = q.item_for(cid, "first_email")
    q.open(item["id"], "taga")

    assert "{{legal_footer}}" in item["proposed_body"]
    with pytest.raises(ReviewError, match="legal_footer"):
        q.approve(
            item["id"],
            "taga",
            body=item["proposed_body"].replace("{{legal_footer}}", ""),
        )


def test_text_changed_after_approval_is_never_sent(
    settings_factory, tmp_path, monkeypatch
):
    """A forged or tampered approval: the stored text no longer matches the
    hash taken when the person approved it."""
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _contact(agent)
    agent._process_pending_contacts(dry_run=True)
    q = _queue(agent)
    item = q.item_for(cid, "first_email")
    q.open(item["id"], "taga")
    q.approve(item["id"], "taga")

    with agent.db._transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET final_body = final_body || '<p>Buy now!</p>' WHERE id = ?",
            (item["id"],),
        )
    agent._process_pending_contacts(dry_run=True)

    assert not _dry_run_logged(agent.db, cid)


def test_a_forged_approval_without_a_hash_is_refused(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _contact(agent)
    agent._process_pending_contacts(dry_run=True)
    item = _queue(agent).item_for(cid, "first_email")

    with agent.db._transaction() as conn:
        conn.execute(
            """UPDATE review_queue SET status = 'approved', final_subject = 'x',
                      final_body = proposed_body WHERE id = ?""",
            (item["id"],),
        )
    agent._process_pending_contacts(dry_run=True)

    assert not _dry_run_logged(agent.db, cid)


def test_auto_keeps_the_old_unattended_behaviour(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch, level="auto")
    cid = _contact(agent)
    agent._process_pending_contacts(dry_run=True)

    assert _dry_run_logged(agent.db, cid)
    assert _queue(agent).item_for(cid, "first_email") is None


def test_manual_means_the_agent_sends_nothing_itself(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch, level="manual")
    cid = _contact(agent)
    agent._process_pending_contacts(dry_run=True)

    assert not _dry_run_logged(agent.db, cid)
    assert _queue(agent).item_for(cid, "first_email") is None


def test_an_unknown_level_reads_as_approve_never_auto(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch, level="yolo")
    cid = _contact(agent)
    agent._process_pending_contacts(dry_run=True)

    assert not _dry_run_logged(agent.db, cid)
    assert _queue(agent).item_for(cid, "first_email")["status"] == "pending"


def test_the_daily_cap_stops_rubber_stamping(db):
    sid = db.log_search("q")
    q = ReviewQueue(db, daily_cap=2)
    ids = []
    for n in range(3):
        cid = db.add_contact(
            sid, f"buyer{n}@maker{n}.example", organization=f"Maker {n}"
        )
        ids.append(q.propose(cid, "first_email", "s", "<p>b</p>{{legal_footer}}"))
    for item_id in ids[:2]:
        q.open(item_id, "taga")
        q.approve(item_id, "taga")
    q.open(ids[2], "taga")
    with pytest.raises(ReviewError, match="daily"):
        q.approve(ids[2], "taga")


def test_metrics_pair_approvals_with_edits(db):
    sid = db.log_search("q")
    q = ReviewQueue(db)
    a = q.propose(
        db.add_contact(sid, "a@one.example"),
        "first_email",
        "s",
        "<p>a</p>{{legal_footer}}",
    )
    b = q.propose(
        db.add_contact(sid, "b@two.example"),
        "first_email",
        "s",
        "<p>b</p>{{legal_footer}}",
    )
    c = q.propose(
        db.add_contact(sid, "c@three.example"),
        "first_email",
        "s",
        "<p>c</p>{{legal_footer}}",
    )
    for i in (a, b, c):
        q.open(i, "taga")
    q.approve(a, "taga")
    q.approve(b, "taga", body="<p>b, better</p>{{legal_footer}}")
    q.reject(c, "taga", "a distributor")

    m = q.metrics()
    assert m["decided"] == 3 and m["approved"] == 2 and m["rejected"] == 1
    assert m["approved_without_edits"] == 0.5
    assert q.get(b)["approved_hash"] == text_hash(
        "s", "<p>b, better</p>{{legal_footer}}"
    )


def test_erasure_takes_the_text_and_keeps_the_decision(db):
    """A proposal names the person, so erasure takes its text; the decision is
    the record of human oversight, so it stays, and a waiting proposal can
    never be approved into a send afterwards."""
    from modules.export import anonymise_contact_rows, records_held_about

    sid = db.log_search("q")
    cid = db.add_contact(
        sid, "anna.weber@doorlocks.example", organization="Door Locks GmbH"
    )
    q = ReviewQueue(db)
    item_id = q.propose(
        cid,
        "first_email",
        "For Anna",
        "<p>Dear Anna Weber</p>{{legal_footer}}",
        evidence={"organization": "Door Locks GmbH"},
    )
    assert records_held_about(db, "anna.weber@doorlocks.example")["review_queue"]

    anonymise_contact_rows(db, "anna.weber@doorlocks.example")
    db.conn.commit()

    item = q.get(item_id)
    assert (
        item["proposed_body"] == "[erased]" and item["proposed_subject"] == "[erased]"
    )
    assert item["evidence"] is None
    assert item["status"] == "rejected"
    held = db.conn.execute("SELECT * FROM review_queue").fetchall()
    assert not any("Anna" in str(v) for row in held for v in tuple(row))
