"""Regression tests for the Batch 2 duplicate-send defects.

These cover the three mechanisms that could deliver the same cold email to the
same person more than once, plus the verification bypass.
"""

import sqlite3
import threading

import pytest

from modules.database import Database
from modules.retry_queue import RetryQueue

# ── 1. contacts.email uniqueness ─────────────────────────────────────────────
# add_contact deduped with a SELECT followed by an INSERT. sqlite3 only opens
# the write transaction at the DML, so the two ran unserialised: the agent
# worker and a Flask Direct Send could both see "no existing contact", both
# insert, and both email the same prospect.


def test_contacts_email_has_a_unique_index(db):
    raw = sqlite3.connect(db.db_path)
    raw.row_factory = sqlite3.Row
    unique = [
        r["name"] for r in raw.execute("PRAGMA index_list(contacts)") if r["unique"]
    ]
    raw.close()
    assert unique, "contacts.email must carry a UNIQUE index"


def test_concurrent_add_contact_creates_exactly_one_row(db):
    """The original probe produced 9 rows from 12 threads."""
    sid = db.log_search("q", source="test")
    results, errors = [], []
    barrier = threading.Barrier(12)

    def worker():
        try:
            barrier.wait()
            results.append(db.add_contact(sid, "Race@Brand.de"))
        except Exception as e:  # noqa: BLE001 - test needs the failure surfaced
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw = sqlite3.connect(db.db_path)
    rows = raw.execute(
        "SELECT COUNT(*) FROM contacts WHERE email = 'race@brand.de'"
    ).fetchone()[0]
    raw.close()

    assert errors == [], f"add_contact must not raise under contention: {errors}"
    assert rows == 1, f"expected exactly 1 contact row, got {rows}"
    assert len([r for r in results if r]) == 1, "exactly one caller may get an id"
    assert len(db.get_pending_contacts()) == 1


def test_case_and_whitespace_variants_are_the_same_contact(db):
    sid = db.log_search("q", source="test")
    first = db.add_contact(sid, "Person@Brand.de")
    assert first is not None
    assert db.add_contact(sid, "  PERSON@brand.de  ") is None


def test_migration_merges_duplicates_and_repoints_children(tmp_path):
    """An existing dirty DB must survive the upgrade with history intact.

    Duplicates are referenced by email_log / retry_queue / sequence_enrollments
    with foreign keys ON, so they cannot simply be deleted - the children have
    to be repointed onto the surviving row first.
    """
    path = str(tmp_path / "dirty.db")
    db = Database(path)
    sid = db.log_search("q", source="test")

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys=ON")
    for variant in ("Dupe@Brand.de", "dupe@brand.de", " DUPE@brand.de "):
        raw.execute(
            "INSERT INTO contacts (search_id, email) VALUES (?, ?)", (sid, variant)
        )
    raw.commit()
    ids = [r[0] for r in raw.execute("SELECT id FROM contacts ORDER BY id")]
    raw.execute(
        "INSERT INTO email_log (contact_id, recipient_email, subject) VALUES (?,?,?)",
        (ids[1], "dupe@brand.de", "sent earlier"),
    )
    raw.commit()
    raw.close()

    Database(path)  # re-open -> runs the migration

    raw = sqlite3.connect(path)
    remaining = raw.execute(
        "SELECT COUNT(*) FROM contacts WHERE email LIKE '%dupe@brand.de%'"
    ).fetchone()[0]
    orphans = raw.execute(
        "SELECT COUNT(*) FROM email_log "
        "WHERE contact_id NOT IN (SELECT id FROM contacts)"
    ).fetchone()[0]
    history = raw.execute("SELECT COUNT(*) FROM email_log").fetchone()[0]
    violations = len(raw.execute("PRAGMA foreign_key_check").fetchall())
    raw.close()

    assert remaining == 1, "duplicates must be merged onto one keeper"
    assert orphans == 0, "child rows must be repointed, never orphaned"
    assert history == 1, "send history must be preserved"
    assert violations == 0


# ── 2. Retry queue: idempotency + claim-on-read ──────────────────────────────


@pytest.fixture
def rq(db):
    return RetryQueue(db, max_retries=3, base_delay_minutes=5.0)


def _contact(db):
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "retry@brand.de")
    log_id = db.log_email(cid, "retry@brand.de", "subj")
    return cid, log_id


