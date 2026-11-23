"""The client and competitor lists are encrypted at rest (LISTS_KEY).

Both lists were plain workbooks on disk, so any copy of the folder (a
backup, a shared zip, a synced folder) showed SUPPLECO's customers and rivals.

Now (third round, stage 2): with LISTS_KEY set, the agent reads only an
encrypted list, in memory only, writes Mark as client back encrypted, and
refuses a plain list as it refuses a missing one, so live sending stops. A
wrong key or a changed file is refused in words, never read as an empty list
(an empty client list would email SUPPLECO's customers). Without the key,
doctor warns that the lists are plain. `main.py lists` encrypts a list,
checks the configured ones, and decrypts one only to a named --out.

Every list here is invented.
"""

import io
import os
import zipfile

import pytest
from openpyxl import Workbook

import main
from agent import OutreachAgent
from config import load_settings
from modules import list_crypto
from modules.clients import load_from_xlsx
from modules.compliance import mask_personal
from modules.database import Database
from modules.list_crypto import MAGIC, ListRefused, read_list

KEY = "invented-lists-key-" + "0" * 32
OTHER_KEY = "another-lists-key-" + "1" * 32
CLIENTS = ["Fictitious Alpha S.r.l.", "Imaginary Beta GmbH"]
RIVALS = ["Pretend Rival Integratori S.r.l."]
PUBLIC = "https://out.suppleco.example"


def _workbook_bytes(names) -> bytes:
    wb = Workbook()
    for name in names:
        wb.active.append([name])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _plain(path, names=CLIENTS):
    path.write_bytes(_workbook_bytes(names))
    return path


def _encrypted(path, names=CLIENTS, key=KEY):
    path.write_bytes(list_crypto.encrypt(_workbook_bytes(names), key))
    return path


def _settings(settings_factory, tmp_path, **kw):
    if "client_list_path" not in kw:
        kw["client_list_path"] = str(_encrypted(tmp_path / "clients.xlsx.enc"))
    kw.setdefault("LISTS_KEY", KEY)
    return settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        imap_enabled=False,
        base_url=PUBLIC,
        **kw,
    )


def _doctor(settings, label, for_page=False):
    return {
        c.label: c
        for c in main.run_doctor_checks(settings, network=False, for_page=for_page)
    }[label]


def _flip_a_byte(path):
    data = bytearray(path.read_bytes())
    middle = len(MAGIC) + (len(data) - len(MAGIC)) // 2
    data[middle] = ord("A") if data[middle] != ord("A") else ord("B")
    path.write_bytes(bytes(data))


# -- the file -----------------------------------------------------------------


def test_an_encrypted_list_shows_nothing_without_the_key(tmp_path):
    path = _encrypted(tmp_path / "clients.xlsx.enc")
    data = path.read_bytes()

    assert data.startswith(MAGIC)
    assert not zipfile.is_zipfile(path), "the workbook is readable as it is"
    for name in CLIENTS:
        assert name.encode() not in data
    with pytest.raises(ListRefused, match="could not be decrypted"):
        load_from_xlsx(path, key=OTHER_KEY)


def test_the_key_reads_it_back(tmp_path):
    path = _encrypted(tmp_path / "clients.xlsx.enc")

    assert load_from_xlsx(path, key=KEY) == CLIENTS


def test_a_plain_list_is_refused_while_the_key_is_set(tmp_path):
    path = _plain(tmp_path / "clients.xlsx")

    with pytest.raises(ListRefused, match="plain file, and LISTS_KEY is set"):
        load_from_xlsx(path, key=KEY)


def test_an_encrypted_list_without_the_key_is_refused(tmp_path):
    path = _encrypted(tmp_path / "clients.xlsx.enc")

    with pytest.raises(ListRefused, match="LISTS_KEY is blank"):
        load_from_xlsx(path, key="")


@pytest.mark.parametrize(
    "damage",
    [
        _flip_a_byte,
        lambda p: p.write_bytes(p.read_bytes()[:-40]),
        lambda p: p.write_bytes(MAGIC),
    ],
    ids=["one byte changed", "cut short", "nothing after the header"],
)
def test_a_changed_file_is_refused_never_read_as_empty(tmp_path, damage):
    path = _encrypted(tmp_path / "clients.xlsx.enc")
    damage(path)

    with pytest.raises(ListRefused, match="could not be decrypted"):
        read_list(path, KEY)


