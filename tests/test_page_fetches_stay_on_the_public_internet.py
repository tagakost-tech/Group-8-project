"""Page fetches reach only the public internet, one checked hop at a time.

The scraper fetches whatever search results, redirects and scraped links
name: outside input. Before, a result or a redirect could point it at this
machine (127.0.0.1, ::1), the office network (10.x, 192.168.x), a cloud
metadata service (169.254.169.254) or any other special address, and the
answer was parsed as a company's site (server-side request forgery). Both
clients followed redirects themselves, so only the first URL was ever seen
by our code, and a body was read whole, however big.

Now every hop is resolved and checked first (every address the name has,
in every form the resolver reads, IPv4 inside IPv6 included), only http(s)
is followed, at most five redirects, the connection goes to the address
that was checked, and reading stops at 2 MB. Every test uses a fake
resolver and a fake transport: nothing here touches the network.
"""

import ipaddress
import tracemalloc
import zlib

import httpx
import pytest

import modules.search as search_mod
from modules.search import (
    MAX_PAGE_BYTES,
    MAX_REDIRECTS,
    AddressNotPublic,
    SearchModule,
    _HttpxFetcher,
    _ImpersonatingFetcher,
    _public_target,
)

PUBLIC = "93.184.216.34"
DNS = {
    "brand.test": [PUBLIC],
    "other.test": ["151.101.1.69"],
    "intranet.test": ["10.0.0.5"],
    "router.test": ["192.168.1.1"],
    "me.test": ["127.0.0.1"],
    "me6.test": ["::1"],
    "metadata.test": ["169.254.169.254"],
    "cgnat.test": ["100.64.0.7"],
    "multicast.test": ["224.0.0.251"],
    "nowhere-addr.test": ["0.0.0.0"],
    "mapped.test": ["::ffff:127.0.0.1"],
    "nat64.test": ["64:ff9b::a00:5"],
    "sixtofour.test": ["2002:c0a8:101::1"],
    "ula.test": ["fd12:3456::1"],
    "linklocal6.test": ["fe80::1"],
    "reserved.test": ["240.0.0.1"],
    "mixed.test": [PUBLIC, "10.0.0.5"],
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    asked = []

    def resolve(host):
        asked.append(host)
        if host not in DNS:
            raise OSError(f"[Errno 11001] getaddrinfo failed for {host}")
        return [ipaddress.ip_address(a) for a in DNS[host]]

    monkeypatch.setattr(search_mod, "resolve_host", resolve)
    return asked


REFUSED_URLS = [
    "http://127.0.0.1/",
    "http://127.0.0.2:8080/admin",
    "http://[::1]/",
    "http://10.1.2.3/",
    "http://172.16.0.1/",
    "http://192.168.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/",
    "http://100.64.0.1/",
    "http://224.0.0.1/",
    "http://[::ffff:127.0.0.1]/",
    "http://[fe80::1]/",
    "http://[fd00::1]/",
    "http://2130706433/",
    "http://0x7f000001/",
    "http://127.1/",
    "http://017700000001/",
    "http://intranet.test/",
    "http://router.test/",
    "http://me.test/",
    "http://me6.test/",
    "http://metadata.test/",
    "http://cgnat.test/",
    "http://multicast.test/",
    "http://nowhere-addr.test/",
    "http://mapped.test/",
    "http://nat64.test/",
    "http://sixtofour.test/",
    "http://ula.test/",
    "http://linklocal6.test/",
    "http://reserved.test/",
    "http://mixed.test/",
    "file:///etc/passwd",
    "ftp://brand.test/",
    "gopher://brand.test:70/",
]


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_a_host_off_the_public_internet_is_refused(url):
    with pytest.raises(AddressNotPublic):
        _public_target(url)


def test_a_refusal_is_one_of_the_errors_the_scraper_already_handles():
    assert issubclass(AddressNotPublic, httpx.HTTPError)


@pytest.mark.parametrize(
    "url",
    ["https://brand.test/kontakt", "http://brand.test:8080/", "https://93.184.216.34/"],
)
def test_a_public_host_is_accepted(url):
    target = _public_target(url)
    assert str(target.ip) == PUBLIC


def test_a_name_that_does_not_resolve_is_a_transport_error():
    with pytest.raises(httpx.ConnectError):
        _public_target("https://gone.test/")


# ── The fetchers, on fake transports ────────────────────────────────────────


class FakeCurlSession:
    """Stands in for curl_cffi's Session.get(stream=True)."""

    def __init__(self, session, answers):
        self.session = session
        self.answers = answers
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(
            {"url": url, "kwargs": kwargs, "options": dict(self.session.curl_options)}
        )
        return self.answers(url)


class Streamed:
    def __init__(self, status_code, chunks=(), headers=None):
        self.status_code = status_code
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.chunks_read = 0
        self.closed = False

    def iter_content(self):
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk

    def close(self):
        self.closed = True


def _curl_fetcher(monkeypatch, answers):
    if search_mod.cffi_requests is None:
        pytest.skip("curl_cffi not installed")
    f = _ImpersonatingFetcher({"User-Agent": "x"}, timeout=5.0)
    fake = FakeCurlSession(f._session, answers)
    monkeypatch.setattr(f._session, "get", fake.get)
    return f, fake


def _curl_answer(status, body=b"", location=None, chunks=None):
    headers = {"location": location} if location else {}
    return Streamed(status, chunks if chunks is not None else [body], headers)


def _as_sent(response):
    """The answer as a network delivers it: streamed and still encoded.
    httpx reads (and decodes) a bytes or json body as soon as the Response
    is built, which no real connection does."""
    if not response.is_stream_consumed:
        return response
    raw = b"".join(response.stream)
    return httpx.Response(
        response.status_code, headers=response.headers, content=iter([raw])
    )


def _httpx_fetcher(answers):
    f = _HttpxFetcher({"User-Agent": "x"}, timeout=5.0)
    seen = []

    def handler(request):
        seen.append(request)
        return _as_sent(answers(request))

    f._client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    return f, seen


def _httpx_answer(status, body=b"", location=None, chunks=None):
    headers = {"location": location} if location else {}
    if chunks is not None:
        return httpx.Response(status, headers=headers, content=iter(chunks))
    return httpx.Response(status, headers=headers, content=body)


def _requested_names(kind, sent):
    if kind == "curl":
        return [httpx.URL(c["url"]).host for c in sent.calls]
    return [r.headers["host"].split(":")[0] for r in sent]


def _fetcher(kind, monkeypatch, route):
    """route(url_text) -> (status, body, location, chunks)."""
    if kind == "curl":

        def answer(url):
            status, body, location, chunks = route(url)
            return _curl_answer(status, body, location, chunks)

        return _curl_fetcher(monkeypatch, answer)

    def answer(request):
        url = f"{request.url.scheme}://{request.headers['host']}{request.url.raw_path.decode()}"
        status, body, location, chunks = route(url)
        return _httpx_answer(status, body, location, chunks)

    return _httpx_fetcher(answer)


KINDS = ["curl", "httpx"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "url", ["http://intranet.test/", "http://169.254.169.254/", "http://[::1]:8080/"]
)
def test_nothing_is_sent_to_a_refused_host(kind, url, monkeypatch):
    f, sent = _fetcher(kind, monkeypatch, lambda u: (200, b"secret", None, None))
    with pytest.raises(AddressNotPublic):
        f.get(url)
    assert (sent.calls if kind == "curl" else sent) == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "location",
    [
        "http://169.254.169.254/latest/meta-data/iam/",
        "http://intranet.test/admin",
        "http://127.0.0.1:2375/containers/json",
        "http://[::ffff:10.0.0.1]/",
        "file:///etc/passwd",
    ],
)
def test_a_redirect_to_a_refused_address_is_not_followed(kind, location, monkeypatch):
    f, sent = _fetcher(kind, monkeypatch, lambda u: (302, b"", location, None))
    with pytest.raises(AddressNotPublic):
        f.get("https://brand.test/")
    assert _requested_names(kind, sent) == ["brand.test"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_public_redirect_is_followed_hop_by_hop(kind, monkeypatch):
    def route(url):
        if url == "https://brand.test/":
            return 301, b"", "/kontakt", None
        if url == "https://brand.test/kontakt":
            return 302, b"", "https://other.test/impressum", None
        return 200, b"<p>einkauf@other.test</p>", None, None

    f, sent = _fetcher(kind, monkeypatch, route)
    page = f.get("https://brand.test/")
    assert page.status_code == 200
    assert page.content == b"<p>einkauf@other.test</p>"
    assert page.url == "https://other.test/impressum"
    assert _requested_names(kind, sent) == ["brand.test", "brand.test", "other.test"]


@pytest.mark.parametrize("kind", KINDS)
def test_redirects_stop_after_the_limit(kind, monkeypatch):
    f, sent = _fetcher(kind, monkeypatch, lambda u: (302, b"", "/again", None))
    with pytest.raises(httpx.TooManyRedirects):
        f.get("https://brand.test/")
    assert len(_requested_names(kind, sent)) == MAX_REDIRECTS + 1


@pytest.mark.parametrize("kind", KINDS)
def test_reading_stops_at_the_size_limit(kind, monkeypatch):
    chunk = b"x" * (256 * 1024)
    chunks = [chunk] * 40  # 10 MB on offer
    f, _sent = _fetcher(kind, monkeypatch, lambda u: (200, b"", None, chunks))
    page = f.get("https://brand.test/")
    assert len(page.content) == MAX_PAGE_BYTES
    assert page.content == b"x" * MAX_PAGE_BYTES


def test_curl_stops_reading_and_closes_the_stream_at_the_limit(monkeypatch):
    answer = _curl_answer(200, chunks=[b"x" * (256 * 1024)] * 40)
    f, _sent = _curl_fetcher(monkeypatch, lambda u: answer)
    page = f.get("https://brand.test/")
    assert len(page.content) == MAX_PAGE_BYTES
    assert answer.chunks_read == MAX_PAGE_BYTES // (256 * 1024) + 1
    assert answer.closed


def test_httpx_stops_reading_at_the_limit(monkeypatch):
    produced = []

    def body():
        for _ in range(40):
            produced.append(1)
            yield b"x" * (256 * 1024)

    f, _seen = _httpx_fetcher(lambda r: httpx.Response(200, content=body()))
    page = f.get("https://brand.test/")
    assert len(page.content) == MAX_PAGE_BYTES
    assert len(produced) < 40


@pytest.mark.parametrize("kind", KINDS)
def test_a_small_page_is_read_whole(kind, monkeypatch):
    f, _sent = _fetcher(kind, monkeypatch, lambda u: (200, b"<p>hi</p>", None, None))
    assert f.get("https://brand.test/").content == b"<p>hi</p>"


# ── The connection goes where the check looked ─────────────────────────────


def test_curl_is_pinned_to_the_checked_address(monkeypatch):
    f, fake = _curl_fetcher(monkeypatch, lambda u: _curl_answer(200, b"ok"))
    f.get("https://brand.test/kontakt#top")
    (call,) = fake.calls
    from curl_cffi import CurlOpt

    assert call["options"][CurlOpt.RESOLVE] == [f"brand.test:443:{PUBLIC}"]
    assert call["url"] == "https://brand.test/kontakt"
    assert call["kwargs"]["allow_redirects"] is False
    assert call["kwargs"]["stream"] is True


def test_curl_reads_the_host_httpx_read(monkeypatch):
    """A URL two parsers read differently must not slip past the pin: curl
    gets the URL as httpx wrote it, with the host httpx checked."""
    f, fake = _curl_fetcher(monkeypatch, lambda u: _curl_answer(200, b"ok"))
    f.get("https://intranet.test\\@brand.test/")
    (call,) = fake.calls
    assert httpx.URL(call["url"]).host == "brand.test"
    assert "\\" not in call["url"]


def test_an_ipv6_address_is_pinned_in_brackets(monkeypatch):
    monkeypatch.setitem(DNS, "v6.test", ["2606:4700::1111"])
    f, fake = _curl_fetcher(monkeypatch, lambda u: _curl_answer(200, b"ok"))
    f.get("http://v6.test:8080/")
    from curl_cffi import CurlOpt

    assert fake.calls[0]["options"][CurlOpt.RESOLVE] == [
        "v6.test:8080:[2606:4700::1111]"
    ]


def test_httpx_connects_to_the_checked_address_under_the_sites_name():
    f, seen = _httpx_fetcher(lambda r: httpx.Response(200, content=b"ok"))
    f.get("https://brand.test:8443/kontakt")
    (request,) = seen
    assert request.url.host == PUBLIC
    assert request.url.port == 8443
    assert request.headers["host"] == "brand.test:8443"
    assert request.extensions["sni_hostname"] == "brand.test"


def test_a_dns_answer_that_changes_after_the_check_moves_nothing(monkeypatch):
    """DNS rebinding: the name is public when checked, private a moment later.
    The connection goes to the address that was checked, whatever the
    resolver says next."""
    answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])
    monkeypatch.setattr(
        search_mod,
        "resolve_host",
        lambda host: [ipaddress.ip_address(a) for a in next(answers)],
    )
    f, seen = _httpx_fetcher(lambda r: httpx.Response(200, content=b"ok"))
    f.get("https://brand.test/")
    assert seen[0].url.host == PUBLIC


