"""Known vulnerabilities in the installed packages, found by pip-audit.

`main.py security-check` runs this, weekly from a scheduled task
(docs/DEPLOY.md section 14). It only reads: pip-audit is asked about the
packages installed for the Python that runs the agent, and never told to fix
anything, so nothing is installed, upgraded or removed. Updating is a
person's job, done on a copy of the install (DEPLOY section 14).

A known vulnerability in a package the agent uses (requirements.txt and what
those packages need in turn) is an error in the activity log, so the
dashboard's errors panel shows it. One in a tool only (pip, the test
packages) is a warning. A check that could not run is an error too: a weekly
task that fails quietly would look like a clean result. So is a package the
agent uses that pip-audit skipped (not found on pypi.org), and a run that
looked up no package at all.
"""

import importlib.util
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import NamedTuple

AUDIT_TIMEOUT_SECONDS = 600
# The ids printed for one package; the rest are counted.
IDS_SHOWN = 5


class CheckFailed(Exception):
    """pip-audit is missing, failed, or gave no result."""


class Finding(NamedTuple):
    package: str
    version: str
    ids: tuple[str, ...]
    fixed_in: str | None
    used: bool


class Report(NamedTuple):
    checked: int
    not_checked: tuple[str, ...]
    findings: tuple[Finding, ...]
    # Packages the agent uses that pip-audit skipped: not known to be safe.
    used_not_checked: tuple[str, ...] = ()

    @property
    def used(self) -> list[Finding]:
        return [f for f in self.findings if f.used]

    @property
    def tools(self) -> list[Finding]:
        return [f for f in self.findings if not f.used]


def pip_audit_command() -> list[str]:
    """The one command run. No --fix, no requirement file: pip-audit then
    neither upgrades nor builds an environment of its own to install into."""
    return [
        sys.executable,
        "-m",
        "pip_audit",
        "--format",
        "json",
        "--progress-spinner",
        "off",
    ]


def run_pip_audit() -> dict:
    """pip-audit's JSON for the packages installed here."""
    if importlib.util.find_spec("pip_audit") is None:
        raise CheckFailed(
            "pip-audit is not installed for this Python; install "
            "requirements-dev.txt in a copy of the install (docs/DEPLOY.md "
            "section 14)"
        )
    try:
        proc = subprocess.run(
            pip_audit_command(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=AUDIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CheckFailed(
            f"pip-audit did not finish within {AUDIT_TIMEOUT_SECONDS} seconds"
        ) from None
    except OSError as e:
        raise CheckFailed(f"pip-audit could not be started ({e})") from None
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = None
    if not isinstance(result, dict) or not isinstance(result.get("dependencies"), list):
        said = (proc.stderr or "").strip().splitlines()
        raise CheckFailed(
            f"pip-audit gave no result (exit code {proc.returncode}"
            + (f": {said[-1]}" if said else "")
            + "); it needs to reach pypi.org"
        )
    return result


def _requirement_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def packages_the_agent_uses(requirements: Path) -> set[str]:
    """The packages in requirements.txt and, as installed here, every package
    they need in turn, with the extras they ask for. Normalised names."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    todo = [Requirement(line) for line in _requirement_lines(requirements)]
    extras_seen: dict[str, set[str]] = {}
    while todo:
        req = todo.pop()
        name = canonicalize_name(req.name)
        asked = set(req.extras)
        if name in extras_seen and asked <= extras_seen[name]:
            continue
        extras = extras_seen.setdefault(name, set())
        extras.update(asked)
        try:
            needs = metadata.requires(req.name) or []
        except metadata.PackageNotFoundError:
            continue
        for text in needs:
            need = Requirement(text)
            if need.marker is None or any(
                need.marker.evaluate({"extra": extra}) for extra in extras | {""}
            ):
                todo.append(need)
    return set(extras_seen)


def _first_fix(installed: str, fixes: list) -> str | None:
    """The lowest fixed version above the installed one."""
    from packaging.version import InvalidVersion, Version

    try:
        current = Version(installed)
        candidates = sorted(Version(f) for f in fixes if Version(f) > current)
    except InvalidVersion:
        return str(fixes[0]) if fixes else None
    return str(candidates[0]) if candidates else None


def _fixed_in(installed: str, vulns: list) -> str | None:
    """The lowest version that fixes every one of them, or None when one has
    no fix yet."""
    from packaging.version import InvalidVersion, Version

    firsts = [_first_fix(installed, list(v.get("fix_versions") or [])) for v in vulns]
    if any(f is None for f in firsts):
        return None
    try:
        return str(max(Version(f) for f in firsts))
    except InvalidVersion:
        return firsts[-1]


def read_report(result: dict, used: set[str] | None) -> Report:
    """pip-audit's JSON as findings. used is None when it is not known which
    packages the agent uses: every finding then counts as one."""
    from packaging.utils import canonicalize_name

    findings, not_checked, used_not_checked, checked = [], [], [], 0
    for dep in result["dependencies"]:
        name, version = dep.get("name", "?"), dep.get("version", "?")
        if dep.get("skip_reason"):
            not_checked.append(name)
            if used is None or canonicalize_name(name) in used:
                used_not_checked.append(name)
            continue
        checked += 1
        vulns = dep.get("vulns") or []
        if not vulns:
            continue
        ids = tuple(dict.fromkeys(v.get("id", "?") for v in vulns))
        findings.append(
            Finding(
                package=name,
                version=version,
                ids=ids,
                fixed_in=_fixed_in(version, vulns),
                used=used is None or canonicalize_name(name) in used,
            )
        )
    findings.sort(key=lambda f: (not f.used, f.package.lower()))
    return Report(checked, tuple(not_checked), tuple(findings), tuple(used_not_checked))


def describe(finding: Finding) -> str:
    shown = ", ".join(finding.ids[:IDS_SHOWN])
    more = len(finding.ids) - IDS_SHOWN
    if more > 0:
        shown += f" and {more} more"
    fix = (
        f"fixed in {finding.fixed_in}"
        if finding.fixed_in
        else "no fixed version yet for at least one"
    )
    return f"{finding.package} {finding.version}: {fix} ({shown})"


def check(requirements: Path) -> Report:
    """Run pip-audit and sort its findings. Raises CheckFailed."""
    result = run_pip_audit()
    try:
        used = packages_the_agent_uses(requirements)
    except (OSError, ValueError, ImportError):
        used = None
    report = read_report(result, used)
    if not report.checked:
        # An office filter answering 404 for every lookup reads as "not on
        # PyPI" for each package: nothing was checked, so nothing is known.
        raise CheckFailed(
            f"pip-audit looked up none of the {len(report.not_checked)} "
            "installed packages; it needs to reach pypi.org"
        )
    return report
