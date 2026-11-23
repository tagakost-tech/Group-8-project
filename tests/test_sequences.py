"""Tests for the SequenceEngine: idempotent setup, enrollment, due detection."""

STEPS = [{"delay_days": 3, "subject": "s", "template_name": "followup_1"}]


def _engine(db):
    from modules.sequences import SequenceEngine
    return SequenceEngine(db)


def test_get_or_create_default_is_idempotent(db):
    eng = _engine(db)
    id1 = eng.get_or_create_default("Standard", STEPS)
    id2 = eng.get_or_create_default("Standard", STEPS)
    assert id1 == id2


def test_enroll_is_idempotent(db):
    eng = _engine(db)
    sid = db.log_search("q")
    cid = db.add_contact(sid, "b@example.com")
    seq_id = eng.get_or_create_default("Standard", STEPS)
    e1 = eng.enroll_contact(cid, seq_id)
    e2 = eng.enroll_contact(cid, seq_id)
    assert e1 == e2


def test_due_detection_after_delay(db):
    eng = _engine(db)
    sid = db.log_search("q")
    cid = db.add_contact(sid, "a@example.com")
    seq_id = eng.get_or_create_default("Standard", STEPS)
    enr = eng.enroll_contact(cid, seq_id)

    # Not due immediately after enrollment.
    assert all(x["enrollment_id"] != enr for x in eng.get_due_followups())

    # Backdate the enrollment so the 3-day delay has elapsed.
    db.conn.execute(
        "UPDATE sequence_enrollments SET enrolled_at = datetime('now','-10 days') WHERE id = ?",
        (enr,))
    db.conn.commit()

    due = eng.get_due_followups()
    assert any(x["enrollment_id"] == enr for x in due)
