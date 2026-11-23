"""The handover archive gives SUPPLECO the agent and its data, and nothing else.

SUPPLECO gets a full copy: the code, the docs, and its own data - the database
with every contact, opt-out, bounce, block and erasure record, the journal,
and the encrypted lists - plus ERASURE_KEY and LISTS_KEY, without which the
database's opt-outs and erasures would no longer be recognised. Four groups
stay out: folders named "internal" and docs/review, .env and its variants,
.git, and working files (logs, caches, old backups and other databases, key
files).

scripts/make_handover.py packs it from a checked backup, never the live file,
scans what it packs and refuses on a secret or client data outside the data,
and encrypts it under a passphrase (scrypt, AES-GCM). `main.py
handover-restore` checks it, unpacks it into a new folder, refuses one that
already holds a .env or a database, and writes the two keys into a file only
this account can read. No key is ever printed or logged. Invented data only.
"""

import getpass
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from openpyxl import Workbook

import main
from conftest import TEST_ERASURE_KEY
from modules import handover, journal, list_crypto
from modules.database import Database, erasure_key_fingerprint

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import make_handover

PASSPHRASE = "violet harbour seven lanterns"
LISTS_KEY = "invented-lists-key-" + "L" * 32
UNSUBSCRIBE = "invented-unsubscribe-secret-" + "U" * 32
DASHBOARD = "invented-dashboard-pass-9Q"
MAILBOX = "invented-mailbox-pass-7Z"
OPTED_OUT = ["anna.rossi@brandco.example", "luca.verdi@other.example"]
BOUNCED = "gone@missing.example"
PROSPECT = "marta.neri@prospect.example"
CLIENT = "Invented Client Holding S.r.l."


