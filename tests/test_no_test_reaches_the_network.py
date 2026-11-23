"""No test reaches another machine: the guard in conftest.py, proved.

The suite used to send the default prospecting queries to public search
engines, open an SMTP session with smtp.gmail.com and make live DNS lookups.
tests/network_guard_probes.py reaches out in each way the suite did and
hides the error as degrading code does; it runs here in a child pytest,
because each of its probes must FAIL there. A floor beneath the guard,
laid when the probes are imported, refuses the same calls and writes down
any that reach it, so a broken guard shows without a packet leaving.
"""

import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROBES = "tests/network_guard_probes.py"
WAYS_OUT = [
    "test_a_tcp_connection",
    "test_a_tcp_connection_that_returns_an_error_code",
    "test_a_udp_datagram",
    "test_a_name_lookup",
    "test_a_curl_cffi_request",
    "test_a_curl_cffi_async_request",
    "test_a_primp_request",
    "test_a_primp_async_request",
    "test_a_primp_request_without_a_client",
]


def _run_probes(tmp_path, *args):
    floor = tmp_path / "floor.log"
    env = dict(os.environ, NETWORK_FLOOR_LOG=str(floor), PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA", PROBES, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        # The run fails by design: every way out must fail its probe.
        check=False,
    )
    outcomes = {}
    for line in proc.stdout.splitlines():
        word, _, rest = line.partition(" ")
        if word in ("PASSED", "FAILED", "ERROR", "SKIPPED") and "::" in rest:
            name = rest.split("::", 1)[1].split(" ")[0]
            outcomes.setdefault(name, set()).add(word)
    reached = floor.read_text(encoding="utf-8").splitlines() if floor.exists() else []
    return outcomes, reached, proc.stdout


def test_every_way_out_fails_its_test_and_none_reaches_the_floor(tmp_path):
    outcomes, reached, out = _run_probes(tmp_path)

    for name in WAYS_OUT:
        assert outcomes.get(name, set()) & {"FAILED", "ERROR"}, f"{name} was let through:\n{out}"
    assert reached == [], f"the guard let these reach the floor: {reached}"


def test_this_machine_stays_reachable(tmp_path):
    outcomes, reached, out = _run_probes(tmp_path, "-k", "this_machine")

    assert outcomes.get("test_this_machine_can_still_be_reached") == {"PASSED"}, out
    assert reached == []


def test_the_browser_tests_are_guarded_too(tmp_path):
    outcomes, reached, out = _run_probes(tmp_path, "-m", "ui")

    assert outcomes.get("test_a_browser_test_is_guarded_too", set()) & {"FAILED", "ERROR"}, out
    chromium = outcomes.get("test_chromium_in_a_browser_test_reaches_no_other_server", set())
    if chromium == {"SKIPPED"}:
        pytest.skip("Chromium is not available here")
    assert chromium & {"FAILED", "ERROR"}, out
    assert "Chromium GET https://dashboard-cdn.invalid/chart.js" in out
    assert reached == [], f"the guard let these reach the floor: {reached}"
