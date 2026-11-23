"""An edit to the client or competitor spreadsheet takes effect without a restart.

SUPPLECO maintains both lists by hand. The agent read them once, at startup,
so a company added as a client that morning could still be cold-emailed all
day - by a campaign that had been running since before the edit. The only
live reload was the dashboard's own "mark as client" button.

Now every round of work (a campaign loop, a single run, a Direct Send)
first checks whether either file changed and re-reads it. A save caught
half-written - Excel, OneDrive sync - keeps the previous list rather than
screening with nothing, and a broken file is not re-read until it changes.
"""

import os

import pytest
from openpyxl import Workbook

from agent import OutreachAgent


def _xlsx(path, names):
    wb = Workbook()
    for n in names:
        wb.active.append([n])
    wb.save(path)
    _touch(path)


def _touch(path):
    """A later modification time, as a real edit minutes later would have -
    two saves inside one test can otherwise share a timestamp."""
    st = os.stat(path)
    later = st.st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(later, later))


@pytest.fixture
def files(tmp_path):
    clients, rivals = tmp_path / "clients.xlsx", tmp_path / "rivals.xlsx"
    _xlsx(clients, ["Some Other Company S.r.l."])
    _xlsx(rivals, ["Naturacare"])
    return clients, rivals


def _agent(settings_factory, tmp_path, files, **kw):
    clients, rivals = files
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            client_list_path=str(clients),
            competitor_list_path=str(rivals),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            heuristic_discovery_enabled=False,
            target_queries="",
            **kw,
        )
    )


def test_a_client_added_to_the_spreadsheet_is_screened_without_a_restart(
    settings_factory, tmp_path, files
):
    a = _agent(settings_factory, tmp_path, files)
    assert not a._active_client("Brand GmbH")
    _xlsx(files[0], ["Some Other Company S.r.l.", "Brand GmbH"])
    a.refresh_lists_if_changed()
    assert a._active_client("Brand GmbH")


def test_a_competitor_added_is_screened_too(settings_factory, tmp_path, files):
    a = _agent(settings_factory, tmp_path, files)
    assert not a._known_competitor("NutraPro")
    _xlsx(files[1], ["Naturacare", "NutraPro"])
    a.refresh_lists_if_changed()
    assert a._known_competitor("NutraPro")


def test_the_reload_is_logged(settings_factory, tmp_path, files):
    """The operator sees that the edit reached the agent."""
    a = _agent(settings_factory, tmp_path, files)
    _xlsx(files[0], ["Some Other Company S.r.l.", "Brand GmbH"])
    a.refresh_lists_if_changed()
    rows = [r for r in a.db.get_recent_activity(20) if r["action"] == "LIST_RELOADED"]
    assert rows and "client" in rows[0]["details"].lower()


def test_an_unchanged_file_is_not_re_read(
    settings_factory, tmp_path, files, monkeypatch
):
    a = _agent(settings_factory, tmp_path, files)
    reads = []
    from modules import clients

    real = clients.load_from_xlsx
    monkeypatch.setattr(
        clients, "load_from_xlsx", lambda p, **kw: reads.append(p) or real(p, **kw)
    )
    a.refresh_lists_if_changed()
    a.refresh_lists_if_changed()
    assert reads == []


def test_a_half_written_save_keeps_the_previous_list(settings_factory, tmp_path, files):
    a = _agent(settings_factory, tmp_path, files)
    files[0].write_bytes(b"PK\x03\x04 half a zip")
    _touch(files[0])
    a.refresh_lists_if_changed()
    assert a._active_client("Some Other Company S.r.l."), (
        "screening fell back to nothing"
    )
    _xlsx(files[0], ["Some Other Company S.r.l.", "Brand GmbH"])
    a.refresh_lists_if_changed()
    assert a._active_client("Brand GmbH"), "the completed save was not picked up"


def test_a_broken_file_is_not_re_read_every_loop(
    settings_factory, tmp_path, files, monkeypatch
):
    a = _agent(settings_factory, tmp_path, files)
    files[0].write_bytes(b"not a spreadsheet")
    _touch(files[0])
    reads = []
    from modules import clients

    real = clients.load_from_xlsx
    monkeypatch.setattr(
        clients, "load_from_xlsx", lambda p, **kw: reads.append(p) or real(p, **kw)
    )
    for _ in range(3):
        a.refresh_lists_if_changed()
    assert len(reads) == 1


def test_the_campaign_loop_rechecks_before_its_backlog(
    settings_factory, tmp_path, files, monkeypatch
):
    """Drives the real loop: the edit must be in force before the backlog is
    screened in that same iteration."""
    a = _agent(settings_factory, tmp_path, files)
    _xlsx(files[0], ["Some Other Company S.r.l.", "Brand GmbH"])
    seen = []
    monkeypatch.setattr(
        a,
        "_process_pending_contacts",
        lambda dry: seen.append(a._active_client("Brand GmbH")),
    )
    a._interruptible_sleep = lambda *_a, **_k: setattr(a, "_running", False)
    a.run_continuous(dry_run=True, loop_delay=60)
    assert seen and seen[0]


def test_a_single_run_rechecks_before_it_searches(
    settings_factory, tmp_path, files, monkeypatch
):
    """`python main.py run` and the dashboard's one-off run never enter the
    continuous loop, so they need their own check. The search is stubbed: it
    records whether the edit was already in force when the run began."""
    a = _agent(settings_factory, tmp_path, files)
    _xlsx(files[0], ["Some Other Company S.r.l.", "Brand GmbH"])
    seen = []

    def search(query, search_id=None):
        seen.append(a._active_client("Brand GmbH"))
        return []

    monkeypatch.setattr(a.search, "search_and_extract", search)
    a.run_single(queries=["supplement brands"], dry_run=True)
    assert seen and seen[0], "the run searched before re-reading the client list"


def test_a_direct_send_rechecks_first(settings_factory, tmp_path, files):
    """The dashboard's Direct Send agent never runs a loop."""
    a = _agent(settings_factory, tmp_path, files)
    assert a.send_single_email(
        "anna@brand.de", organization="Brand GmbH", dry_run=True
    )[0]
    _xlsx(files[0], ["Some Other Company S.r.l.", "Brand GmbH"])
    ok, _ = a.send_single_email(
        "marco@brand.de", organization="Brand GmbH", dry_run=True
    )
    assert ok is False, "a colleague at a client added this morning was emailed"