@pytest.fixture
def dashboard_socket():
    """A port held, not listened on, for the whole test: the handover finds
    no dashboard there, and no other test running beside it can take it."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        yield held


def _write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def _client_list(path: Path) -> Path:
    wb = Workbook()
    wb.active.append([CLIENT])
    plain = path.with_name("clients.xlsx")
    wb.save(plain)
    path.write_bytes(list_crypto.encrypt(plain.read_bytes(), LISTS_KEY))
    plain.unlink()
    return path


@pytest.fixture
def install(tmp_path, settings_factory, dashboard_socket):
    """An invented install, with one file in every group that must stay out."""
    root = tmp_path / "agent"
    _write(root / "main.py", "print('the agent')\n")
    _write(root / "modules" / "tool.py", "VALUE = 1\n")
    _write(root / "docs" / "USER_MANUAL.md", "# Manual\n")
    _write(root / ".env.example", "DASHBOARD_PASSWORD=\nERASURE_KEY=\n")
    _write(root / "data" / "spool" / "optouts" / "held.json", '{"token": "x"}\n')
    out = {
        "docs/review/prices.md": "the owner's pricing\n",
        "docs/review/deep/deal.md": "deal notes\n",
        "notes-internal/meeting.md": "internal notes\n",
        "docs/Internal/security.md": "internal security notes\n",
        ".env": f"DASHBOARD_PASSWORD={DASHBOARD}\n",
        ".env.bak": f"SMTP_PASSWORD={MAILBOX}\n",
        "config/prod.env": f"SMTP_PASSWORD={MAILBOX}\n",
        ".git/config": "[core]\n",
        "modules/.git": "gitdir: elsewhere\n",
        "logs/agent.log": "a log line\n",
        "modules/__pycache__/tool.cpython-311.pyc": "cache",
        ".pytest_cache/v/cache": "cache",
        ".venv/pyvenv.cfg": "home = python\n",
        "tests/fixture.db": "a test database",
        "data/backups/agent-20260101-000000.db": "an old backup",
        "data/journal.tmp": "a rewrite's copy",
        "certs/agent.pem": "certificate",
        "certs/agent.key": "key",
        "certs/agent.pfx": "pfx",
        "certs/agent.p12": "p12",
    }
    for rel, text in out.items():
        _write(root / rel, text)
    lists = _client_list(root / "data" / "clients.xlsx.enc")
    s = settings_factory(
        db_path=str(root / "data" / "agent.db"),
        client_list_path=str(lists),
        LISTS_KEY=LISTS_KEY,
        UNSUBSCRIBE_SECRET=UNSUBSCRIBE,
        DASHBOARD_PASSWORD=DASHBOARD,
        SMTP_PASSWORD=MAILBOX,
        dashboard_port=dashboard_socket.getsockname()[1],
    )
    db = Database(s.get_abs_db_path())
    search = db.log_search("q")
    for email in [*OPTED_OUT, BOUNCED, PROSPECT]:
        db.add_contact(search, email, organization="Brand")
    for email in OPTED_OUT:
        db.add_to_suppression(email, reason="unsubscribe")
    db.add_to_suppression(BOUNCED, reason="bounce")
    db.close_thread_conn()
    return root, s, out


def _make(root, s, archive, passphrase=PASSPHRASE, lines=None):
    return make_handover.make_handover(
        root, s, archive, passphrase, say=(lines.append if lines is not None else print)
    )


def _restore(archive, into, passphrase=PASSPHRASE, lines=None, doctor=None):
    ran = []
    folder = handover.restore(
        archive,
        lambda: passphrase,
        into,
        out=(lines.append if lines is not None else print),
        doctor=doctor or ran.append,
    )
    return folder, ran


def _all_files(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


def test_a_round_trip_keeps_every_opt_out_and_carries_the_keys(
    install, tmp_path, capsys, caplog
):
    root, s, out = install
    archive = tmp_path / "handover" / "suppleco-agent.suppleco"
    archive.parent.mkdir()
    made, restored = [], []
    counts = _make(root, s, archive, lines=made)
    assert counts["contacts"] == 4 and counts["suppressed"] == 3
    assert (archive.parent / handover.OPENER_NAME).read_bytes() == (
        ROOT / "modules" / "handover.py"
    ).read_bytes()
    folder, doctor_ran = _restore(
        archive, tmp_path / "suppleco" / "agent", lines=restored
    )

    assert doctor_ran == [folder]
    packed, _, _ = handover.unpack(handover.unseal(archive.read_bytes(), PASSPHRASE))
    # SQLite makes the -wal and -shm files when the database is first read.
    on_disk = {rel for rel in _all_files(folder) if not rel.endswith(("-wal", "-shm"))}
    assert on_disk == set(packed) | {handover.KEYS_FILE}
    shipped = set(packed) | {handover.KEYS_FILE}
    db = Database(str(folder / "data" / "agent.db"))
    for email in [*OPTED_OUT, BOUNCED]:
        assert db.is_suppressed(email), email
    assert not db.is_suppressed(PROSPECT)
    assert journal.unreplayed(folder / "data" / "agent.db") == 0
    db.close_thread_conn()

    keys = (folder / handover.KEYS_FILE).read_text(encoding="utf-8").splitlines()
    assert f"ERASURE_KEY={TEST_ERASURE_KEY}" in keys
    assert f"LISTS_KEY={LISTS_KEY}" in keys
    assert list_crypto.read_list(folder / "data" / "clients.xlsx.enc", LISTS_KEY)

    for rel in out:
        assert rel not in shipped, rel
    assert shipped == {
        "main.py",
        "modules/tool.py",
        "docs/USER_MANUAL.md",
        ".env.example",
        "data/spool/optouts/held.json",
        "data/clients.xlsx.enc",
        "data/agent.db",
        "data/journal.jsonl",
        handover.README,
        handover.KEYS_FILE,
    }
    readme = (folder / handover.README).read_text(encoding="utf-8")
    assert "Never change `ERASURE_KEY`" in readme

    captured = capsys.readouterr()
    printed = "\n".join(made + restored) + captured.out + captured.err + caplog.text
    logged = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in (root / "logs").rglob("*")
        if p.is_file()
    )
    for secret in (
        TEST_ERASURE_KEY,
        LISTS_KEY,
        UNSUBSCRIBE,
        DASHBOARD,
        MAILBOX,
        PASSPHRASE,
    ):
        assert secret not in printed
        assert secret not in logged
        assert secret.encode() not in archive.read_bytes()


def test_an_erased_address_stays_suppressed_under_the_carried_key(install, tmp_path):
    root, s, _ = install
    erased = "erased.person@gone.example"
    db = Database(s.get_abs_db_path())
    db.suppress_erased(erased)
    db.close_thread_conn()
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=[])
    folder, _ = _restore(archive, tmp_path / "new", lines=[])
    lines = (folder / handover.KEYS_FILE).read_text(encoding="utf-8").splitlines()
    carried = dict(
        line.split("=", 1) for line in lines if "=" in line and not line.startswith("#")
    )
    assert carried["ERASURE_KEY"] == s.erasure_key
    restored = Database(str(folder / "data" / "agent.db"))
    try:
        assert restored.is_erased(erased) and restored.is_suppressed(erased)
    finally:
        restored.close_thread_conn()


def test_the_live_database_is_never_packed_only_a_fresh_backup(install, tmp_path):
    root, s, _ = install
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=[])
    backups = sorted((root / "data" / "backups").glob("agent-2*.db"))
    newest = [b for b in backups if b.name != "agent-20260101-000000.db"]
    assert newest, "no backup was taken"
    files, _, _ = handover.unpack(handover.unseal(archive.read_bytes(), PASSPHRASE))
    assert files["data/agent.db"] == newest[-1].read_bytes()


def test_a_wrong_passphrase_writes_nothing(install, tmp_path):
    root, s, _ = install
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=[])
    into = tmp_path / "new"
    with pytest.raises(handover.HandoverRefused, match="passphrase is wrong"):
        _restore(archive, into, passphrase=PASSPHRASE + "x", lines=[])
    assert not into.exists()


@pytest.mark.parametrize("where", ["header", "body", "end"])
def test_a_changed_archive_is_refused_and_writes_nothing(install, tmp_path, where):
    root, s, _ = install
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=[])
    blob = bytearray(archive.read_bytes())
    at = {
        "header": len(handover.MAGIC) + 2,
        "body": handover.HEADER_BYTES + len(blob) // 2 // 1,
        "end": len(blob) - 1,
    }[where]
    at = min(at, len(blob) - 1)
    blob[at] ^= 0x01
    archive.write_bytes(bytes(blob))
    into = tmp_path / "new"
    with pytest.raises(handover.HandoverRefused, match="changed or damaged"):
        _restore(archive, into, lines=[])
    assert not into.exists()


def _sealed_zip(entries: dict[str, bytes], manifest_files: dict[str, str]) -> bytes:
    import io
    import json
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in entries.items():
            zf.writestr(handover.PAYLOAD + name, data)
        zf.writestr(
            handover.MANIFEST,
            json.dumps({"format": handover.FORMAT, "files": manifest_files}),
        )
        zf.writestr(handover.KEYS, json.dumps({"ERASURE_KEY": TEST_ERASURE_KEY}))
    return handover.seal(buffer.getvalue(), PASSPHRASE)


@pytest.mark.parametrize(
    "entries, listed",
    [
        ({"main.py": b"changed"}, {"main.py": handover.digest(b"as packed")}),
        ({"main.py": b"x", "extra.py": b"y"}, {"main.py": handover.digest(b"x")}),
        ({"../outside.py": b"x"}, {"../outside.py": handover.digest(b"x")}),
        ({"C:/outside.py": b"x"}, {"C:/outside.py": handover.digest(b"x")}),
    ],
    ids=["a file unlike its hash", "a file not listed", "a path out", "a drive"],
)
def test_contents_unlike_the_archives_own_list_are_refused(tmp_path, entries, listed):
    archive = _write(tmp_path / "a.suppleco", _sealed_zip(entries, listed))
    into = tmp_path / "new"
    with pytest.raises(handover.HandoverRefused, match="do not match"):
        _restore(archive, into, lines=[])
    assert not into.exists()
    assert not (tmp_path / "outside.py").exists()


def test_a_file_that_is_no_archive_is_refused(tmp_path):
    other = _write(tmp_path / "x.suppleco", b"PK\x03\x04 not an archive")
    with pytest.raises(handover.HandoverRefused, match="not a handover archive"):
        _restore(other, tmp_path / "new", lines=[])


@pytest.mark.parametrize(
    "present, refusal",
    [
        (".env", "already holds a .env"),
        (".ENV", "already holds a .env"),
        ("data/agent.db", "already holds a database"),
        ("old/other.sqlite", "already holds a database"),
        ("notes.txt", "is not empty"),
    ],
)
def test_a_folder_with_a_database_or_a_env_is_refused_before_the_passphrase(
    install, tmp_path, present, refusal
):
    root, s, _ = install
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=[])
    into = tmp_path / "existing"
    kept = _write(into / present, "kept as it was")

    def never():
        raise AssertionError("the passphrase was asked for a folder that is refused")

    with pytest.raises(handover.HandoverRefused, match=refusal):
        handover.restore(archive, never, into, out=lambda *_: None, doctor=lambda f: 0)
    assert kept.read_text(encoding="utf-8") == "kept as it was"
    assert _all_files(into) == {present}


def test_an_archive_whose_erasure_key_does_not_open_its_database_is_refused(
    install, tmp_path
):
    root, s, _ = install
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=[])
    files, keys, manifest = handover.unpack(
        handover.unseal(archive.read_bytes(), PASSPHRASE)
    )
    keys["ERASURE_KEY"] = "another-erasure-key-" + "9" * 32
    archive.write_bytes(
        handover.seal(handover.pack(files, keys, manifest["made"]), PASSPHRASE)
    )
    with pytest.raises(handover.HandoverRefused, match="not the one agent.db"):
        _restore(archive, tmp_path / "new", lines=[])


@pytest.mark.parametrize(
    "rel, text, kind",
    [
        ("docs/notes.md", "key: gsk_" + "A1b2" * 6, "a key, token or private key"),
        ("docs/notes.txt", "-----BEGIN RSA " + "PRIVATE KEY-----\n", "a key, token"),
        ("docs/setup.md", "SMTP_PASSWORD=Hunter-" + "42xyz\n", "a password or secret"),
        (
            "modules/settings_copy.py",
            'API_KEY = "' + "abc123" * 3 + '"\n',
            "a password",
        ),
        (
            "docs/howto.md",
            f"the dashboard password is {DASHBOARD}\n",
            "DASHBOARD_PASSWORD",
        ),
        ("tests/x.py", f"ERASE = '{TEST_ERASURE_KEY}'\n", "ERASURE_KEY"),
        ("README.txt", f"{LISTS_KEY}\n", "LISTS_KEY"),
        (
            "docs/example.md",
            f"write to {OPTED_OUT[0]}\n",
            "address(es) the database holds",
        ),
        (
            "docs/clients.md",
            f"We supply {CLIENT.lower()}.\n",
            "name(s) from the client",
        ),
        ("Clienti.xlsx", b"PK\x03\x04 a workbook", "a spreadsheet outside data/"),
        ("docs/export.csv", "name\nanyone\n", "a spreadsheet outside data/"),
    ],
)
def test_a_planted_secret_or_client_data_outside_the_data_is_refused(
    install, tmp_path, rel, text, kind
):
    root, s, _ = install
    _write(root / rel, text)
    archive = tmp_path / "a.suppleco"
    lines = []
    with pytest.raises(handover.HandoverRefused, match="nothing was written"):
        _make(root, s, archive, lines=lines)
    assert not archive.exists()
    assert not archive.with_name(archive.name + ".partial").exists()
    said = "\n".join(lines)
    assert rel in said and kind in said
    for secret in (
        TEST_ERASURE_KEY,
        LISTS_KEY,
        DASHBOARD,
        CLIENT.lower(),
        OPTED_OUT[0],
    ):
        assert secret not in said


def test_client_data_inside_the_data_folder_is_allowed(install, tmp_path):
    root, s, _ = install
    _write(
        root / "data" / "exports" / "answer.json", f'{{"email": "{OPTED_OUT[0]}"}}\n'
    )
    _write(root / "data" / "plain-copy.xlsx", b"PK\x03\x04 a workbook")
    _make(root, s, tmp_path / "a.suppleco", lines=[])


def test_an_encrypted_list_whose_letters_spell_a_key_shape_is_packed(install, tmp_path):
    """An encrypted list is base64 text. About one in fifty spells "sk-" and
    twenty more letters somewhere, the shape of an API key: such a list was
    refused as a secret, now and then, at random."""
    root, s, _ = install
    lists = root / "data" / "competitors.xlsx.backup-2026-11-01.enc"
    _write(lists, list_crypto.MAGIC + b"gAAAAABn" + b"x" * 40 + b"sk-" + b"Q" * 24)
    _make(root, s, tmp_path / "a.suppleco", lines=[])


def test_a_blank_erasure_key_is_refused_in_words(install, tmp_path):
    root, s, _ = install
    s.erasure_key = ""
    with pytest.raises(handover.HandoverRefused, match="ERASURE_KEY is blank"):
        _make(root, s, tmp_path / "a.suppleco", lines=[])
    assert not (tmp_path / "a.suppleco").exists()


def test_a_backup_that_fails_its_check_is_refused_in_words(
    install, tmp_path, monkeypatch
):
    root, s, _ = install

    def failing(db):
        raise RuntimeError("the backup failed its integrity check: page 7")

    monkeypatch.setattr("modules.backup.make_backup", failing)
    with pytest.raises(handover.HandoverRefused, match="backup of the database failed"):
        _make(root, s, tmp_path / "a.suppleco", lines=[])
    assert not (tmp_path / "a.suppleco").exists()


def test_the_shipped_code_and_docs_pass_the_scan():
    """The real working copy, as it would be packed: nothing in it may make
    the owner's handover refuse."""
    files, _ = make_handover.collect(ROOT, PurePosixPath("data"), None)
    staged = {rel: path.read_bytes() for rel, path in files.items()}
    assert "main.py" in staged and "docs/DEPLOY.md" in staged
    assert not any(rel.startswith("docs/review/") for rel in staged)
    assert make_handover.scan(staged, {}, PurePosixPath("data"), set(), set()) == []


