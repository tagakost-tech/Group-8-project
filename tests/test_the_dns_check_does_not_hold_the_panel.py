"""The deliverability panel's SPF/DMARC check must not hold the dashboard up.

GET /api/deliverability used to ask DNS for two TXT records, one after the
other with 3 s each, on every call and with no memory, and it asked even when
no sending domain was set. A browser walk-through waited 7 s for it while the
Insights and Campaigns panels said 'Loading...'. These tests fake the
resolver: no real DNS query is made.
"""

import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import ClassVar

import dns.resolver
import pytest

import main
from modules import clock


class _Resolver:
    """Stands in for dns.resolver.Resolver and records every name asked for."""

    asked: ClassVar[list] = []
    behaviour = "found"
    release = threading.Event()

    def __init__(self, *_a, **_k):
        self.timeout = self.lifetime = None

    def resolve(self, name, rdtype, *_a, **_k):
        type(self).asked.append(name)
        if self.behaviour == "hang":
            self.release.wait(10)
            raise dns.resolver.NXDOMAIN()
        if self.behaviour == "raise":
            raise OSError("network is unreachable")
        marker = b"v=DMARC1; p=none" if name.startswith("_dmarc.") else b"v=spf1 -all"

        return [SimpleNamespace(strings=[marker])]


@pytest.fixture
def resolver(monkeypatch):
    _Resolver.asked = []
    _Resolver.behaviour = "found"
    _Resolver.release = threading.Event()
    monkeypatch.setattr(dns.resolver, "Resolver", _Resolver)
    yield _Resolver
    _Resolver.release.set()


def _client(tmp_path, settings_factory, username):
    s = settings_factory(
        db_path=str(tmp_path / "dns.db"),
        smtp_username=username,
        blacklist_check_enabled=False,
    )
    return main.create_dashboard_app(s).test_client()


def test_no_sending_domain_means_no_lookup_and_a_plain_message(
    tmp_path, settings_factory, resolver
):
    client = _client(tmp_path, settings_factory, "")
    sending = client.get("/api/deliverability").get_json()["sending"]
    assert resolver.asked == []
    assert sending["spf"] is None and sending["dmarc"] is None
    assert sending["dns_note"] == (
        "No sending domain is set, so SPF and DMARC were not checked."
    )


def test_the_answer_is_remembered_for_ten_minutes(
    tmp_path, settings_factory, resolver, monkeypatch
):
    client = _client(tmp_path, settings_factory, "info@suppleco.example")
    first = client.get("/api/deliverability").get_json()["sending"]
    assert sorted(resolver.asked) == ["_dmarc.suppleco.example", "suppleco.example"]
    assert first["spf"] is True and first["dmarc"] is True
    assert first["dns_note"] is None

    start = clock.now()
    monkeypatch.setattr(clock, "now", lambda: start + timedelta(minutes=9))
    again = client.get("/api/deliverability").get_json()["sending"]
    assert len(resolver.asked) == 2, "a second call within ten minutes looked again"
    assert again["spf"] is True and again["dmarc"] is True

    monkeypatch.setattr(clock, "now", lambda: start + timedelta(minutes=11))
    client.get("/api/deliverability")
    assert len(resolver.asked) == 4, "after ten minutes it did not look again"


def test_a_recheck_looks_again_at_once(tmp_path, settings_factory, resolver):
    client = _client(tmp_path, settings_factory, "info@suppleco.example")
    client.get("/api/deliverability")
    client.get("/api/deliverability?recheck=1")
    assert len(resolver.asked) == 4


def test_a_resolver_that_hangs_is_cut_off_at_the_deadline(
    tmp_path, settings_factory, resolver, monkeypatch
):
    monkeypatch.setattr(main, "AUTH_RECORDS_DEADLINE_SECONDS", 0.5)
    resolver.behaviour = "hang"
    client = _client(tmp_path, settings_factory, "info@suppleco.example")
    started = time.monotonic()
    sending = client.get("/api/deliverability").get_json()["sending"]
    took = time.monotonic() - started
    # Both lookups share the one deadline: one after the other would take 1 s.
    assert took < 0.9, f"the request was held {took:.2f}s"
    assert sending["spf"] is None and sending["dmarc"] is None
    assert sending["dns_note"] == "SPF and DMARC could not be checked just now."


def test_a_resolver_that_raises_says_could_not_be_checked(
    tmp_path, settings_factory, resolver
):
    resolver.behaviour = "raise"
    client = _client(tmp_path, settings_factory, "info@suppleco.example")
    sending = client.get("/api/deliverability").get_json()["sending"]
    assert sending["spf"] is None and sending["dmarc"] is None
    assert sending["dns_note"] == "SPF and DMARC could not be checked just now."


def test_a_domain_without_records_is_reported_absent(
    tmp_path, settings_factory, monkeypatch
):
    class _NoRecords(_Resolver):
        def resolve(self, name, *_a, **_k):
            raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "Resolver", _NoRecords)
    client = _client(tmp_path, settings_factory, "info@suppleco.example")
    sending = client.get("/api/deliverability").get_json()["sending"]
    assert sending["spf"] is False and sending["dmarc"] is False
    assert sending["dns_note"] is None


def test_the_memory_is_bounded(tmp_path, settings_factory, resolver, monkeypatch):
    """One entry per sending domain, and never more than the cap."""
    monkeypatch.setattr(main, "AUTH_RECORDS_MAX_DOMAINS", 2)
    s = settings_factory(db_path=str(tmp_path / "b.db"), blacklist_check_enabled=False)
    app = main.create_dashboard_app(s)
    client = app.test_client()
    for domain in ("a.example", "b.example", "c.example"):
        s.smtp_username = f"info@{domain}"
        client.get("/api/deliverability")
    assert len(app.config["AUTH_RECORDS_MEMORY"]) == 2