def test_a_refusal_never_names_the_key(tmp_path):
    path = _encrypted(tmp_path / "clients.xlsx.enc")
    with pytest.raises(ListRefused) as refused:
        load_from_xlsx(path, key=OTHER_KEY)
    assert KEY not in str(refused.value) and OTHER_KEY not in str(refused.value)


# -- the agent ------------------------------------------------------------------


def test_the_agent_screens_with_an_encrypted_list(settings_factory, tmp_path):
    agent = OutreachAgent(_settings(settings_factory, tmp_path))

    assert agent._active_client("Fictitious Alpha")
    assert agent._send_blocker(False) is None


def test_the_agent_writes_no_plain_copy_while_reading(settings_factory, tmp_path):
    before = set(tmp_path.iterdir())
    OutreachAgent(_settings(settings_factory, tmp_path))
    added = {p.name for p in set(tmp_path.iterdir()) - before}

    assert not [n for n in added if n.endswith(".xlsx")], added


@pytest.mark.parametrize(
    "make",
    [
        lambda d: _plain(d / "clients.xlsx"),
        lambda d: _encrypted(d / "clients.xlsx.enc", key=OTHER_KEY),
    ],
    ids=["a plain list", "another key"],
)
def test_a_client_list_it_refuses_refuses_live_sends(settings_factory, tmp_path, make):
    path = make(tmp_path)
    agent = OutreachAgent(
        _settings(settings_factory, tmp_path, client_list_path=str(path))
    )

    assert agent.client_register is None, "a refused list was read as a list"
    reason = agent._send_blocker(False)
    assert reason and "CLIENT_LIST_PATH" in reason, reason
    rows = agent.db.conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE action = 'CLIENT_LIST_MISSING'"
    ).fetchone()[0]
    assert rows == 1
    assert agent._send_blocker(True) is None, "dry runs still work"


def test_a_competitor_list_it_refuses_refuses_live_sends(settings_factory, tmp_path):
    rivals = _plain(tmp_path / "rivals.xlsx", RIVALS)
    agent = OutreachAgent(
        _settings(
            settings_factory,
            tmp_path,
            competitor_filter_enabled=True,
            competitor_list_path=str(rivals),
        )
    )

    reason = agent._send_blocker(False)
    assert reason and "COMPETITOR_LIST_PATH" in reason, reason


def test_an_encrypted_competitor_list_screens(settings_factory, tmp_path):
    rivals = _encrypted(tmp_path / "rivals.xlsx.enc", RIVALS)
    agent = OutreachAgent(
        _settings(
            settings_factory,
            tmp_path,
            competitor_filter_enabled=True,
            competitor_list_path=str(rivals),
        )
    )

    assert agent._known_competitor("Pretend Rival Integratori")
    assert agent._send_blocker(False) is None


def test_the_ready_card_counts_an_encrypted_competitor_list(settings_factory, tmp_path):
    rivals = _encrypted(tmp_path / "rivals.xlsx.enc", RIVALS)
    s = _settings(
        settings_factory,
        tmp_path,
        competitor_list_path=str(rivals),
        blacklist_check_enabled=False,
    )

    resp = main.create_dashboard_app(s).test_client().get("/api/readiness")

    assert resp.status_code == 200
    assert resp.get_json()["competitor"] == {"state": "ok", "count": 1}


def test_a_changed_file_on_reload_keeps_the_list_it_had(settings_factory, tmp_path):
    agent = OutreachAgent(_settings(settings_factory, tmp_path))
    path = tmp_path / "clients.xlsx.enc"
    _flip_a_byte(path)
    later = os.stat(path).st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(later, later))

    agent.refresh_lists_if_changed()

    assert agent._active_client("Fictitious Alpha"), "the screen fell back to none"
    assert agent._send_blocker(False) is None


# -- Mark as client -------------------------------------------------------------


def _mark(settings, tmp_path, organization="Nuova Azienda SRL"):
    db = Database(settings.get_abs_db_path())
    cid = db.add_contact(
        db.log_search("q"), "buyer@nuova.it", organization=organization
    )
    resp = (
        main.create_dashboard_app(settings)
        .test_client()
        .post(f"/api/contacts/{cid}/action", json={"action": "mark_client"})
    )
    return resp, db, cid


