"""The mutation runner only says CAUGHT when a test actually caught something.

It read any non-zero pytest exit as "the suite objected". Two of those exits
mean nothing was measured at all:

- exit 5, no tests collected - e.g. a mutation pointed only at UI tests,
  which the default `-m "not ui"` deselects;
- exit 2, collection or import error - the mutation broke the file's syntax,
  so every test died before running. A broken mutation of last week's Direct
  Send consent-row guard read as CAUGHT exactly this way.

And it replaced the FIRST match of an anchor without checking there was only
one, so a non-unique anchor could mutate a different site from the one the
spec describes and still report on it.

And it never ran a mutation's tests on the unmutated code, so a test that
already failed read CAUGHT whatever the mutation did. At 0ea19cf, in a clean
clone without SUPPLECO's client list, 147 of the 529 mutations named such a
test: SND21 read CAUGHT on a file that failed 10 of its 16 tests unmutated.

Each case runs the real `run()`, or `main()`, against a tiny throwaway repo.
"""

import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mutation_audit
from mutation_audit import Mutation, run, state_of

MOD = "def f():\n    return 1\n\n\ndef g():\n    return 1\n"
TEST = "from mod import f\n\n\ndef test_f():\n    assert f() == 1\n"
# test_f catches a broken f(), but the file fails on the unmutated code too.
BROKEN = TEST + "\n\ndef test_already_broken():\n    assert False\n"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "mod.py").write_text(MOD, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text(TEST, encoding="utf-8")
    (tmp_path / "tests" / "test_nothing.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
        encoding="utf-8",
    )
    return tmp_path


def _one(repo, old, new, tests=("tests/test_mod.py",)):
    m = Mutation("X1", "g", "mod.py", old, new, list(tests), "test mutation")
    run([m], repo)
    return m


def test_a_failing_test_is_caught(repo):
    m = _one(repo, "def f():\n    return 1", "def f():\n    return 2")
    assert state_of(m) == "CAUGHT"


def test_a_passing_suite_is_a_survivor(repo):
    m = _one(repo, "def g():\n    return 1", "def g():\n    return 2")
    assert state_of(m) == "SURVIVED"


def test_no_tests_collected_is_not_caught(repo):
    m = _one(
        repo,
        "def f():\n    return 1",
        "def f():\n    return 2",
        tests=("tests/test_nothing.py",),
    )
    assert state_of(m) == "SKIPPED", m.note
    assert "no tests ran" in m.note


def test_a_mutation_that_breaks_the_syntax_is_invalid(repo):
    m = _one(repo, "def f():", "def f(:")
    assert state_of(m) == "INVALID", m.note


def test_an_ambiguous_anchor_is_not_applied(repo):
    """'return 1' appears in both f and g."""
    m = _one(repo, "    return 1", "    return 2")
    assert state_of(m) == "SKIPPED"
    assert "2 times" in m.note
    assert (repo / "mod.py").read_text(encoding="utf-8") == MOD


def test_the_working_file_is_restored(repo):
    _one(repo, "def f():\n    return 1", "def f():\n    return 2")
    assert (repo / "mod.py").read_text(encoding="utf-8") == MOD


def test_every_mutation_in_the_spec_has_its_own_id():
    """Two entries sharing an id cannot be told apart in the report, and a
    tool that edits the spec by id edits both - which is how one of them was
    once silently overwritten."""
    from mutation_spec import MUTATIONS

    ids = [m.id for m in MUTATIONS]
    assert sorted({i for i in ids if ids.count(i) > 1}) == []


def test_a_marked_test_runs_when_the_mutation_names_its_marker(repo):
    """The project's pytest.ini deselects UI tests by default (-m "not ui"),
    so a mutation only a UI test can catch read "no tests ran". A mutation
    that names the marker runs those tests."""
    (repo / "pytest.ini").write_text(
        '[pytest]\naddopts = -m "not slow"\nmarkers =\n    slow: slow tests\n',
        encoding="utf-8",
    )
    (repo / "tests" / "test_slow.py").write_text(
        "import pytest\nfrom mod import f\n\n\n@pytest.mark.slow\n"
        "def test_f():\n    assert f() == 1\n",
        encoding="utf-8",
    )
    old, new = "def f():\n    return 1", "def f():\n    return 2"
    unmarked = Mutation("X1", "g", "mod.py", old, new, ["tests/test_slow.py"], "t")
    marked = Mutation(
        "X2", "g", "mod.py", old, new, ["tests/test_slow.py"], "t", marker="slow"
    )
    run([unmarked, marked], repo)
    assert state_of(unmarked) == "SKIPPED"
    assert state_of(marked) == "CAUGHT", marked.note


def test_a_test_that_already_fails_is_not_a_catch(repo):
    (repo / "tests" / "test_broken.py").write_text(BROKEN, encoding="utf-8")
    m = _one(
        repo,
        "def f():\n    return 1",
        "def f():\n    return 2",
        tests=("tests/test_broken.py",),
    )
    assert state_of(m) == "BASELINE-FAIL", m.note
    assert "tests/test_broken.py::test_already_broken" in m.note
    assert (repo / "mod.py").read_text(encoding="utf-8") == MOD


def test_baseline_failures_are_counted_and_fail_the_run(
    repo, tmp_path_factory, monkeypatch, capsys
):
    (repo / "tests" / "test_broken.py").write_text(BROKEN, encoding="utf-8")
    spec = types.ModuleType("mutation_spec")
    spec.MUTATIONS = [
        Mutation(
            "X1",
            "g",
            "mod.py",
            "def f():\n    return 1",
            "def f():\n    return 2",
            ["tests/test_broken.py"],
            "test mutation",
        )
    ]
    monkeypatch.setitem(sys.modules, "mutation_spec", spec)
    monkeypatch.setattr(mutation_audit, "ROOT", repo)
    # Its own temp folder: a run deletes old copies from the one it uses.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("temp")))
    monkeypatch.setattr(sys, "argv", ["mutation_audit.py"])

    code = mutation_audit.main()
    out = capsys.readouterr().out
    assert "0 caught" in out and "1 baseline-fail" in out, out
    assert code == 1


def test_each_set_of_tests_runs_once_on_the_unmutated_code(repo, monkeypatch):
    runs = []
    real = subprocess.run
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: runs.append(a[0]) or real(*a, **k)
    )
    f = Mutation(
        "X1",
        "g",
        "mod.py",
        "def f():\n    return 1",
        "def f():\n    return 2",
        ["tests/test_mod.py"],
        "t",
    )
    g = Mutation(
        "X2",
        "g",
        "mod.py",
        "def g():\n    return 1",
        "def g():\n    return 2",
        ["tests/test_mod.py"],
        "t",
    )
    run([f, g], repo)
    assert [state_of(f), state_of(g)] == ["CAUGHT", "SURVIVED"]
    assert len(runs) == 3, "one run on the unmutated code, then one per mutation"
