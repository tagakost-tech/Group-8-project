"""Tests for DNSBL blacklist monitoring of the sending domain."""

from modules.blacklist import (
    CLEAN,
    LISTED,
    PARTIAL,
    UNAVAILABLE,
    BlacklistChecker,
)


class _Ans:
    """Stand-in for a dnspython A-record answer."""

    def __init__(self, address):
        self.address = address


def _checker(monkeypatch, behaviour):
    """Build a checker whose DNS lookups are driven by `behaviour(name)`."""

    chk = BlacklistChecker(cache_ttl=0)  # no caching in tests

    class FakeResolver:
        timeout = lifetime = 1

        def resolve(self, name, rtype):
            return behaviour(name)

    monkeypatch.setattr(chk, "_resolver", lambda: FakeResolver())
    return chk


def _nxdomain(_name):
    import dns.resolver

    raise dns.resolver.NXDOMAIN()


# ── domain checks ───────────────────────────────────────────────────────────


def test_clean_domain(monkeypatch):
    chk = _checker(monkeypatch, _nxdomain)
    summary = chk.summarize(chk.check_domain("suppleco.com"))
    assert summary["status"] == CLEAN
    assert summary["listed_on"] == []


def test_listed_domain(monkeypatch):
    chk = _checker(monkeypatch, lambda n: [_Ans("127.0.1.2")])
    summary = chk.summarize(chk.check_domain("spammy.com"))
    assert summary["status"] == LISTED
    assert "Spamhaus DBL" in summary["listed_on"]


def test_blocked_resolver_is_not_reported_clean(monkeypatch):
    """A public-DNS refusal (127.255.255.x) must NOT look like 'clean' -
    that would hide a real listing."""
    chk = _checker(monkeypatch, lambda n: [_Ans("127.255.255.254")])
    results = chk.check_domain("suppleco.com")
    assert all(r["status"] == UNAVAILABLE for r in results)
    assert chk.summarize(results)["status"] == UNAVAILABLE


def test_dns_error_is_unavailable_not_clean(monkeypatch):
    def boom(_name):
        raise Exception("timeout")  # noqa: TRY002 - "any failure", deliberately

    chk = _checker(monkeypatch, boom)
    assert chk.summarize(chk.check_domain("suppleco.com"))["status"] == UNAVAILABLE


def test_partial_failure_still_reports_listing(monkeypatch):
    """One list errors, another says listed -> overall LISTED."""

    def mixed(name):
        if "surbl" in name:
            raise Exception("timeout")  # noqa: TRY002 - "any failure", deliberately
        return [_Ans("127.0.1.2")]

    chk = _checker(monkeypatch, mixed)
    s = chk.summarize(chk.check_domain("spammy.com"))
    assert s["status"] == LISTED


def test_incomplete_coverage_is_partial_not_clean(monkeypatch):
    """Some lists answered 'not listed', others never answered. The silent ones
    could be exactly the ones listing us, so this is not an all-clear."""

    def half_down(name):
        if "surbl" in name:
            raise Exception("timeout")  # noqa: TRY002 - "any failure", deliberately
        return _nxdomain(name)

    chk = _checker(monkeypatch, half_down)
    s = chk.summarize(chk.check_domain("suppleco.com"))
    assert s["status"] == PARTIAL
    assert 0 < s["checked"] < s["total"]


# ── IP checks ───────────────────────────────────────────────────────────────


def test_ip_is_reversed_for_lookup(monkeypatch):
    seen = []

    def capture(name):
        seen.append(name)
        return _nxdomain(name)

    chk = _checker(monkeypatch, capture)
    chk.check_ip("192.0.2.5")
    assert any(n.startswith("5.2.0.192.") for n in seen), seen


def test_invalid_inputs_return_nothing(monkeypatch):
    chk = _checker(monkeypatch, _nxdomain)
    assert chk.check_ip("not-an-ip") == []
    assert chk.check_domain("") == []
    assert chk.check_domain("localhost") == []  # no dot -> skip


# ── caching ─────────────────────────────────────────────────────────────────


def test_results_are_cached(monkeypatch):
    calls = []

    def counting(name):
        calls.append(name)
        return _nxdomain(name)

    chk = BlacklistChecker(cache_ttl=300)

    class FakeResolver:
        timeout = lifetime = 1

        def resolve(self, name, rtype):
            return counting(name)

    monkeypatch.setattr(chk, "_resolver", lambda: FakeResolver())

    chk.check_domain("suppleco.com")
    first = len(calls)
    chk.check_domain("suppleco.com")  # served from cache
    assert len(calls) == first
