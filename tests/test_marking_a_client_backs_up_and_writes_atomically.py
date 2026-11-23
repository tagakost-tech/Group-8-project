"""Marking a contact as a client backs up the workbook and writes it atomically.

The dashboard's "Mark as client" action writes to SUPPLECO's hand-edited client
workbook. Opening the workbook with openpyxl and saving directly over the path
kept no backup and truncated the original file before writing the new archive,
so a full disk or crash mid-write left a damaged spreadsheet.
"""

import os
import zipfile
from pathlib import Path

from openpyxl import Workbook

import main
from modules import clients
from modules.clients import append_to_xlsx, load_from_xlsx
from modules.database import Database

INITIAL_CLIENTS = [
    "ABIOGEN PHARMA S.p.A.",
    "ZUCCARO S.R.L.",
]


def _workbook(tmp_path: Path, names=INITIAL_CLIENTS) -> Path:
    path = tmp_path / "clients.xlsx"
    wb = Workbook()
    for name in names:
        wb.active.append([name])
    wb.save(path)
    wb.close()
    return path


def test_marking_a_client_saves_a_dated_backup_next_to_the_workbook(
    tmp_path, settings_factory
):
    path = _workbook(tmp_path)
    s = settings_factory(db_path=str(tmp_path / "m.db"), client_list_path=str(path))
    app = main.create_dashboard_app(s)
    db = Database(str(tmp_path / "m.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "buyer@nuova.it", organization="Nuova Azienda SRL")

    resp = app.test_client().post(
        f"/api/contacts/{cid}/action", json={"action": "mark_client"}
    )

    assert resp.status_code == 200, resp.get_json()
    assert load_from_xlsx(path, key="") == [*INITIAL_CLIENTS, "Nuova Azienda SRL"]

    backups = sorted(tmp_path.glob("clients.backup-*.xlsx"))
    assert len(backups) == 1
    assert load_from_xlsx(backups[0], key="") == INITIAL_CLIENTS


def test_only_the_newest_backups_are_kept(tmp_path, monkeypatch):
    path = _workbook(tmp_path)
    limit = getattr(clients, "BACKUP_COPIES", 5)
    stamps = iter(range(100))
    monkeypatch.setattr(
        clients, "_stamp", lambda: f"20261102-0900{next(stamps):02d}", raising=False
    )

    total = limit + 3
    for i in range(total):
        assert append_to_xlsx(path, f"Nuova Azienda {i} SRL", key="") is True

    backups = sorted(tmp_path.glob("clients.backup-*.xlsx"))
    assert len(backups) == limit
    assert backups[0].name == "clients.backup-20261102-090003.xlsx"
    assert load_from_xlsx(backups[-1], key="") == [
        *INITIAL_CLIENTS,
        *(f"Nuova Azienda {i} SRL" for i in range(total - 1)),
    ]


def test_an_already_listed_client_writes_no_backup(tmp_path):
    path = _workbook(tmp_path)

    assert append_to_xlsx(path, "Zuccaro srl", key="") is False
    assert list(tmp_path.glob("clients.backup-*.xlsx")) == []


def test_a_full_disk_mid_write_leaves_the_workbook_undamaged(
    tmp_path, settings_factory, monkeypatch
):
    path = _workbook(tmp_path)
    s = settings_factory(db_path=str(tmp_path / "m.db"), client_list_path=str(path))
    app = main.create_dashboard_app(s)
    db = Database(str(tmp_path / "m.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "buyer@nuova.it", organization="Nuova Azienda SRL")

    real_writestr = zipfile.ZipFile.writestr
    writes = []

    def fail_mid_write(self, zinfo_or_arcname, data, *args, **kwargs):
        writes.append(1)
        if len(writes) >= 2:
            raise OSError("No space left on device")
        return real_writestr(self, zinfo_or_arcname, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_mid_write)

    resp = app.test_client().post(
        f"/api/contacts/{cid}/action", json={"action": "mark_client"}
    )

    assert resp.status_code == 500
    assert db.get_contact_by_id(cid)["status"] == "pending"
    assert load_from_xlsx(path, key="") == INITIAL_CLIENTS
    assert list(tmp_path.glob("*tmp*")) == []


def test_append_writes_a_temporary_file_in_the_same_folder_and_replaces(
    tmp_path, monkeypatch
):
    path = _workbook(tmp_path)
    real_replace = os.replace
    swaps = []

    def spy_replace(src, dst):
        src_path, dst_path = Path(src), Path(dst)
        swaps.append((src_path, dst_path, src_path.exists()))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    assert append_to_xlsx(path, "Nuova Azienda SRL", key="") is True
    assert len(swaps) == 1
    src_path, dst_path, existed = swaps[0]
    assert existed is True
    assert src_path.parent == path.parent
    assert src_path != path
    assert dst_path == path
    assert not src_path.exists()