def test_second_retry_for_the_same_contact_does_not_create_a_row(db, rq):
    """A failing retry re-entered the failure branch and queued another row at
    attempt=0, so max_attempts never terminated the family - and every row was
    a real send to the same recipient."""
    cid, log_id = _contact(db)

    first = rq.add_to_retry(log_id, cid, "smtp timeout")
    second = rq.add_to_retry(log_id, cid, "smtp timeout again")

    raw = sqlite3.connect(db.db_path)
    rows = raw.execute(
        "SELECT COUNT(*) FROM retry_queue WHERE contact_id = ?", (cid,)
    ).fetchone()[0]
    raw.close()

    assert rows == 1, f"one open retry per contact, got {rows}"
    assert first == second, "the existing row id should be returned"


def test_get_due_retries_claims_the_rows(db, rq):
    """Rows were handed out still 'pending' and only updated after the send, so
    a crash in between meant the next pass sent the same email again."""
    cid, log_id = _contact(db)
    rq.add_to_retry(log_id, cid, "err")

    raw = sqlite3.connect(db.db_path)
    raw.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    raw.commit()
    raw.close()

    first_pass = rq.get_due_retries()
    assert len(first_pass) == 1

    # Simulate the process dying here, before any mark_* call.
    second_pass = rq.get_due_retries()
    assert second_pass == [], "a claimed row must not be handed out twice"


def test_stale_claims_are_recovered(db, rq):
    """A crash must cost a delayed retry, not a lost one."""
    cid, log_id = _contact(db)
    rq.add_to_retry(log_id, cid, "err")

    raw = sqlite3.connect(db.db_path)
    raw.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    raw.commit()
    raw.close()

    rq.get_due_retries()  # claim, then "crash"
    assert rq.get_due_retries() == []

    recovered = rq.reset_stale_in_flight(older_than_minutes=1)
    assert recovered == 1
    assert len(rq.get_due_retries()) == 1


def test_in_flight_rows_still_count_as_outstanding(db, rq):
    cid, log_id = _contact(db)
    rq.add_to_retry(log_id, cid, "err")
    raw = sqlite3.connect(db.db_path)
    raw.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    raw.commit()
    raw.close()

    rq.get_due_retries()
    assert rq.get_retry_stats()["pending"] == 1, "claimed work is still outstanding"


def test_marking_a_claimed_row_succeeded_still_works(db, rq):
    cid, log_id = _contact(db)
    rq.add_to_retry(log_id, cid, "err")
    raw = sqlite3.connect(db.db_path)
    raw.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    raw.commit()
    raw.close()

    claimed = rq.get_due_retries()
    rq.mark_retry_success(claimed[0]["id"])

    raw = sqlite3.connect(db.db_path)
    status = raw.execute(
        "SELECT status FROM retry_queue WHERE id = ?", (claimed[0]["id"],)
    ).fetchone()[0]
    raw.close()
    assert status == "succeeded"


# ── 3. Verification verdict must not be lost to a bookkeeping failure ────────
# The discard `continue` sat inside the try, after update_contact_verification.
# If that write raised (locked DB), the verdict was never evaluated and an
# address with no MX record fell through and was emailed - a guaranteed hard
# bounce, logged only as "Verification failed", which reads as "we skipped it".


def test_unverified_contact_is_discarded_even_if_the_db_write_fails(
    tmp_path, settings_factory
):
    from agent import OutreachAgent

    s = settings_factory(db_path=str(tmp_path / "v.db"), verify_mx=True)
    agent = OutreachAgent(s)

    class _Verifier:
        def verify_email(self, email, skip_smtp=True):
            return {"valid": False, "reason": "no MX record"}

    agent.email_verifier = _Verifier()
    # _qualify_contacts breaks out of its loop when _running is false, so this
    # must be armed or the test passes vacuously without reaching the code.
    agent._running = True

    # Bookkeeping fails exactly as a locked DB would.
    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    agent.db.update_contact_verification = _boom

    sid = agent.db.log_search("q", source="test")
    cid = agent.db.add_contact(sid, "nomx@brand.de")
    contacts = [{"id": cid, "email": "nomx@brand.de", "name": "X", "lead_score": 90}]

    # Control: the same contact WITHOUT a failing verifier must qualify, proving
    # nothing else in the pipeline is silently rejecting it.
    control_agent = OutreachAgent(s)
    control_agent._running = True
    control_agent.email_verifier = None
    assert control_agent._qualify_contacts(
        [{"id": cid, "email": "nomx@brand.de", "name": "X", "lead_score": 90}]
    ), "control must qualify, otherwise this test proves nothing"

    qualified = agent._qualify_contacts(contacts)

    assert qualified == [], (
        "an address that failed verification must never reach the send list, "
        "even when recording the verdict fails"
    )


