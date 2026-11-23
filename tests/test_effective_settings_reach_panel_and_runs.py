"""Runtime overrides reach the settings panel, validation, Direct Send, single runs and resets.

Covers:
- SEND-WEB-02 (R9, V2): resetting an override in the dashboard reverts the
  running agent's Settings and resyncs consumers such as DomainThrottle on the
  next loop, logging CONFIG_CHANGED for the revert.
- SEND-WEB-04 (R2, R2b, R3, V4c): effective_settings(base, db) is used by
  GET /api/config ('current' and 'settings'), POST /api/config validation,
  GET /api/deliverability, GET /api/domains, and Direct Send (send_agent).
  Combined overrides (such as a send window moved below the .env start hour)
  validate together, falling back key by key when a stored row is invalid.
- MISSED-01 (V4a): standalone single runs (OutreachAgent + run_single) apply
  stored runtime overrides before sending.
"""

import pytest

import config
import main
from agent import OutreachAgent
from modules.database import Database
from modules.email_sender import EmailSender


def _settings(settings_factory, tmp_path, **kw):
    base = {
        "db_path": str(tmp_path / "d.db"),
        "verify_emails": False,
        "send_window_enabled": False,
        "blacklist_check_enabled": False,
        "imap_enabled": False,
        "ai_humanize_emails": False,
        "client_filter_enabled": False,
        "warmup_enabled": False,
        "lead_scoring_enabled": False,
        "attach_profile": False,
        **kw,
    }
    return settings_factory(**base)


@pytest.fixture
def wire(monkeypatch):
    """Every message handed to SMTP (nothing leaves the machine)."""
    sent = []

    def _deliver(self, msg, blocked=None):
        sent.append(msg)
        return True, "250 2.0.0 Ok", False, False

    monkeypatch.setattr(EmailSender, "_deliver", _deliver)
    return sent


# ── SEND-WEB-02: Resetting an override reaches the running agent ───────────


def test_resetting_an_override_reverts_the_running_agent_and_logs_it(
    tmp_path, settings_factory
):
    """R9: raising max_bounce_rate to 0.2 and then resetting it to .env (0.05)
    must revert the running agent on its next loop and log CONFIG_CHANGED."""
    s = _settings(settings_factory, tmp_path, max_bounce_rate=0.05)
    client = main.create_dashboard_app(s).test_client()
    agent = OutreachAgent(s)

    up = client.post("/api/config", json={"settings": {"max_bounce_rate": 0.2}})
    assert up.status_code == 200
    agent._apply_runtime_overrides()
    assert agent.settings.max_bounce_rate == 0.2

    reset = client.post(
        "/api/config", json={"reset": True, "settings": {"max_bounce_rate": "0.05"}}
    )
    assert reset.status_code == 200
    assert reset.get_json()["applied"] == {"max_bounce_rate": 0.05}
    assert Database(s.get_abs_db_path()).get_runtime_overrides() == {}

    agent._apply_runtime_overrides()
    assert agent.settings.max_bounce_rate == 0.05, (
        "the loosened bounce threshold stayed in force after the operator reset it"
    )
    changes = [
        a["details"]
        for a in agent.db.get_recent_activity(10)
        if a["action"] == "CONFIG_CHANGED"
    ]
    assert any("max_bounce_rate: 0.2 -> 0.05" in d for d in changes), changes


def test_resetting_the_domain_cap_reverts_domain_throttle_too(
    tmp_path, settings_factory
):
    """V2: DomainThrottle keeps its own copy of max_per_domain_per_day, so a
    reset must call _resync_settings_consumers and restore the tighter cap."""
    s = _settings(settings_factory, tmp_path, max_per_domain_per_day=3)
    client = main.create_dashboard_app(s).test_client()
    agent = OutreachAgent(s)

    up = client.post(
        "/api/config", json={"settings": {"max_per_domain_per_day": 8}}
    )
    assert up.status_code == 200
    agent._apply_runtime_overrides()
    assert (
        agent.settings.max_per_domain_per_day,
        agent.domain_throttle.max_per_domain_per_day,
    ) == (8, 8)

    # Even when the input box holds the overridden value "8" (what GET /api/config
    # now populates), clicking Reset restores the .env value of 3.
    reset = client.post(
        "/api/config",
        json={"reset": True, "settings": {"max_per_domain_per_day": "8"}},
    )
    assert reset.status_code == 200

    agent._apply_runtime_overrides()
    assert (
        agent.settings.max_per_domain_per_day,
        agent.domain_throttle.max_per_domain_per_day,
    ) == (3, 3)
    assert reset.get_json()["applied"] == {"max_per_domain_per_day": 3}


