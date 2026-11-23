"""Operator-editable configuration.

Config was read-only because Settings loads from .env at startup and the
running agent holds the object, so a dashboard edit would either not take
effect or need a restart to mean anything. The fix is an allow-list of
operational dials persisted to the DB and re-read by the agent each loop.

The tests that matter here are the ones about what CANNOT be changed.
"""

import sqlite3

import pytest

import main
from config import RUNTIME_EDITABLE, Settings, validate_override
from modules.database import Database


def _client(tmp_path, settings_factory, **overrides):
    s = settings_factory(db_path=str(tmp_path / "cfg.db"), **overrides)
    app = main.create_dashboard_app(s)
    return app.test_client(), Database(str(tmp_path / "cfg.db"))


# ── The allow-list is the security boundary ────────────────────────────────


@pytest.mark.parametrize(
    "field",
    [
        "smtp_password",
        "smtp_username",
        "smtp_host",
        "imap_password",
        "llm_api_key",
        "llm_base_url",
        "llm_provider",
        "unsubscribe_secret",
        "dashboard_password",
        "db_path",
        "base_url",
    ],
)
def test_credentials_and_plumbing_are_not_runtime_editable(field):
    """An allow-list, not a deny-list: a field is uneditable until someone puts
    it on the list deliberately, so a newly-added Settings field is never
    remotely writable by accident. A dashboard write path to SMTP credentials
    turns one XSS into account takeover."""
    assert field not in RUNTIME_EDITABLE


def test_feature_flags_are_not_runtime_editable():
    """The agent builds its modules once at construction. Flipping a flag
    afterwards changes the setting without creating the object it is supposed
    to control, so the dashboard would report a feature as on while nothing
    implements it."""
    for flag in [
        "sequences_enabled",
        "imap_enabled",
        "warmup_enabled",
        "retry_enabled",
        "lead_scoring_enabled",
        "verify_emails",
    ]:
        assert flag not in RUNTIME_EDITABLE


def test_posting_an_uneditable_field_is_refused(tmp_path, settings_factory):
    client, db = _client(tmp_path, settings_factory)

    resp = client.post("/api/config", json={"settings": {"smtp_password": "hunter2"}})

    assert resp.status_code == 400
    assert "smtp_password" in resp.get_json()["error"]
    assert db.get_runtime_overrides() == {}, "an uneditable field was persisted"


# ── Validation must survive the trip ───────────────────────────────────────


def test_an_invalid_value_is_refused(tmp_path, settings_factory):
    """Settings does not set validate_assignment, so plain assignment would
    accept this silently and every validator in config.py would be decoration."""
    client, db = _client(tmp_path, settings_factory)

    resp = client.post("/api/config", json={"settings": {"max_bounce_rate": 99}})

    assert resp.status_code == 400
    assert db.get_runtime_overrides() == {}


def test_a_cross_field_rule_is_still_enforced(tmp_path, settings_factory):
    """send_window_end_after_start is a MODEL validator, not a field one - it
    only runs if the whole model is rebuilt."""
    client, db = _client(tmp_path, settings_factory)

    resp = client.post(
        "/api/config",
        json={"settings": {"send_window_start_hour": 20, "send_window_end_hour": 6}},
    )

    assert resp.status_code == 400
    assert db.get_runtime_overrides() == {}


def test_one_bad_value_rejects_the_whole_batch(tmp_path, settings_factory):
    """Validated against a single candidate before anything is written, so a
    bad third field cannot leave the first two applied."""
    client, db = _client(tmp_path, settings_factory)

    resp = client.post(
        "/api/config",
        json={
            "settings": {
                "min_lead_score_to_email": 40,
                "max_per_domain_per_day": 5,
                "max_bounce_rate": 99,  # the poison
            }
        },
    )

    assert resp.status_code == 400
    assert db.get_runtime_overrides() == {}, (
        "a partially-applied batch left the config in a state the operator "
        "never asked for"
    )


