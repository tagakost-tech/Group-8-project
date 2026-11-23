"""A client list that screens no company stops live sends, like a missing one.

The send check refused live mail while the client screen had no list at all,
but a workbook that loads and holds no company the screen can match - an
empty sheet, rows of nothing but legal forms - passed it, so every existing
customer could be cold-emailed with nothing to say so. doctor only checked
that the file existed, so it passed an empty or an unreadable workbook too.

A reload can empty the list again after it recovered. The refusal's activity
row was written once per process, so that second episode left none.
"""

import os

import pytest
from openpyxl import Workbook

from agent import OutreachAgent
from main import run_doctor_checks

PUBLIC = "https://out.suppleco.example"
SCREENS_NOTHING = pytest.mark.parametrize(
    "rows", [[], ["S.r.l.", "GmbH"]], ids=["no rows", "only legal forms"]
)


def _workbook(path, rows):
    wb = Workbook()
    for row in rows:
        wb.active.append([row])
    wb.save(path)
    return str(path)


def _settings(settings_factory, tmp_path, client_list_path):
    return settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        imap_enabled=False,
        base_url=PUBLIC,
        client_filter_enabled=True,
        client_list_path=client_list_path,
    )


def _doctor(settings):
    return {c.label: c for c in run_doctor_checks(settings, network=False)}[
        "Client list file"
    ]


@SCREENS_NOTHING
def test_a_list_that_screens_no_company_refuses_live_sends(
    settings_factory, tmp_path, rows
):
    path = _workbook(tmp_path / "clients.xlsx", rows)
    agent = OutreachAgent(_settings(settings_factory, tmp_path, path))

    reason = agent._send_blocker(False)

    assert reason and "client list" in reason.lower(), reason
    assert agent._send_blocker(True) is None, "dry runs still work"


def test_a_list_with_a_client_lets_live_sends_through(settings_factory, tmp_path):
    path = _workbook(tmp_path / "clients.xlsx", ["Farmacia Rossi S.r.l."])
    agent = OutreachAgent(_settings(settings_factory, tmp_path, path))

    assert agent._send_blocker(False) is None


def _resave(path, rows):
    """A later modification time, as a real edit minutes later would have -
    two saves inside one test can otherwise share a timestamp."""
    _workbook(path, rows)
    later = os.stat(path).st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(later, later))


def test_a_list_emptied_again_is_reported_again(settings_factory, tmp_path):
    """One row per episode. Only a list that never loaded was refused before,
    and a loaded list never unloads, so one row per process was enough. The
    warm-up and bounce guards clear their latch the same way when they lift."""
    path = tmp_path / "clients.xlsx"
    agent = OutreachAgent(_settings(settings_factory, tmp_path, _workbook(path, [])))
    assert agent._send_blocker(False) and agent._send_blocker(False)
    _resave(path, ["Farmacia Rossi S.r.l."])
    agent.refresh_lists_if_changed()
    assert agent._send_blocker(False) is None, "the control: the list screens again"
    _resave(path, [])
    agent.refresh_lists_if_changed()

    assert agent._send_blocker(False) and agent._send_blocker(False)
    rows = agent.db.conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE action = 'CLIENT_LIST_MISSING'"
    ).fetchone()[0]
    assert rows == 2, f"{rows} rows for two episodes of two refusals each"


@SCREENS_NOTHING
def test_doctor_fails_a_list_that_screens_no_company(settings_factory, tmp_path, rows):
    path = _workbook(tmp_path / "clients.xlsx", rows)

    check = _doctor(_settings(settings_factory, tmp_path, path))

    assert check.ok is False and check.blocking, check.detail


def test_doctor_fails_a_workbook_it_cannot_read(settings_factory, tmp_path):
    """The agent refuses live sends for it, so doctor must not pass it."""
    bad = tmp_path / "clients.xlsx"
    bad.write_bytes(b"this is not a zip archive")

    check = _doctor(_settings(settings_factory, tmp_path, str(bad)))

    assert check.ok is False and check.blocking, check.detail


def test_doctor_passes_a_list_with_a_client(settings_factory, tmp_path):
    path = _workbook(tmp_path / "clients.xlsx", ["Farmacia Rossi S.r.l."])

    assert _doctor(_settings(settings_factory, tmp_path, path)).ok is True
