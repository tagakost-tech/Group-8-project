"""A backlog contact is qualified once, and its legal basis recorded once.

The backlog is re-read every loop. Contacts that were verified on an earlier
pass take a fast path; everyone else goes back through _qualify_contacts.
But "verified" is only ever set by the MX check - so with MX verification
off, EVERY contact still waiting (warm-up cap reached, send window closed)
was re-qualified every five minutes: another AI relevance call billed, and
another consent-log row inserted, since log_consent was a plain INSERT.
Three hundred pending contacts meant three hundred duplicate "legitimate
interest" records per loop.

is_verified cannot simply be set instead: it means "MX confirmed", and the
dashboard's verified count and funnel read it. Qualification gets its own
timestamp.
"""

from agent import OutreachAgent


def test_consent_is_recorded_once_per_basis(db):
    db.log_consent("Info@Brand.de", "legitimate_interest", "found on brand.de")
    db.log_consent("info@brand.de", "legitimate_interest", "found on brand.de/kontakt")
    n = db.conn.execute("SELECT COUNT(*) FROM consent_log").fetchone()[0]
    assert n == 1


class _CountingAI:
    is_configured = True

    def __init__(self):
        self.calls = 0

    def classify_relevance(self, contact):
        self.calls += 1
        return True

    def classify_company_country(self, contact):
        return None


def _agent(settings_factory, tmp_path):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            lead_scoring_enabled=False,
            ai_relevance_enabled=True,
            # The backlog is qualified only when a send can follow (33793e9);
            # without this the tests passed on weekdays and failed at weekends.
            send_window_enabled=False,
        )
    )
    a._running = True
    a.ai_gen = _CountingAI()
    a._pick_sender = lambda: None  # nothing can be sent: contacts stay pending
    return a


def test_a_backlog_contact_is_not_reclassified_every_loop(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    sid = a.db.log_search("q")
    a.db.add_contact(sid, email="info@brand.de", organization="Brand GmbH")

    a._process_pending_contacts(dry_run=False)
    a._process_pending_contacts(dry_run=False)

    assert a.ai_gen.calls == 1, "the AI classifier was paid twice for one contact"
    # Qualification records no basis: the contact carries it from the moment
    # it was stored, and only a live send writes the basis record
    # (test_each_send_records_its_notice_and_basis).
    rows = a.db.conn.execute("SELECT COUNT(*) FROM consent_log").fetchone()[0]
    assert rows == 0


def test_with_mx_checks_on_only_a_real_verification_counts(
    settings_factory, tmp_path, monkeypatch
):
    """qualified_at stands in for verification only when there is none. If
    MX checks are on, an unverified contact must go through them."""
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=True,
            verify_mx=True,
            imap_enabled=False,
            ai_humanize_emails=False,
            send_window_enabled=False,
        )
    )
    assert a.email_verifier, "setup: verification must be enabled"
    a._running = True
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="info@brand.de", organization="Brand GmbH")
    a.db.mark_contact_qualified(cid)
    seen = []
    monkeypatch.setattr(a, "_qualify_contacts", lambda cs: seen.extend(cs) or [])
    monkeypatch.setattr(a, "_email_contacts", lambda *x, **k: None)
    a._process_pending_contacts(dry_run=False)
    assert [c["id"] for c in seen] == [cid]


def test_verification_is_not_claimed_when_it_did_not_happen(settings_factory, tmp_path):
    """The honest alternative: qualified, yes; MX-verified, no."""
    a = _agent(settings_factory, tmp_path)
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="info@brand.de", organization="Brand GmbH")
    a._process_pending_contacts(dry_run=False)
    c = a.db.get_contact_by_id(cid)
    assert c["qualified_at"] is not None
    assert not c["is_verified"]