def test_neither_client_follows_redirects_or_a_proxy_itself():
    f = _HttpxFetcher({}, timeout=5.0)
    try:
        assert f._client.follow_redirects is False
        assert f._client._trust_env is False
    finally:
        f.close()
    if search_mod.cffi_requests is not None:
        c = _ImpersonatingFetcher({}, timeout=5.0)
        try:
            assert c._session.allow_redirects is False
            assert c._session.trust_env is False
        finally:
            c.close()


# ── The scraper uses the guard ──────────────────────────────────────────────


def test_the_scraper_never_reads_a_private_site(db, monkeypatch, fake_dns):
    sm = SearchModule(db, rate_limit=0.0)
    if sm._client.uses_impersonation:
        fake = FakeCurlSession(
            sm._client._session, lambda u: _curl_answer(200, b"x@intranet.test")
        )
        monkeypatch.setattr(sm._client._session, "get", fake.get)
        sent = fake.calls
    else:
        sent = []
        sm._client._client = httpx.Client(
            transport=httpx.MockTransport(
                lambda r: _as_sent(sent.append(r) or httpx.Response(200))
            ),
        )
    try:
        assert sm.scrape_site("http://intranet.test/") == []
        assert sent == []
    finally:
        sm.close()


# ── A host curl would read differently ─────────────────────────────────────


