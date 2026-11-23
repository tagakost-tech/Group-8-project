"""No setting exported in the shell reaches a test.

pydantic-settings reads the process environment as well as .env, and the
suite's _env_file=None turns off only the file. So SMTP credentials exported
in the shell that runs the tests, the way a CI job or a deploy script hands
them to the app, reached every test that did not set its own, and a live-path
test that reached SMTP would log in to that mailbox and send. DRY_RUN,
CLIENT_FILTER_ENABLED and every other setting leaked the same way, and changed
what the tests test.

A variable a test sets itself comes too late to show this, so the proof runs
the suite's own fixtures in a child pytest whose environment exports them.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from config import Settings

ROOT = Path(__file__).resolve().parent.parent

# Each differs from its default and is valid, so a leak shows as a wrong value
# rather than a validation error. One is lower case: Linux keeps the case, and
# pydantic-settings reads either. LLM_API_KEY is declared with an alias.
EXPORTED = {
    "SMTP_USERNAME": "leak-probe@example.invalid",
    "SMTP_PASSWORD": "leak-probe-password",
    "DRY_RUN": "true",
    "CLIENT_FILTER_ENABLED": "false",
    "imap_username": "leak-probe@example.invalid",
    "LLM_API_KEY": "leak-probe-key",
}


def _leaked(settings) -> list[str]:
    fields = [key.lower() for key in EXPORTED]
    return [
        f"{name}={getattr(settings, name)!r}"
        for name in fields
        if getattr(settings, name) != Settings.model_fields[name].default
    ]


@pytest.fixture(scope="module")
def module_settings(tmp_path_factory):
    # Built once per module, as tests/ui builds the dashboard it serves: before
    # any function-scoped fixture of the first test has run.
    from conftest import _browser_dashboard_settings

    return _browser_dashboard_settings(tmp_path_factory.mktemp("module_settings"))


def test_settings_built_for_a_test_hold_their_defaults(
    settings_factory, module_settings
):
    built = {
        "settings_factory": settings_factory(),
        "the browser tests' dashboard": module_settings,
        "Settings built directly": Settings(_env_file=None),
    }
    leaks = {where: _leaked(s) for where, s in built.items() if _leaked(s)}
    assert not leaks, f"settings taken from the shell: {leaks}"


def test_a_setting_exported_in_the_shell_reaches_no_test():
    probe = Path(__file__).relative_to(ROOT).as_posix()
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{probe}::test_settings_built_for_a_test_hold_their_defaults",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        env={**os.environ, **EXPORTED},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_a_retired_cloud_key_exported_in_the_shell_stops_no_test():
    """Start-up refuses GEMINI_API_KEY and the rest from the environment
    too, so a developer who still exports one would fail every test."""
    probe = Path(__file__).relative_to(ROOT).as_posix()
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{probe}::test_settings_built_for_a_test_hold_their_defaults",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        env={**os.environ, **dict.fromkeys(Settings.RETIRED_SETTINGS, "leak-probe")},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
