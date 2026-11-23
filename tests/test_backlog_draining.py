"""Unit tests for BacklogDrainer (modules/backlog_draining.py).

Verifies:
- Pending contact backlog draining (process_pending_contacts): fast-path partitioning (already verified contacts skip AI/MX verification).
- Qualification pipeline (qualify_contacts): scoring, screening, heuristic relevance, and GDPR consent logging.
- Follow-up cadence draining (process_sequences): due step processing, advancing enrollment, and closed contact gating.
- Retry queue draining (process_retries): recovering in-flight leases, preventing duplicate sends, and backoff scheduling.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from modules.backlog_draining import BacklogDrainer
from modules.database import Database
from modules.dispatch_pipeline import DispatchPipeline


class DummySender:
    def __init__(self, key: str = "main", name: str = "Test Sender"):
        self.key = key
        self.sender_name = name
        self.closed = False

    def close(self):
        self.closed = True

    def dispatch_to_contact(self, *args, **kwargs):
        return 1


class DummyAgent:
    """Agent dummy providing required methods and state for BacklogDrainer."""

    def __init__(self, db: Database, settings: Any):
        self.db = db
        self.settings = settings
        self._running = True
        self._last_dispatch = 0
        self._sending_paused = False
        self._pause_reason = None
        self._guard_logged: dict[str, bool] = {}
        self._email_template = "<html><body>{{legal_footer}}</body></html>"
        self._email_template_it = None
        self._stats: dict[str, int] = {
            "scored": 0,
            "verified": 0,
            "low_score_skipped": 0,
            "client_skipped": 0,
            "competitor_skipped": 0,
            "irrelevant_skipped": 0,
            "unverified_skipped": 0,
            "emails_sent": 0,
            "emails_failed": 0,
        }
        self.sender = DummySender("acct1", "Sender 1")
        self.senders = [("acct1", "Sender 1", self.sender)]
        self.email_sender = self.sender
        self.warmup = None
        self.domain_throttle = None
        self.retry_queue = None
        self.sequences = None
        self.file_handler = MagicMock()
        self.file_handler.get_email_template.return_value = (
            "<html><body>Followup {{legal_footer}}</body></html>"
        )
        self.lead_scorer = None
        self.relevance_filter = None
        self.ai_gen = None
        self.email_verifier = None
        self.client_register = None
        self.competitor_register = None

        self.drainer = BacklogDrainer(self)

        # Tracing hooks
        self.emailed_contacts: list[dict] = []
        self.qualify_calls: list[list[dict]] = []

    def _email_contacts(self, qualified: list[dict], dry_run: bool = False):
        self.emailed_contacts.extend(qualified)

    def _qualify_contacts(self, contacts: list[dict]) -> list[dict]:
        self.qualify_calls.append(contacts)
        return self.drainer.qualify_contacts(contacts)

    def _process_pending_contacts(self, dry_run: bool = False):
        return self.drainer.process_pending_contacts(dry_run)

    def _process_sequences(self, dry_run: bool = False):
        return self.drainer.process_sequences(dry_run)

    def _process_retries(self):
        return self.drainer.process_retries()

    def _load_followup_template(self, name: str | None) -> str:
        return self.drainer.load_followup_template(name or "")

    def _active_client(self, org: str | None) -> str | None:
        if org and "ClientCorp" in org:
            return org
        return None

    def _known_competitor(self, org: str | None) -> str | None:
        if org and "RivalCorp" in org:
            return org
        return None

    def _contact_is_closed(self, contact: dict) -> str | None:
        email = contact.get("email", "")
        if self.db.is_suppressed(email):
            return "suppressed"
        status = contact.get("status")
        if status in ("responded", "active_client", "unsubscribed", "bounced"):
            return status
        return None

    def _send_blocker(self, dry_run: bool) -> str | None:
        return None

    def _first_email_blocker(self, dry_run: bool, sender=None) -> str | None:
        return None

    def _within_send_window(self) -> bool:
        return True

    def _bounce_guard_tripped(self) -> bool:
        return False

    def _blacklist_guard_tripped(self) -> bool:
        return False

    def _screen_contact(self, contact: dict):
        # The real screens, run against this stand-in's registers.
        return DispatchPipeline(self).screen_contact(contact)

    def _send_possible_now(self) -> bool:
        return not self._sending_paused and self._within_send_window()

    def _send_verdict(self, contact: dict, *, purpose: str, dry_run: bool):
        # The real shared check, run against this stand-in's guards.
        return DispatchPipeline(self).send_verdict(
            contact, purpose=purpose, dry_run=dry_run
        )

    def _pick_sender(self):
        return "acct1", "Sender 1", self.sender

    def _refresh_domain_backoff(self):
        pass

    def _domain_backed_off(self, email: str):
        return None

    def _interruptible_sleep(self, seconds: float):
        pass

    def _send_delay(self) -> float:
        return 0.0

    def _process_contact(self, *args, **kwargs) -> bool:
        return True


@pytest.fixture
def drainer_fixture(tmp_path, settings_factory):
    test_db = Database(str(tmp_path / "backlog_test.db"))
    st = settings_factory(
        min_lead_score_to_email=30,
        verify_mx=False,
        verify_emails=False,
    )
    agent = DummyAgent(test_db, st)
    return agent, agent.drainer


# ---------------------------------------------------------------------------
# 1. Pending contact backlog draining (process_pending_contacts)
# ---------------------------------------------------------------------------


def test_process_pending_contacts_fast_path_skips_qualify_for_verified_leads(
    drainer_fixture,
):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("pharma query")

    # Lead 1 is verified
    cid1 = agent.db.add_contact(
        sid, "verified@pharma.com", name="Alice", organization="Alpha"
    )
    agent.db.update_contact_verification(cid1, is_verified=True, details="MX valid")

    # Lead 2 is not verified
    agent.db.add_contact(sid, "unverified@pharma.com", name="Bob", organization="Beta")

    drainer.process_pending_contacts(dry_run=False)

    # Lead 1 went via fast-path; Lead 2 had to be qualified
    assert len(agent.qualify_calls) == 1
    qualified_arg_emails = [c["email"] for c in agent.qualify_calls[0]]
    assert "unverified@pharma.com" in qualified_arg_emails
    assert "verified@pharma.com" not in qualified_arg_emails

    # Both ended up in email dispatch
    emailed_emails = [c["email"] for c in agent.emailed_contacts]
    assert "verified@pharma.com" in emailed_emails
    assert "unverified@pharma.com" in emailed_emails


def test_process_pending_contacts_screens_active_client_in_fast_path(drainer_fixture):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("test")

    cid = agent.db.add_contact(
        sid, "client@clientcorp.com", organization="ClientCorp S.r.l."
    )
    agent.db.update_contact_verification(cid, is_verified=True, details="OK")

    drainer.process_pending_contacts(dry_run=False)

    # Must be skipped and marked active_client
    emailed_emails = [c["email"] for c in agent.emailed_contacts]
    assert "client@clientcorp.com" not in emailed_emails

    contact = agent.db.get_contact_by_id(cid)
    assert contact["status"] == "active_client"
    assert agent._stats["client_skipped"] >= 1


def test_process_pending_contacts_screens_irrelevant_via_filter_in_fast_path(
    drainer_fixture,
):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("test")

    cid = agent.db.add_contact(sid, "wrong@store.com", organization="Pet Store")
    agent.db.update_contact_verification(cid, is_verified=True, details="OK")

    agent.relevance_filter = MagicMock()
    agent.relevance_filter.assess.return_value = {
        "relevant": False,
        "reason": "pet store",
    }

    drainer.process_pending_contacts(dry_run=False)

    emailed_emails = [c["email"] for c in agent.emailed_contacts]
    assert "wrong@store.com" not in emailed_emails

    contact = agent.db.get_contact_by_id(cid)
    assert contact["status"] == "irrelevant"
    assert agent._stats["irrelevant_skipped"] >= 1


def test_an_empty_backlog_writes_nothing(drainer_fixture):
    """The backlog is checked every loop, usually empty. Without the early
    return each check logs "Processing 0 pending contact(s)" to the activity
    log and opens a send batch for nothing. (The original assertion - nothing
    emailed - held either way, so it could not fail.)"""
    agent, drainer = drainer_fixture
    drainer.process_pending_contacts(dry_run=False)
    assert [
        r for r in agent.db.get_recent_activity(20) if r["action"] == "BACKLOG"
    ] == []
    assert agent.emailed_contacts == []


# ---------------------------------------------------------------------------
# 2. Qualification pipeline (qualify_contacts)
# ---------------------------------------------------------------------------


def test_qualify_contacts_scores_and_keeps_the_stored_basis(drainer_fixture):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("test")
    cid = agent.db.add_contact(
        sid, "prospect@pharma.de", name="Hans", organization="Pharma GmbH"
    )

    agent.lead_scorer = MagicMock()
    agent.lead_scorer.score_contact.return_value = {"score": 75, "tier": "B"}

    contact = dict(agent.db.get_contact_by_id(cid))
    qualified = drainer.qualify_contacts([contact])

    assert len(qualified) == 1
    assert qualified[0]["lead_score"] == 75
    assert qualified[0]["lead_tier"] == "B"
    assert agent._stats["scored"] >= 1

    # The basis is on the contact since it was stored; qualification writes
    # no basis record - a live send does (test_each_send_records_its_notice_
    # and_basis), so a dry run can no longer leave one behind.
    consents = agent.db.conn.execute(
        "SELECT * FROM consent_log WHERE email = ?", ("prospect@pharma.de",)
    ).fetchall()
    assert consents == []
    assert agent.db.get_contact_by_id(cid)["basis"] == "legitimate_interest"


def test_qualify_contacts_skips_low_score_contact(drainer_fixture):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("test")
    cid = agent.db.add_contact(sid, "low@generic.com", name="Unknown")

    agent.settings.min_lead_score_to_email = 40
    agent.lead_scorer = MagicMock()
    agent.lead_scorer.score_contact.return_value = {"score": 15, "tier": "D"}

    contact = dict(agent.db.get_contact_by_id(cid))
    qualified = drainer.qualify_contacts([contact])

    assert len(qualified) == 0
    assert agent._stats["low_score_skipped"] >= 1
    updated = agent.db.get_contact_by_id(cid)
    assert updated["status"] == "low_score"


def test_qualify_contacts_screens_competitor(drainer_fixture):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("test")
    cid = agent.db.add_contact(sid, "sales@rivalcorp.com", organization="RivalCorp SpA")

    contact = dict(agent.db.get_contact_by_id(cid))
    qualified = drainer.qualify_contacts([contact])

    assert len(qualified) == 0
    assert agent._stats["irrelevant_skipped"] >= 1
    updated = agent.db.get_contact_by_id(cid)
    assert updated["status"] == "irrelevant"
    assert "competitor" in (updated["discard_reason"] or "")


def test_qualify_contacts_aborts_when_not_running(drainer_fixture):
    agent, drainer = drainer_fixture
    agent._running = False

    contacts = [{"id": 1, "email": "c1@test.com"}, {"id": 2, "email": "c2@test.com"}]
    qualified = drainer.qualify_contacts(contacts)
    assert len(qualified) == 0


# ---------------------------------------------------------------------------
# 3. Follow-up cadence draining (process_sequences)
# ---------------------------------------------------------------------------


def test_process_sequences_advances_enrollment_on_send_success(drainer_fixture):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("test")
    cid = agent.db.add_contact(sid, "followup@partner.com", name="Luca")

    agent.sequences = MagicMock()
    agent.sequences.get_due_followups.return_value = [
        {
            "enrollment_id": 101,
            "contact_id": cid,
            "current_step": 1,
            "subject": "Quick follow-up",
            "template_name": "followup1",
        }
    ]

    drainer.process_sequences(dry_run=False)
    agent.sequences.advance_enrollment.assert_called_once_with(101, 1)


def test_process_sequences_cancels_enrollment_if_contact_is_closed(drainer_fixture):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("test")
    cid = agent.db.add_contact(sid, "unsub@partner.com", name="Luca")
    agent.db.update_contact_status(cid, "unsubscribed")

    agent.sequences = MagicMock()
    agent.sequences.get_due_followups.return_value = [
        {
            "enrollment_id": 202,
            "contact_id": cid,
            "step_number": 2,
            "subject": "Follow-up",
        }
    ]

    drainer.process_sequences(dry_run=False)
    agent.sequences.cancel_enrollment.assert_called_once()
    assert "stopped" in agent.sequences.cancel_enrollment.call_args[0][1]


def test_process_sequences_defers_when_outside_send_window(drainer_fixture):
    """Deferred, not cancelled: the follow-up must still go out tomorrow.

    Uses a real contact. The original used contact id 1 without creating it,
    so with the window check deleted the loop cancelled the enrollment as
    "contact not found" and still never advanced it - the test passed
    against the very defect it names.
    """
    agent, drainer = drainer_fixture
    agent._within_send_window = lambda: False
    sid = agent.db.log_search("test")
    cid = agent.db.add_contact(sid, "followup@partner.com", name="Luca")

    agent.sequences = MagicMock()
    agent.sequences.get_due_followups.return_value = [
        {"enrollment_id": 303, "contact_id": cid, "subject": "s"}
    ]

    drainer.process_sequences(dry_run=False)
    agent.sequences.advance_enrollment.assert_not_called()
    agent.sequences.cancel_enrollment.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Retry queue draining (process_retries)
# ---------------------------------------------------------------------------


def test_process_retries_resets_stale_in_flight_and_marks_success(drainer_fixture):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("test")
    cid = agent.db.add_contact(sid, "retry_target@nutra.com", name="Target")

    agent.retry_queue = MagicMock()
    agent.retry_queue.get_due_retries.return_value = [
        {
            "id": 55,
            "contact_id": cid,
            "attempt": 1,
        }
    ]

    drainer.process_retries()

    agent.retry_queue.reset_stale_in_flight.assert_called_once()
    agent.retry_queue.mark_retry_success.assert_called_once_with(55)


def test_process_retries_cancels_retry_for_already_emailed_contact(drainer_fixture):
    agent, drainer = drainer_fixture
    sid = agent.db.log_search("test")
    cid = agent.db.add_contact(sid, "already_done@nutra.com", name="Target")

    # Contact was already emailed in database
    agent.db.log_email(cid, "already_done@nutra.com", "Subject", delivery_status="sent")

    agent.retry_queue = MagicMock()
    agent.retry_queue.get_due_retries.return_value = [
        {
            "id": 77,
            "contact_id": cid,
            "attempt": 0,
        }
    ]

    drainer.process_retries()
    agent.retry_queue.cancel_retry.assert_called_once_with(77, "already emailed")


def test_process_retries_releases_unresolved_retries_on_interrupt(drainer_fixture):
    agent, drainer = drainer_fixture
    agent.retry_queue = MagicMock()
    agent.retry_queue.get_due_retries.return_value = [
        {"id": 10, "contact_id": 1, "attempt": 0},
        {"id": 20, "contact_id": 2, "attempt": 0},
    ]

    # Stop running immediately
    agent._running = False
    drainer.process_retries()

    # Both unhandled retries must be released back to pending
    agent.retry_queue.release.assert_called_once_with([10, 20])
