"""Tests for the new pending-contact backlog processing in the agent.

These exercise the fix that makes previously-discovered 'pending' contacts
actually get emailed (in dry-run mode, so no real email is sent).
"""

from conftest import _dry_run_logged


def _agent(settings_factory, tmp_path, **overrides):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "agent.db"),
        verify_emails=False,  # avoid real DNS/MX lookups in tests
        dry_run=True,
        delay_between_emails=0.0,
        send_window_enabled=False,  # tests must not depend on the wall clock
        **overrides,
    )
    return OutreachAgent(s)


def test_pending_backlog_processed_in_dry_run(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path, min_lead_score_to_email=0)
    sid = agent.db.log_search("q")
    cid = agent.db.add_contact(
        sid, "john.doe@pharma.com", role="CEO", organization="PharmaCorp"
    )
    assert cid
    agent._running = True
    agent._process_pending_contacts(dry_run=True)

    # Asserted on the EMAIL LOG. This used to check contacts.status ==
    # "dry_run", which codified the bug that a dry run mutates the contact -
    # the coupling that made one dry run against a live database render the
    # whole backlog unsendable.
    assert _dry_run_logged(agent.db, cid), "the backlog contact was not processed"
    # A dry run is a simulation: the contact must stay sendable afterwards.
    assert agent.db.get_contact_by_id(cid)["status"] == "pending"
    assert agent._stats["emails_sent"] >= 1


def test_low_score_contact_filtered(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path, min_lead_score_to_email=50)
    sid = agent.db.log_search("q")
    cid = agent.db.add_contact(sid, "random@junk.com")
    agent._running = True
    agent._process_pending_contacts(dry_run=True)

    contact = agent.db.get_contact_by_id(cid)
    assert contact["status"] == "low_score"
    assert agent._stats["emails_sent"] == 0


def test_relevance_filter_drops_offtarget(settings_factory, tmp_path):
    agent = _agent(
        settings_factory,
        tmp_path,
        relevance_filter_enabled=True,
        min_lead_score_to_email=0,
    )
    sid = agent.db.log_search("q")
    cid = agent.db.add_contact(sid, "anonymous@cnn.com", organization="CNN")
    agent._running = True
    agent._process_pending_contacts(dry_run=True)

    contact = agent.db.get_contact_by_id(cid)
    assert contact["status"] == "irrelevant"
    assert agent._stats["emails_sent"] == 0


def test_opening_line_falls_back_when_ai_disabled(settings_factory, tmp_path):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        ai_personalization_enabled=False,
    )
    agent = OutreachAgent(s)
    line = agent._build_opening_line({"organization": "Acme GmbH"})
    assert "Acme GmbH" in line and line.strip()


def test_discovery_focus_hint(settings_factory, tmp_path):
    from agent import OutreachAgent

    s = settings_factory(db_path=str(tmp_path / "hint.db"), verify_emails=False)
    agent = OutreachAgent(s)
    # No searches yet -> no signal.
    assert agent._discovery_focus_hint() == ""
    # A productive search should surface in the hint.
    sid = agent.db.log_search("cosmetics manufacturer France")
    agent.db.update_search(sid, results_count=7, status="completed")
    assert "cosmetics manufacturer France" in agent._discovery_focus_hint()


def test_process_sequences_sends_due_followup_dry_run(settings_factory, tmp_path):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "seq.db"),
        verify_emails=False,
        dry_run=True,
        delay_between_emails=0.0,
        send_window_enabled=False,
        sequences_enabled=True,
        min_lead_score_to_email=0,
    )
    agent = OutreachAgent(s)
    assert agent.sequences and agent.sequence_id

    sid = agent.db.log_search("q")
    cid = agent.db.add_contact(sid, "seq@pharma.de", organization="Pharma GmbH")
    enr = agent.sequences.enroll_contact(cid, agent.sequence_id)
    # Backdate so the first follow-up step is due.
    agent.db.conn.execute(
        "UPDATE sequence_enrollments SET enrolled_at = datetime('now','-10 days') WHERE id = ?",
        (enr,),
    )
    agent.db.conn.commit()

    agent._running = True
    agent._process_sequences(dry_run=True)
    assert agent._stats["emails_sent"] >= 1


# ── The verified fast-path must still be gated ──────────────────────────────
# A contact verified on an earlier pass skips _qualify_contacts entirely (to
# avoid re-running AI + MX over the whole backlog every loop). That shortcut
# has its own copy of the relevance gate, and nothing covered it: deleting it
# would let previously-verified junk - or a direct competitor - get emailed.


def _verified_contact(agent, email, **kw):
    sid = agent.db.log_search("q")
    cid = agent.db.add_contact(sid, email, **kw)
    agent.db.update_contact_verification(cid, is_verified=True, details="MX ok")
    return cid


def test_verified_backlog_contact_still_passes_the_relevance_gate(
    settings_factory, tmp_path
):
    agent = _agent(
        settings_factory,
        tmp_path,
        relevance_filter_enabled=True,
        min_lead_score_to_email=0,
    )
    cid = _verified_contact(agent, "anonymous@cnn.com", organization="CNN")
    agent._running = True
    agent._process_pending_contacts(dry_run=True)

    assert agent.db.get_contact_by_id(cid)["status"] == "irrelevant"
    assert agent._stats["emails_sent"] == 0


def test_verified_backlog_competitor_is_not_emailed(settings_factory, tmp_path):
    """SUPPLECO *is* a contract manufacturer: a rival matches every supplement
    keyword we search for, and offering to manufacture for them is the single
    most embarrassing mail this agent could send."""
    agent = _agent(
        settings_factory,
        tmp_path,
        relevance_filter_enabled=True,
        min_lead_score_to_email=0,
    )
    cid = _verified_contact(
        agent,
        "info@nutrastar.de",
        organization="Nutrastar Supplement Contract Manufacturing GmbH",
    )
    agent._running = True
    agent._process_pending_contacts(dry_run=True)

    contact = agent.db.get_contact_by_id(cid)
    assert contact["status"] == "irrelevant"
    assert "competitor" in (contact["discard_reason"] or "")
    assert agent._stats["emails_sent"] == 0


def test_verified_relevant_contact_is_emailed_without_reverification(
    settings_factory, tmp_path
):
    """The point of the fast path: a good verified lead goes straight out and
    does NOT pay for another MX lookup."""
    agent = _agent(
        settings_factory,
        tmp_path,
        relevance_filter_enabled=True,
        min_lead_score_to_email=0,
    )
    cid = _verified_contact(
        agent,
        "einkauf@vitalbrand.de",
        organization="VitalBrand GmbH",
        qualification_rationale="Brand selling food supplements and vitamins",
    )
    agent._running = True
    agent._process_pending_contacts(dry_run=True)

    assert _dry_run_logged(agent.db, cid), "the verified lead was not sent"
    assert agent._stats["verified"] == 0  # no re-verification