@pytest.mark.parametrize("url", ["http://x%2eevil.test/", "http://loc%61lhost.test/"])
def test_a_host_with_a_percent_escape_is_refused_before_it_is_resolved(
    url, monkeypatch, fake_dns
):
    """httpx keeps "%2e" in a host as it is; curl decodes it to ".". A
    zone that answers a public address for "x%2eevil.test" passed the check,
    curl then asked DNS for "x.evil.test" itself (the pin named the other
    spelling) and could be sent anywhere."""
    host = httpx.URL(url).host
    monkeypatch.setitem(DNS, host, [PUBLIC])
    with pytest.raises(AddressNotPublic):
        _public_target(url)
    assert fake_dns == []


@pytest.mark.parametrize("kind", KINDS)
def test_neither_client_is_asked_for_a_percent_escaped_host(kind, monkeypatch):
    monkeypatch.setitem(DNS, "x%2eevil.test", [PUBLIC])
    f, sent = _fetcher(kind, monkeypatch, lambda u: (200, b"secret", None, None))
    with pytest.raises(AddressNotPublic):
        f.get("http://x%2eevil.test/")
    assert (sent.calls if kind == "curl" else sent) == []


# ── A compressed body is inflated a little at a time ──────────────────────


def _gzip(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return c.compress(data) + c.flush()


def _zeros_gzipped(megabytes: int) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    block = b"\0" * (1024 * 1024)
    return b"".join(c.compress(block) for _ in range(megabytes)) + c.flush()


def test_a_compressed_bomb_never_expands_past_the_limit_in_memory():
    """The httpx fallback let httpx undo the encoding: it expands a whole
    network chunk in one call, so 979 bytes sent as "gzip, gzip" were 1 GB
    in memory before the 2 MB cap was looked at."""
    bomb = _gzip(_zeros_gzipped(256))
    assert len(bomb) < 4096
    f, _seen = _httpx_fetcher(
        lambda r: httpx.Response(
            200, headers={"Content-Encoding": "gzip, gzip"}, content=iter([bomb])
        )
    )
    tracemalloc.start()
    try:
        page = f.get("https://brand.test/")
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page.content == b"\0" * MAX_PAGE_BYTES
    assert peak < 32 * 1024 * 1024


HTML = b"<html><body><a href='mailto:info@brand.test'>info@brand.test</a></body></html>"


def _raw_deflate(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return c.compress(data) + c.flush()


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        ("", HTML),
        ("identity", HTML),
        ("gzip", _gzip(HTML)),
        ("x-gzip", _gzip(HTML)),
        ("deflate", zlib.compress(HTML)),
        ("deflate", _raw_deflate(HTML)),
        ("gzip, gzip", _gzip(_gzip(HTML))),
        ("GZIP", _gzip(HTML)),
    ],
    ids=[
        "none",
        "identity",
        "gzip",
        "x-gzip",
        "deflate",
        "raw-deflate",
        "gzip-twice",
        "GZIP",
    ],
)
def test_the_httpx_fallback_still_reads_a_compressed_page(encoding, body):
    f, seen = _httpx_fetcher(
        lambda r: httpx.Response(
            200, headers={"Content-Encoding": encoding}, content=body
        )
    )
    assert f.get("https://brand.test/").content == HTML
    assert seen[0].headers["accept-encoding"] == "gzip, deflate"


def test_a_big_compressed_page_is_cut_at_the_limit_whole_and_in_order():
    text = b"".join(b"line %07d\n" % i for i in range(400_000))
    f, _seen = _httpx_fetcher(
        lambda r: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=_gzip(text)
        )
    )
    assert f.get("https://brand.test/").content == text[:MAX_PAGE_BYTES]


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, br"])
def test_an_encoding_that_was_not_asked_for_is_refused(encoding):
    f, _seen = _httpx_fetcher(
        lambda r: httpx.Response(
            200, headers={"Content-Encoding": encoding}, content=b"??"
        )
    )
    with pytest.raises(httpx.DecodingError):
        f.get("https://brand.test/")


def test_a_corrupt_compressed_body_is_a_fetch_error():
    f, _seen = _httpx_fetcher(
        lambda r: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=b"not gzip"
        )
    )
    with pytest.raises(httpx.HTTPError):
        f.get("https://brand.test/")
