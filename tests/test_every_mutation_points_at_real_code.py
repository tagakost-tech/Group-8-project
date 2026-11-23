"""Every mutation in the spec can be applied, and its tests can run.

The audit finds a stale anchor only when the mutation is run, which for most
of them is rarely: it reports SKIPPED, and a mutation that measures nothing
says nothing. A mutation that names a browser test without the ui marker is
worse, because it still reads CAUGHT or SURVIVED: pytest.ini deselects the
browser tests (-m "not ui"), so the test it names never runs. SEC30 and SEC31
named the content-policy browser test that way.

These checks read the spec and the files only, so they run in the normal
suite in about a second.
"""

import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mutation_spec import MUTATIONS

UI = "tests/ui/"


@cache
def _source(file: str) -> str:
    # Read as the audit reads it, so the anchors match the same text.
    return (ROOT / file).read_text(encoding="utf-8")


def _test_file(test: str) -> str:
    return test.split("::", 1)[0]


def test_every_id_is_its_own():
    ids = [m.id for m in MUTATIONS]
    assert sorted({i for i in ids if ids.count(i) > 1}) == []


def test_every_mutation_names_a_file_that_exists():
    assert [(m.id, m.file) for m in MUTATIONS if not (ROOT / m.file).is_file()] == []


def test_every_anchor_occurs_exactly_once_in_its_file():
    """None found: the spec is stale. More than one: the audit refuses it,
    since it would break the first match, which may not be the one meant."""
    wrong = [
        (m.id, m.file, _source(m.file).count(m.old))
        for m in MUTATIONS
        if (ROOT / m.file).is_file() and _source(m.file).count(m.old) != 1
    ]
    assert wrong == []


def test_every_mutation_changes_its_file():
    assert [m.id for m in MUTATIONS if m.old == m.new] == []


def test_every_named_test_file_exists():
    missing = [
        (m.id, t)
        for m in MUTATIONS
        for t in m.tests
        if not (ROOT / _test_file(t)).is_file()
    ]
    assert missing == []


def test_a_mutation_that_names_a_browser_test_runs_the_browser_tests():
    unmarked = [
        m.id
        for m in MUTATIONS
        if any(_test_file(t).startswith(UI) for t in m.tests) and m.marker != "ui"
    ]
    assert unmarked == []


def test_a_mutation_marked_ui_names_only_browser_tests():
    """-m ui deselects every test without the marker, so an ordinary test
    named beside a browser test would never run either."""
    mixed = [
        m.id
        for m in MUTATIONS
        if m.marker == "ui" and any(not _test_file(t).startswith(UI) for t in m.tests)
    ]
    assert mixed == []


def test_a_browser_test_file_carries_the_ui_marker():
    """The marker on the mutation selects the tests that carry it, so a
    browser test file without pytestmark = pytest.mark.ui would read "no tests
    ran" under every mutation that names it."""
    named = {_test_file(t) for m in MUTATIONS for t in m.tests if t.startswith(UI)}
    unmarked = [
        f for f in sorted(named) if "pytestmark = pytest.mark.ui" not in _source(f)
    ]
    assert unmarked == []
