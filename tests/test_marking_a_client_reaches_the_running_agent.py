"""Marking a client on the dashboard takes effect without a restart.

"Mark as client" appended the company to the client spreadsheet and set the
contact's status - but the agent screens against a register it loaded ONCE,
at startup. Colleagues at the same company stayed fair game, and so did the
contact's own follow-ups, until someone happened to restart the agent.
"""

import pytest
from openpyxl import Workbook

import main


@pytest.fixture
def dash(tmp_path, settings_factory):
    xlsx = tmp_path / "clients.xlsx"
    wb = Workbook()
    wb.active.append(["Some Other Company S.r.l."])
    wb.save(xlsx)
    s = settings_factory(
        db_path=str(tmp_path / "d.db"),
        client_list_path=str(xlsx),
        verify_emails=False,
        imap_enabled=False,
        ai_humanize_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
    )
    return main.create_dashboard_app(s).test_client(), main.Database(
        s.get_abs_db_path()
    )


def _send(client, email):
    return client.post(
        "/api/send-manual",
        json={"email": email, "organization": "Brand GmbH", "dry_run": True},
    ).get_json()


def test_a_newly_marked_client_is_screened_at_once(dash):
    client, db = dash
    # Stored first: a preview of an address the agent does not hold stores
    # nothing, so it cannot create the row this test marks.
    cid = db.add_contact(db.log_search("q"), "anna@brand.de", organization="Brand GmbH")
    assert _send(client, "anna@brand.de")["ok"], "setup: first send should pass"

    resp = client.post(f"/api/contacts/{cid}/action", json={"action": "mark_client"})
    assert resp.status_code == 200

    second = _send(client, "marco@brand.de")
    assert second["ok"] is False, "a colleague at a marked client was emailed"


def test_the_running_campaign_agent_reloads_too(
    tmp_path, settings_factory, monkeypatch
):
    """The campaign agent is the one that sends at volume; the first test only
    ever created the Direct Send agent. A running campaign agent routes Direct
    Sends through itself, so a reload that skipped it screened nobody."""
    import threading

    from agent import OutreachAgent

    stop = threading.Event()
    reloads = []

    class RunningAgent(OutreachAgent):
        def run_continuous(self, *a, **k):
            stop.wait(10)

        def load_client_register(self):
            reloads.append(self)
            super().load_client_register()

    xlsx = tmp_path / "clients.xlsx"
    wb = Workbook()
    wb.active.append(["Some Other Company S.r.l."])
    wb.save(xlsx)
    s = settings_factory(
        db_path=str(tmp_path / "d.db"),
        client_list_path=str(xlsx),
        verify_emails=False,
        imap_enabled=False,
    )
    monkeypatch.setattr(main, "OutreachAgent", RunningAgent)
    client = main.create_dashboard_app(s).test_client()
    db = main.Database(s.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), "anna@brand.de", organization="Brand GmbH")
    try:
        assert (
            client.post("/api/agent/start", json={"mode": "continuous"}).status_code
            == 200
        )
        campaign = reloads[-1]  # constructed, and loaded once at startup
        before = reloads.count(campaign)
        resp = client.post(
            f"/api/contacts/{cid}/action", json={"action": "mark_client"}
        )
        assert resp.status_code == 200
        assert reloads.count(campaign) == before + 1, (
            "the running campaign agent kept its stale list"
        )
        assert campaign._active_client("Brand GmbH")
    finally:
        stop.set()
