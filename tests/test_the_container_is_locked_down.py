"""The image carries only what runs, and the container can do little else.

The build context sent docs/ (the meeting decks and reports), tests/ and
scripts/ into the image, and *.xlsx kept out only a client list at the top
of the project. docker-compose.yml published the dashboard on every network
interface of the host, and ran it with a writable file system and Docker's
default Linux capabilities. The base image was python:3.11-slim, a tag that
moves. Most requirements had an upper bound, three did not. Nothing kept
the review folders marked "internal - not for SUPPLECO" out of git.

Now (2026-11 security hardening, stage 3), checked here by reading the files
(Docker cannot run on the machine this was written on):
- .dockerignore leaves out docs/, tests/, scripts/, every .xlsx, .pdf and
  .md file, and the agent tooling; benchmark/ stays, `main.py ai-benchmark`
  reads its cases;
- the port is published on 127.0.0.1 only; the container is read-only
  (data, logs, assets, templates and /tmp writable), drops every capability
  and cannot gain new privileges;
- the base image names an exact Python version;
- every runtime requirement stops below the installed version's next major;
- git ignores every "not for SUPPLECO" folder under docs/review.
"""

import re
from importlib import metadata

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

from main import PROJECT_ROOT as ROOT


def _lines(name):
    return [
        line.strip()
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize(
    "pattern",
    [
        ".env",
        "data/",
        "logs/",
        "**/*.xlsx",
        "**/*.pdf",
        "**/*.md",
        "docs/",
        "tests/",
        "scripts/",
        ".claude/",
        ".agents/",
        ".git/",
        # Review of stage 3: a copy of .env, or of the database, outside
        # data/ went into the image.
        "**/.env*",
        "**/*.db",
        "**/*.db-*",
        "**/*.sqlite*",
        # The Microsoft sign-in certificate and its private key.
        "**/*.pem",
        "**/*.key",
        "**/*.pfx",
        "**/*.p12",
    ],
)
def test_the_image_leaves_out(pattern):
    assert pattern in _lines(".dockerignore")


@pytest.mark.parametrize("pattern", ["*.pem", "*.key", "*.pfx", "*.p12"])
def test_git_ignores_the_sign_in_key(pattern):
    assert pattern in _lines(".gitignore")


def test_the_image_keeps_what_the_app_reads():
    ignored = _lines(".dockerignore")
    for needed in ("benchmark/", "templates/", "static/", "modules/", "requirements.txt"):
        assert needed not in ignored, needed
    assert (ROOT / "benchmark" / "cases").is_dir()


def _service():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    (service,) = compose["services"].values()
    return service


def test_the_dashboard_is_published_on_this_machine_only():
    ports = _service()["ports"]
    assert ports
    for port in ports:
        assert str(port).startswith("127.0.0.1:"), port


def test_the_container_cannot_change_itself_or_gain_privileges():
    service = _service()
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]


def test_what_the_app_writes_is_still_writable():
    service = _service()
    assert "/tmp" in service["tmpfs"]
    mounted = {volume.split(":")[1] for volume in service["volumes"]}
    for folder in ("/app/data", "/app/logs", "/app/assets", "/app/templates"):
        assert folder in mounted, folder


def test_the_base_image_names_an_exact_version():
    (base,) = re.findall(
        r"^FROM\s+(\S+)", (ROOT / "Dockerfile").read_text(encoding="utf-8"), re.MULTILINE
    )
    assert re.fullmatch(r"python:3\.15\.\d+-slim-[a-z]+", base), base


def _requirements():
    return [Requirement(line) for line in _lines("requirements.txt")]


# tzdata's version is the year of its time-zone rules (2025.1, 2026.1), not
# a promise about its code: an upper bound only stops this year's rules from
# installing, and Europe/Rome's send window then runs on stale data. certifi's
# is the date of its list of trusted certificate authorities: a bound would
# keep an authority found untrustworthy after that date.
NO_UPPER_BOUND = {"tzdata", "certifi"}


@pytest.mark.parametrize(
    "req",
    [r for r in _requirements() if r.name not in NO_UPPER_BOUND],
    ids=lambda r: r.name,
)
def test_every_requirement_stops_below_the_next_major_version(req):
    uppers = [s for s in req.specifier if s.operator == "<"]
    assert len(uppers) == 1, f"{req.name} has no upper bound"
    try:
        installed = Version(metadata.version(req.name))
    except metadata.PackageNotFoundError:
        pytest.skip(f"{req.name} is not installed here")
    assert installed in req.specifier, (req.name, str(installed))
    assert Version(uppers[0].version).major == installed.major + 1, req.name


def test_tzdata_has_a_floor_and_no_ceiling():
    (req,) = [r for r in _requirements() if r.name == "tzdata"]
    operators = {s.operator for s in req.specifier}
    assert operators == {">="}, str(req.specifier)
    assert Version("2026.1") in req.specifier
    assert Version("2030.1") in req.specifier
    assert Version("2023.4") not in req.specifier


def test_certifi_has_a_floor_and_no_ceiling():
    (req,) = [r for r in _requirements() if r.name == "certifi"]
    assert {s.operator for s in req.specifier} == {">="}, str(req.specifier)
    assert Version("2025.1.31") not in req.specifier
    assert Version("2026.10.18") in req.specifier
    assert Version("2030.1.1") in req.specifier


def _gitignore_regex(pattern):
    """The subset of gitignore syntax the rule uses: **/, *, [..], a final /."""
    body = pattern.rstrip("/")
    out, i = "", 0
    while i < len(body):
        if body.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif body[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif body[i] == "[":
            end = body.index("]", i)
            out, i = out + body[i : end + 1], end + 1
        else:
            out, i = out + re.escape(body[i]), i + 1
    return re.compile(out)


def _ignored_folder(path):
    rules = [
        _gitignore_regex(line)
        for line in _lines(".gitignore")
        if line.startswith("docs/review/") and line.endswith("/")
    ]
    return any(rule.fullmatch(path) for rule in rules)


@pytest.mark.parametrize(
    "folder",
    [
        "docs/review/internal - not for SUPPLECO",
        "docs/review/meeting-2026-11-12/internal - not for SUPPLECO",
        "docs/review/Internal - Not for Suppleco",
        "docs/review/evidence/notes (internal - NOT FOR SUPPLECO)",
        "docs/review/internal-not-for-SUPPLECO",
        "docs/review/evidence/internal_not_for_suppleco",
    ],
)
def test_git_ignores_internal_review_folders(folder):
    assert _ignored_folder(folder)


@pytest.mark.parametrize(
    "folder", ["docs/review/evidence", "docs/review/meeting-2026-11-02", "docs/internal"]
)
def test_git_keeps_the_review_folders_meant_for_suppleco(folder):
    assert not _ignored_folder(folder)
