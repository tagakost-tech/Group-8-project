"""Tests for the hardening pass: private/LAN base-URL gating, and the
timezone-safe follow-up scheduler."""

from datetime import datetime, timedelta


def _agent(settings_factory, tmp_path, **overrides):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        dry_run=True,
        delay_between_emails=0.0,
        **overrides,
    )
    return OutreachAgent(s)


# ── private / LAN base URL is not "public" (S3) ────────────────────────────


def test_base_url_public_classification(settings_factory, tmp_path):
    def pub(url):
        return _agent(settings_factory, tmp_path, base_url=url)._is_public_base_url()

    assert pub("https://reach.example.com") is True
    assert pub("http://outreach.mycompany.io") is True
    assert pub("http://127.0.0.1:5000") is False
    assert pub("http://localhost:5000") is False
    assert pub("http://192.168.1.20:5000") is False  # LAN
    assert pub("http://10.0.0.5:5000") is False  # private
    assert pub("https://192.168.0.9") is False  # private even over https
    assert pub("") is False


# ── timezone-safe scheduler: legacy naive rows must not crash ──────────────


def test_due_followups_handles_legacy_naive_timestamp(settings_factory, tmp_path):
    a = _agent(
        settings_factory, tmp_path, sequences_enabled=True, min_lead_score_to_email=0
    )
    assert a.sequences and a.sequence_id
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, "seq@brand.de", organization="Brand")
    enr = a.sequences.enroll_contact(cid, a.sequence_id)
    # Simulate a legacy row saved with a NAIVE (no tzinfo) timestamp in the past.
    # Naive ON PURPOSE - this is the legacy row shape under test.
    past_naive = (datetime.now() - timedelta(days=30)).isoformat()  # noqa: DTZ005
    a.db.conn.execute(
        "UPDATE sequence_enrollments SET enrolled_at = ? WHERE id = ?",
        (past_naive, enr),
    )
    a.db.conn.commit()
    # Must not raise (naive vs aware) and should be due.
    due = a.sequences.get_due_followups()
    assert any(d["enrollment_id"] == enr for d in due)
