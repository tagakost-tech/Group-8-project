"""robots.txt must fail OPEN when it cannot be judged.

The whole compliance argument for honouring robots is that a DISALLOW is
respected. It does not follow that anything unreadable should be treated as a
disallow. Unknown is not forbidden - and reading it that way is expensive in
the direction nobody notices: a skipped host looks exactly like a host with no
addresses on it, so the prospect never appears and no one wonders why.

_robots_allows says this in its own docstring - "unknown must not read as
forbidden - otherwise one DNS blip silently retires a good prospect domain for
good" - and nothing tested it. A mutation flipping the ValueError branch from
True to False survived the entire suite.

The malformed-URL path matters because these URLs come from scraped search
results, not from us: an unterminated IPv6 bracket in a href is enough.
"""

import pytest

from modules.search import SearchModule


@pytest.fixture
def sm():
    """Built via __new__ so no client, DB or network is created. The lazy
    _robots_cache exists precisely so the guard holds on such an object."""
    return SearchModule.__new__(SearchModule)


@pytest.mark.parametrize("url", ["http://[oops", "http://[::1", "http://["])
def test_an_unparseable_url_is_allowed_not_forbidden(sm, url):
    """urlparse raises on a malformed IPv6 host. That tells us nothing about
    the site's wishes, so it must not be read as a refusal."""
    assert sm._robots_allows(url) is True, (
        f"{url!r} was treated as disallowed - unknown is not forbidden, and a "
        f"host skipped this way is indistinguishable from one with no leads"
    )


def test_an_unreachable_robots_file_is_allowed(sm):
    """A DNS failure or a timeout fetching robots.txt is the same class of
    ignorance. One blip must not retire the host for the rest of the run.

    The failure is injected at the HTTP client, not at _fetch_robots - that
    method owns the catch, and replacing it would test a scenario the code
    cannot reach. This is the shape a real outage has.
    """

    class _DeadClient:
        def get(self, url):
            raise OSError("name resolution failed")

    sm._client = _DeadClient()
    assert sm._robots_allows("https://brand.de/kontakt") is True


def test_a_robots_file_that_is_not_there_is_allowed(sm):
    """404 is the normal case for a small company site, and a 5xx is the
    server's problem. Neither is a prohibition."""

    class _NotFound:
        def get(self, url):
            return type("R", (), {"status_code": 404, "text": ""})()

    sm._client = _NotFound()
    assert sm._robots_allows("https://brand.de/kontakt") is True


def test_a_real_disallow_is_still_honoured(sm, monkeypatch):
    """Control. Failing open must not mean ignoring robots - the compliance
    obligation is the reason this code exists at all."""

    class _DenyAll:
        def can_fetch(self, agent, url):
            return False

    monkeypatch.setattr(sm, "_fetch_robots", lambda host: _DenyAll(), raising=False)
    assert sm._robots_allows("https://brand.de/kontakt") is False
