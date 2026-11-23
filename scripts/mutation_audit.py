"""Break the code on purpose, and see whether the tests notice.

A passing suite says the code does what the tests check. It says nothing about
whether the tests check anything. This measures the difference: each mutation
introduces one real defect, and a mutation the suite does not catch is a bug
that could ship green.

That is not hypothetical here. A sample of eight mutations found three
survivors - no test pinned the lead-tier boundaries, and `advance_enrollment`
was monkeypatched by every test that touched it, so a sequence that re-sent
step 1 forever would have shipped with 712 tests passing.

MUTATIONS ARE APPLIED TO A COPY OF THE REPO, NEVER THE WORKING TREE.
On 2026-08-04 two agents mutated production source in the live checkout and
kept running after reporting completion; the suppression path, the sequence
engine and the AI output guard all sat silently broken, and only a commit made
twenty minutes earlier made it recoverable. The copy is not an optimisation.

A run that is killed never deletes its copy: two full copies, each holding
SUPPLECO's client list, sat in %TEMP% for days, beside about 250 empty ones. A
finished run could not delete its copy's folders either, because they keep
this checkout's read-only mark, and it ignored the error. So the copy leaves
out the client list (*.xlsx), settings files (.env and its variants) and the
agents' folders; each run first deletes copies more than 12 hours old;
read-only folders are deleted too; and a copy that still cannot be deleted is
reported, not ignored.

Each distinct set of named tests runs once on the unmutated code before any
mutation that names it, because a test that already fails there fails
whatever the mutation does. At 0ea19cf, in a clean clone without the client
list, 147 of the 529 mutations named such a test, and SND21 read CAUGHT on a
file that failed 10 of its 16 tests unmutated.

Usage:
    python scripts/mutation_audit.py                 # everything
    python scripts/mutation_audit.py suppression     # one group
    python scripts/mutation_audit.py SEC30 SEC31     # these mutations
    python scripts/mutation_audit.py main.py         # every mutation of a file
    python scripts/mutation_audit.py --list          # show the spec
    python scripts/mutation_audit.py suppression --workers 4

Groups, ids and file paths (as the spec writes them, from the project folder;
modules\\export.py works too) can be mixed, and a run takes every mutation
any of them names. A name that matches nothing stops the run before it
starts, so a misspelt id is never left out quietly. So does an option it does
not know (--worker 4, --workers4, -n 4): ignored, it ran every mutation, for
hours, one at a time. --help prints this text.

--workers N runs the mutations in N copies of the repo side by side, one
worker each. Mutations that name the same tests go to the same copy, so each
set of tests still runs once on the unmutated code there. Every copy is made
and deleted like the one of a serial run, and the report is the same, line for
line and in the same order. The default is 1: one copy, one mutation at a time.

Reading the output:
    CAUGHT         the suite failed, as it should - the behaviour is defended
    SURVIVED       the suite passed against broken code - a real gap
    SKIPPED        nothing was measured: the anchor no longer matches the
                   source or matches more than once, a named test file does
                   not exist, or every named test was deselected
    INVALID        the mutation itself broke import or collection, so every
                   test died before running - fix the mutation, not the tests
    BASELINE-FAIL  the named tests fail, time out or cannot run on the
                   unmutated code, so the mutation was not applied: a failure
                   would prove nothing - fix those tests first
    LEFT BEHIND    a copy could not be deleted - delete it by hand
"""

from __future__ import annotations

import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Left out of the copy. The caches, data and logs only make it slow, and no
# test run imports them. The rest must never leave the project folder:
# SUPPLECO's client list (*.xlsx, encrypted or not), settings files, the Microsoft sign-in key
# and the agents' folders.
_SKIP = shutil.ignore_patterns(
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "logs",
    "data",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "node_modules",
    "*.xlsx",
    "*.xlsx.*",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    ".claude",
    ".agents",
)
# The settings template is in git, so every clone holds it anyway, and
# tests/test_env_template_matches_defaults.py reads it.
_KEEP = {".env.example"}