def test_marking_a_client_writes_the_list_back_encrypted(settings_factory, tmp_path):
    s = _settings(settings_factory, tmp_path)
    path = tmp_path / "clients.xlsx.enc"

    resp, _db, _cid = _mark(s, tmp_path)

    assert resp.status_code == 200, resp.get_json()
    assert path.read_bytes().startswith(MAGIC), "the list was written back plain"
    assert load_from_xlsx(path, key=KEY) == [*CLIENTS, "Nuova Azienda SRL"]
    backups = list(tmp_path.glob("clients.xlsx.backup-*.enc"))
    assert len(backups) == 1
    assert load_from_xlsx(backups[0], key=KEY) == CLIENTS
    leftovers = [
        p.name for p in tmp_path.iterdir() if p.suffix == ".xlsx" or "tmp" in p.name
    ]
    assert leftovers == [], leftovers


def test_marking_a_client_in_a_plain_list_is_refused_with_the_key_set(
    settings_factory, tmp_path
):
    path = _plain(tmp_path / "clients.xlsx")
    before = path.read_bytes()
    s = _settings(settings_factory, tmp_path, client_list_path=str(path))

    resp, db, cid = _mark(s, tmp_path)

    assert resp.status_code == 409
    assert "plain file" in resp.get_json()["error"]
    assert path.read_bytes() == before
    assert list(tmp_path.glob("clients.backup-*")) == []
    assert db.get_contact_by_id(cid)["status"] == "pending"


# -- doctor -------------------------------------------------------------------


def test_doctor_warns_while_the_lists_are_plain(settings_factory, tmp_path):
    s = _settings(
        settings_factory,
        tmp_path,
        LISTS_KEY="",
        client_list_path=str(_plain(tmp_path / "clients.xlsx")),
    )

    check = _doctor(s, "Lists encrypted")

    assert check.ok is False and not check.blocking
    assert "LISTS_KEY blank" in check.detail
    assert _doctor(s, "Client list file").ok is True


def test_doctor_passes_encrypted_lists(settings_factory, tmp_path):
    s = _settings(settings_factory, tmp_path)

    assert _doctor(s, "Lists encrypted").ok is True
    client = _doctor(s, "Client list file")
    assert client.ok is True and "2 companies" in client.detail


@pytest.mark.parametrize("for_page", [False, True], ids=["terminal", "page"])
def test_doctor_fails_a_plain_list_while_the_key_is_set(
    settings_factory, tmp_path, for_page
):
    path = _plain(tmp_path / "clients.xlsx")
    s = _settings(settings_factory, tmp_path, client_list_path=str(path))

    client = _doctor(s, "Client list file", for_page)

    assert client.ok is False and client.blocking
    assert "plain file, and LISTS_KEY is set" in client.detail
    assert _doctor(s, "Lists encrypted", for_page).ok is False


def test_doctor_fails_a_list_the_key_cannot_open(settings_factory, tmp_path):
    path = _encrypted(tmp_path / "clients.xlsx.enc", key=OTHER_KEY)
    s = _settings(settings_factory, tmp_path, client_list_path=str(path))

    client = _doctor(s, "Client list file")

    assert client.ok is False and client.blocking
    assert "could not be decrypted" in client.detail


def test_doctor_names_a_plain_copy_left_beside_the_encrypted_list(
    settings_factory, tmp_path
):
    s = _settings(settings_factory, tmp_path)
    _plain(tmp_path / "clients.xlsx")
    _plain(tmp_path / "clients.backup-20261101-090000.xlsx")

    check = _doctor(s, "Lists encrypted")

    assert check.ok is False and not check.blocking
    assert "clients.xlsx" in check.detail
    assert "clients.backup-20261101-090000.xlsx" in check.detail


# -- the key ------------------------------------------------------------------


def test_a_short_lists_key_refuses_to_start(monkeypatch):
    short = "k" * 31
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", "u" * 32)
    monkeypatch.setenv("LISTS_KEY", short)
    with pytest.raises(ValueError, match="LISTS_KEY is shorter than 32") as refused:
        load_settings()
    assert short not in str(refused.value)


