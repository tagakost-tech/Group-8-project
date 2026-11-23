"""The settings panel cannot switch off the bounce breaker or the domain back-off.

Both rate validators accepted exactly 1, and both guards act only on a rate
strictly above their threshold, so at 1 they wait for more bounces than sends.
1 is also what an operator types who means 1%: the panel's labels gave no
unit. The breaker's minimum-sends dial had no validator at all, so a typo of
2000 for 20 keeps it silent for weeks at warm-up volumes, and the back-off's
minimum had no upper bound.

Each change goes the way an operator's does: posted from the panel, then
picked up by the running agent at the top of its next loop.
"""

import pytest
from pydantic import ValidationError

import main


def _agent_and_panel(tmp_path, settings_factory):
    from agent import OutreachAgent

    s = settings_factory(db_path=str(tmp_path / "guards.db"))
    return OutreachAgent(s), main.create_dashboard_app(s).test_client()


def _every_recent_send_bounced(*_args, **_kwargs):
    return {
        "recent_sent": 200,
        "recent_bounced": 200,
        "bounce_rate": 1.0,
        "window_days": 7,
    }


def _seed(db, domain, sent, bounced):
    sid = db.log_search("q", source="test")
    for i in range(sent):
        cid = db.add_contact(sid, f"user{i}@{domain}")
        db.log_email(cid, f"user{i}@{domain}", "s", delivery_status="sent")
    for i in range(bounced):
        db.add_to_suppression(f"user{i}@{domain}", "bounce")


@pytest.mark.parametrize(
    "change",
    [
        {"max_bounce_rate": 1},
        {"max_bounce_rate": 1.0},
        {"bounce_guard_min_sends": 2000},
        {"bounce_guard_min_sends": 1_000_000},
    ],
)
def test_the_panel_cannot_switch_off_the_bounce_breaker(
    tmp_path, settings_factory, change
):
    agent, panel = _agent_and_panel(tmp_path, settings_factory)
    agent.db.get_deliverability_metrics = _every_recent_send_bounced
    assert agent._bounce_guard_tripped() is True, "control: the defaults trip"

    resp = panel.post("/api/config", json={"settings": change})
    agent._apply_runtime_overrides()

    assert agent._bounce_guard_tripped() is True, (
        f"{change} from the panel switched the breaker off: all 200 recent "
        f"sends bounced and sending carries on"
    )
    assert resp.status_code == 400, resp.get_json()


@pytest.mark.parametrize(
    "change",
    [
        {"max_domain_bounce_rate": 1},
        {"max_domain_bounce_rate": 1.0},
        {"domain_bounce_min_sends": 1_000_000},
    ],
)
def test_the_panel_cannot_switch_off_the_domain_back_off(
    tmp_path, settings_factory, change
):
    agent, panel = _agent_and_panel(tmp_path, settings_factory)
    _seed(agent.db, "bouncing.de", sent=10, bounced=10)
    agent._refresh_domain_backoff()
    assert agent._domain_backed_off("buyer@bouncing.de"), "control: backed off"

    resp = panel.post("/api/config", json={"settings": change})
    agent._apply_runtime_overrides()
    agent._refresh_domain_backoff()

    assert agent._domain_backed_off("buyer@bouncing.de"), (
        f"{change} from the panel switched the back-off off: every send to "
        f"bouncing.de bounced and it is still emailed"
    )
    assert resp.status_code == 400, resp.get_json()


@pytest.mark.parametrize("key", ["max_bounce_rate", "max_domain_bounce_rate"])
def test_the_refusal_tells_the_operator_the_rate_is_a_fraction(
    tmp_path, settings_factory, key
):
    """An operator who typed 1 meaning 1% learns what to type instead."""
    _agent, panel = _agent_and_panel(tmp_path, settings_factory)

    resp = panel.post("/api/config", json={"settings": {key: 1}})

    assert resp.status_code == 400
    assert "a fraction" in resp.get_json()["error"]
    assert "%" in resp.get_json()["error"]


@pytest.mark.parametrize("key", ["max_bounce_rate", "max_domain_bounce_rate"])
@pytest.mark.parametrize("value", [0.51, 0.99, 1, 2, 5, 99])
def test_a_rate_above_one_half_is_refused(settings_factory, key, value):
    with pytest.raises(ValidationError, match="fraction in"):
        settings_factory(**{key: value})


@pytest.mark.parametrize("key", ["max_bounce_rate", "max_domain_bounce_rate"])
@pytest.mark.parametrize("value", [0.01, 0.02, 0.3, 0.5])
def test_a_rate_up_to_one_half_is_accepted(settings_factory, key, value):
    assert getattr(settings_factory(**{key: value}), key) == value


@pytest.mark.parametrize("value", [-1, 0, 501, 2000, 1_000_000])
def test_the_breaker_minimum_is_refused_outside_1_to_500(settings_factory, value):
    with pytest.raises(ValidationError, match="between 1 and 500"):
        settings_factory(bounce_guard_min_sends=value)


@pytest.mark.parametrize("value", [1, 20, 100, 500])
def test_the_breaker_minimum_is_accepted_from_1_to_500(settings_factory, value):
    s = settings_factory(bounce_guard_min_sends=value)
    assert s.bounce_guard_min_sends == value


@pytest.mark.parametrize("value", [101, 500, 1_000_000])
def test_the_domain_minimum_is_refused_above_100(settings_factory, value):
    with pytest.raises(ValidationError, match="at most 100"):
        settings_factory(domain_bounce_min_sends=value)


@pytest.mark.parametrize("value", [1, 5, 50, 100])
def test_the_domain_minimum_is_accepted_up_to_100(settings_factory, value):
    s = settings_factory(domain_bounce_min_sends=value)
    assert s.domain_bounce_min_sends == value
