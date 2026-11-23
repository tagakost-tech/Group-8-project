"""Fetching with a browser's TLS fingerprint, without changing anything else.

Measured on 23 real target sites reached through the agent's own discovery
path: plain httpx fetched 19 (83%) and harvested 33 addresses; the same list
under curl_cffi's Chrome impersonation fetched 21 (91%) and harvested 37.
The site it unblocked, alsalab.it, is a genuine Italian supplement lab; the
two it could not are a department store and a visa service, which the
relevance gate discards anyway.

The risk is not the fetching, it is the EXCEPTIONS. curl_cffi raises its own
family - DNSError, ConnectionError, Timeout - and none of them are
httpx.HTTPError subclasses (they inherit OSError instead). Every handler in
search.py names httpx types, so an untranslated curl_cffi failure would sail
straight past them: out of _scrape_page, out of scrape_site, and into the
broad catch in _search_and_extract, abandoning the whole site including the
addresses already found on it.

So the adapter translates at the boundary and the module keeps ONE exception
vocabulary. These tests pin that translation, because nothing else would
notice it was missing until a DNS blip cost a site mid-crawl.
"""

import ipaddress

import httpx
import pytest

from modules.search import SearchModule, _build_fetcher


@pytest.fixture
def sm(db):
    return SearchModule(db, rate_limit=0.0)


# ── The adapter presents an httpx-shaped surface ──────────────────────────


@pytest.fixture
def fetcher():
    """A real fetcher, always closed afterwards."""
    f = _build_fetcher({"User-Agent": "x"}, timeout=5.0)
    yield f
    f.close()


@pytest.fixture
def impersonating(fetcher, monkeypatch):
    """The fetcher, skipping the test if this host has no curl_cffi wheel.

    Every host resolves to one public address (TEST-NET would be refused, so
    a real-looking one): each fetch is checked before it is sent, and a test
    may not ask real DNS."""
    if not getattr(fetcher, "uses_impersonation", False):
        pytest.skip("curl_cffi not installed; the httpx path needs no translation")
    monkeypatch.setattr(
        "modules.search.resolve_host",
        lambda host: [ipaddress.ip_address("93.184.216.34")],
    )
    return fetcher


class _Streamed:
    """A curl_cffi response as stream=True gives it: headers first, then the
    body chunk by chunk."""

    def __init__(self, status_code, content, headers=None):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.headers = headers or {}

    def iter_content(self):
        yield self.content

    def close(self):
        pass


def test_the_fetcher_exposes_the_interface_search_py_uses(fetcher):
    for attr in ("get", "close"):
        assert hasattr(fetcher, attr), attr


def test_a_transport_failure_is_raised_as_an_httpx_error(monkeypatch, impersonating):
    """The whole point of the adapter. curl_cffi's DNSError inherits OSError,
    not httpx.HTTPError, so without translation every httpx handler in
    search.py silently stops applying."""
    from curl_cffi.requests import exceptions as cffi_exc

    f = impersonating

    def boom(*a, **kw):
        raise cffi_exc.DNSError("could not resolve host")

    monkeypatch.setattr(f._session, "get", boom)
    with pytest.raises(httpx.HTTPError):
        f.get("https://nonexistent.invalid/")


def test_a_failing_status_raises_the_httpx_status_error(monkeypatch, impersonating):
    """_scrape_page calls raise_for_status() and catches
    httpx.HTTPStatusError to log the code. That must keep working."""
    f = impersonating

    monkeypatch.setattr(f._session, "get", lambda *a, **kw: _Streamed(403, b"forbidden"))
    resp = f.get("https://blocked.example/")
    assert resp.status_code == 403
    with pytest.raises(httpx.HTTPStatusError):
        resp.raise_for_status()


def test_the_body_is_returned_as_bytes_so_encoding_sniffing_still_works(monkeypatch, impersonating):
    """search.py hands BeautifulSoup response.content, never .text, so a
    latin-1 page declaring its charset only in <meta> is read correctly. The
    adapter must not quietly turn that into a decoded str."""
    f = impersonating

    raw = "<html><head><title>Grüne Näh</title></head><body>x</body></html>".encode(
        "latin-1"
    )

    monkeypatch.setattr(f._session, "get", lambda *a, **kw: _Streamed(200, raw))
    assert f.get("https://brand.de/").content == raw


# ── The crawl must survive a failure on one probe ─────────────────────────


def test_a_probe_failure_does_not_lose_the_whole_site(monkeypatch, sm):
    """scrape_site probes several contact paths in a loop. If one raises
    instead of returning empty, the site is abandoned - including any address
    already found. This is what the exception translation buys."""
    home = (
        "<html><head><title>Brand GmbH</title></head>"
        "<body>Kontakt: einkauf@brand.de</body></html>"
    )
    calls = {"n": 0}

    def fake_get(url, *a, **kw):
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow:\n",
                request=httpx.Request("GET", url),
            )
        calls["n"] += 1
        if calls["n"] > 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text=home, request=httpx.Request("GET", url))

    monkeypatch.setattr(sm._client, "get", fake_get)
    contacts = sm.scrape_site("https://brand.de/")
    assert [c["email"] for c in contacts] == ["einkauf@brand.de"]


# ── Degrading gracefully when the wheel is unavailable ────────────────────


def test_it_falls_back_to_httpx_when_curl_cffi_is_missing(monkeypatch):
    """curl_cffi ships compiled wheels. If one is unavailable for the host
    Python, the agent must still run - slightly less reach, not broken."""
    import modules.search as search_mod

    monkeypatch.setattr(search_mod, "cffi_requests", None)
    f = _build_fetcher({"User-Agent": "x"}, timeout=5.0)
    try:
        assert getattr(f, "uses_impersonation", False) is False
        assert hasattr(f, "get") and hasattr(f, "close")
    finally:
        f.close()


def test_impersonation_is_actually_used_when_the_wheel_is_present():
    """The guard against a silent downgrade.

    Every other impersonation test SKIPS when uses_impersonation is False, so
    quietly never building the impersonating fetcher turned three tests into
    skips and the suite stayed green - the reach improvement gone with nothing
    to show it. If curl_cffi imports, it must be used.
    """
    import modules.search as search_mod

    if search_mod.cffi_requests is None:
        pytest.skip("curl_cffi genuinely unavailable on this host")

    f = _build_fetcher({"User-Agent": "x"}, timeout=5.0)
    try:
        assert getattr(f, "uses_impersonation", False) is True, (
            "curl_cffi is installed but the scraper silently fell back to httpx"
        )
    finally:
        f.close()