def test_a_32_character_lists_key_starts(monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", "u" * 32)
    monkeypatch.setenv("LISTS_KEY", "k" * 32)
    assert load_settings().lists_key == "k" * 32


def test_the_key_is_masked_in_a_log_line():
    assert KEY not in mask_personal(f"LISTS_KEY={KEY}")


def test_the_key_stays_out_of_a_settings_repr(settings_factory, tmp_path):
    s = _settings(settings_factory, tmp_path)
    assert KEY not in repr(s) + str(s)


def test_encrypted_lists_stay_out_of_git_the_image_and_audit_copies(
    tmp_path, monkeypatch
):
    """Every file the agent keeps beside an encrypted list, named as the
    agent names it: the list, the dated backup Mark as client makes
    (<name>.xlsx.backup-<date>.enc, which "*.xlsx.enc" let through), and the
    temporary file a crash leaves behind."""
    import fnmatch

    from config import PROJECT_ROOT
    from modules.clients import append_to_xlsx
    from scripts.mutation_audit import _ignore

    path = _encrypted(tmp_path / "Elenco Clienti (1).xlsx.enc")
    swapped = []
    real_replace = os.replace

    def spy_replace(src, dst):
        swapped.append(os.path.basename(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    assert append_to_xlsx(path, "Nuova Azienda SRL", key=KEY) is True
    names = sorted({p.name for p in tmp_path.iterdir()} | set(swapped))
    assert len(names) == 3, names

    for file in (".gitignore", ".dockerignore"):
        lines = (PROJECT_ROOT / file).read_text(encoding="utf-8").splitlines()
        patterns = [
            line.removeprefix("**/") for line in lines if line and line[0] != "#"
        ]
        through = [n for n in names if not any(fnmatch.fnmatch(n, p) for p in patterns)]
        assert through == [], f"{file} lets through {through}"
    assert _ignore(".", names) == set(names)


# -- main.py lists ------------------------------------------------------------


def _cli(settings, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", "lists", *argv])
    try:
        main.main()
        status = 0
    except SystemExit as e:
        status = e.code
    return status, capsys.readouterr().out


def test_encrypt_writes_a_checked_copy_and_keeps_the_plain_file(
    settings_factory, tmp_path, monkeypatch, capsys
):
    plain = _plain(tmp_path / "clients.xlsx")
    s = _settings(settings_factory, tmp_path, client_list_path=str(plain))

    status, out = _cli(s, monkeypatch, capsys, "encrypt", str(plain))

    target = tmp_path / "clients.xlsx.enc"
    assert status == 0, out
    assert read_list(target, KEY) == plain.read_bytes()
    assert plain.exists(), "the plain file was deleted without --delete-plain"
    assert "decrypts to the same file" in out
    assert "Now set CLIENT_LIST_PATH=" in out and "clients.xlsx.enc" in out
    assert f"delete the plain file {plain}" in out
    assert KEY not in out


def test_encrypt_runs_without_the_erasure_key(
    settings_factory, tmp_path, monkeypatch, capsys
):
    plain = _plain(tmp_path / "clients.xlsx")
    s = _settings(
        settings_factory, tmp_path, ERASURE_KEY="", client_list_path=str(plain)
    )

    status, out = _cli(s, monkeypatch, capsys, "encrypt", str(plain))

    assert status == 0, out
    assert (tmp_path / "clients.xlsx.enc").exists()


def test_encrypt_with_delete_plain_overwrites_and_deletes_it(
    settings_factory, tmp_path, monkeypatch, capsys
):
    plain = _plain(tmp_path / "clients.xlsx")
    s = _settings(settings_factory, tmp_path, client_list_path=str(plain))
    overwritten = []
    real = list_crypto.overwrite_and_delete
    monkeypatch.setattr(
        list_crypto,
        "overwrite_and_delete",
        lambda p: overwritten.append(p) or real(p),
    )

    status, out = _cli(s, monkeypatch, capsys, "encrypt", str(plain), "--delete-plain")

    assert status == 0, out
    assert not plain.exists()
    assert overwritten == [plain]
    assert load_from_xlsx(tmp_path / "clients.xlsx.enc", key=KEY) == CLIENTS


def test_overwrite_and_delete_writes_zeros_first(tmp_path, monkeypatch):
    path = _plain(tmp_path / "clients.xlsx")
    size = path.stat().st_size
    seen = []
    real_unlink = type(path).unlink

    def spy_unlink(self, *a, **k):
        seen.append(self.read_bytes())
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(type(path), "unlink", spy_unlink)
    list_crypto.overwrite_and_delete(path)

    assert seen == [b"\0" * size]
    assert not path.exists()


def test_encrypt_names_plain_backups_still_on_disk(
    settings_factory, tmp_path, monkeypatch, capsys
):
    plain = _plain(tmp_path / "clients.xlsx")
    backup = _plain(tmp_path / "clients.backup-20261101-090000.xlsx")
    s = _settings(settings_factory, tmp_path, client_list_path=str(plain))

    status, out = _cli(s, monkeypatch, capsys, "encrypt", str(plain))

    assert status == 0, out
    assert str(backup) in out


@pytest.mark.parametrize(
    "case",
    ["no key", "already encrypted", "not a workbook", "target exists", "missing"],
)
def test_encrypt_refuses_and_writes_nothing(
    settings_factory, tmp_path, monkeypatch, capsys, case
):
    source = tmp_path / "clients.xlsx"
    target = tmp_path / "clients.xlsx.enc"
    key = KEY
    if case == "no key":
        _plain(source)
        key = ""
    elif case == "already encrypted":
        _encrypted(source)
    elif case == "not a workbook":
        source.write_bytes(b"not a workbook")
    elif case == "target exists":
        _plain(source)
        target.write_bytes(b"clients marked since")
    s = _settings(
        settings_factory, tmp_path, LISTS_KEY=key, client_list_path=str(source)
    )
    before = target.read_bytes() if target.exists() else None

    status, out = _cli(s, monkeypatch, capsys, "encrypt", str(source))

    assert status == 1, out
    assert (target.read_bytes() if target.exists() else None) == before


def test_encrypt_deletes_a_copy_that_does_not_decrypt_to_the_same_file(
    settings_factory, tmp_path, monkeypatch, capsys
):
    plain = _plain(tmp_path / "clients.xlsx")
    s = _settings(settings_factory, tmp_path, client_list_path=str(plain))
    real = list_crypto.encrypt
    monkeypatch.setattr(
        list_crypto, "encrypt", lambda data, key: real(data + b"!", key)
    )

    status, out = _cli(s, monkeypatch, capsys, "encrypt", str(plain), "--delete-plain")

    assert status == 1, out
    assert not (tmp_path / "clients.xlsx.enc").exists()
    assert plain.exists(), "the plain file went with a copy that was not checked"


def test_encrypt_keeps_nothing_when_the_write_fails(
    settings_factory, tmp_path, monkeypatch, capsys
):
    plain = _plain(tmp_path / "clients.xlsx")
    s = _settings(settings_factory, tmp_path, client_list_path=str(plain))

    def full_disk(data, key):
        raise OSError("No space left on device")

    monkeypatch.setattr(list_crypto, "encrypt", full_disk)

    status, out = _cli(s, monkeypatch, capsys, "encrypt", str(plain), "--delete-plain")

    assert status == 1 and "could not be written" in out
    assert not (tmp_path / "clients.xlsx.enc").exists()
    assert plain.exists()


def test_decrypt_needs_an_out_path(settings_factory, tmp_path, monkeypatch, capsys):
    path = _encrypted(tmp_path / "clients.xlsx.enc")
    s = _settings(settings_factory, tmp_path)

    status, _out = _cli(s, monkeypatch, capsys, "decrypt", str(path))

    assert status == 2
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".xlsx") == []


def test_decrypt_writes_the_plain_copy_with_a_warning(
    settings_factory, tmp_path, monkeypatch, capsys
):
    path = _encrypted(tmp_path / "clients.xlsx.enc")
    out_path = tmp_path / "plain-copy.xlsx"
    s = _settings(settings_factory, tmp_path)

    status, out = _cli(
        s, monkeypatch, capsys, "decrypt", str(path), "--out", str(out_path)
    )

    assert status == 0, out
    assert "WARNING" in out
    assert load_from_xlsx(out_path, key="") == CLIENTS


def test_decrypt_never_overwrites(settings_factory, tmp_path, monkeypatch, capsys):
    path = _encrypted(tmp_path / "clients.xlsx.enc")
    out_path = tmp_path / "plain-copy.xlsx"
    out_path.write_bytes(b"something else")
    s = _settings(settings_factory, tmp_path)

    status, _out = _cli(
        s, monkeypatch, capsys, "decrypt", str(path), "--out", str(out_path)
    )

    assert status == 1
    assert out_path.read_bytes() == b"something else"


def test_decrypt_with_the_wrong_key_writes_nothing(
    settings_factory, tmp_path, monkeypatch, capsys
):
    path = _encrypted(tmp_path / "clients.xlsx.enc", key=OTHER_KEY)
    out_path = tmp_path / "plain-copy.xlsx"
    s = _settings(settings_factory, tmp_path, client_list_path=str(path))

    status, out = _cli(
        s, monkeypatch, capsys, "decrypt", str(path), "--out", str(out_path)
    )

    assert status == 1 and "could not be decrypted" in out
    assert not out_path.exists()


def test_decrypt_to_a_missing_folder_says_so(
    settings_factory, tmp_path, monkeypatch, capsys
):
    path = _encrypted(tmp_path / "clients.xlsx.enc")
    out_path = tmp_path / "no such folder" / "plain-copy.xlsx"
    s = _settings(settings_factory, tmp_path)

    status, out = _cli(
        s, monkeypatch, capsys, "decrypt", str(path), "--out", str(out_path)
    )

    assert status == 1 and "could not be written" in out


def test_decrypt_keeps_no_half_written_plain_copy(
    settings_factory, tmp_path, monkeypatch, capsys
):
    """A disk that fills up mid-write left half the workbook in plain form,
    with a traceback and no warning."""
    path = _encrypted(tmp_path / "clients.xlsx.enc")
    out_path = tmp_path / "plain-copy.xlsx"
    s = _settings(settings_factory, tmp_path)
    real_open = open

    class HalfWritten:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, data):
            self.f.write(data[: len(data) // 2])
            self.f.flush()
            raise OSError("No space left on device")

    def full_disk(file, *args, **kwargs):
        f = real_open(file, *args, **kwargs)
        return HalfWritten(f) if os.path.samefile(file, out_path) else f

    monkeypatch.setattr(list_crypto, "open", full_disk, raising=False)

    status, out = _cli(
        s, monkeypatch, capsys, "decrypt", str(path), "--out", str(out_path)
    )

    assert status == 1 and "could not be written" in out
    assert not out_path.exists(), "half the list was left on disk in plain form"
    assert "WARNING" not in out


def test_check_passes_encrypted_lists(settings_factory, tmp_path, monkeypatch, capsys):
    s = _settings(settings_factory, tmp_path)

    status, out = _cli(s, monkeypatch, capsys, "check")

    assert status == 0, out
    assert "encrypted, 2 companies" in out


def test_check_fails_a_list_the_agent_would_refuse(
    settings_factory, tmp_path, monkeypatch, capsys
):
    path = _plain(tmp_path / "clients.xlsx")
    s = _settings(settings_factory, tmp_path, client_list_path=str(path))

    status, out = _cli(s, monkeypatch, capsys, "check")

    assert status == 1
    assert "REFUSED" in out and "plain file" in out


def test_check_names_plain_copies(settings_factory, tmp_path, monkeypatch, capsys):
    s = _settings(settings_factory, tmp_path)
    _plain(tmp_path / "clients.xlsx")

    status, out = _cli(s, monkeypatch, capsys, "check")

    assert status == 0, out
    assert "A plain copy is still on disk" in out and "clients.xlsx -" in out


def test_check_says_the_lists_are_plain_without_the_key(
    settings_factory, tmp_path, monkeypatch, capsys
):
    path = _plain(tmp_path / "clients.xlsx")
    s = _settings(settings_factory, tmp_path, LISTS_KEY="", client_list_path=str(path))

    status, out = _cli(s, monkeypatch, capsys, "check")

    assert status == 0, out
    assert "LISTS_KEY is blank" in out and "plain, 2 companies" in out