def test_a_running_dashboard_stops_the_handover(install, tmp_path, dashboard_socket):
    root, s, _ = install
    dashboard_socket.listen()
    with pytest.raises(handover.HandoverRefused, match="dashboard is running"):
        _make(root, s, tmp_path / "a.suppleco", lines=[])
    assert not (tmp_path / "a.suppleco").exists()


def test_a_held_click_that_matches_no_link_is_named_before_the_handover(
    install, tmp_path
):
    """Such a click is matched only under the owner's UNSUBSCRIBE_SECRET,
    which does not travel; the owner is told before anything is sent."""
    from modules import optout_spool

    root, s, _ = install
    assert optout_spool.set_aside_click(
        s.get_abs_db_path(), "u" + "ab" * 16, "one-click"
    )
    lines = []
    _make(root, s, tmp_path / "a.suppleco", lines=lines)
    said = "\n".join(lines)
    assert "1 held click(s)" in said and "UNSUBSCRIBE_SECRET" in said
    assert UNSUBSCRIBE not in said


def test_a_held_click_of_a_held_address_travels_as_its_opt_out(install, tmp_path):
    """A click on a link the database lost the record of is applied, at the
    recipient's next email, only when the token made under the install's
    UNSUBSCRIBE_SECRET is that click's. SUPPLECO sets a new secret, so it would
    never be applied there and the person who said no could be emailed: the
    owner's install, whose secret it is, ties it to the address before the
    archive is made."""
    from modules import optout_spool
    from modules.unsubscribe import UnsubscribeHandler

    root, s, _ = install
    token = UnsubscribeHandler(None, UNSUBSCRIBE).token_for(PROSPECT)
    assert optout_spool.set_aside_click(s.get_abs_db_path(), token, "one-click", True)
    lines = []
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=lines)
    said = "\n".join(lines)
    assert "1 held click(s) on an unsubscribe link matched a contact" in said
    assert "Note:" not in said and PROSPECT not in said

    folder, _ = _restore(archive, tmp_path / "new", lines=[])
    restored = str(folder / "data" / "agent.db")
    assert not optout_spool.unmatched_tokens(restored)
    assert optout_spool.pending_addresses(restored) == {PROSPECT}
    db = Database(restored)
    try:
        assert optout_spool.replay_optout_spool(db) == 1
        assert db.is_suppressed(PROSPECT)
        assert db.company_blocked(PROSPECT)
    finally:
        db.close_thread_conn()