# ── SEND-WEB-04: Panel GET, POST validation, deliverability, Direct Send ──


def test_config_panel_reports_the_value_in_force_after_a_save(
    tmp_path, settings_factory
):
    """R2: GET /api/config must show the overridden value in 'current' and
    'settings', not the .env value it replaced."""
    s = _settings(settings_factory, tmp_path, max_per_domain_per_day=3)
    client = main.create_dashboard_app(s).test_client()

    saved = client.post(
        "/api/config", json={"settings": {"max_per_domain_per_day": 1}}
    )
    assert saved.status_code == 200

    got = client.get("/api/config").get_json()
    assert "max_per_domain_per_day" in got["overridden"]
    assert got["current"]["max_per_domain_per_day"] == 1
    assert got["settings"]["max_per_domain_per_day"] == 1


def test_a_change_contradicting_a_stored_override_is_refused_on_post(
    tmp_path, settings_factory
):
    """R2b: with send_window_end_hour=12 stored in the DB, posting
    send_window_start_hour=13 must be refused with 400 rather than accepted
    against the .env end hour of 19."""
    s = _settings(
        settings_factory,
        tmp_path,
        send_window_enabled=True,
        send_window_start_hour=8,
        send_window_end_hour=19,
    )
    client = main.create_dashboard_app(s).test_client()

    first = client.post(
        "/api/config", json={"settings": {"send_window_end_hour": 12}}
    )
    assert first.status_code == 200

    second = client.post(
        "/api/config", json={"settings": {"send_window_start_hour": 13}}
    )
    assert second.status_code == 400
    assert Database(s.get_abs_db_path()).get_runtime_overrides() == {
        "send_window_end_hour": 12
    }


def test_moving_both_send_window_bounds_together_succeeds_and_loads(
    tmp_path, settings_factory, caplog
):
    """Validates stored overrides together: moving the window to 5..6 (where
    end=6 is below the .env start=8) or from 8..12 to 13..19 in one save must
    be accepted by POST and loaded cleanly by effective_settings."""
    s = _settings(
        settings_factory,
        tmp_path,
        send_window_enabled=True,
        send_window_start_hour=8,
        send_window_end_hour=19,
    )
    client = main.create_dashboard_app(s).test_client()
    db = Database(s.get_abs_db_path())

    early = client.post(
        "/api/config",
        json={"settings": {"send_window_start_hour": 5, "send_window_end_hour": 6}},
    )
    assert early.status_code == 200, early.get_json()
    eff = config.effective_settings(s, db)
    assert (eff.send_window_start_hour, eff.send_window_end_hour) == (5, 6)
    assert not [r for r in caplog.records if "Rejecting config" in r.getMessage()]

    late = client.post(
        "/api/config",
        json={"settings": {"send_window_start_hour": 13, "send_window_end_hour": 19}},
    )
    assert late.status_code == 200, late.get_json()
    eff2 = config.effective_settings(s, db)
    assert (eff2.send_window_start_hour, eff2.send_window_end_hour) == (13, 19)


def test_direct_send_enforces_a_lowered_domain_cap_without_a_campaign_running(
    tmp_path, settings_factory, wire
):
    """R3: when no campaign is running, Direct Send uses send_agent, which must
    apply runtime overrides before each send."""
    s = _settings(settings_factory, tmp_path, max_per_domain_per_day=3)
    client = main.create_dashboard_app(s).test_client()

    # Warm up send_agent before the config change so a fix that only applies
    # overrides when constructing OutreachAgent still fails here.
    first = client.post(
        "/api/send-manual",
        json={"email": "anna@brand.de", "organization": "Brand", "channel": "referral"},
    )
    assert first.status_code == 200
    assert len(wire) == 1

    r = client.post(
        "/api/config", json={"settings": {"max_per_domain_per_day": 1}}
    )
    assert r.status_code == 200

    for addr in ("bruno@brand.de", "carla@brand.de"):
        resp = client.post(
            "/api/send-manual",
            json={"email": addr, "organization": "Brand", "channel": "referral"},
        )
        assert resp.status_code == 400

    assert [str(m["To"]) for m in wire] == ["anna@brand.de"]


