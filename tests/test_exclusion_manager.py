"""Unit tests for ExclusionManager (modules/exclusion_manager.py).

Verifies:
- Excel fingerprinting via mtime and size (_list_stamp).
- Resilient loading (_load_register): corrupt workbook handling preserves prior list and records failure stamp.
- Empty spreadsheet handling (0 rows) does not raise index/key errors.
- Dynamic reload trigger (refresh_lists_if_changed) detects modified mtime and updates register.
- Screening methods: active_client, known_competitor, contact_is_closed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from openpyxl import Workbook

from modules.database import Database
from modules.exclusion_manager import ExclusionManager


def _make_xlsx(path: Path, rows: list[list[str]]) -> None:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)


def _touch_later(path: Path, seconds: int = 5) -> None:
    st = os.stat(path)
    later = st.st_mtime_ns + (seconds * 1_000_000_000)
    os.utime(path, ns=(later, later))


class DummyAgent:
    """Minimal agent dummy providing required attributes for ExclusionManager."""

    def __init__(self, db: Database, settings: Any):
        self.db = db
        self.settings = settings
        self.client_register = None
        self.competitor_register = None
        self._list_stamps: dict[str, Any] = {}
        self._list_failed: dict[str, Any] = {}

    def load_client_register(self) -> bool:
        mgr = getattr(self, "exclusion_manager", None)
        if mgr:
            return mgr.load_client_register()
        return False

    def load_competitor_register(self) -> bool:
        mgr = getattr(self, "exclusion_manager", None)
        if mgr:
            return mgr.load_competitor_register()
        return False

    # The stage calls these through the agent, like every sibling call.
    def _load_register(self, kind: str, raw_path: str) -> bool:
        return self.exclusion_manager._load_register(kind, raw_path)

    @staticmethod
    def _list_stamp(path: Any) -> tuple | None:
        return ExclusionManager._list_stamp(path)


@pytest.fixture
def dummy_agent(tmp_path, settings_factory):
    test_db = Database(str(tmp_path / "ex_test.db"))
    st = settings_factory(
        client_filter_enabled=True,
        client_list_path=str(tmp_path / "clients.xlsx"),
        competitor_filter_enabled=True,
        competitor_list_path=str(tmp_path / "competitors.xlsx"),
    )
    agent = DummyAgent(test_db, st)
    mgr = ExclusionManager(agent)
    agent.exclusion_manager = mgr
    return agent, mgr


# ---------------------------------------------------------------------------
# 1. Excel fingerprinting via mtime and size (_list_stamp)
# ---------------------------------------------------------------------------


def test_list_stamp_returns_mtime_and_size_for_existing_file(tmp_path):
    f = tmp_path / "sample.xlsx"
    _make_xlsx(f, [["Company A"]])
    stamp = ExclusionManager._list_stamp(f)
    assert stamp is not None
    mtime_ns, size = stamp
    assert isinstance(mtime_ns, int)
    assert isinstance(size, int)
    assert size > 0
    stat = f.stat()
    assert mtime_ns == stat.st_mtime_ns
    assert size == stat.st_size


def test_list_stamp_returns_none_for_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.xlsx"
    assert ExclusionManager._list_stamp(missing) is None


def test_list_stamp_handles_oserror_gracefully():
    mock_path = MagicMock()
    mock_path.stat.side_effect = OSError("Permission denied")
    assert ExclusionManager._list_stamp(mock_path) is None


# ---------------------------------------------------------------------------
# 2. Resilient loading (_load_register)
# ---------------------------------------------------------------------------


def test_load_register_missing_file_returns_false_and_preserves_state(
    dummy_agent, tmp_path
):
    agent, mgr = dummy_agent
    missing_path = str(tmp_path / "nonexistent.xlsx")
    assert mgr._load_register("client", missing_path) is False
    assert agent.client_register is None
    assert "client" not in agent._list_stamps


def test_load_register_success_populates_register_and_stamp(dummy_agent, tmp_path):
    agent, mgr = dummy_agent
    cpath = tmp_path / "clients.xlsx"
    _make_xlsx(cpath, [["Alpha Pharma S.r.l."], ["Beta Health GmbH"]])

    assert mgr._load_register("client", str(cpath)) is True
    assert agent.client_register is not None
    assert len(agent.client_register) >= 2
    assert "client" in agent._list_stamps
    assert "client" not in agent._list_failed


def test_load_register_corrupt_workbook_preserves_prior_list_and_records_failure_stamp(
    dummy_agent, tmp_path
):
    agent, mgr = dummy_agent
    cpath = tmp_path / "clients.xlsx"
    _make_xlsx(cpath, [["Initial Pharma S.p.A."]])

    # First load succeeds
    assert mgr._load_register("client", str(cpath)) is True
    initial_reg = agent.client_register
    assert initial_reg is not None
    assert mgr.active_client("Initial Pharma S.p.A.") is not None

    # Corrupt the file with random invalid bytes
    cpath.write_bytes(b"PK\x03\x04corrupted_half_zip_payload")
    _touch_later(cpath, seconds=2)

    # Second load fails but preserves prior register
    assert mgr._load_register("client", str(cpath)) is False
    assert agent.client_register is initial_reg
    assert mgr.active_client("Initial Pharma S.p.A.") is not None
    assert "client" in agent._list_failed
    assert agent._list_failed["client"] == ExclusionManager._list_stamp(cpath)


def test_load_register_unmatchable_rows_logged_without_crash(
    dummy_agent, tmp_path, caplog
):
    """A row that normalises to nothing is in the file but not in the filter,
    so it must be named in the log - that is the difference between a gap
    someone can fix and a client who quietly gets cold-emailed. (The original
    never looked at the log, so deleting the warning left it green.)"""
    import logging

    agent, mgr = dummy_agent
    cpath = tmp_path / "clients.xlsx"
    # Row with punctuation/whitespace only normalizes to empty string in ClientRegister
    _make_xlsx(cpath, [["Valid Pharma"], ["---"], ["   "]])
    with caplog.at_level(logging.WARNING):
        assert mgr._load_register("client", str(cpath)) is True
    assert agent.client_register is not None
    assert len(agent.client_register) >= 1
    assert any("'---'" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. Empty spreadsheet handling (0 rows)
# ---------------------------------------------------------------------------


def test_empty_spreadsheet_zero_rows_does_not_raise(dummy_agent, tmp_path):
    agent, mgr = dummy_agent
    cpath = tmp_path / "empty_clients.xlsx"
    wb = Workbook()
    # Sheet has 0 rows added
    wb.save(cpath)

    agent.settings.client_list_path = str(cpath)
    assert mgr.load_client_register() is True
    assert agent.client_register is not None
    assert len(agent.client_register) == 0
    # Screening with empty register returns None without IndexError/KeyError
    assert mgr.active_client("Any Company") is None


def test_empty_spreadsheet_headers_only_does_not_raise(dummy_agent, tmp_path):
    agent, mgr = dummy_agent
    cpath = tmp_path / "headers_only.xlsx"
    _make_xlsx(cpath, [["Company Name"]])

    agent.settings.client_list_path = str(cpath)
    assert mgr.load_client_register() is True
    assert agent.client_register is not None
    # No error when matching
    assert mgr.active_client("Any Corp") is None


# ---------------------------------------------------------------------------
# 4. Dynamic reload trigger (refresh_lists_if_changed)
# ---------------------------------------------------------------------------


def test_refresh_lists_if_changed_detects_mtime_change_and_reloads(
    dummy_agent, tmp_path
):
    agent, mgr = dummy_agent
    cpath = tmp_path / "clients.xlsx"
    _make_xlsx(cpath, [["Existing Client 1"]])
    assert mgr.load_client_register() is True
    assert mgr.active_client("Brand New Corp") is None

    # Now edit the file and update timestamp
    _make_xlsx(cpath, [["Existing Client 1"], ["Brand New Corp"]])
    _touch_later(cpath, seconds=3)

    mgr.refresh_lists_if_changed()
    assert mgr.active_client("Brand New Corp") is not None

    # Check activity log for LIST_RELOADED
    logs = agent.db.get_recent_activity(10)
    assert any(
        log["action"] == "LIST_RELOADED" and "Client" in log["details"] for log in logs
    )


def test_refresh_lists_if_changed_does_not_reload_unchanged_file(
    dummy_agent, tmp_path, monkeypatch
):
    agent, mgr = dummy_agent
    cpath = tmp_path / "clients.xlsx"
    _make_xlsx(cpath, [["Existing Client 1"]])
    assert mgr.load_client_register() is True

    # Track calls to load_client_register
    called = []
    monkeypatch.setattr(
        agent, "load_client_register", lambda: called.append(True) or True
    )

    mgr.refresh_lists_if_changed()
    assert len(called) == 0


def test_refresh_lists_if_changed_does_not_retry_unchanged_failed_file(
    dummy_agent, tmp_path, monkeypatch
):
    agent, mgr = dummy_agent
    cpath = tmp_path / "clients.xlsx"
    cpath.write_bytes(b"not a valid excel file")

    agent.load_client_register()
    assert "client" in agent._list_failed

    called = []
    monkeypatch.setattr(
        agent, "load_client_register", lambda: called.append(True) or False
    )

    # Calling refresh multiple times without changing file must not re-trigger loader
    mgr.refresh_lists_if_changed()
    mgr.refresh_lists_if_changed()
    assert len(called) == 0


def test_a_blank_list_path_is_not_re_reported_every_loop(dummy_agent, caplog):
    """A blank path with the filter on is reported once, at startup.

    The refresh runs every loop. Without its own blank-path guard it stats
    PROJECT_ROOT / "" - the project folder, which exists - calls the loader,
    and the loader logs "CLIENT_LIST_PATH is blank" again: an error line
    every five minutes for as long as the agent runs. (The original version
    of this test turned the filters OFF, where the loader's own check makes
    the outcome identical with or without the guard, so it could not fail.)
    """
    import logging

    agent, mgr = dummy_agent
    agent.settings.client_list_path = ""
    agent.settings.competitor_list_path = "   "
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            mgr.refresh_lists_if_changed()
    assert not [r for r in caplog.records if "blank" in r.getMessage()]
    assert agent.client_register is None


# ---------------------------------------------------------------------------
# 5. Screening methods: active_client, known_competitor, contact_is_closed
# ---------------------------------------------------------------------------


def test_active_client_matches_and_handles_none(dummy_agent, tmp_path):
    _agent, mgr = dummy_agent
    cpath = tmp_path / "clients.xlsx"
    _make_xlsx(cpath, [["SUPPLECO Partner S.r.l."]])
    mgr.load_client_register()

    assert mgr.active_client("SUPPLECO Partner S.r.l.") is not None
    assert mgr.active_client("SUPPLECO Partner") is not None
    assert mgr.active_client("Unrelated Brand") is None
    assert mgr.active_client(None) is None
    assert mgr.active_client("") is None


def test_active_client_fails_open_on_exception(dummy_agent):
    agent, mgr = dummy_agent
    agent.client_register = MagicMock()
    agent.client_register.match.side_effect = RuntimeError("Matching engine crash")

    # Must return None instead of raising
    assert mgr.active_client("Any Company") is None


def test_known_competitor_matches_and_handles_none(dummy_agent, tmp_path):
    _agent, mgr = dummy_agent
    rpath = tmp_path / "competitors.xlsx"
    _make_xlsx(rpath, [["Rival Nutra International"]])
    mgr.load_competitor_register()

    assert mgr.known_competitor("Rival Nutra International") is not None
    assert mgr.known_competitor("Rival Nutra") is not None
    assert mgr.known_competitor("Unrelated Brand") is None
    assert mgr.known_competitor(None) is None
    assert mgr.known_competitor("") is None


def test_known_competitor_fails_open_on_exception(dummy_agent):
    agent, mgr = dummy_agent
    agent.competitor_register = MagicMock()
    agent.competitor_register.match.side_effect = RuntimeError(
        "Competitor engine crash"
    )

    # Must return None instead of raising
    assert mgr.known_competitor("Any Rival") is None


def test_contact_is_closed_screening(dummy_agent):
    agent, mgr = dummy_agent

    # Suppressed email
    agent.db.add_to_suppression("suppressed@target.com", reason="unsubscribed")
    assert (
        mgr.contact_is_closed({"email": "suppressed@target.com", "status": "pending"})
        == "suppressed"
    )

    # Closed statuses
    for closed_status in (
        "responded",
        "active_client",
        "unsubscribed",
        "erased",
        "bounced",
        "blocked",
    ):
        assert (
            mgr.contact_is_closed({"email": "open@target.com", "status": closed_status})
            == closed_status
        )

    # Discarded statuses
    for discarded_status in ("irrelevant", "ai_irrelevant", "low_score", "unverified"):
        assert (
            mgr.contact_is_closed(
                {"email": "open@target.com", "status": discarded_status}
            )
            == discarded_status
        )

    # Open / pending statuses return None
    assert (
        mgr.contact_is_closed({"email": "open@target.com", "status": "pending"}) is None
    )
    assert (
        mgr.contact_is_closed({"email": "open@target.com", "status": "qualified"})
        is None
    )
    assert mgr.contact_is_closed({"email": "open@target.com", "status": None}) is None