# ── 4. The same defect in the other three discard paths ─────────────────────
# _qualify_contacts had four places that decide "drop this contact" and then
# record the decision. Verification (above) was fixed in Batch 2; the low-score,
# AI-irrelevant and heuristic-irrelevant paths had the identical shape - the
# `continue` sat after the DB write inside the try, so a locked DB skipped the
# discard and the rejected contact fell through to the send list with its
# skipped-counter already incremented. The counter said skipped; the mailbox
# said sent.


def _boom(*a, **kw):
    raise sqlite3.OperationalError("database is locked")


def _agent_with_one_contact(tmp_path, settings_factory, name, **overrides):
    from agent import OutreachAgent

    s = settings_factory(db_path=str(tmp_path / f"{name}.db"), **overrides)
    agent = OutreachAgent(s)
    agent._running = True
    agent.email_verifier = None
    sid = agent.db.log_search("q", source="test")
    cid = agent.db.add_contact(sid, f"{name}@brand.de")
    return agent, cid


def _one(cid, name):
    return [{"id": cid, "email": f"{name}@brand.de", "name": "X"}]


def test_low_score_contact_is_discarded_even_if_the_db_write_fails(
    tmp_path, settings_factory
):
    agent, cid = _agent_with_one_contact(
        tmp_path, settings_factory, "lowscore", min_lead_score_to_email=50
    )

    class _Scorer:
        def score_contact(self, contact):
            return {"score": 1, "tier": "D"}

    agent.lead_scorer = _Scorer()
    agent.relevance_filter = None
    agent.db.update_contact_status = _boom

    # Control: the identical contact with a passing score must qualify, so a
    # `[]` result below is attributable to the score and nothing else.
    control, ccid = _agent_with_one_contact(
        tmp_path, settings_factory, "lowscore_ok", min_lead_score_to_email=50
    )

    class _GoodScorer:
        def score_contact(self, contact):
            return {"score": 99, "tier": "A"}

    control.lead_scorer = _GoodScorer()
    control.relevance_filter = None
    assert control._qualify_contacts(_one(ccid, "lowscore_ok")), (
        "control must qualify, otherwise this test proves nothing"
    )

    assert agent._qualify_contacts(_one(cid, "lowscore")) == [], (
        "a below-threshold lead must never reach the send list, even when "
        "recording the verdict fails"
    )


def test_ai_rejected_contact_is_discarded_even_if_the_db_write_fails(
    tmp_path, settings_factory
):
    agent, cid = _agent_with_one_contact(
        tmp_path, settings_factory, "airej", ai_relevance_enabled=True
    )
    agent.lead_scorer = None
    agent.relevance_filter = None

    class _AI:
        is_configured = True

        def classify_relevance(self, contact):
            return False

        def classify_company_country(self, contact):
            return None

    agent.ai_gen = _AI()
    agent.db.update_contact_status = _boom

    control, ccid = _agent_with_one_contact(
        tmp_path, settings_factory, "airej_ok", ai_relevance_enabled=True
    )
    control.lead_scorer = None
    control.relevance_filter = None

    class _AIYes(_AI):
        def classify_relevance(self, contact):
            return True

    control.ai_gen = _AIYes()
    assert control._qualify_contacts(_one(ccid, "airej_ok")), (
        "control must qualify, otherwise this test proves nothing"
    )

    assert agent._qualify_contacts(_one(cid, "airej")) == [], (
        "a contact the AI classified off-target must never reach the send list, "
        "even when recording the verdict fails"
    )


def test_irrelevant_contact_is_discarded_even_if_the_db_write_fails(
    tmp_path, settings_factory
):
    agent, cid = _agent_with_one_contact(tmp_path, settings_factory, "irrel")
    agent.lead_scorer = None

    class _Filter:
        def assess(self, contact):
            return {"relevant": False, "reason": "off-target"}

    agent.relevance_filter = _Filter()
    agent.db.update_contact_status = _boom

    control, ccid = _agent_with_one_contact(tmp_path, settings_factory, "irrel_ok")
    control.lead_scorer = None
    control.relevance_filter = None
    assert control._qualify_contacts(_one(ccid, "irrel_ok")), (
        "control must qualify, otherwise this test proves nothing"
    )

    assert agent._qualify_contacts(_one(cid, "irrel")) == [], (
        "a contact the relevance filter rejected must never reach the send "
        "list, even when recording the verdict fails"
    )
