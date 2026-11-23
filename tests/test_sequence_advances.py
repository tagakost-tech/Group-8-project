"""A follow-up sequence has to actually move forward.

advance_enrollment was called by nothing real. Both test files that touch the
follow-up path — test_followup_guards.py and test_send_path_guards.py —
monkeypatch it away, so a version that never advanced would have shipped with
the suite green, and every enrolled prospect would have received step 1 again
on every pass, indefinitely.

That is the worst shape of bug this project can produce: it does not crash, it
does not log an error, and the only person who finds out is the recipient.
"""

import pytest

from modules.database import Database
from modules.sequences import SequenceEngine

STEPS = [
    {"delay_days": 3, "subject": "Following up", "template_name": None},
    {"delay_days": 7, "subject": "One more thought", "template_name": None},
]


@pytest.fixture
def seq(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    engine = SequenceEngine(db=db)
    sid = db.log_search("q", source="test")
    contact_id = db.add_contact(sid, "buyer@brand.de", organization="Brand")
    seq_id = engine.create_sequence("followups", STEPS)
    enrollment_id = engine.enroll_contact(contact_id, seq_id)
    return engine, db, enrollment_id


def _row(db, enrollment_id):
    cur = db.conn.execute(
        "SELECT current_step, status, last_sent_at FROM sequence_enrollments "
        "WHERE id = ?",
        (enrollment_id,),
    )
    return dict(zip([c[0] for c in cur.description], cur.fetchone()))


def test_advancing_moves_to_the_next_step(seq):
    """The plain case, and the one nothing covered: step 0 becomes step 1."""
    engine, db, eid = seq
    assert _row(db, eid)["current_step"] == 0

    engine.advance_enrollment(eid)

    assert _row(db, eid)["current_step"] == 1, (
        "the enrollment did not advance - the same follow-up would be sent "
        "again on the next pass, and on every pass after it"
    )


def test_advancing_stamps_the_send_time(seq):
    """get_due_followups computes "due" from last_sent_at as well as
    current_step, so a step that advances without stamping the time comes back
    as due immediately."""
    engine, db, eid = seq
    assert _row(db, eid)["last_sent_at"] is None

    engine.advance_enrollment(eid)

    assert _row(db, eid)["last_sent_at"] is not None


def test_the_sequence_completes_after_its_last_step(seq):
    """Two steps in, two advances out: the second must finish the enrollment
    rather than point at a step that does not exist."""
    engine, db, eid = seq

    engine.advance_enrollment(eid)
    assert _row(db, eid)["status"] == "active"

    engine.advance_enrollment(eid)
    row = _row(db, eid)
    assert row["status"] == "completed", (
        "a finished sequence stayed active - it would keep being selected"
    )


def test_a_completed_enrollment_is_no_longer_due(seq):
    """The property that matters downstream: once completed, the agent must
    not pick it up again."""
    engine, db, eid = seq
    engine.advance_enrollment(eid)
    engine.advance_enrollment(eid)

    assert _row(db, eid)["status"] == "completed"
    due_ids = [d["enrollment_id"] for d in engine.get_due_followups()]
    assert eid not in due_ids


def test_advancing_an_unknown_enrollment_is_harmless(tmp_path):
    """It must not raise: this runs after the mail is already sent, and an
    exception there propagates into the send loop."""
    db = Database(str(tmp_path / "u.db"))
    SequenceEngine(db=db).advance_enrollment(99999)  # must not raise


def test_advancing_twice_does_not_skip_a_step(seq):
    """Guard against an over-eager fix: each call moves exactly one step."""
    engine, db, eid = seq
    engine.advance_enrollment(eid)
    assert _row(db, eid)["current_step"] == 1
    assert _row(db, eid)["status"] == "active"
