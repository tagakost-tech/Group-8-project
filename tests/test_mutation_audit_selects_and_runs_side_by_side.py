"""The mutation audit takes a file path as a selector, and can run side by side.

A run of every mutation of one file needed each id typed out, and a full run
took hours one mutation at a time. Now a file path selects the mutations of
that file, and --workers N runs the selection in N copies of the repo at once.
The report must not change with the number of workers: the same states, the
same notes, the same totals, in the same order, every copy deleted, and no
client list or settings file in any copy.

Each case runs the real main() on a tiny source tree of invented files.
"""

import os
import re
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import mutation_audit
from mutation_audit import Mutation, select, shares

MOD = "def f():\n    return 1\n\n\ndef g():\n    return 1\n"
OTHER = "def h():\n    return 1\n"
TEST = (
    "from mod import f\nfrom other import h\n\n\n"
    "def test_f():\n    assert f() == 1\n\n\n"
    "def test_h():\n    assert h() == 1\n"
)
BROKEN = "def test_already_broken():\n    assert False\n"
HOUR = 60 * 60


def _m(id_, file, old, new, tests=("tests/test_mod.py",), group="g"):
    return Mutation(id_, group, file, old, new, list(tests), f"breaks {id_}")


def _spec():
    """One mutation of each outcome, over two files and three sets of tests."""
    f = ("def f():\n    return 1", "def f():\n    return 2")
    return [
        _m("X1", "mod.py", *f),
        _m("X2", "mod.py", "def g():\n    return 1", "def g():\n    return 2"),
        _m("X3", "other.py", "def h():\n    return 1", "def h():\n    return 2"),
        _m("X4", "other.py", "no such text", "x", group="other"),
        _m("X5", "mod.py", "def f():", "def f(:", group="other"),
        _m("X6", "mod.py", *f, tests=("tests/test_broken.py",), group="other"),
    ]


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    (root / "tests").mkdir(parents=True)
    (root / "mod.py").write_text(MOD, encoding="utf-8")
    (root / "other.py").write_text(OTHER, encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text(TEST, encoding="utf-8")
    (root / "tests" / "test_broken.py").write_text(BROKEN, encoding="utf-8")
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


def _audit(monkeypatch, source, *argv, spec=None):
    module = types.ModuleType("mutation_spec")
    module.MUTATIONS = spec if spec is not None else _spec()
    monkeypatch.setitem(sys.modules, "mutation_spec", module)
    monkeypatch.setattr(mutation_audit, "ROOT", source)
    monkeypatch.setattr(sys, "argv", ["mutation_audit.py", *argv])
    return mutation_audit.main()


def _report(out: str) -> str:
    """The report from its first result line to the end."""
    first = re.search(
        r"^  (?:CAUGHT|SURVIVED|SKIPPED|INVALID|BASELINE-FAIL) ", out, re.MULTILINE
    )
    assert first, out
    return out[first.start() :]


def _ids(chosen):
    return [m.id for m in chosen]


# -- selecting ---------------------------------------------------------------


def test_a_file_path_selects_every_mutation_of_that_file():
    chosen, unmatched = select(_spec(), ["other.py"])
    assert _ids(chosen) == ["X3", "X4"]
    assert unmatched == []


@pytest.mark.parametrize(
    "path", ["modules/export.py", "modules\\export.py", "./modules/export.py"]
)
def test_a_file_path_is_matched_whichever_way_it_is_written(path):
    spec = [
        _m("E1", "modules/export.py", "a", "b"),
        _m("E2", "modules/exporter.py", "a", "b"),
        _m("E3", "export.py", "a", "b"),
    ]
    chosen, unmatched = select(spec, [path])
    assert _ids(chosen) == ["E1"]
    assert unmatched == []


def test_groups_ids_and_files_mix_and_keep_the_spec_order():
    chosen, unmatched = select(_spec(), ["X6", "other.py", "g"])
    assert _ids(chosen) == ["X1", "X2", "X3", "X4", "X6"]
    assert unmatched == []


def test_no_selector_selects_everything():
    chosen, unmatched = select(_spec(), [])
    assert _ids(chosen) == ["X1", "X2", "X3", "X4", "X5", "X6"]
    assert unmatched == []


def test_a_name_that_matches_nothing_stops_the_run(source, temp, monkeypatch, capsys):
    """A misspelt id was dropped, and the run reported on the others as if
    they were everything asked for."""
    ran = []
    monkeypatch.setattr(mutation_audit, "run", lambda ms, repo: ran.append(ms))

    code = _audit(monkeypatch, source, "X1", "X11", "nope.py")

    out = capsys.readouterr().out
    assert code == 2
    assert "X11" in out and "nope.py" in out, out
    assert "'X1'" not in out.split("nothing matches", 1)[1].split("(", 1)[0]
    assert ran == [] and list(temp.iterdir()) == []


@pytest.mark.parametrize(
    "argv", [("--workers", "0"), ("--workers=x",), ("--workers",), ("--workers=-2",)]
)
def test_a_bad_number_of_workers_stops_the_run(source, temp, monkeypatch, argv):
    ran = []
    monkeypatch.setattr(mutation_audit, "run", lambda ms, repo: ran.append(ms))

    assert _audit(monkeypatch, source, "X1", *argv) == 2
    assert ran == [] and list(temp.iterdir()) == []


@pytest.mark.parametrize(
    "argv", [("--workers4",), ("--worker", "4"), ("-n", "4"), ("X1", "--worker=4")]
)
def test_an_option_it_does_not_know_stops_the_run(
    source, temp, monkeypatch, capsys, argv
):
    """Ignored, a mistyped --workers ran every mutation, for hours, one at a
    time; with only the option given, the whole spec."""
    ran = []
    monkeypatch.setattr(mutation_audit, "run", lambda ms, repo: ran.append(ms))

    assert _audit(monkeypatch, source, *argv) == 2
    assert "unknown option" in capsys.readouterr().out
    assert ran == [] and list(temp.iterdir()) == []


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_the_usage_and_runs_nothing(
    source, temp, monkeypatch, capsys, flag
):
    ran = []
    monkeypatch.setattr(mutation_audit, "run", lambda ms, repo: ran.append(ms))

    assert _audit(monkeypatch, source, flag) == 0
    assert "--workers N" in capsys.readouterr().out
    assert ran == [] and list(temp.iterdir()) == []


# -- running side by side ----------------------------------------------------


def test_mutations_that_name_the_same_tests_share_a_copy():
    """Each set of tests runs once on the unmutated code in its copy, so
    splitting a set between copies would run its baseline twice."""
    # Interleaved, as a spec's sections are: dealt out one by one, the sets
    # would be torn apart.
    spec = [
        _m(id_, "mod.py", "a", "b", tests=(f"tests/test_{id_[0]}.py",))
        for id_ in ("a0", "b0", "c0", "a1", "b1", "a2")
    ]
    split = shares(spec, 2)
    assert sorted(sorted(_ids(share)) for share in split) == [
        ["a0", "a1", "a2"],
        ["b0", "b1", "c0"],
    ]


def test_every_mutation_goes_to_exactly_one_copy():
    split = shares(_spec(), 3)
    assert len(split) == 3
    assert sorted(_ids(m for share in split for m in share)) == [
        "X1",
        "X2",
        "X3",
        "X4",
        "X5",
        "X6",
    ]


def test_a_set_larger_than_an_even_share_is_split():
    spec = [_m(f"S{i}", "mod.py", "a", "b") for i in range(8)]
    split = shares(spec, 4)
    assert [len(share) for share in split] == [2, 2, 2, 2]


def test_never_more_copies_than_mutations():
    assert len(shares(_spec()[:2], 8)) == 2


def test_the_parallel_run_reports_what_the_serial_run_does(
    source, temp, monkeypatch, capsys
):
    serial_code = _audit(monkeypatch, source, "g", "other")
    serial = capsys.readouterr().out
    assert list(temp.iterdir()) == []

    # A copy a killed run left behind is still deleted first.
    (temp / "mutation-audit-killed").mkdir()
    then = time.time() - 13 * HOUR
    os.utime(temp / "mutation-audit-killed", (then, then))

    parallel_code = _audit(monkeypatch, source, "g", "other", "--workers", "3")
    parallel = capsys.readouterr().out

    assert "deleted 1 copy folder(s)" in parallel, parallel
    assert parallel.count("copying ") == 3, parallel
    assert serial.count("copying ") == 1, serial
    assert _report(parallel) == _report(serial)
    for state, id_ in (
        ("CAUGHT", "X1"),
        ("SURVIVED", "X2"),
        ("CAUGHT", "X3"),
        ("SKIPPED", "X4"),
        ("INVALID", "X5"),
        ("BASELINE-FAIL", "X6"),
    ):
        assert re.search(rf"^  {state} +{id_} ", parallel, re.MULTILINE), parallel
    assert "2 caught, 1 survived, 1 skipped, 1 invalid, 1 baseline-fail" in parallel
    assert serial_code == parallel_code == 1
    assert "LEFT BEHIND" not in parallel
    assert list(temp.iterdir()) == []
    assert (source / "mod.py").read_text(encoding="utf-8") == MOD
    assert (source / "other.py").read_text(encoding="utf-8") == OTHER


def test_every_copy_of_a_parallel_run_holds_no_client_data(source, temp, monkeypatch):
    for name in (
        "Suppleco_Elenco_Clienti_17072026 (1).xlsx",
        ".env",
        ".env.local",
        "microsoft-signin.pem",
        ".claude/launch.json",
    ):
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text("invented\n", encoding="utf-8")
    copies = {}

    def spy(mutations, repo):
        copies[repo] = [p.relative_to(repo).as_posix() for p in repo.rglob("*")]

    monkeypatch.setattr(mutation_audit, "run", spy)
    _audit(monkeypatch, source, "--workers=3")

    assert len(copies) == 3
    for copied in copies.values():
        assert "mod.py" in copied
        leaked = [
            p
            for p in copied
            if p.endswith((".xlsx", ".pem"))
            or p.split("/")[0] in (".env", ".env.local", ".claude")
        ]
        assert leaked == []
    assert list(temp.iterdir()) == []


def test_a_worker_that_fails_still_leaves_no_copy(source, temp, monkeypatch):
    calls = []

    def boom(mutations, repo):
        calls.append(repo)
        if len(calls) == 1:
            raise RuntimeError("worker failed")

    monkeypatch.setattr(mutation_audit, "run", boom)
    with pytest.raises(RuntimeError, match="worker failed"):
        _audit(monkeypatch, source, "--workers", "2")

    assert len(calls) == 2
    assert list(temp.iterdir()) == []