# Every copy's folder starts with this. One older than _STALE_AFTER belongs to
# a run that ended without deleting it: a full run takes far less.
_PREFIX = "mutation-audit-"
_STALE_AFTER = 12 * 60 * 60  # seconds


def _ignore(folder: str, names: list[str]) -> set[str]:
    return _SKIP(folder, names) - _KEEP


@dataclass
class Mutation:
    """One deliberate defect, and the tests that ought to object to it."""

    id: str
    group: str
    file: str
    old: str
    new: str
    tests: list[str]
    describes: str  # the real-world bug this stands in for
    # A pytest marker to select, for tests the project deselects by default
    # (pytest.ini runs -m "not ui"). Without it a UI-only mutation reads
    # "no tests ran".
    marker: str = ""
    caught: bool | None = field(default=None, compare=False)
    note: str = field(default="", compare=False)
    # The mutation itself was broken (it made the file unimportable), so the
    # run says nothing about the tests. Distinct from SKIPPED, which means the
    # spec drifted from the code.
    invalid: bool = field(default=False, compare=False)
    # The named tests did not pass on the unmutated code, so the mutation was
    # never applied: a failure under it would have proved nothing.
    baseline_failed: bool = field(default=False, compare=False)


def state_of(m: Mutation) -> str:
    if m.baseline_failed:
        return "BASELINE-FAIL"
    if m.invalid:
        return "INVALID"
    if m.caught is None:
        return "SKIPPED"
    return "CAUGHT" if m.caught else "SURVIVED"


def _pytest(m: Mutation, repo: Path) -> subprocess.CompletedProcess[str] | None:
    """Run m's named tests in repo: the finished run, or None if it timed out."""
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *m.tests,
                "-q",
                "-p",
                "no:randomly",
                "--tb=no",
                "-x",
                *(["-m", m.marker] if m.marker else []),
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            check=False,  # a non-zero exit is the signal, not an error
        )
    except subprocess.TimeoutExpired:
        return None


def _baseline_problem(proc: subprocess.CompletedProcess[str] | None) -> str:
    """Why the named tests cannot judge a mutation, or "" when they can."""
    if proc is None:
        return "the named tests time out on the unmutated code"
    # 5 is "no tests ran", which the run under the mutation reports as SKIPPED.
    if proc.returncode in (0, 5):
        return ""
    first = re.search(r"^(?:FAILED|ERROR) (\S+)", proc.stdout, re.MULTILINE)
    where = f": {first[1]}" if first else ""
    if proc.returncode == 1:
        return f"the named tests already fail on the unmutated code{where}"
    return f"pytest exit {proc.returncode} on the unmutated code{where}"


def run(mutations: list[Mutation], repo: Path) -> None:
    # Each distinct set of named tests, with its marker, on the unmutated code.
    baselines: dict[tuple, subprocess.CompletedProcess[str] | None] = {}
    for m in mutations:
        target = repo / m.file
        if not target.exists():
            m.caught, m.note = None, f"{m.file} not found"
            continue

        original = target.read_text(encoding="utf-8")
        hits = original.count(m.old)
        if hits == 0:
            m.caught, m.note = None, "anchor not found - spec is stale"
            continue
        # replace(..., 1) takes the FIRST match, so a non-unique anchor can
        # break a different site from the one the spec describes and still
        # report on it.
        if hits > 1:
            m.caught, m.note = None, f"anchor matches {hits} times - make it unique"
            continue

        # pytest exits 4 on a path it cannot collect, which is non-zero, which
        # this runner would otherwise read as "the suite objected". Five
        # mutations named test files that do not exist in this repo and were
        # all reported CAUGHT without running a single test. Checking first
        # turns that into the SKIPPED it always was.
        missing = [t for t in m.tests if not (repo / t.split("::")[0]).exists()]
        if missing:
            m.caught, m.note = None, f"named tests do not exist: {', '.join(missing)}"
            continue

        # Every mutation restores its file, so the copy is unmutated here.
        key = (tuple(m.tests), m.marker)
        if key not in baselines:
            baselines[key] = _pytest(m, repo)
        if problem := _baseline_problem(baselines[key]):
            m.baseline_failed, m.note = True, problem
            continue

        target.write_text(original.replace(m.old, m.new, 1), encoding="utf-8")
        try:
            proc = _pytest(m, repo)
        finally:
            target.write_text(original, encoding="utf-8")
        if proc is None:
            # A mutation that hangs the suite is not "caught" in any useful
            # sense, and saying so beats recording a false pass.
            m.caught, m.note = None, "timed out"
            continue
        # Only exit 1 means a test failed. Reading every non-zero exit as
        # CAUGHT counted runs where nothing was measured: 5 is "no tests
        # collected" (e.g. UI tests deselected by -m "not ui"), and 2-4 are
        # collection, internal and usage errors - typically a mutation that
        # broke the file's syntax, so every test died before it ran.
        code = proc.returncode
        if code == 0:
            m.caught, m.note = False, "suite passed against broken code"
        elif code == 1:
            m.caught = True
        elif code == 5:
            m.caught, m.note = (
                None,
                "no tests ran - the named tests were all deselected",
            )
        else:
            m.caught, m.invalid = None, True
            m.note = f"pytest exit {code}: the mutation broke import or collection"