LATER_OPT_OUT = '{"at":"2026-11-19T10:00:00+01:00", "kind": "opt_out", "seq": 999}\n'


def test_a_database_behind_its_journal_stops_the_handover(install, tmp_path):
    root, s, _ = install
    live = Path(s.get_abs_db_path())
    with open(journal.journal_path(live), "a", encoding="utf-8") as f:
        f.write(LATER_OPT_OUT)
    with pytest.raises(handover.HandoverRefused, match="behind its journal"):
        _make(root, s, tmp_path / "a.suppleco", lines=[])


def test_an_opt_out_journalled_after_the_backup_stops_the_handover(
    install, tmp_path, monkeypatch
):
    """The agent, still running, records an opt-out after the backup was
    taken: the packed database would not hold it."""
    from modules import backup

    root, s, _ = install
    live = Path(s.get_abs_db_path())
    real = backup.make_backup

    def then_an_opt_out(db):
        made = real(db)
        with open(journal.journal_path(live), "a", encoding="utf-8") as f:
            f.write(LATER_OPT_OUT)
        return made

    monkeypatch.setattr(backup, "make_backup", then_an_opt_out)
    with pytest.raises(handover.HandoverRefused, match="wrote to the journal after"):
        _make(root, s, tmp_path / "a.suppleco", lines=[])
    assert not (tmp_path / "a.suppleco").exists()


