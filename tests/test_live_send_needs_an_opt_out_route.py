"""No live email goes out unless an opt-out to it would actually be read.

Every email offers a way to object. With BASE_URL on this machine the offer
is a mailto to the sending mailbox, and with IMAP off nothing reads that
mailbox: the objection arrives and is never acted on (GDPR Art. 21(3)).
doctor reported this as a warning and the agent sent anyway - the prototype
ran for months with no inbox check. The send check now refuses it, using the
same rule doctor reports, so the two cannot disagree. Dry runs still work.
"""

import pytest

import main
from agent import OutreachAgent
from modules import clock
from modules.compliance import opt_out_route

LOCAL = "http://127.0.0.1:5000"


def _agent(settings_factory, tmp_path, **overrides):
    return OutreachAgent(
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


def test_a_local_base_url_with_no_inbox_reader_refuses_live_sends(
    settings_factory, tmp_path
):
    agent = _agent(settings_factory, tmp_path, base_url=LOCAL, imap_enabled=False)
    reason = agent._send_blocker(False)
    assert reason and "IMAP_ENABLED" in reason


def test_dry_runs_are_never_refused_for_it(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path, base_url=LOCAL, imap_enabled=False)
    assert agent._send_blocker(True) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "https://outreach.suppleco.example", "imap_enabled": False},
        {"base_url": LOCAL, "imap_enabled": True},
    ],
)
def test_either_route_lets_live_sends_through(settings_factory, tmp_path, overrides):
    agent = _agent(settings_factory, tmp_path, **overrides)
    # The inbox route counts once each inbox has been read completely: see
    # tests/test_live_send_needs_the_inbox_read.py.
    for monitor in agent.imap_monitors:
        agent.db.set_state(monitor._watermark_key, clock.now_iso())
    assert agent._send_blocker(False) is None


def test_the_refusal_reaches_the_dashboard_once(settings_factory, tmp_path):
    agent = _agent(settings_factory, tmp_path, base_url=LOCAL, imap_enabled=False)
    agent._send_blocker(False)
    agent._send_blocker(False)
    rows = agent.db.conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE action = 'OPT_OUT_ROUTE_MISSING'"
    ).fetchone()[0]
    assert rows == 1


@pytest.mark.parametrize(
    "base_url,imap", [(LOCAL, False), (LOCAL, True), ("https://x.example", False)]
)
def test_doctor_reports_the_same_rule(settings_factory, base_url, imap):
    s = settings_factory(base_url=base_url, imap_enabled=imap)
    by_label = {c.label: c for c in main.run_doctor_checks(s, network=False)}
    route = by_label["Opt-out route"]
    assert route.ok == opt_out_route(s)[0]
    assert route.blocking, "the send check refuses it, so doctor must say so"


def test_a_public_but_plain_http_address_is_not_a_route(settings_factory, tmp_path):
    """One-click unsubscribe is only offered over https (email_sender adds
    List-Unsubscribe-Post for https links alone), so plain http on a public
    host needs the inbox monitor just as a local address does."""
    agent = _agent(
        settings_factory,
        tmp_path,
        base_url="http://outreach.suppleco.example",
        imap_enabled=False,
    )
    reason = agent._send_blocker(False)
    assert reason and "https" in reason
