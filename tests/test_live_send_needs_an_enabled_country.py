"""No live email goes to a country counsel has not enabled.

Whether email marketing to a company needs prior consent depends on the
recipient's country (the review's country table, decision L1.1), and nothing
in the agent knew about countries: once the legal identity was filled in,
every address could be emailed. Until the full country rules (work item W2)
exist, an interim lock: live sending is refused while ENABLED_COUNTRIES is
blank, and a recipient whose country-code domain is not enabled is refused.
An address on .com, .eu or another generic domain shows no country, so it
counts as needing consent (PS-10) and is refused unless counsel switches
UNKNOWN_COUNTRY_ALLOWED on. Dry runs still preview everything.
"""

import pytest

import main
from agent import OutreachAgent
from modules.compliance import recipient_country


@pytest.mark.parametrize(
    "email,country",
    [
        ("info@brand.de", "DE"),
        ("info@BRAND.FR", "FR"),
        ("info@brand.co.uk", "GB"),
        ("info@brand.uk", "GB"),
        ("info@brand.com", None),
        ("info@brand.eu", None),
        ("info@brand.swiss", None),
        ("not-an-address", None),
    ],
)
def test_the_country_comes_from_the_country_code_domain(email, country):
    assert recipient_country(email) == country


def _agent(settings_factory, tmp_path, monkeypatch, **overrides):
    agent = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            ai_humanize_emails=False,
            **overrides,
        )
    )
    agent.dispatched = []
    monkeypatch.setattr(
        agent.email_sender,
        "dispatch_to_contact",
        lambda **k: agent.dispatched.append(k.get("to_email", k)) or 1,
    )
    return agent


def _send(agent, email, dry_run=False):
    sid = agent.db.log_search("q", source="test")
    cid = agent.db.add_contact(sid, email, organization="Brand")
    return agent._process_contact(agent.db.get_contact_by_id(cid), dry_run=dry_run)


def test_no_country_enabled_refuses_every_live_send(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch, ENABLED_COUNTRIES="")
    reason = agent._send_blocker(False)
    assert reason and "ENABLED_COUNTRIES" in reason
    assert agent._send_blocker(True) is None


def test_a_recipient_in_an_enabled_country_is_emailed(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch, ENABLED_COUNTRIES="FR, gb")
    assert _send(agent, "info@brand.fr") is True
    assert _send(agent, "sales@brand.co.uk") is True


def test_a_recipient_in_another_country_is_refused(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch, ENABLED_COUNTRIES="FR")
    assert _send(agent, "info@brand.de") is False
    assert agent.dispatched == []


def test_a_dry_run_still_previews_any_country(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch, ENABLED_COUNTRIES="FR")
    assert _send(agent, "info@brand.de", dry_run=True) is True


@pytest.mark.parametrize("allowed,expected", [(False, False), (True, True)])
def test_an_address_with_no_country_needs_counsels_switch(
    settings_factory, tmp_path, monkeypatch, allowed, expected
):
    agent = _agent(
        settings_factory,
        tmp_path,
        monkeypatch,
        ENABLED_COUNTRIES="FR",
        UNKNOWN_COUNTRY_ALLOWED=allowed,
    )
    assert _send(agent, "info@brand.com") is expected


def _doctor(settings_factory, **overrides):
    s = settings_factory(**overrides)
    return {c.label: c for c in main.run_doctor_checks(s, network=False)}[
        "Country lock"
    ]


def test_doctor_fails_while_no_country_is_enabled(settings_factory):
    check = _doctor(settings_factory, ENABLED_COUNTRIES="")
    assert check.ok is False and check.blocking


def test_doctor_names_the_enabled_countries(settings_factory):
    check = _doctor(
        settings_factory, ENABLED_COUNTRIES="FR,GB", UNKNOWN_COUNTRY_ALLOWED=False
    )
    assert check.ok is True
    assert "FR" in check.detail and "GB" in check.detail


@pytest.mark.parametrize("value", ["Italy", "IT,France", "I"])
def test_country_names_instead_of_codes_are_refused_at_load(settings_factory, value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="two-letter"):
        settings_factory(ENABLED_COUNTRIES=value)
