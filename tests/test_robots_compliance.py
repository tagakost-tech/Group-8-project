"""The scraper must honour robots.txt - and must fail OPEN when it cannot.

Two forces pull in opposite directions here.

Honouring it: since the EDPB's July 2026 scraping guidelines, and under the
EU AI Act / EDPB Opinion 28/2024, robots.txt carries GDPR and copyright
weight. COMPLIANCE.md rests the whole operation on legitimate interest, and
that balancing test is materially weaker if the agent takes data from sites
that asked it not to. Measured on 23 real target sites, 4 (all B2B
directories) disallow the paths the agent fetches - and yield zero addresses
anyway, so complying costs nothing.

Failing open: an unreachable robots.txt is UNKNOWN, not "forbidden". Reading
a DNS blip or a timeout as a prohibition would silently retire good domains -
the codebase's own recurring defect families, an empty result read as a
definite answer and a transient treated as permanent.
"""

import httpx
import pytest

from modules.search import SearchModule


@pytest.fixture
def sm(db):
    return SearchModule(db, rate_limit=0.0)


def _robots(monkeypatch, sm, body, status=200, raises=None):
    """Serve one robots.txt body to the module under test."""
    calls = []

    def fake_get(url, *a, **kw):
        calls.append(url)
        if raises is not None:
            raise raises
        return httpx.Response(
            status,
            text=body,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(sm._client, "get", fake_get)
    return calls


DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
ALLOW_ALL = "User-agent: *\nDisallow:\n"
DISALLOW_CONTACT = "User-agent: *\nDisallow: /contact\n"


def test_a_site_that_forbids_crawling_is_not_fetched(monkeypatch, sm):
    _robots(monkeypatch, sm, DISALLOW_ALL)
    assert sm._robots_allows("https://www.europages.co.uk/some/page") is False


def test_a_site_that_permits_crawling_is_fetched(monkeypatch, sm):
    _robots(monkeypatch, sm, ALLOW_ALL)
    assert sm._robots_allows("https://goerlich-pharma.com/kontakt") is True


def test_only_the_disallowed_path_is_blocked(monkeypatch, sm):
    """A rule against /contact must not cost us the rest of the site."""
    _robots(monkeypatch, sm, DISALLOW_CONTACT)
    assert sm._robots_allows("https://brand.de/contact") is False
    assert sm._robots_allows("https://brand.de/impressum") is True


# ── Failing open: unknown must never read as forbidden ────────────────────


def test_an_unreachable_robots_file_does_not_block_the_site(monkeypatch, sm):
    """A DNS failure or timeout means we do not know, and 'we do not know'
    must not silently retire a good prospect domain."""
    _robots(monkeypatch, sm, "", raises=httpx.ConnectTimeout("timed out"))
    assert sm._robots_allows("https://brand.de/kontakt") is True


def test_a_missing_robots_file_means_allowed(monkeypatch, sm):
    """404 is the normal case for a small company site: no robots.txt at all
    is permission, not prohibition.

    The body deliberately CONTAINS a disallow rule. A soft-404 serves an error
    page whose text is arbitrary, and the HTTP status is what decides - not
    whatever the body happens to say. Asserting against an inert body like
    "Not Found" proved nothing: it parses as an empty ruleset, which allows
    everything, so the test passed with the status check deleted.
    """
    _robots(monkeypatch, sm, DISALLOW_ALL, status=404)
    assert sm._robots_allows("https://brand.de/") is True


def test_a_server_error_does_not_block_the_site(monkeypatch, sm):
    """Same reasoning as the 404: a 5xx is the server failing, not answering."""
    _robots(monkeypatch, sm, DISALLOW_ALL, status=500)
    assert sm._robots_allows("https://brand.de/") is True


def test_an_unparseable_robots_file_means_allowed(monkeypatch, sm):
    _robots(monkeypatch, sm, "\x00\xff not robots at all")
    assert sm._robots_allows("https://brand.de/") is True


# ── It must be cheap: one fetch per host, not one per page ────────────────


def test_robots_is_fetched_once_per_host(monkeypatch, sm):
    """The agent hits several pages per site. Re-fetching robots.txt for each
    would double the request count and the politeness delay."""
    calls = _robots(monkeypatch, sm, ALLOW_ALL)
    for path in ("/", "/contact", "/impressum", "/kontakt"):
        sm._robots_allows(f"https://brand.de{path}")
    assert len(calls) == 1, f"expected 1 robots.txt fetch, got {len(calls)}"


def test_each_host_is_checked_separately(monkeypatch, sm):
    calls = _robots(monkeypatch, sm, ALLOW_ALL)
    sm._robots_allows("https://one.de/")
    sm._robots_allows("https://two.it/")
    assert len(calls) == 2


# ── The guard must actually be WIRED IN, not merely present ───────────────


def test_the_scraper_refuses_a_disallowed_page(monkeypatch, sm):
    """A helper nobody calls is not a guard. This drives the real scrape entry
    point and asserts the page is never fetched."""
    fetched = []

    def fake_get(url, *a, **kw):
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200, text=DISALLOW_ALL, request=httpx.Request("GET", url)
            )
        fetched.append(url)
        return httpx.Response(
            200,
            text="<html><body>info@brand.de</body></html>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(sm._client, "get", fake_get)

    assert sm._scrape_page("https://www.europages.co.uk/listing") == []
    assert fetched == [], f"fetched a disallowed page: {fetched}"


def test_the_scraper_still_fetches_an_allowed_page(monkeypatch, sm):
    fetched = []

    def fake_get(url, *a, **kw):
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200, text=ALLOW_ALL, request=httpx.Request("GET", url)
            )
        fetched.append(url)
        return httpx.Response(
            200,
            text="<html><head><title>Bio Supplements GmbH</title></head>"
            "<body>Kontakt: einkauf@brand.de</body></html>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(sm._client, "get", fake_get)

    contacts = sm._scrape_page("https://brand.de/kontakt")
    assert fetched == ["https://brand.de/kontakt"]
    assert any(c["email"] == "einkauf@brand.de" for c in contacts)
