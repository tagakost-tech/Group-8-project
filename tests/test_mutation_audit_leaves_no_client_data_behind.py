"""The mutation audit leaves no copy of SUPPLECO's client list behind.

The audit mutates a copy of the repo in the temp folder, and the copy held
everything in the project folder: SUPPLECO's client workbook, and a .env if
there was one. A run that is killed, as a tool call that times out kills it,
never reaches its clean-up, and the clean-up ignored every error it met. Two
full copies, each with the client list, were found in %TEMP% days later.
Even a run that finished left its copy's empty folders there, because Windows
would not delete them, and nothing said so.

Now the copy leaves out the client list, settings files and the agents'
folders, each run first deletes copies more than 12 hours old, read-only
folders are deleted too, and a copy that cannot be deleted is reported. Each
case runs the real main() on a tiny source tree of invented files, never the
real workbook or a real .env.
"""

import os
import shutil
import stat
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mutation_audit
from mutation_audit import Mutation

MOD = "def f():\n    return 1\n"
TEST = "from mod import f\n\n\ndef test_f():\n    assert f() == 1\n"
HOUR = 60 * 60


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    (root / "tests").mkdir(parents=True)
    (root / "mod.py").write_text(MOD, encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text(TEST, encoding="utf-8")
    (root / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def temp(tmp_path, monkeypatch):
    """The temp folder the audit uses, so no test deletes anything from the
    machine's own."""
    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def _audit(monkeypatch, source):
    spec = types.ModuleType("mutation_spec")
    spec.MUTATIONS = [
        Mutation(
            "X1",
            "g",
            "mod.py",
            "return 1",
            "return 2",
            ["tests/test_mod.py"],
            "test mutation",
        )
    ]
    monkeypatch.setitem(sys.modules, "mutation_spec", spec)
    monkeypatch.setattr(mutation_audit, "ROOT", source)
    monkeypatch.setattr(sys, "argv", ["mutation_audit.py"])
    return mutation_audit.main()


def test_the_copy_holds_no_client_list_settings_or_agent_folders(
    source, temp, monkeypatch
):
    for name in (
        "Suppleco_Elenco_Clienti_17072026 (1).xlsx",
        "lists/rivals.xlsx",
        ".env",
        ".env.local",
        ".env.example",
        ".claude/launch.json",
        ".agents/notes.md",
    ):
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text("invented\n", encoding="utf-8")
    copied = []

    def spy(mutations, repo):
        copied.extend(p.relative_to(repo).as_posix() for p in repo.rglob("*"))

    monkeypatch.setattr(mutation_audit, "run", spy)
    _audit(monkeypatch, source)

    private = (".env", ".env.local", ".claude", ".agents")
    leaked = [p for p in copied if p.endswith(".xlsx") or p.split("/")[0] in private]
    assert leaked == []
    # The template is in git, and tests/test_env_template_matches_defaults.py
    # reads it.
    assert {"mod.py", "tests/test_mod.py", ".env.example"} <= set(copied)


def test_the_copy_holds_no_sign_in_key(source, temp, monkeypatch):
    """The Microsoft sign-in key signs in to SUPPLECO's mailboxes."""
    for name in ("microsoft-signin.pem", "keys/agent.key", "a.pfx", "b.p12"):
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text("invented\n", encoding="utf-8")
    copied = []

    def spy(mutations, repo):
        copied.extend(p.relative_to(repo).as_posix() for p in repo.rglob("*"))

    monkeypatch.setattr(mutation_audit, "run", spy)
    _audit(monkeypatch, source)

    assert [p for p in copied if p.endswith((".pem", ".key", ".pfx", ".p12"))] == []
    assert "mod.py" in copied


def test_a_run_first_deletes_the_copies_killed_runs_left(source, temp, monkeypatch):
    for name, age in (
        ("mutation-audit-killed", 13 * HOUR),
        ("mutation-audit-running", 1 * HOUR),
        ("another-tools-folder", 13 * HOUR),
    ):
        (temp / name / "repo").mkdir(parents=True)
        (temp / name / "repo" / "mod.py").write_text(MOD, encoding="utf-8")
        then = time.time() - age
        os.utime(temp / name, (then, then))

    _audit(monkeypatch, source)

    # The run's own copy is gone too.
    assert sorted(p.name for p in temp.iterdir()) == [
        "another-tools-folder",
        "mutation-audit-running",
    ]


@pytest.mark.skipif(
    sys.platform != "win32", reason="only Windows refuses to delete a read-only folder"
)
def test_a_copy_of_read_only_folders_is_still_deleted(
    source, temp, monkeypatch, capsys
):
    """Every folder of this checkout carries Windows' read-only mark, the copy
    keeps it, and Windows will not delete a read-only folder. So every run left
    its copy's empty folders behind, the shape of the 250 empty trees found in
    %TEMP%, and the clean-up that ignored errors never said so."""
    os.chmod(source / "tests", stat.S_IREAD)
    try:
        _audit(monkeypatch, source)
    finally:
        os.chmod(source / "tests", stat.S_IWRITE)

    assert "LEFT BEHIND" not in capsys.readouterr().out
    assert list(temp.iterdir()) == []


def test_a_copy_that_cannot_be_deleted_is_reported(source, temp, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)

    _audit(monkeypatch, source)

    [left] = temp.iterdir()
    out = capsys.readouterr().out
    assert "LEFT BEHIND" in out, out
    assert str(left) in out.split("LEFT BEHIND", 1)[1]