def test_a_valid_change_is_accepted_and_persisted(tmp_path, settings_factory):
    """Control: without this the guards above would pass by never working."""
    client, db = _client(tmp_path, settings_factory)

    resp = client.post(
        "/api/config", json={"settings": {"min_lead_score_to_email": 42}}
    )

    assert resp.status_code == 200, resp.get_json()
    assert db.get_runtime_overrides() == {"min_lead_score_to_email": 42}


def test_a_form_post_cannot_reach_it(tmp_path, settings_factory):
    """Same protection as every other write route: a cross-origin HTML form
    using enctype=text/plain is a CORS-simple request and fires no preflight."""
    client, db = _client(tmp_path, settings_factory)

    resp = client.post(
        "/api/config",
        data='{"settings": {"min_lead_score_to_email": 1}}',
        content_type="text/plain",
    )

    assert resp.status_code == 415
    assert db.get_runtime_overrides() == {}


# ── The agent has to actually pick it up ───────────────────────────────────


def _agent(tmp_path, settings_factory, **overrides):
    from agent import OutreachAgent

    return OutreachAgent(
        settings_factory(db_path=str(tmp_path / "cfg.db"), **overrides)
    )


def test_the_agent_picks_up_a_change_without_a_restart(tmp_path, settings_factory):
    agent = _agent(tmp_path, settings_factory, min_lead_score_to_email=15)
    assert agent.settings.min_lead_score_to_email == 15

    agent.db.set_runtime_override("min_lead_score_to_email", 60)
    agent._apply_runtime_overrides()

    assert agent.settings.min_lead_score_to_email == 60


def test_a_corrupt_override_does_not_take_the_agent_down(tmp_path, settings_factory):
    """A hand-edited or half-written row must not stop the loop; the previous
    good value stays in force."""
    agent = _agent(tmp_path, settings_factory, min_lead_score_to_email=15)
    agent.db.set_state("cfg:min_lead_score_to_email", "{not json")

    agent._apply_runtime_overrides()  # must not raise

    assert agent.settings.min_lead_score_to_email == 15


def test_an_override_that_fails_validation_is_skipped(tmp_path, settings_factory):
    """Belt and braces: the API validates before persisting, but a row written
    by any other route must not be trusted either."""
    agent = _agent(tmp_path, settings_factory, max_bounce_rate=0.05)
    agent.db.set_runtime_override("max_bounce_rate", 99)

    agent._apply_runtime_overrides()  # must not raise

    assert agent.settings.max_bounce_rate == 0.05


def test_an_override_for_an_uneditable_field_is_ignored(tmp_path, settings_factory):
    """Defence in depth: even a row written straight into the database cannot
    change a credential, because validate_override checks the allow-list too."""
    agent = _agent(tmp_path, settings_factory)
    before = agent.settings.smtp_password
    agent.db.set_runtime_override("smtp_password", "hunter2")

    agent._apply_runtime_overrides()

    assert agent.settings.smtp_password == before


def test_a_locked_database_does_not_stop_the_loop(tmp_path, settings_factory):
    agent = _agent(tmp_path, settings_factory)

    def _boom():
        raise sqlite3.OperationalError("database is locked")

    agent.db.get_runtime_overrides = _boom
    agent._apply_runtime_overrides()  # must not raise


# ── validate_override itself ───────────────────────────────────────────────


def test_validate_override_never_mutates_its_input():
    s = Settings(_env_file=None, min_lead_score_to_email=10)
    out = validate_override(s, "min_lead_score_to_email", 55)
    assert s.min_lead_score_to_email == 10, "the caller's Settings was mutated"
    assert out.min_lead_score_to_email == 55


def test_validate_override_refuses_a_field_off_the_list():
    s = Settings(_env_file=None)
    with pytest.raises(ValueError, match="not editable"):
        validate_override(s, "smtp_password", "hunter2")