def test_deliverability_and_domains_panels_report_the_overrides_in_force(
    tmp_path, settings_factory, monkeypatch
):
    """V4c: /api/deliverability and /api/domains must read effective_settings,
    not the .env Settings."""
    import dns.resolver

    from modules.domain_throttle import DomainThrottle

    class _NoRecords:
        """The panel's SPF and DMARC lookups find nothing, without a resolver."""

        def resolve(self, *_a, **_k):
            raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "Resolver", _NoRecords)

    s = _settings(
        settings_factory,
        tmp_path,
        smtp_username="",
        bounce_guard_enabled=True,
        max_bounce_rate=0.05,
        bounce_guard_min_sends=2,
        send_window_enabled=True,
        send_window_start_hour=8,
        send_window_end_hour=19,
        max_per_domain_per_day=5,
    )
    db = Database(s.get_abs_db_path())
    DomainThrottle(db, 5).record_domain_send("brand.de")
    sid = db.log_search("q")
    for i in range(3):
        cid = db.add_contact(sid, f"u{i}@brand.de", organization="Brand")
        eid = db.log_email(cid, f"u{i}@brand.de", "Subj", "Body")
        db.update_email_status(eid, "sent")
    db.add_to_suppression("u0@brand.de", "bounce")

    client = main.create_dashboard_app(s).test_client()
    before = client.get("/api/deliverability").get_json()
    assert before["guard"]["tripped"] is True

    saved = client.post(
        "/api/config",
        json={
            "settings": {
                "max_bounce_rate": 0.5,
                "send_window_end_hour": 12,
                "max_per_domain_per_day": 2,
            }
        },
    )
    assert saved.status_code == 200

    deliv = client.get("/api/deliverability").get_json()
    assert deliv["thresholds"]["max_bounce_rate"] == 0.5
    assert deliv["guard"]["tripped"] is False
    assert deliv["send_window"]["end"] == 12

    domains = client.get("/api/domains").get_json()
    assert domains == [
        {"domain": "brand.de", "count": 1, "limit": 2, "remaining": 1}
    ]


# ── MISSED-01: Single runs apply stored runtime overrides ─────────────────


def test_a_standalone_single_run_obeys_a_lowered_domain_cap(
    tmp_path, settings_factory, wire
):
    """V4a: a standalone run_single must apply the dashboard's stored overrides
    before draining contacts, whether the agent was built before or after the
    override was saved."""
    s = _settings(
        settings_factory,
        tmp_path,
        max_per_domain_per_day=5,
        target_queries="supplement distributor",
    )
    client = main.create_dashboard_app(s).test_client()
    agent = OutreachAgent(s)
    agent.search.search_and_extract = lambda q, search_id=None: []

    sid = agent.db.log_search("seed")
    for addr in ("anna@brand.de", "bruno@brand.de", "carla@brand.de"):
        agent.db.add_contact(
            sid, addr, organization="Brand", source_url="https://brand.de"
        )

    r = client.post(
        "/api/config", json={"settings": {"max_per_domain_per_day": 1}}
    )
    assert r.status_code == 200

    agent.run_single(dry_run=False)

    assert agent.settings.max_per_domain_per_day == 1
    assert agent.domain_throttle.max_per_domain_per_day == 1
    assert len(wire) == 1

    # And an agent built after the override was saved starts with it in force,
    # while keeping .env as its base so a later reset still reverts.
    fresh = OutreachAgent(s)
    assert fresh.settings.max_per_domain_per_day == 1
    assert fresh.domain_throttle.max_per_domain_per_day == 1
    client.post(
        "/api/config",
        json={"reset": True, "settings": {"max_per_domain_per_day": "1"}},
    )
    fresh._apply_runtime_overrides()
    assert fresh.settings.max_per_domain_per_day == 5
    assert fresh.domain_throttle.max_per_domain_per_day == 5

