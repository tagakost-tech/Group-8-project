"""A competitor list that cannot be read stops live sends, as the client list does.

SUPPLECO supplied the names of 35 competitors and was told that a company on the
list is never emailed, and that live sending stops if the list cannot be read.
Only the second half was true of the client list. A missing, corrupt or empty
competitor list left the screen with nothing to match, the screen failed open,
and live email went on with only the keyword guess standing between the agent
and a rival. doctor checked only that the file existed, as a warning, and the
Ready to send card still said a live first email could go out.
"""

import os

import pytest
from openpyxl import Workbook

import main
from agent import OutreachAgent
from main import run_doctor_checks

PUBLIC = "https://out.suppleco.example"


def _workbook(path, rows):
    wb = Workbook()
    for row in rows:
        wb.active.append([row])
    wb.save(path)
    return path


def _missing(path):
    return path


def _corrupt(path):
    path.write_bytes(b"this is not a zip archive")
    return path


def _empty(path):
    return _workbook(path, [])


BROKEN = pytest.mark.parametrize(
    "make", [_missing, _corrupt, _empty], ids=["missing", "corrupt", "empty"]
)


def _settings(settings_factory, tmp_path, competitor_list_path, **kw):
    kw.setdefault("competitor_filter_enabled", True)
    return settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        imap_enabled=False,
        base_url=PUBLIC,
        competitor_list_path=str(competitor_list_path),
        **kw,
    )


def _doctor(settings):
    return {c.label: c for c in run_doctor_checks(settings, network=False)}[
        "Competitor list file"
    ]


@BROKEN
def test_a_list_that_cannot_be_read_refuses_live_sends(
    settings_factory, tmp_path, make
):
    path = make(tmp_path / "rivals.xlsx")
    agent = OutreachAgent(_settings(settings_factory, tmp_path, path))

    reason = agent._send_blocker(False)

    assert reason and "COMPETITOR_LIST_PATH" in reason, reason
    assert agent._send_blocker(True) is None, "dry runs still work"


def test_a_list_with_a_competitor_lets_live_sends_through(settings_factory, tmp_path):
    path = _workbook(tmp_path / "rivals.xlsx", ["Rival Integratori S.r.l."])
    agent = OutreachAgent(_settings(settings_factory, tmp_path, path))

    assert agent._send_blocker(False) is None
    assert agent._known_competitor("Rival Integratori")


def test_no_list_configured_lets_live_sends_through(settings_factory, tmp_path):
    agent = OutreachAgent(_settings(settings_factory, tmp_path, ""))

    assert agent._send_blocker(False) is None


def test_the_screen_turned_off_lets_live_sends_through(settings_factory, tmp_path):
    agent = OutreachAgent(
        _settings(
            settings_factory,
            tmp_path,
            tmp_path / "rivals.xlsx",
            competitor_filter_enabled=False,
        )
    )

    assert agent._send_blocker(False) is None


def test_a_repaired_list_lifts_the_refusal_without_a_restart(
    settings_factory, tmp_path
):
    """The same reload that re-reads an edited client list re-reads this one."""
    path = _corrupt(tmp_path / "rivals.xlsx")
    agent = OutreachAgent(_settings(settings_factory, tmp_path, path))
    assert agent._send_blocker(False)

    _workbook(path, ["Rival Integratori S.r.l."])
    later = os.stat(path).st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(later, later))
    agent.refresh_lists_if_changed()

    assert agent._send_blocker(False) is None
    rows = agent.db.conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE action = 'COMPETITOR_LIST_MISSING'"
    ).fetchone()[0]
    assert rows == 1, f"{rows} rows for one episode of refusals"


@BROKEN
def test_doctor_refuses_a_list_that_cannot_be_read(settings_factory, tmp_path, make):
    path = make(tmp_path / "rivals.xlsx")

    check = _doctor(_settings(settings_factory, tmp_path, path))

    assert check.ok is False and check.blocking, check.detail
    assert "rivals.xlsx" in check.detail


def test_doctor_passes_a_list_with_a_competitor(settings_factory, tmp_path):
    path = _workbook(tmp_path / "rivals.xlsx", ["Rival Integratori S.r.l."])

    check = _doctor(_settings(settings_factory, tmp_path, path))

    assert check.ok is True, check.detail
    assert "1 companies" in check.detail


@BROKEN
def test_the_ready_to_send_card_refuses_live_first_emails(
    settings_factory, tmp_path, make
):
    path = make(tmp_path / "rivals.xlsx")
    app = main.create_dashboard_app(
        _settings(settings_factory, tmp_path, path, blacklist_check_enabled=False)
    )

    body = app.test_client().get("/api/readiness").get_json()

    row = next(c for c in body["checks"] if c["label"] == "Competitor list file")
    assert row["ok"] is False and row["blocking"] is True, row
    assert row["detail"].startswith("rivals.xlsx "), row
    assert body["can_send_live"] is False