def stale_copies(temp: Path) -> list[Path]:
    """Copies that earlier runs left in temp. A killed run never reaches its
    finally block, and a finished one could not delete read-only folders."""
    cutoff = time.time() - _STALE_AFTER
    return sorted(
        p for p in temp.glob(f"{_PREFIX}*") if p.is_dir() and p.stat().st_mtime < cutoff
    )


def remove(folder: Path) -> str:
    """Delete folder, and say what is left of it: "" once it is gone."""
    errors: list[str] = []

    def writable_then_retry(func, path, _exc):
        # The copy keeps each folder's read-only mark - every folder of this
        # checkout has one - and Windows will not delete a read-only folder.
        try:
            os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
            func(path)
        except OSError as e:
            errors.append(str(e))

    shutil.rmtree(folder, onerror=writable_then_retry)
    if not folder.exists():
        return ""
    files = sum(1 for p in folder.rglob("*") if p.is_file())
    first = f"; first error: {errors[0]}" if errors else ""
    return f"{folder} still holds {files} file(s){first}"


def _as_spec_path(arg: str) -> str:
    """A file path as the spec writes it: forward slashes, from the project
    folder."""
    path = arg.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def select(
    mutations: list[Mutation], selectors: list[str]
) -> tuple[list[Mutation], list[str]]:
    """The mutations the selectors name, in spec order, and the selectors that
    name none. A selector is a group, an id or the file a mutation breaks; no
    selector names every mutation."""
    if not selectors:
        return list(mutations), []

    def names(m: Mutation, s: str) -> bool:
        return s in (m.group, m.id) or m.file == _as_spec_path(s)

    chosen = [m for m in mutations if any(names(m, s) for s in selectors)]
    unmatched = [s for s in selectors if not any(names(m, s) for m in mutations)]
    return chosen, unmatched


def shares(mutations: list[Mutation], workers: int) -> list[list[Mutation]]:
    """Split the mutations among at most `workers` copies.

    Mutations that name the same tests stay together where they can, so each
    set of tests runs once on the unmutated code; a set larger than an even
    share is split, at the cost of one more baseline run per part.
    """
    if not mutations:
        return []
    count = min(workers, len(mutations))
    most = math.ceil(len(mutations) / count)
    by_tests: dict[tuple, list[Mutation]] = {}
    for m in mutations:
        by_tests.setdefault((tuple(m.tests), m.marker), []).append(m)
    parts = [
        batch[i : i + most]
        for batch in by_tests.values()
        for i in range(0, len(batch), most)
    ]
    out: list[list[Mutation]] = [[] for _ in range(count)]
    for part in sorted(parts, key=len, reverse=True):
        min(out, key=len).extend(part)
    return [share for share in out if share]


def _run_in_a_copy(share: list[Mutation], temp: Path) -> None:
    """Copy the repo into a new folder in temp, run the share there, and
    delete the folder, saying so if any of it is left."""
    work = Path(tempfile.mkdtemp(prefix=_PREFIX, dir=temp))
    repo = work / "repo"
    print(f"copying {len(share)} mutation(s) worth of repo to {repo}", flush=True)
    try:
        shutil.copytree(ROOT, repo, ignore=_ignore)
        run(share, repo)
    finally:
        if left := remove(work):
            print(f"\nLEFT BEHIND - this run's copy could not be deleted: {left}")


