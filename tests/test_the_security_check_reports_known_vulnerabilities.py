"""`main.py security-check` says when a package has a known vulnerability.

Nothing told SUPPLECO when a package the agent runs on had a published
vulnerability: the versions installed on the day of the install stayed until
someone looked. Now IT schedules `security-check` weekly. It asks pip-audit
about the packages installed for the agent's own Python and records the
answer in the activity log: an error, which the dashboard's errors panel
shows, when a package the agent uses (requirements.txt and what those need in
turn) has one, with the version that fixes it; a warning when only a tool
such as pip has one. A check that could not run (pip-audit missing, no
answer from pypi.org) is an error too, never a quiet clean result. It never
installs or upgrades anything: updates are made on a copy of the install
(docs/DEPLOY.md section 14).
"""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import main
from modules import security_check
from modules.database import Database

REQUIREMENTS = main.PROJECT_ROOT / "requirements.txt"


def _dep(name, version, *vulns):
    return {
        "name": name,
        "version": version,
        "vulns": [
            {"id": vid, "fix_versions": list(fixes), "aliases": [], "description": ""}
            for vid, fixes in vulns
        ],
    }


CLEAN = {"dependencies": [_dep("flask", "3.1.3"), _dep("pip", "24.0")], "fixes": []}
TOOL_ONLY = {
    "dependencies": [_dep("flask", "3.1.3"), _dep("pip", "24.0", ("PYSEC-1", ["25.3"]))],
    "fixes": [],
}
AGENT_PACKAGE = {
    "dependencies": [
        # Not in requirements.txt: flask needs it.
        _dep("werkzeug", "3.1.8", ("PYSEC-2026-4312", ["3.1.9"])),
        _dep(
            "cryptography",
            "48.0.0",
            ("GHSA-537c", ["48.0.1"]),
            ("PYSEC-2026-3552", ["50.0.0"]),
            ("PYSEC-2026-3553", ["49.0.0"]),
        ),
        _dep("pip", "24.0", ("PYSEC-1", ["25.3"])),
        {"name": "private-wheel", "version": "1.0", "skip_reason": "not on PyPI"},
    ],
    "fixes": [],
}


@pytest.fixture
def audit(monkeypatch):
    """pip-audit, played: what it was asked and what it answers."""
    state = SimpleNamespace(calls=[], answer=CLEAN, returncode=0, stderr="")

    def run(argv, **kwargs):
        state.calls.append((argv, kwargs))
        out = state.answer if isinstance(state.answer, str) else json.dumps(state.answer)
        return subprocess.CompletedProcess(argv, state.returncode, out, state.stderr)

    monkeypatch.setattr(security_check.subprocess, "run", run)
    real_find_spec = security_check.importlib.util.find_spec
    monkeypatch.setattr(
        security_check.importlib.util,
        "find_spec",
        lambda name: object() if name == "pip_audit" else real_find_spec(name),
    )
    return state


def _check(settings_factory, tmp_path):
    s = settings_factory(db_path=str(tmp_path / "a.db"))
    try:
        main.cmd_security_check(None, s)
        code = 0
    except SystemExit as e:
        code = e.code
    db = Database(s.get_abs_db_path())
    rows = [r for r in db.get_recent_activity(50) if r["action"] == "SECURITY_CHECK"]
    return code, rows, db


def test_a_vulnerable_package_the_agent_uses_is_an_error_on_the_dashboard(
    settings_factory, tmp_path, audit, capsys
):
    audit.answer, audit.returncode = AGENT_PACKAGE, 1
    code, rows, db = _check(settings_factory, tmp_path)
    assert code == 1
    (row,) = rows
    assert row["status"] == "error"
    assert [e["action"] for e in db.get_recent_errors()] == ["SECURITY_CHECK"]
    assert "cryptography 48.0.0: fixed in 50.0.0" in row["details"]
    assert "werkzeug 3.1.8: fixed in 3.1.9" in row["details"]
    assert "pip 24.0" not in row["details"]
    out = capsys.readouterr().out
    assert "cryptography 48.0.0: fixed in 50.0.0" in out
    assert "pip 24.0: fixed in 25.3" in out
    assert "private-wheel" in out
    assert "Nothing was installed or upgraded" in out


def test_a_vulnerable_tool_alone_is_a_warning(settings_factory, tmp_path, audit):
    audit.answer, audit.returncode = TOOL_ONLY, 1
    code, rows, db = _check(settings_factory, tmp_path)
    assert code == 0
    (row,) = rows
    assert row["status"] == "warning" and "pip 24.0: fixed in 25.3" in row["details"]
    assert db.get_recent_errors() == []


def test_a_clean_answer_is_recorded_too(settings_factory, tmp_path, audit):
    code, rows, _ = _check(settings_factory, tmp_path)
    assert code == 0
    (row,) = rows
    assert row["status"] == "success" and "No known vulnerabilities" in row["details"]


@pytest.mark.parametrize(
    "answer,returncode,stderr",
    [
        ("", 1, "ConnectionError: could not reach pypi.org"),
        ("not json", 1, ""),
        ({"something": "else"}, 0, ""),
    ],
)
def test_a_check_that_gives_no_answer_is_an_error_not_a_clean_result(
    settings_factory, tmp_path, audit, capsys, answer, returncode, stderr
):
    audit.answer, audit.returncode, audit.stderr = answer, returncode, stderr
    code, rows, db = _check(settings_factory, tmp_path)
    assert code == 1
    (row,) = rows
    assert row["status"] == "error" and "could not run" in row["details"]
    # The reason is pip-audit's own, never "none of the 0 installed packages".
    assert f"pip-audit gave no result (exit code {returncode}" in row["details"]
    assert stderr in row["details"]
    assert db.get_recent_errors()
    assert "could not run" in capsys.readouterr().out


