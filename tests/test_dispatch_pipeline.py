"""Unit tests for DispatchPipeline (modules/dispatch_pipeline.py).

Verifies:
- Lead score priority ordering (by_lead_score descending sort with None handling and stable preservation).
- Pacing delay (send_delay): respects emails_per_hour floor and adds non-negative jitter.
- Sender rotation (pick_sender): round-robin cycling across active sender accounts, honoring warmup daily allowances.
- Address screening: certified mail (_is_certified_mail blocking .pec.), consumer webmail domains (PERSONAL_DOMAINS e.g., @gmail.com), and case-insensitive suppression checks.
- Template rendering & compliance: mandatory legal footer rendering, unsubscribe URL signing, and tracking pixel blind mode.
- Direct / manual send (send_single_email).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from modules.clients import ClientRegister
from modules.database import Database
from modules.dispatch_pipeline import DispatchPipeline
from modules.file_handler import FileHandler
from modules.search import PERSONAL_DOMAINS


class DummySender:
    def __init__(self, key: str = "main", name: str = "Test Sender"):
        self.key = key
        self.sender_name = name
        self.auth_failed = False
        self.closed = False

    def close(self):
        self.closed = True

    def dispatch_to_contact(self, *args, **kwargs):
        return 1

    def send_email(self, *args, **kwargs):
        return True, "250 OK"


class DummyWarmup:
    def __init__(self, allowed_accounts: set[str] | None = None):
        self.allowed = allowed_accounts

    def can_send(self, account: str = "") -> bool:
        if self.allowed is None:
            return True
        return account in self.allowed

    def record_send(self, account: str = "") -> None:
        pass


class DummyAgent:
    """Minimal agent dummy providing required attributes for DispatchPipeline."""

    def __init__(self, db: Database, settings: Any):
        self.db = db
        self.settings = settings
        self._rr = 0
        self._running = True
        self._last_dispatch = 0
        self._sending_paused = False
        self._pause_reason = None
        self._sending_domain = "suppleco.com"
        self._pdf_path = None
        self._guard_logged: dict[str, bool] = {}
        self._domain_backoff: dict[str, dict] = {}
        self._stats: dict[str, int] = {
            "sent": 0,
            "failed": 0,
            "emails_sent": 0,
            "emails_failed": 0,
            "dry_run": 0,
            "warmup_skipped": 0,
            "domain_skipped": 0,
            "retries_queued": 0,
        }
        self.sender_dummy = DummySender("acct1", "Sender 1")
        self.senders = [
            ("acct1", "Sender 1", self.sender_dummy),
            ("acct2", "Sender 2", DummySender("acct2", "Sender 2")),
        ]
        self._account_keys = ["acct1", "acct2"]
        self.email_sender = self.sender_dummy
        self.warmup = DummyWarmup()
        self.domain_throttle = None
        self.retry_queue = None
        self.analytics = None
        self.ai_gen = None
        self.unsubscribe_handler = None
        self.sequences = None
        # A loaded list naming one unrelated company: it screens no contact
        # here. An empty or missing list stops live sends
        # (test_an_empty_client_list_refuses_live_sends.py).
        self.client_register = ClientRegister(["Zeta Unrelated Holding S.p.A."])
        self.competitor_register = None
        self._list_stamps: dict[str, Any] = {}
        self._list_failed: dict[str, Any] = {}
        self._template_variants: list[Any] = []
        self._subject_variants: list[str] = ["Partner with {{company_name}}"]
        self._email_template = "<html><body><p>Hello {{name}}</p>{{legal_footer}}{{unsubscribe_url}}{{tracking_pixel}}</body></html>"
        # No Italian first email: every recipient here gets the one above.
        self._email_template_it = None
        # Resolves PDF_PATH, which the first email attaches.
        self.file_handler = FileHandler()
        self.pipeline = DispatchPipeline(self)

    # Agent delegates matching OutreachAgent facade
    def _by_lead_score(self, contacts: list[dict]) -> list[dict]:
        return self.pipeline.by_lead_score(contacts)

    def _send_delay(self) -> float:
        return self.pipeline.send_delay()

    def _send_blocker(self, dry_run: bool) -> str | None:
        return self.pipeline.send_blocker(dry_run)

    def _first_email_blocker(self, dry_run: bool, sender=None) -> str | None:
        return self.pipeline.first_email_blocker(dry_run, sender=sender)

    def _refresh_domain_backoff(self) -> None:
        self.pipeline.refresh_domain_backoff()

    def _domain_backed_off(self, email: str) -> dict | None:
        return self.pipeline.domain_backed_off(email)

    def _pick_sender(self):
        return self.pipeline.pick_sender()

    def _email_contacts(self, qualified: list[dict], dry_run: bool = False):
        return self.pipeline.email_contacts(qualified, dry_run)

    def _process_contact(self, *args, **kwargs) -> bool:
        return self.pipeline.process_contact(*args, **kwargs)

    def _screen_contact(self, contact: dict):
        return self.pipeline.screen_contact(contact)

    def _send_verdict(self, contact: dict, *, purpose: str, dry_run: bool):
        return self.pipeline.send_verdict(contact, purpose=purpose, dry_run=dry_run)

    def send_single_email(self, *args, **kwargs):
        return self.pipeline.send_single_email(*args, **kwargs)

    def _is_public_base_url(self) -> bool:
        return self.pipeline.is_public_base_url()

    def _unsubscribe_target(self, email: str) -> str:
        return self.pipeline.unsubscribe_target(email)

    def _build_greeting(self, contact: dict, language: str = "en") -> str:
        return self.pipeline._build_greeting(contact, language)

    def _build_email_body(self, contact: dict) -> str:
        return self.pipeline.build_email_body(contact)

    def _build_opening_line(self, contact: dict) -> str:
        return self.pipeline.build_opening_line(contact)

    def _within_send_window(self) -> bool:
        return True

    def _bounce_guard_tripped(self) -> bool:
        return False

    def _blacklist_guard_tripped(self) -> bool:
        return False

    def _active_client(self, org: str | None) -> str | None:
        return None

    def _known_competitor(self, org: str | None) -> str | None:
        return None

    def _interruptible_sleep(self, seconds: float) -> None:
        pass

    def refresh_lists_if_changed(self) -> None:
        pass

    def refresh_templates_if_changed(self) -> None:
        pass


@pytest.fixture
def agent_fixture(tmp_path, settings_factory):
    test_db = Database(str(tmp_path / "dispatch_test.db"))
    st = settings_factory(
        delay_between_emails=5.0,
        emails_per_hour=0,
        send_jitter_seconds=0.0,
        base_url="https://outreach.suppleco.com",
    )
    agent = DummyAgent(test_db, st)
    return agent, agent.pipeline


# ---------------------------------------------------------------------------
# 1. Lead score priority ordering (by_lead_score)
# ---------------------------------------------------------------------------


def test_by_lead_score_orders_descending():
    contacts = [
        {"email": "c1@test.com", "lead_score": 20},
        {"email": "c2@test.com", "lead_score": 95},
        {"email": "c3@test.com", "lead_score": 50},
    ]
    sorted_contacts = DispatchPipeline.by_lead_score(contacts)
    scores = [c["lead_score"] for c in sorted_contacts]
    assert scores == [95, 50, 20]


def test_by_lead_score_handles_none_and_missing_scores():
    contacts = [
        {"email": "c_none@test.com", "lead_score": None},
        {"email": "c_high@test.com", "lead_score": 80},
        {"email": "c_missing@test.com"},
        {"email": "c_low@test.com", "lead_score": 10},
    ]
    sorted_contacts = DispatchPipeline.by_lead_score(contacts)
    emails = [c["email"] for c in sorted_contacts]
    assert emails[0] == "c_high@test.com"
    assert emails[1] == "c_low@test.com"
    # c_none and c_missing both treat missing/None as 0
    assert set(emails[2:]) == {"c_none@test.com", "c_missing@test.com"}


def test_by_lead_score_preserves_stable_order_for_equal_scores():
    contacts = [
        {"email": "first@test.com", "lead_score": 50, "order": 1},
        {"email": "second@test.com", "lead_score": 50, "order": 2},
        {"email": "third@test.com", "lead_score": 50, "order": 3},
    ]
    sorted_contacts = DispatchPipeline.by_lead_score(contacts)
    assert [c["order"] for c in sorted_contacts] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 2. Pacing delay (send_delay)
# ---------------------------------------------------------------------------


def test_send_delay_returns_base_delay_when_no_hourly_cap(agent_fixture):
    agent, pipeline = agent_fixture
    agent.settings.delay_between_emails = 3.5
    agent.settings.emails_per_hour = 0
    agent.settings.send_jitter_seconds = 0.0
    assert pipeline.send_delay() == 3.5


def test_send_delay_respects_emails_per_hour_floor(agent_fixture):
    agent, pipeline = agent_fixture
    agent.settings.delay_between_emails = 2.0
    agent.settings.emails_per_hour = 60  # 3600 / 60 = 60.0s floor
    agent.settings.send_jitter_seconds = 0.0
    assert pipeline.send_delay() == 60.0


def test_send_delay_adds_non_negative_jitter(agent_fixture):
    agent, pipeline = agent_fixture
    agent.settings.delay_between_emails = 10.0
    agent.settings.emails_per_hour = 0
    agent.settings.send_jitter_seconds = 5.0

    for _ in range(20):
        delay = pipeline.send_delay()
        assert 10.0 <= delay <= 15.0


# ---------------------------------------------------------------------------
# 3. Sender rotation (pick_sender)
# ---------------------------------------------------------------------------


def test_pick_sender_cycles_round_robin_across_active_accounts(agent_fixture):
    agent, pipeline = agent_fixture
    agent.senders = [
        ("acct1", "Sender 1", DummySender("acct1")),
        ("acct2", "Sender 2", DummySender("acct2")),
        ("acct3", "Sender 3", DummySender("acct3")),
    ]
    agent._rr = 0

    pick1 = pipeline.pick_sender()
    assert pick1 is not None and pick1[0] == "acct1"
    assert agent._rr == 1

    pick2 = pipeline.pick_sender()
    assert pick2 is not None and pick2[0] == "acct2"
    assert agent._rr == 2

    pick3 = pipeline.pick_sender()
    assert pick3 is not None and pick3[0] == "acct3"
    assert agent._rr == 0

    pick4 = pipeline.pick_sender()
    assert pick4 is not None and pick4[0] == "acct1"
    assert agent._rr == 1


def test_pick_sender_skips_accounts_exceeding_warmup_allowance(agent_fixture):
    agent, pipeline = agent_fixture
    agent.senders = [
        ("acct1", "Sender 1", DummySender("acct1")),
        ("acct2", "Sender 2", DummySender("acct2")),
    ]
    # acct1 is capped, acct2 can send
    agent.warmup = DummyWarmup(allowed_accounts={"acct2"})
    agent._rr = 0

    pick = pipeline.pick_sender()
    assert pick is not None and pick[0] == "acct2"


def test_pick_sender_returns_none_when_all_accounts_capped(agent_fixture):
    agent, pipeline = agent_fixture
    agent.senders = [
        ("acct1", "Sender 1", DummySender("acct1")),
        ("acct2", "Sender 2", DummySender("acct2")),
    ]
    # No accounts allowed
    agent.warmup = DummyWarmup(allowed_accounts=set())
    agent._rr = 0

    assert pipeline.pick_sender() is None


# ---------------------------------------------------------------------------
# 4. Address screening
# ---------------------------------------------------------------------------


def test_address_screening_blocks_certified_mail(agent_fixture):
    agent, pipeline = agent_fixture
    pec_contact = {"email": "ufficio@pec.aziendasanitaria.it", "name": "Direttore"}

    # Live attempt
    res = pipeline.process_contact(pec_contact, dry_run=False)
    assert res is False

    # Verify activity log
    logs = agent.db.get_recent_activity(10)
    assert any(
        l["action"] == "ADDRESS_SCREENED" and "certified" in l["details"] for l in logs
    )


def test_address_screening_blocks_personal_webmail(agent_fixture):
    agent, pipeline = agent_fixture
    assert "gmail.com" in PERSONAL_DOMAINS

    gmail_contact = {"email": "private_doctor@gmail.com", "name": "Dr. Smith"}
    res = pipeline.process_contact(gmail_contact, dry_run=False)
    assert res is False

    logs = agent.db.get_recent_activity(10)
    assert any(
        l["action"] == "ADDRESS_SCREENED" and "personal" in l["details"] for l in logs
    )


def test_address_screening_suppression_is_case_insensitive(agent_fixture):
    agent, pipeline = agent_fixture
    agent.db.add_to_suppression("DoNotEmail@Partner.it", reason="opted_out")

    suppressed_contact = {"email": "DONOTEMAIL@PARTNER.IT", "name": "Partner"}
    res = pipeline.process_contact(suppressed_contact, dry_run=False)
    assert res is False

    logs = agent.db.get_recent_activity(10)
    assert any(l["action"] == "SUPPRESSED" for l in logs)


# ---------------------------------------------------------------------------
# 5. Template rendering & compliance
# ---------------------------------------------------------------------------


def test_process_contact_refuses_live_send_if_legal_footer_missing_from_template(
    agent_fixture,
):
    agent, pipeline = agent_fixture
    # Template without {{legal_footer}} placeholder
    agent._email_template = "<html><body><p>Hello {{name}}</p></body></html>"

    contact = {
        "email": "buyer@valid-pharma.de",
        "name": "Buyer",
        "basis": "legitimate_interest",
    }
    res = pipeline.process_contact(contact, dry_run=False)
    assert res is False

    logs = agent.db.get_recent_activity(10)
    assert any(l["action"] == "LEGAL_FOOTER_MISSING" for l in logs)


def test_unsubscribe_target_signs_url_when_base_url_is_public(agent_fixture):
    agent, pipeline = agent_fixture
    agent.settings.base_url = "https://outreach.suppleco.com"
    agent.unsubscribe_handler = MagicMock()
    agent.unsubscribe_handler.generate_unsubscribe_url.return_value = (
        "https://outreach.suppleco.com/unsubscribe?email=test@a.de&token=valid_hmac"
    )

    url = pipeline.unsubscribe_target("test@a.de")
    assert "token=valid_hmac" in url
    agent.unsubscribe_handler.generate_unsubscribe_url.assert_called_once()


def test_unsubscribe_target_falls_back_to_mailto_when_base_url_is_local(agent_fixture):
    agent, pipeline = agent_fixture
    agent.settings.base_url = "http://localhost:5000"
    agent.settings.smtp_username = "outreach@suppleco.com"

    url = pipeline.unsubscribe_target("test@a.de")
    assert url.startswith("mailto:outreach@suppleco.com?subject=Unsubscribe")


@pytest.mark.parametrize(
    "tracking_on, base_url",
    [(False, "https://outreach.suppleco.com"), (True, "http://127.0.0.1:5000")],
    ids=["tracking-off", "local-base-url"],
)
def test_tracking_pixel_omitted_when_open_tracking_disabled_or_local(
    agent_fixture, tracking_on, base_url
):
    """Each case removes exactly one condition of the pixel gate; analytics is
    present in both. (The original left analytics unset, so the gate's first
    term short-circuited and the setting it names was never consulted.)"""
    agent, pipeline = agent_fixture
    agent.analytics = MagicMock()
    agent.settings.open_tracking_enabled = tracking_on
    agent.settings.base_url = base_url

    calls = []
    agent.sender_dummy.dispatch_to_contact = lambda *args, **kwargs: (
        calls.append(kwargs) or 1
    )

    contact = {"id": 1, "email": "buyer@valid-pharma.de", "name": "Buyer"}
    res = pipeline.process_contact(contact, dry_run=True)
    assert res is True
    assert len(calls) == 1
    assert calls[0]["tracking_id"] is None
    assert "/track/" not in calls[0]["html_body"]


def test_tracking_pixel_injected_when_open_tracking_enabled_and_public(agent_fixture):
    agent, pipeline = agent_fixture
    agent.settings.open_tracking_enabled = True
    agent.analytics = MagicMock()
    agent.settings.base_url = "https://outreach.suppleco.com"

    calls = []
    agent.sender_dummy.dispatch_to_contact = lambda *args, **kwargs: (
        calls.append(kwargs) or 1
    )

    contact = {
        "id": 1,
        "email": "buyer@valid-pharma.de",
        "name": "Buyer",
        "basis": "legitimate_interest",
    }
    res = pipeline.process_contact(contact, dry_run=False)
    assert res is True
    assert len(calls) == 1
    assert calls[0]["tracking_id"] is not None
    assert f"{agent.settings.base_url}/track/" in calls[0]["html_body"]


# ---------------------------------------------------------------------------
# 6. Direct / manual send (send_single_email)
# ---------------------------------------------------------------------------


def test_send_single_email_validates_malformed_email(agent_fixture):
    _agent, pipeline = agent_fixture
    ok, msg = pipeline.send_single_email("not-an-email")
    assert ok is False
    assert "valid email address" in msg


def test_send_single_email_screens_suppressed_email(agent_fixture):
    agent, pipeline = agent_fixture
    agent.db.add_to_suppression("stop@brand.de", reason="manual_unsubscribe")
    ok, msg = pipeline.send_single_email("stop@brand.de")
    assert ok is False
    assert "suppressed" in msg


def test_send_single_email_screens_pec_address(agent_fixture):
    _agent, pipeline = agent_fixture
    ok, msg = pipeline.send_single_email("legal@cert.postacert.it")
    assert ok is False
    assert "PEC" in msg


def test_send_single_email_screens_personal_mailbox(agent_fixture):
    _agent, pipeline = agent_fixture
    ok, msg = pipeline.send_single_email("someone@yahoo.com")
    assert ok is False
    assert "personal mailbox" in msg


def test_send_single_email_dry_run_success(agent_fixture):
    _agent, pipeline = agent_fixture
    ok, msg = pipeline.send_single_email(
        "buyer@brand-supplements.com",
        name="Laura",
        organization="Brand Supplements",
        dry_run=True,
    )
    assert ok is True
    assert "Draft looks good" in msg


def test_send_single_email_live_success(agent_fixture):
    agent, pipeline = agent_fixture
    ok, msg = pipeline.send_single_email(
        "ceo@nutra-partner.eu",
        name="Marco",
        organization="Nutra Partner",
        role="CEO",
        dry_run=False,
    )
    assert ok is True
    assert "Email sent to" in msg
    # Verify contact record and consent were written to db
    c = agent.db.get_contact_by_email("ceo@nutra-partner.eu")
    assert c is not None
    assert c["name"] == "Marco"
    assert c["organization"] == "Nutra Partner"