def _options(argv: list[str]) -> tuple[list[str], int]:
    """The selectors and the number of workers. Raises ValueError on an
    option it does not know, and on a --workers that is not a whole number of
    1 or more."""
    selectors: list[str] = []
    workers = "1"
    rest = iter(argv)
    for arg in rest:
        if arg == "--workers":
            workers = next(rest, "")
        elif arg.startswith("--workers="):
            workers = arg.split("=", 1)[1]
        elif arg.startswith("-"):
            raise ValueError(
                f"unknown option {arg!r}: the options are --list, --help and "
                "--workers N"
            )
        else:
            selectors.append(arg)
    if not workers.isdigit() or int(workers) < 1:
        raise ValueError(
            f"--workers needs a whole number of 1 or more, not {workers!r}"
        )
    return selectors, int(workers)


def main() -> int:
    from mutation_spec import MUTATIONS  # spec lives beside this, imported late

    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    if "--list" in sys.argv:
        for m in MUTATIONS:
            print(f"{m.id:12} {m.group:14} {m.describes}")
        return 0
    try:
        args, workers = _options(sys.argv[1:])
    except ValueError as e:
        print(e)
        return 2

    # A misspelt name ran the others and reported them as if it were all.
    selected, unmatched = select(MUTATIONS, args)
    if unmatched:
        groups = sorted({m.group for m in MUTATIONS})
        print(
            f"nothing matches {unmatched} (no group, id or file of that name); "
            f"groups: {', '.join(groups)}"
        )
        return 2

    temp = Path(tempfile.gettempdir())
    deleted = 0
    for stale in stale_copies(temp):
        if left := remove(stale):
            print(f"LEFT BEHIND by an earlier run, and still not deleted: {left}")
        else:
            deleted += 1
    if deleted:
        print(f"deleted {deleted} copy folder(s) that earlier runs left in {temp}")

    if workers == 1:
        _run_in_a_copy(selected, temp)
    else:
        split = shares(selected, workers)
        print(f"{len(split)} worker(s), each in its own copy")
        with ThreadPoolExecutor(max_workers=len(split)) as pool:
            done = [pool.submit(_run_in_a_copy, share, temp) for share in split]
        for future in done:
            future.result()  # a worker's error, raised once every copy is gone

    states = {m.id: state_of(m) for m in selected}
    survived = [m for m in selected if states[m.id] == "SURVIVED"]
    skipped = [m for m in selected if states[m.id] == "SKIPPED"]
    invalid = [m for m in selected if states[m.id] == "INVALID"]
    baseline = [m for m in selected if states[m.id] == "BASELINE-FAIL"]
    caught = len(selected) - len(survived) - len(skipped) - len(invalid) - len(baseline)

    print()
    for m in selected:
        print(f"  {states[m.id]:13} {m.id:12} {m.describes}")
        if m.note:
            print(f"                {m.note}")

    print()
    print(
        f"{caught} caught, {len(survived)} survived, {len(skipped)} skipped, "
        f"{len(invalid)} invalid, {len(baseline)} baseline-fail"
    )
    if survived:
        print("\nSURVIVORS - each is a defect the suite would not notice:")
        for m in survived:
            print(f"  {m.id:12} {m.file}")
            print(f"               {m.describes}")
            print(f"               covered by: {' '.join(m.tests)}")
    if baseline:
        print("\nBASELINE-FAIL - these fail with no mutation at all; fix them first:")
        for tests in sorted({" ".join(m.tests) for m in baseline}):
            print(f"  {tests}")
    # Skipped entries mean the spec drifted from the code, which is its own
    # kind of silent failure - a stale anchor tests nothing and says nothing.
    # Invalid ones are broken mutations, and baseline failures broken tests,
    # and neither measures anything either.
    return 1 if (survived or skipped or invalid or baseline) else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
