"""A new install gets the versions that fix the known vulnerabilities.

A pip-audit scan (18 November 2026) found published vulnerabilities in
packages the agent runs on, and requirements.txt allowed the vulnerable
versions: a fresh install could pick up werkzeug 3.1.8 through flask, or
cryptography 48.0.0. The floors are raised, also for the packages the agent
does not name itself but gets through flask, httpx, email-validator and
beautifulsoup4, so pip cannot settle on a vulnerable version. waitress, the
dashboard's web server, is required too.
"""

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from main import PROJECT_ROOT as ROOT

# The package, a version below the fix, and the first fixed version the scan named.
FIXED = [
    ("werkzeug", "3.1.8", "3.1.9"),
    ("dnspython", "2.9.1", "2.9.2"),
    ("idna", "3.14", "3.15"),
    ("certifi", "2026.10.17", "2026.10.18"),
    ("anyio", "4.14.1", "4.14.2"),
    ("pydantic-settings", "2.14.1", "2.14.2"),
    ("soupsieve", "3.0.2", "4.0.0"),
    ("cryptography", "49.0.0", "50.0.0"),
]


def _requirements():
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    reqs = [Requirement(line) for line in lines if line.strip() and not line.startswith("#")]
    return {r.name.lower(): r for r in reqs}


@pytest.mark.parametrize("name,vulnerable,fixed", FIXED, ids=[f[0] for f in FIXED])
def test_the_vulnerable_version_cannot_be_installed(name, vulnerable, fixed):
    req = _requirements()[name]
    assert Version(vulnerable) not in req.specifier, str(req)
    assert Version(fixed) in req.specifier, str(req)


def test_cryptography_may_be_the_release_the_tests_ran_on():
    spec = _requirements()["cryptography"].specifier
    assert Version("50.0.2") in spec
    assert Version("51.0.0") not in spec


def test_the_dashboard_server_is_required():
    assert "waitress" in _requirements()


def test_pip_audit_is_a_development_tool_only():
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert any(line.startswith("pip-audit") for line in dev.splitlines())
    assert "pip-audit" not in _requirements()