@pytest.mark.parametrize(
    "passphrase, archive_in, refusal",
    [
        ("too short", False, "at least 16"),
        (PASSPHRASE, True, "outside the install"),
    ],
)
def test_a_short_passphrase_or_an_archive_inside_the_install_is_refused(
    install, tmp_path, passphrase, archive_in, refusal
):
    root, s, _ = install
    archive = (root if archive_in else tmp_path) / "a.suppleco"
    with pytest.raises(handover.HandoverRefused, match=refusal):
        _make(root, s, archive, passphrase=passphrase, lines=[])
    assert not archive.exists()


def test_the_passphrase_is_asked_twice_and_must_match(monkeypatch):
    answers = iter([PASSPHRASE, PASSPHRASE + "!"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    with pytest.raises(handover.HandoverRefused, match="differ"):
        make_handover._ask_passphrase()


def test_handover_restore_runs_before_any_setting_is_read(
    install, tmp_path, monkeypatch, capsys
):
    root, s, _ = install
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=[])

    def no_settings():
        raise AssertionError("handover-restore read the settings")

    monkeypatch.setattr(main, "load_settings", no_settings)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": PASSPHRASE)
    monkeypatch.setattr(handover, "run_doctor", lambda folder: 0)
    into = tmp_path / "new"
    monkeypatch.setattr(
        "sys.argv", ["main.py", "handover-restore", str(archive), "--into", str(into)]
    )
    main.main()
    assert (into / "data" / "agent.db").is_file()
    printed = capsys.readouterr().out
    assert handover.KEYS_FILE in printed
    assert TEST_ERASURE_KEY not in printed and LISTS_KEY not in printed

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "wrong " + PASSPHRASE)
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "handover-restore", str(archive), "--into", str(tmp_path / "b")],
    )
    with pytest.raises(SystemExit) as stopped:
        main.main()
    assert stopped.value.code == 1
    assert "Refused: The passphrase is wrong" in capsys.readouterr().out


