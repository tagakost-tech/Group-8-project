"""Doctor and the dashboard give the same SPF and DMARC verdict, in the same words.

Doctor had its own lookup. It read every failure to look a record up - a
timeout, a resolver that cannot be reached, dnspython not installed - as a
failed check, "error: ...", where the dashboard said "could not be checked
just now". Now doctor asks the dashboard's lookup: a record found is OK, a
record absent is a WARN ("no SPF record found"), and a lookup that could not
be made is a SKIP, never "no record". The resolver is faked: no real DNS
query is made.
"""

import sys
from types import SimpleNamespace
from typing import ClassVar

import dns.resolver
import pytest

import main
from main import run_doctor_checks

DOMAIN = "suppleco.example"
SPF = f"SPF record ({DOMAIN})"
DMARC = f"DMARC record ({DOMAIN})"


class _Resolver:
    """Stands in for dns.resolver.Resolver. ``answers`` maps each name to
    what it does: "found", "absent" (NXDOMAIN), "empty" (no TXT record of
    that kind) or "down" (the resolver cannot be reached)."""

    answers: ClassVar[dict] = {}

    def __init__(self, *_a, **_k):
        self.timeout = self.lifetime = None

    def resolve(self, name, rdtype, *_a, **_k):
        what = self.answers[name]
        if what == "absent":
            raise dns.resolver.NXDOMAIN()
        if what == "down":
            raise dns.resolver.LifetimeTimeout(timeout=3.0, errors=[])
        if what == "empty":
            return [SimpleNamespace(strings=[b"google-site-verification=abc"])]
        marker = b"v=DMARC1; p=none" if name.startswith("_dmarc.") else b"v=spf1 -all"
        return [SimpleNamespace(strings=[marker])]


def _answers(monkeypatch, spf, dmarc):
    _Resolver.answers = {DOMAIN: spf, f"_dmarc.{DOMAIN}": dmarc}
    monkeypatch.setattr(dns.resolver, "Resolver", _Resolver)


@pytest.fixture
def settings(settings_factory, tmp_path):
    return settings_factory(
        db_path=str(tmp_path / "a.db"),
        smtp_username=f"info@{DOMAIN}",
        smtp_password="a-mailbox-password",
        blacklist_check_enabled=False,
    )


def _doctor(settings):
    checks = run_doctor_checks(settings, network=True, only=(SPF, DMARC))
    return {c.label: (c.ok, c.detail) for c in checks}


def _panel(settings):
    client = main.create_dashboard_app(settings).test_client()
    return client.get("/api/deliverability").get_json()["sending"]


@pytest.mark.parametrize(
    "spf, dmarc, verdict, note",
    [
        ("found", "found", (True, True), None),
        ("found", "absent", (True, False), None),
        ("empty", "found", (False, True), None),
        ("down", "down", (None, None), "SPF and DMARC could not be checked just now."),
        ("found", "down", (True, None), "DMARC could not be checked just now."),
        ("down", "absent", (None, False), "SPF could not be checked just now."),
    ],
)
def test_doctor_and_the_panel_give_one_verdict(
    settings, monkeypatch, spf, dmarc, verdict, note
):
    _answers(monkeypatch, spf, dmarc)

    rows = _doctor(settings)
    panel = _panel(settings)

    assert (rows[SPF][0], rows[DMARC][0]) == verdict
    assert (panel["spf"], panel["dmarc"]) == verdict
    assert panel["dns_note"] == note


@pytest.mark.parametrize(
    "spf, dmarc, words",
    [
        ("found", "found", ("SPF record found", "DMARC record found")),
        ("absent", "empty", ("no SPF record found", "no DMARC record found")),
        (
            "down",
            "down",
            (
                "SPF could not be checked just now",
                "DMARC could not be checked just now",
            ),
        ),
    ],
)
def test_doctor_says_it_in_the_panels_words(settings, monkeypatch, spf, dmarc, words):
    _answers(monkeypatch, spf, dmarc)

    rows = _doctor(settings)

    assert (rows[SPF][1], rows[DMARC][1]) == words


def test_a_lookup_that_could_not_be_made_is_skipped_not_a_missing_record(
    settings, monkeypatch
):
    _answers(monkeypatch, "down", "down")

    rows = _doctor(settings)

    for label in (SPF, DMARC):
        ok, detail = rows[label]
        assert ok is None, f"{label}: a timeout read as {ok!r} ({detail})"
        assert "no " not in detail and "error" not in detail


def test_without_dnspython_both_are_skipped(settings, monkeypatch):
    monkeypatch.setitem(sys.modules, "dns.resolver", None)

    rows = _doctor(settings)

    assert rows == {
        SPF: (None, "SPF could not be checked just now"),
        DMARC: (None, "DMARC could not be checked just now"),
    }


def test_doctor_asks_each_record_once(settings, monkeypatch):
    asked = []

    class _Counting(_Resolver):
        def resolve(self, name, rdtype, *a, **k):
            asked.append(name)
            return super().resolve(name, rdtype, *a, **k)

    _Resolver.answers = {DOMAIN: "found", f"_dmarc.{DOMAIN}": "found"}
    monkeypatch.setattr(dns.resolver, "Resolver", _Counting)

    _doctor(settings)

    assert sorted(asked) == sorted([DOMAIN, f"_dmarc.{DOMAIN}"])