def _skipped(name):
    return {
        "name": name,
        "skip_reason": f"Dependency not found on PyPI and could not be audited: {name}",
    }


def test_a_package_the_agent_uses_that_was_not_looked_up_is_an_error(
    settings_factory, tmp_path, audit, capsys
):
    """pip-audit skips a package PyPI answers 404 for (an office filter, a
    release taken down) and says nothing more: the run read as clean."""
    audit.answer = {
        "dependencies": [_dep("flask", "3.1.3"), _skipped("werkzeug"), _skipped("private-wheel")],
        "fixes": [],
    }
    code, rows, db = _check(settings_factory, tmp_path)
    assert code == 1
    (row,) = rows
    assert row["status"] == "error" and "werkzeug" in row["details"]
    assert "private-wheel" not in row["details"]
    assert [e["action"] for e in db.get_recent_errors()] == ["SECURITY_CHECK"]
    assert "not known to be safe: werkzeug" in capsys.readouterr().out


def test_a_tool_that_was_not_looked_up_leaves_a_clean_result(
    settings_factory, tmp_path, audit
):
    audit.answer = {"dependencies": [_dep("flask", "3.1.3"), _skipped("private-wheel")]}
    code, rows, _ = _check(settings_factory, tmp_path)
    assert code == 0 and rows[0]["status"] == "success"


def test_a_run_that_looked_up_no_package_is_an_error_not_a_clean_result(
    settings_factory, tmp_path, audit
):
    audit.answer = {"dependencies": [_skipped("private-wheel"), _skipped("pip")]}
    code, rows, db = _check(settings_factory, tmp_path)
    assert code == 1
    (row,) = rows
    assert row["status"] == "error" and "could not run" in row["details"]
    assert "none of the 2 installed packages" in row["details"]
    assert db.get_recent_errors()


def test_the_update_procedure_never_upgrades_the_shared_python():
    """DEPLOY section 14 updated "the copy" with `py -3.11 -m pip`, which on
    an install without its own virtual environment (section 1) upgrades the
    one Python the live install runs on: the update happened in place, and
    swapping the folders back undid nothing."""
    deploy = (main.PROJECT_ROOT / "docs" / "DEPLOY.md").read_text("utf-8")
    section = deploy.split("## 14.", 1)[1].split("\n## ", 1)[0]
    update = section.split("### Updating", 1)[1]
    assert "py -3.11 -m venv .venv" in update
    for line in update.splitlines():
        assert "py -3.11 -m pip" not in line, line
        assert "py -3.11 -m pytest" not in line, line
    assert ".venv\\Scripts\\python.exe -m pip install" in update


def test_without_pip_audit_the_check_says_so_as_an_error(
    settings_factory, tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(security_check.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(security_check.importlib.util, "find_spec", lambda name: None)
    code, rows, _ = _check(settings_factory, tmp_path)
    assert code == 1 and calls == []
    (row,) = rows
    assert row["status"] == "error" and "pip-audit is not installed" in row["details"]


def test_a_check_that_hangs_is_an_error(settings_factory, tmp_path, audit, monkeypatch):
    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(security_check.subprocess, "run", hang)
    code, rows, _ = _check(settings_factory, tmp_path)
    assert code == 1 and rows[0]["status"] == "error"


def test_it_only_asks_and_never_installs(settings_factory, tmp_path, audit):
    audit.answer, audit.returncode = AGENT_PACKAGE, 1
    _check(settings_factory, tmp_path)
    ((argv, kwargs),) = audit.calls
    assert argv[:3] == [sys.executable, "-m", "pip_audit"]
    for word in ("--fix", "-r", "--requirement", "install", "--dry-run"):
        assert word not in argv, word
    assert kwargs["timeout"] == security_check.AUDIT_TIMEOUT_SECONDS
    assert kwargs.get("shell") is not True


def test_the_packages_the_agent_uses_include_what_they_need_in_turn():
    used = security_check.packages_the_agent_uses(REQUIREMENTS)
    for package in ("flask", "cryptography", "werkzeug", "jinja2", "idna", "httpcore"):
        assert package in used, package
    for tool in ("pip", "pytest", "pip-audit", "playwright"):
        assert tool not in used, tool


def test_when_it_is_unknown_what_the_agent_uses_every_finding_counts(
    settings_factory, tmp_path, audit, monkeypatch
):
    audit.answer, audit.returncode = TOOL_ONLY, 1
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    code, rows, _ = _check(settings_factory, tmp_path)
    assert code == 1 and rows[0]["status"] == "error"


@pytest.mark.parametrize(
    "installed,vulns,fixed",
    [
        ("48.0.0", [["48.0.1"], ["50.0.0"], ["49.0.0"]], "50.0.0"),
        # Fixed on several lines: the first fix above the installed version.
        ("5.29.3", [["4.25.8", "5.29.5", "6.31.1"], ["5.29.6", "6.33.5"]], "5.29.6"),
        ("1.0", [["1.1"], []], None),
    ],
)
def test_the_fixed_version_is_the_lowest_that_fixes_them_all(installed, vulns, fixed):
    as_json = [{"id": f"V{i}", "fix_versions": f} for i, f in enumerate(vulns)]
    assert security_check._fixed_in(installed, as_json) == fixed


def test_the_command_is_offered():
    assert main.build_parser().parse_args(["security-check"]).command == "security-check"