def test_the_opener_runs_alone_on_a_computer_with_no_agent(install, tmp_path):
    """open_handover.py, the copy sent beside the archive, imports nothing of
    the agent's: it runs from a folder that holds only itself."""
    root, s, _ = install
    alone = tmp_path / "alone"
    alone.mkdir()
    archive = alone / "a.suppleco"
    _make(root, s, archive, lines=[])
    opener = alone / handover.OPENER_NAME
    into = tmp_path / "unpacked"
    code = (
        "import runpy, sys\n"
        "import os\n"
        "assert not any(os.path.isdir(os.path.join(p or '.', 'modules')) for p in sys.path)\n"
        f"opener = runpy.run_path({str(opener)!r})\n"
        f"opener['restore']({str(archive)!r}, lambda: {PASSPHRASE!r}, {str(into)!r},"
        " doctor=lambda folder: 0)\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=alone,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert (into / "data" / "agent.db").is_file()
    assert "Unpacked into" in done.stdout
    for key in (TEST_ERASURE_KEY, LISTS_KEY, PASSPHRASE):
        assert key not in done.stdout + done.stderr


def test_the_key_file_the_settings_name_stays_out_whatever_its_name():
    data, cert = PurePosixPath("data"), PurePosixPath("data/microsoft-signin.cer")
    assert make_handover.excluded_group(cert, False, data, cert) == "key"
    assert make_handover.excluded_group(cert, False, data, None) is None


def test_the_fingerprint_is_the_databases_own():
    assert handover.erasure_key_fingerprint(
        TEST_ERASURE_KEY
    ) == erasure_key_fingerprint(TEST_ERASURE_KEY)
    from modules.database import ERASURE_KEY_FINGERPRINT

    assert handover.FINGERPRINT_STATE == ERASURE_KEY_FINGERPRINT


@pytest.mark.skipif(os.name != "nt", reason="the Windows permissions of the key file")
def test_the_keys_file_is_readable_by_this_account_only_on_windows(install, tmp_path):
    root, s, _ = install
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=[])
    folder, _ = _restore(archive, tmp_path / "new", lines=[])
    listing = subprocess.run(
        ["icacls", str(folder / handover.KEYS_FILE)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).stdout
    grants = [line for line in listing.splitlines() if ":(" in line]
    assert len(grants) == 1, listing
    assert getpass.getuser().lower() in grants[0].lower()
    assert "(I)" not in grants[0]


@pytest.mark.skipif(os.name != "posix", reason="the POSIX mode of the key file")
def test_the_keys_file_is_readable_by_this_account_only_on_posix(install, tmp_path):
    root, s, _ = install
    archive = tmp_path / "a.suppleco"
    _make(root, s, archive, lines=[])
    folder, _ = _restore(archive, tmp_path / "new", lines=[])
    assert (folder / handover.KEYS_FILE).stat().st_mode & 0o777 == 0o600


def test_a_database_still_in_use_stops_the_handover(install, tmp_path):
    root, s, _ = install
    holder = sqlite3.connect(s.get_abs_db_path(), isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(handover.HandoverRefused, match="using the database"):
            _make(root, s, tmp_path / "a.suppleco", lines=[])
    finally:
        holder.execute("ROLLBACK")
        holder.close()
