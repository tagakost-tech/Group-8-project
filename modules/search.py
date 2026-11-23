"""
Web search and contact extraction module.
Searches the web for pharmaceutical/healthcare leads,
scrapes pages for contact information, and validates emails.

Zero Hallucination Policy: Only returns emails actually found on scraped pages.
"""

import ipaddress
import logging
import re
import threading
import time
import zlib
from typing import ClassVar
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

try:
    # The package was renamed from `duckduckgo_search` to `ddgs`; prefer the
    # new name and fall back to the old one for backwards compatibility.
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

try:
    # Browser TLS fingerprinting. Plain HTTP clients have a TLS handshake that
    # anti-bot services recognise on sight; curl_cffi reproduces a real
    # browser's, which is enough to get past a Cloudflare-class block without
    # running a browser. Measured on 23 real target sites: 83% -> 91% fetched,
    # 33 -> 37 addresses. Optional because it ships compiled wheels - if one
    # is unavailable for this Python, we fall back to httpx and lose reach,
    # not function.
    from curl_cffi import CurlOpt
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - exercised by the fallback test
    cffi_requests = None

try:
    from email_validator import EmailNotValidError, validate_email
except ImportError:
    validate_email = None
    EmailNotValidError = Exception

from config import _numeric_ipv4, resolve_host
from modules.company_country import collected_country, page_country
from modules.database import EMAIL_RE, Database

logger = logging.getLogger(__name__)


# Common generic email prefixes to filter out
DEFAULT_BLOCKED_PREFIXES = [
    "noreply",
    "no-reply",
    "support",
    "admin",
    "webmaster",
    "abuse",
    "postmaster",
    "mailer-daemon",
]

# Consumer mailbox providers. An address here belongs to a natural person, and
# B2B legitimate interest does not cover it: Art. 130 Codice Privacy needs
# prior consent for marketing email to individuals. The list was ten US
# providers, so libero.it, gmx.de or orange.fr passed as "business".
# Exact domain match only - gmx-industrie.de is a company, not a mailbox.
PERSONAL_DOMAINS = {
    # International
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.de", "yahoo.fr", "yahoo.es", "yahoo.co.uk",
    "ymail.com", "rocketmail.com",
    "hotmail.com", "hotmail.it", "hotmail.de", "hotmail.fr", "hotmail.es",
    "hotmail.co.uk",
    "live.com", "live.it", "live.de", "live.fr", "live.nl", "live.co.uk",
    "outlook.com", "outlook.it", "outlook.de", "outlook.fr", "outlook.es",
    "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "mail.com",
    "protonmail.com", "protonmail.ch", "proton.me", "pm.me",
    "tutanota.com", "fastmail.com", "yandex.com", "yandex.ru", "zoho.com",
    # Italy
    "libero.it", "virgilio.it", "tiscali.it", "alice.it", "tin.it",
    "fastwebnet.it", "email.it", "inwind.it", "iol.it",
    # Germany / Austria / Switzerland
    "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com", "web.de",
    "t-online.de", "freenet.de", "posteo.de",
    # France
    "orange.fr", "wanadoo.fr", "free.fr", "sfr.fr", "laposte.net",
    # Benelux
    "ziggo.nl", "kpnmail.nl", "telenet.be", "skynet.be",
    # Central / Eastern Europe
    "seznam.cz", "centrum.cz", "wp.pl", "onet.pl", "o2.pl", "interia.pl",
    # UK
    "btinternet.com", "sky.com",
}  # fmt: skip

# Certified-mail (PEC) domains - never a marketing channel.
#
# PEC (Posta Elettronica Certificata) is Italy's legally registered certified
# mail, carrying the evidential weight of a registered letter. It exists for
# contracts, disputes, filings and dealings with the public administration.
# Cold outreach sent there is ineffective and conspicuously wrong to an Italian
# recipient - and SUPPLECO is itself Italian, so its prospects would read it as a
# company that does not know the conventions. Italy is also the single largest
# market in the target list, so these addresses are common on exactly the sites
# the agent scrapes.
#
# Matched on whole domain LABELS, never substrings: this codebase's most
# frequent defect is a substring standing in for a token, and "cert" inside
# "concert.it" or "pec" inside "specialita.it" would silently discard real
# prospects.
CERTIFIED_MAIL_LABELS = frozenset(
    {
        "pec",
        "arubapec",
        "legalmail",
        "postacert",
        "pecimprese",
        "sicurezzapostale",
        "cert",
        # Further providers found missing during the 2026-08-07 review. Each is
        # unambiguously a PEC provider's own domain - postecert is Poste
        # Italiane's, ingpec the engineers' register - so no supplement company
        # uses one as its corporate mail, and the whole-label match keeps the
        # over-blocking risk at zero for ordinary words.
        "postecert",
        "ingpec",
        "cnpec",
        "mypec",
        "peceasy",
    }
)

# File extension patterns to avoid scraping
SKIP_EXTENSIONS = {
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".zip",
    ".tar",
    ".gz",
    ".mp4",
    ".mp3",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
}

# A "domain" ending in one of these is really an asset filename, not a mail
# host (e.g. logo@2x.png, icon@3x.webp) - never a real address.
ASSET_DOMAIN_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".bmp",
    ".tiff",
    ".css",
    ".js",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
)


# What one page fetch may read. The pages this agent wants are company
# homepages and contact pages, well under a megabyte; the first 2 MB of a
# bigger one is kept and the rest is never downloaded.
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# What a host name may be written with. httpx keeps a percent-escape in a
# host as it is, curl decodes it: "a%2eevil.example" was checked as one name
# and connected to as another, which the pin, keyed by name, did not cover.
_PLAIN_HOST_NAME = re.compile(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?", re.IGNORECASE)
_INFLATE_STEP = 64 * 1024


class AddressNotPublic(httpx.TransportError):
    """A page fetch refused before anything was sent: the URL is not http(s),
    or its host is, or resolves to, an address that is not on the public
    internet."""


def _special_kind(ip) -> str:
    """What kind of non-public address ip is, or "" when it is public.

    An IPv6 address that carries an IPv4 one (mapped, 6to4, NAT64) is judged
    by the IPv4 address inside: ::ffff:127.0.0.1 reaches this machine.
    is_global alone misses multicast, which counts as global."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour:
            ip = ip.sixtofour
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.is_unspecified:
        return "an unspecified address"
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_private:
        return "a private address"
    if ip.is_reserved or getattr(ip, "is_site_local", False):
        return "a reserved address"
    if not ip.is_global:
        return "a special-purpose address"
    return ""


class _Target:
    """One hop of a fetch, checked: the URL to ask for, its host, and the
    public address the connection must go to."""

    __slots__ = ("host", "ip", "named", "port", "url")

    def __init__(self, url: httpx.URL, host: str, port: int, ip, named: bool):
        self.url = url
        self.host = host
        self.port = port
        self.ip = ip
        self.named = named


def _public_target(url: str) -> _Target:
    """The checked target for url, resolved once.

    Raises AddressNotPublic for a scheme other than http(s), a host that is
    or resolves to any address _special_kind names (every address it
    resolves to must be public, not only the first), and httpx.ConnectError
    for a name that does not resolve. Search results, redirects and scraped
    links are all outside input: without this, a page could send the agent
    to the router's admin page, a cloud metadata address or a service on
    this machine, and the answer would be parsed as a company's site."""
    parsed = httpx.URL(url).copy_with(fragment=None)
    if parsed.scheme not in ("http", "https"):
        raise AddressNotPublic(
            f"Refused a {parsed.scheme or 'scheme-less'} URL: only http and https "
            "are fetched"
        )
    host = parsed.raw_host.decode("ascii").strip("[]")
    if not host:
        raise AddressNotPublic("Refused a URL with no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses, named = public_addresses(host)
    return _Target(parsed, host, port, addresses[0], named)


def public_addresses(host: str) -> tuple[list, bool]:
    """Every address host has, each one checked to be on the public
    internet; and whether host is a name (resolved here) rather than an
    address. The connection must then go to one of these addresses, never
    to the name resolved again.

    Raises AddressNotPublic for a host that is, or resolves to, any address
    _special_kind names, or that is not a plain host name;
    httpx.ConnectError for a name that does not resolve."""
    try:
        addresses = [ipaddress.ip_address(host)]
        named = False
    except ValueError:
        numeric = _numeric_ipv4(host)
        if numeric is None:
            raise AddressNotPublic(f"Refused {host}: no address fits it") from None
        named = numeric is False
        if named and not _PLAIN_HOST_NAME.fullmatch(host):
            raise AddressNotPublic(f"Refused {host}: not a plain host name")
        if named:
            try:
                addresses = resolve_host(host)
            except (OSError, UnicodeError, ValueError) as e:
                raise httpx.ConnectError(f"Could not resolve {host}: {e}") from e
            if not addresses:
                raise httpx.ConnectError(f"Could not resolve {host}")
        else:
            addresses = [numeric]
    for address in addresses:
        kind = _special_kind(address)
        if kind:
            raise AddressNotPublic(
                f"Refused {host}: it resolves to {kind}, not the public internet"
            )
    return addresses, named


class _Page:
    """What a fetch returns: the last hop's status and at most
    MAX_PAGE_BYTES of its body, as BYTES, so BeautifulSoup can still sniff a
    <meta charset> the HTTP header omits."""

    __slots__ = ("content", "status_code", "url")

    def __init__(self, url: str, status_code: int, content: bytes):
        self.url = url
        self.status_code = status_code
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )
        return self


def _read_capped(chunks) -> tuple[bytes, bool]:
    """The body from an iterator of byte chunks, stopped once it passes
    MAX_PAGE_BYTES; and whether it was cut there."""
    body = bytearray()
    for chunk in chunks:
        body += chunk
        if len(body) > MAX_PAGE_BYTES:
            return bytes(body[:MAX_PAGE_BYTES]), True
    return bytes(body), False


def _inflate(chunks, wbits: int, raw_fallback: bool):
    """The chunks inflated, at most _INFLATE_STEP bytes at a time, so a
    small compressed body can never be expanded whole in one call."""
    d = zlib.decompressobj(wbits)
    started = False
    for chunk in chunks:
        data = chunk
        while True:
            try:
                out = d.decompress(data, _INFLATE_STEP)
            except zlib.error as e:
                if raw_fallback and not started:
                    started = True
                    d = zlib.decompressobj(-zlib.MAX_WBITS)
                    continue
                raise httpx.DecodingError(f"Could not inflate the body: {e}") from e
            started = True
            if out:
                yield out
            data = d.unconsumed_tail
            if d.eof or (not data and len(out) < _INFLATE_STEP):
                break
        if d.eof:
            return


def _decoded(chunks, content_encoding: str):
    """The raw body chunks with every Content-Encoding layer undone, each
    one a little at a time. httpx's own decoders expand a whole network
    chunk at once: 979 bytes sent as "gzip, gzip" became 1 GB in memory
    before the 2 MB cap was ever looked at."""
    for coding in reversed([c.strip().lower() for c in content_encoding.split(",")]):
        if coding in ("", "identity"):
            continue
        if coding in ("gzip", "x-gzip"):
            chunks = _inflate(chunks, 16 + zlib.MAX_WBITS, raw_fallback=False)
        elif coding == "deflate":
            chunks = _inflate(chunks, zlib.MAX_WBITS, raw_fallback=True)
        else:
            raise httpx.DecodingError(f"Refused a body in the {coding} encoding")
    return chunks


class _GuardedFetcher:
    """A .get(url) / .close() client whose every hop is checked first.

    Redirects are followed here, one hop at a time, never by the client
    library: each new address is checked like the first, a non-http(s)
    one is refused, and there are at most MAX_REDIRECTS of them. Each hop
    connects to the address that was checked, so a DNS answer that changes
    between the check and the connection cannot move it. The body is
    streamed and reading stops at MAX_PAGE_BYTES. Every failure is an
    httpx.HTTPError, the one exception vocabulary this module catches.
    """

    uses_impersonation = False

    def get(self, url: str) -> _Page:
        for _hop in range(MAX_REDIRECTS + 1):
            target = _public_target(url)
            status, location, body, cut = self._hop(target)
            if status in _REDIRECT_CODES and location:
                url = str(target.url.join(location))
                continue
            if cut:
                logger.info(
                    "Read only the first %d bytes of %s", MAX_PAGE_BYTES, target.url
                )
            return _Page(str(target.url), status, body)
        raise httpx.TooManyRedirects(f"More than {MAX_REDIRECTS} redirects from {url}")

    def _hop(self, target: _Target) -> tuple[int, str | None, bytes, bool]:
        raise NotImplementedError


class _Deadline(threading.local):
    """When the fetch this thread is making must be over (time.monotonic),
    or None outside a fetch."""

    at: float | None = None

    def left(self, timeout: float | None, expired: type[httpx.TimeoutException]):
        """The time one network wait may take: timeout, cut to what is left
        of the fetch. Raises expired once nothing is left."""
        if self.at is None:
            return timeout
        left = self.at - time.monotonic()
        if left <= 0:
            raise expired("The page fetch ran past its total time limit")
        return left if timeout is None else min(timeout, left)


class _DeadlineStream:
    """A connection whose every read and write waits no longer than what is
    left of the fetch. httpx's own timeout counts per read, so a server
    sending one byte every few seconds never trips it."""

    def __init__(self, stream, deadline: _Deadline):
        self._stream = stream
        self._deadline = deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(
            max_bytes, self._deadline.left(timeout, httpx.ReadTimeout)
        )

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, self._deadline.left(timeout, httpx.WriteTimeout))

    def close(self) -> None:
        self._stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return _DeadlineStream(
            self._stream.start_tls(
                ssl_context,
                server_hostname,
                self._deadline.left(timeout, httpx.ConnectTimeout),
            ),
            self._deadline,
        )

    def get_extra_info(self, info: str):
        return self._stream.get_extra_info(info)


class _DeadlineBackend:
    """httpx's network backend, its connections wrapped in _DeadlineStream."""

    def __init__(self, backend, deadline: _Deadline):
        self._backend = backend
        self._deadline = deadline

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        return _DeadlineStream(
            self._backend.connect_tcp(
                host,
                port,
                timeout=self._deadline.left(timeout, httpx.ConnectTimeout),
                local_address=local_address,
                socket_options=socket_options,
            ),
            self._deadline,
        )

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpx.ConnectError("Page fetches never use a local socket")

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _HttpxFetcher(_GuardedFetcher):
    """The guarded fetcher on plain httpx. The request goes to the checked
    IP address; the Host header and the TLS server name (which the
    certificate is checked against) stay the site's own name. A whole
    fetch, redirects included, takes at most `timeout` seconds, however
    slowly the server sends."""

    def __init__(self, headers: dict, timeout: float):
        self._timeout = timeout
        self._deadline = _Deadline()
        # No connection is kept for the next request. Requests go to the IP
        # address, so the pool cannot tell two sites on one address apart: a
        # kept connection, whose certificate was checked for the first
        # site's name only, would carry the second site's request.
        transport = httpx.HTTPTransport(
            trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)
        )
        # httpx has no setting for a total time limit; its connection pool
        # takes the network backend that every connection is opened with.
        pool = transport._pool
        pool._network_backend = _DeadlineBackend(pool._network_backend, self._deadline)
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers=headers,
            transport=transport,
        )

    def get(self, url: str) -> _Page:
        self._deadline.at = time.monotonic() + self._timeout
        try:
            return super().get(url)
        finally:
            self._deadline.at = None

    def _hop(self, target: _Target) -> tuple[int, str | None, bytes, bool]:
        netloc = target.url.netloc.decode("ascii")
        with self._client.stream(
            "GET",
            target.url.copy_with(host=str(target.ip)),
            headers={"Host": netloc, "Accept-Encoding": "gzip, deflate"},
            extensions={"sni_hostname": target.host},
        ) as response:
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_CODES and location:
                return response.status_code, location, b"", False
            body, cut = _read_capped(
                _decoded(
                    response.iter_raw(), response.headers.get("content-encoding", "")
                )
            )
            return response.status_code, location, body, cut

    def close(self):
        self._client.close()


class _ImpersonatingFetcher(_GuardedFetcher):
    """The guarded fetcher on curl_cffi's browser TLS impersonation.

    Exists so that the swap is invisible to the rest of the module: same
    .get(url) / .close() surface, same httpx exception vocabulary. That
    matters more than it sounds - curl_cffi's failures (DNSError,
    ConnectionError, Timeout) inherit OSError, NOT httpx.HTTPError, so
    without translation every `except httpx.*` in this file would quietly
    stop applying and a DNS blip mid-crawl would abandon a whole site.

    curl is handed the URL as httpx parsed and wrote it, so both read the
    same host, and a RESOLVE entry pins that host to the checked address.
    The pin is set on the shared session and read when a request starts, so
    a lock holds it from the one to the other: another thread's pin cannot
    take its place in between.
    """

    uses_impersonation = True

    def __init__(self, headers: dict, timeout: float, impersonate: str = "chrome"):
        self._impersonate = impersonate
        self._timeout = timeout
        self._session = cffi_requests.Session(
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
            trust_env=False,
        )
        self._pin_lock = threading.Lock()

    def _hop(self, target: _Target) -> tuple[int, str | None, bytes, bool]:
        with self._pin_lock:
            if target.named:
                ip = target.ip
                address = f"[{ip}]" if ip.version == 6 else str(ip)
                self._session.curl_options[CurlOpt.RESOLVE] = [
                    f"{target.host}:{target.port}:{address}"
                ]
            else:
                self._session.curl_options.pop(CurlOpt.RESOLVE, None)
            try:
                response = self._session.get(
                    str(target.url),
                    impersonate=self._impersonate,
                    allow_redirects=False,
                    stream=True,
                )
            except Exception as e:
                # Re-raised as the httpx equivalent so this module keeps one
                # exception vocabulary. Never silently returns None: a failure
                # must still look like a failure to the caller.
                raise httpx.ConnectError(f"{type(e).__name__}: {e}") from e
        try:
            location = response.headers.get("location")
            if response.status_code in _REDIRECT_CODES and location:
                return response.status_code, location, b"", False
            body, cut = _read_capped(response.iter_content())
            return response.status_code, location, body, cut
        except Exception as e:
            raise httpx.ReadError(f"{type(e).__name__}: {e}") from e
        finally:
            try:
                response.close()
            except Exception as e:  # noqa: BLE001 - closing must never raise
                logger.debug("Error closing a streamed response: %s", e)

    def close(self):
        try:
            self._session.close()
        except Exception as e:  # noqa: BLE001 - closing must never raise
            logger.debug("Error closing the impersonating session: %s", e)


def _build_fetcher(headers: dict, timeout: float):
    """The best available guarded client: browser-impersonating if we can."""
    if cffi_requests is not None:
        try:
            return _ImpersonatingFetcher(headers, timeout)
        except Exception as e:  # noqa: BLE001 - degrade, never fail to start
            logger.warning(
                "Could not start the browser-impersonating fetcher (%s); "
                "falling back to httpx with reduced reach.",
                e,
            )
    return _HttpxFetcher(headers, timeout)


class SearchModule:
    """
    Autonomous web search and contact extraction engine.
    Searches Google for leads, scrapes pages, extracts and validates emails.
    """

    def __init__(
        self,
        db: Database,
        rate_limit: float = 2.0,
        max_results: int = 20,
        blocked_prefixes: list[str] | None = None,
    ):
        self.db = db
        self.rate_limit = rate_limit
        self.max_results = max_results
        self.blocked_prefixes = blocked_prefixes or DEFAULT_BLOCKED_PREFIXES
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        # Reuse a single HTTP client for connection pooling (much faster).
        # Browser-impersonating where available - see _build_fetcher.
        self._client = _build_fetcher(self.headers, timeout=15.0)
        # host -> RobotFileParser, or None meaning "asked, and we may proceed".
        # Cached because the agent fetches several pages per site and robots.txt
        # would otherwise be re-fetched for each one.
        self._robots_cache: dict[str, RobotFileParser | None] = {}

    def close(self):
        """Close the shared HTTP client."""
        if self._client:
            self._client.close()

    # ─────────────────────────────────────────────────────────────────
    # robots.txt
    # ─────────────────────────────────────────────────────────────────

    def _robots_allows(self, url: str) -> bool:
        """True if this URL may be fetched under the host's robots.txt.

        Honoured because since the EDPB's July 2026 scraping guidelines, and
        under the EU AI Act / EDPB Opinion 28/2024, robots.txt carries GDPR and
        copyright weight. This operation rests on legitimate interest, and that
        balancing test is materially weaker if the agent takes data from sites
        that asked it not to. It costs nothing measurable: of 23 real target
        sites, the 4 that disallow are B2B directories that publish no
        addresses anyway.

        FAILS OPEN. An unreachable, missing or unparseable robots.txt means
        UNKNOWN, and unknown must not read as forbidden - otherwise one DNS
        blip silently retires a good prospect domain for good.
        """
        try:
            parsed = urlparse(url)
            host = f"{parsed.scheme}://{parsed.netloc}"
        except ValueError:
            return True

        # Lazily, so the guard holds however the object was built. Several
        # tests construct SearchModule via __new__ to isolate one method, and a
        # guard that raises AttributeError on a partially-built object is a
        # guard that fails CLOSED in exactly the place it must not.
        cache = getattr(self, "_robots_cache", None)
        if cache is None:
            cache = self._robots_cache = {}

        if host not in cache:
            cache[host] = self._fetch_robots(host)

        rp = cache[host]
        if rp is None:
            return True
        try:
            # getattr, for the same reason _robots_cache is lazy above: on an
            # object built without __init__, self.headers raised AttributeError,
            # which the handler below swallowed as "allowed" - so a genuine
            # Disallow was ignored. Failing open is right for what we cannot
            # determine, and wrong for a rule we were told and simply failed to
            # read. One sibling was made lazy and the other was not.
            agent = getattr(self, "headers", {}).get("User-Agent", "*")
            return rp.can_fetch(agent, url)
        except Exception:  # noqa: BLE001 - unknown must read as allowed
            # A parser that chokes on its own rules tells us nothing.
            return True

    def _fetch_robots(self, host: str) -> "RobotFileParser | None":
        """Fetch and parse one host's robots.txt. None = proceed (see above)."""
        # ONE try around the whole thing, deliberately. The contract is that
        # anything we cannot determine means "allowed", so every failure mode -
        # transport, DNS, TLS, a redirect loop, an unparseable body, or a
        # response object that simply is not shaped the way we expect - has to
        # land in the same place. Splitting this up left the status_code read
        # outside the guard, where an unexpected response shape raised instead
        # of failing open.
        try:
            resp = self._client.get(f"{host}/robots.txt")
            if getattr(resp, "status_code", 200) != 200:
                # 404 is the normal case for a small company site; a 5xx is the
                # server's problem. Neither is a prohibition.
                return None
            rp = RobotFileParser()
            rp.parse(resp.text.splitlines())
            return rp
        except Exception as e:  # noqa: BLE001 - every failure means "allowed"
            logger.debug("Could not read robots.txt for %s (%s) - proceeding", host, e)
            return None

    # ─────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────

    def search_and_extract(
        self, query: str, search_id: int | None = None
    ) -> list[dict]:
        """
        Full pipeline: search Google -> scrape results -> extract contacts.
        Returns list of contact dicts with keys:
            email, name, role, organization, source_url, qualification_rationale

        `search_id` reuses an already-queued search_log row instead of minting
        a second one. Without it every discovery query produced TWO rows - the
        queued one stuck at 'in_progress' forever (an amber, apparently-hung
        line on the Search Logs tab) while only the duplicate completed, and
        the learning loop's per-query denominator was doubled.
        """
        if search_id is None:
            search_id = self.db.log_search(query, source="google", status="in_progress")
        self.db.log_activity("SEARCH_START", f"Query: {query}")
        try:
            return self._search_and_extract(query, search_id)
        except Exception:
            # A row opened as 'in_progress' must ALWAYS be closed, whatever
            # goes wrong. get_pending_searches selects only 'pending', so a row
            # left open is invisible to the loop forever - not retried, not
            # completed, just an amber line on the dashboard. That is how a
            # backlog of stranded searches accumulates, and every one of them
            # is a query the operator asked for that will never run.
            #
            # Marked 'no_results' rather than 'completed': nothing was found,
            # and the learning loop reads results_count, so claiming success
            # here would teach it that a query which crashed was unproductive
            # ON ITS MERITS and bias discovery away from a perfectly good term.
            try:
                self.db.update_search(
                    search_id,
                    status="no_results",
                    notes="Search failed with an unexpected error - see the log",
                )
            except Exception:
                logger.exception("Could not close search row %s", search_id)
            logger.exception("Search failed for %r", query)
            raise

    def _search_and_extract(self, query: str, search_id: int) -> list[dict]:
        """The body of search_and_extract; see there for the contract."""

        # Enforce European search bias
        search_query = query
        if "europe" not in query.lower() and "eu" not in query.lower().split():
            search_query += " Europe"

        urls = self._ddg_search(search_query)
        if not urls:
            self.db.update_search(
                search_id,
                status="no_results",
                notes="No URLs returned from the search engine",
            )
            self.db.log_activity(
                "SEARCH_COMPLETE", f"No results for: {query}", status="warning"
            )
            return []

        logger.info(f"Found {len(urls)} URLs for query: {query}")

        # Step 2: Scrape each URL for contacts
        all_contacts = []
        urls_scraped = 0

        for url in urls:
            if self._should_skip_url(url):
                continue

            time.sleep(self.rate_limit)
            try:
                contacts = self.scrape_site(url)
            except Exception as e:  # noqa: BLE001 - see below
                # Deliberately broad, and the only broad catch in this module.
                # `url` is third-party scraped input: it comes from whichever
                # backend ddgs happened to route to, so its shape is not ours
                # to predict. Startpage in particular returns RELATIVE redirect
                # paths ("/clev?event=StartpageResultClick&..."), which raise a
                # bare ValueError that none of _scrape_page's httpx handlers
                # cover.
                #
                # Before this, one such URL aborted the whole query - and
                # because the exception escaped search_and_extract, the
                # search_log row was left at 'in_progress' forever, where
                # get_pending_searches (which selects only 'pending') can never
                # see it again. Six of forty-seven queries stranded that way in
                # a single run, which is how the backlog of stranded searches
                # accumulated in the first place.
                logger.warning("Skipping %s - could not scrape it: %s", url, e)
                urls_scraped += 1
                continue
            if contacts:
                all_contacts.extend(contacts)
            urls_scraped += 1  # Count attempted pages either way

        # Step 3: Deduplicate by email
        seen_emails = set()
        unique_contacts = []
        for contact in all_contacts:
            if contact["email"] not in seen_emails:
                seen_emails.add(contact["email"])
                unique_contacts.append(contact)

        # Update search log
        self.db.update_search(
            search_id,
            results_count=len(unique_contacts),
            urls_scraped=urls_scraped,
            status="completed",
            notes=f"Found {len(unique_contacts)} unique contacts from {urls_scraped} pages",
        )
        self.db.log_activity(
            "SEARCH_COMPLETE",
            f"Query: {query} | {len(unique_contacts)} contacts from {urls_scraped} pages",
            status="success",
        )

        # Step 4: Store contacts in database
        stored_contacts = []
        for contact in unique_contacts:
            # Read now, while the page it was found on is known: an Italian
            # company writing from a .com address gets the Italian email.
            country, source = collected_country(
                contact.get("source_url"),
                contact["source_channel"],
                contact.get("page_country"),
            )
            contact_id = self.db.add_contact(
                search_id=search_id,
                email=contact["email"],
                name=contact.get("name"),
                role=contact.get("role"),
                organization=contact.get("organization"),
                source_url=contact.get("source_url"),
                qualification_rationale=contact.get("qualification_rationale"),
                source_channel=contact["source_channel"],
                company_country=country,
                company_country_source=source,
            )
            if contact_id:
                contact["id"] = contact_id
                contact["search_id"] = search_id
                stored_contacts.append(contact)
                self.db.log_activity(
                    "CONTACT_FOUND",
                    f"{contact['email']} from {contact.get('organization', 'Unknown')}",
                    status="success",
                )

        return stored_contacts

    # ─────────────────────────────────────────────────────────────────
    # DuckDuckGo Search
    # ─────────────────────────────────────────────────────────────────

    def _ddg_search(self, query: str) -> list[str]:
        """Search DuckDuckGo and return a list of result URLs.

        Retries with slightly modified queries because DuckDuckGo
        occasionally returns an empty result set or transiently fails.
        """
        if DDGS is None:
            logger.error("ddgs not installed")
            return []

        # Build a small set of query variants to retry with. Order matters:
        # the original query first, then lightly modified fallbacks.
        variants = [query]
        stripped = query.replace('"', "").strip()
        if stripped and stripped != query:
            variants.append(stripped)
        variants.append(f"{query} contact")

        tried = set()
        for attempt, variant in enumerate(variants, 1):
            if variant in tried:
                continue
            tried.add(variant)
            try:
                results = DDGS().text(variant, max_results=self.max_results)
                # Result key was 'href' historically; newer ddgs may use 'url'.
                urls = [u for r in results if (u := r.get("href") or r.get("url"))]
                if urls:
                    if attempt > 1:
                        logger.info(
                            f"DDG search recovered on attempt {attempt} "
                            f"with variant: '{variant}'"
                        )
                    return urls
                logger.warning(
                    f"DDG search returned no results (attempt {attempt}) "
                    f"for variant: '{variant}'"
                )
            except Exception as e:
                # Deliberately broad. DDGS is an unstable third-party scraper:
                # its exception types differ between the `ddgs` and legacy
                # `duckduckgo_search` packages and change between releases, so
                # there is no set to name here. Whatever it raises, the right
                # move is the next query variant - but it is logged with a
                # traceback so a defect on our side is not read as "DDG is down".
                logger.exception(
                    f"DDG search failed (attempt {attempt}) for '{variant}'"
                )
                self.db.log_activity(
                    "SEARCH_ERROR", f"Search failed: {e}", status="error"
                )
            # Brief pause before the next variant to avoid hammering the engine.
            time.sleep(1.0)

        return []

    # ─────────────────────────────────────────────────────────────────
    # Page Scraping
    # ─────────────────────────────────────────────────────────────────

    # Contact-page paths, ordered so the likeliest answer for a given market
    # comes first. Ordering is a hint only, never a filter: naturacare.com
    # answered on /mentions-legales despite a .com domain.
    CONTACT_PATHS_BY_TLD: ClassVar[dict[str, list[str]]] = {
        "de": ["/impressum", "/kontakt", "/contact"],
        "at": ["/impressum", "/kontakt", "/contact"],
        "ch": ["/impressum", "/kontakt", "/contact"],
        "it": ["/contatti", "/contact", "/chi-siamo"],
        "fr": ["/contact", "/mentions-legales", "/nous-contacter"],
        "es": ["/contacto", "/contact", "/aviso-legal"],
        "nl": ["/contact", "/over-ons"],
    }
    CONTACT_PATHS_DEFAULT: ClassVar[list[str]] = [
        "/contact",
        "/contact-us",
        "/kontakt",
        "/contatti",
        "/impressum",
        "/contacto",
        "/mentions-legales",
    ]
    # Mailboxes that reach a buyer, best first. Used only to CHOOSE between
    # addresses already found at one company - never to reject one.
    BUYING_PREFIXES = (
        "einkauf",
        "purchasing",
        "procurement",
        "acquisti",
        "commerciale",
        "sales",
        "vendite",
        "ordini",
        "orders",
        "export",
        "info",
    )

    def scrape_site(self, url: str) -> list[dict]:
        """Scrape a page and, if it published no address, its contact page.

        Measured on 23 real target sites: homepages alone gave 16 addresses,
        following to the contact page gave 27 (+69%). Two companies that
        published nothing at all on the homepage gave up 7 and 1.

        Three guards make that safe; each is measured, and each has a test:

        1. Context and organisation stay with the FIRST page. The relevance
           gate and the AI classifier judge a company from its page text, and
           contact pages carry legal boilerplate, "About us", or base64
           page-builder junk - all three of which the vertical gate rejects.
           Harvesting an address and then binning the lead is worse than not
           harvesting it. The contact page contributes addresses ONLY.
        2. The address must belong to the company. German Impressum pages
           credit the agency that built the site: the only address this hop
           gained on vitalproducts.de was a web design agency's.
        3. One address per company. Sites publish up to seven; each would
           become its own contact, and the domain cap still permits three
           sends a day to one company, which reads as spam.
        """
        parsed = urlparse(url)
        if not parsed.netloc:
            return []

        contacts = self._scrape_page(url)
        if contacts:
            # Guard 2 applies HERE TOO, not only on the hop below. The first
            # page fetched is often a thin contact/Impressum page - the DDG
            # retry appends "contact" to the query - so an agency credit in the
            # footer arrives on the very first fetch, which used to skip this
            # check entirely and return the agency with the organisation set to
            # "Impressum".
            own = [
                c
                for c in contacts
                if self._is_company_address(c["email"], parsed.netloc)
            ]
            if own:
                # Already answered - do not spend four more requests on
                # politeness grounds alone.
                own_page = any(
                    frag in (parsed.path or "").lower() for frag in self._OWN_PAGE_PATHS
                )
                return self._stamp(
                    self._one_per_company(own),
                    "contact_page" if own_page else "own_website",
                )
            if not self._page_is_the_companys_own(url, parsed):
                # A DIRECTORY, exhibitor listing or trade-press page. Guard 2
                # assumes the host IS the company, so "a different mail domain
                # means a web agency" only holds there. On europages, a listing
                # for Nutrivita Srl publishes acquisti@nutrivita.it - the whole
                # point of the page - and the guard threw it away, then spent
                # seven more requests probing /contatti at the DIRECTORY's root
                # where the prospect's address could never be.
                return self._stamp(self._one_per_company(contacts), "third_party")
            logger.info(
                "Discarding %d third-party address(es) on %s - none belong to %s",
                len(contacts),
                url,
                parsed.netloc,
            )
            # Only third-party addresses here. Fall through to the contact
            # pages: the company's own address may still be one hop away.

        host = f"{parsed.scheme}://{parsed.netloc}"
        tld = parsed.netloc.rsplit(".", 1)[-1].lower()
        paths = self.CONTACT_PATHS_BY_TLD.get(tld, self.CONTACT_PATHS_DEFAULT)

        # Keep whatever the first page told us about WHO this is, even though
        # it gave us no usable address (guard 1). Served from what _scrape_page
        # already parsed, so this costs no second request.
        identity = self._page_identity(url)

        for path in paths:
            candidate = urljoin(host, path)
            if candidate.rstrip("/") == url.rstrip("/"):
                continue
            time.sleep(self.rate_limit)
            found = self._scrape_page(candidate)
            if not found:
                continue
            for c in found:
                if identity.get("organization"):
                    c["organization"] = identity["organization"]
                if identity.get("qualification_rationale"):
                    c["qualification_rationale"] = identity["qualification_rationale"]
                # The VAT number is printed in the home page's footer, often
                # not on the contact page that holds the address.
                if not c.get("page_country") and identity.get("page_country"):
                    c["page_country"] = identity["page_country"]
                elif not identity:
                    # The landing page could not be read at all - a 503, a
                    # timeout - so there is no identity to carry over, and what
                    # is here now is the CONTACT page's own description:
                    # "Hier finden Sie das Impressum". Passed downstream, the
                    # vertical gate reads that as the company's business and
                    # rejects a real supplement manufacturer for "no SUPPLECO
                    # vertical signal" - a transient failure deciding a lead,
                    # with a rejection reason that reads like the filter
                    # working. Better to say nothing than to say the wrong
                    # thing: an empty rationale leaves the gate to judge on the
                    # organisation alone.
                    c["qualification_rationale"] = None
                c["source_url"] = candidate
            kept = [
                c for c in found if self._is_company_address(c["email"], parsed.netloc)
            ]
            if kept:
                return self._stamp(self._one_per_company(kept), "contact_page")
            # Addresses existed but none were the company's own (an agency
            # credit). Stop: another page will show the same footer.
            return []
        return []

    @staticmethod
    def _page_country(soup, organization: str | None) -> str | None:
        """What the page says about the country of the company that
        published it (company_country.page_country): its text, its
        language and its organisation name."""
        lang = soup.html.get("lang") if soup.html else None
        return page_country(soup.get_text(" ", strip=True), lang, organization)

    @staticmethod
    def _stamp(contacts: list[dict], channel: str) -> list[dict]:
        """Record where each address was found (decision L1.4)."""
        for c in contacts:
            c["source_channel"] = channel
        return contacts

    def _page_identity(self, url: str) -> dict:
        """Organisation and description for a page, WITHOUT a second request.

        _scrape_page computes these while it has the parsed page in hand, and
        stashes them under _last_identity - including when it found no address,
        which is precisely when the hop needs them. Re-fetching here instead
        cost one redundant download of a page just read, on every site that
        needs the hop, with no politeness delay between the two; and if that
        second request happened to fail, the contact silently kept the CONTACT
        page's title ("Impressum") as its organisation.

        Falls back to fetching only if the stash holds a different page, so the
        method is still correct if called on its own.
        """
        stashed = getattr(self, "_last_identity", None)
        if stashed and stashed.get("url") == url:
            return {
                "organization": stashed.get("organization"),
                "qualification_rationale": stashed.get("qualification_rationale"),
                "page_country": stashed.get("page_country"),
            }

        if not self._robots_allows(url):
            return {}
        try:
            resp = self._client.get(url)
            # A 503's error page is not an identity. Without this the fallback
            # derives an organisation from the URL and a description from
            # whatever the error body says, which reads as real to every caller
            # downstream - and the vertical gate then judges the company on it.
            resp.raise_for_status()
            html = resp.content
        except Exception as e:  # noqa: BLE001 - identity is best-effort
            logger.debug("Could not read identity from %s (%s)", url, e)
            return {}
        try:
            soup = BeautifulSoup(html, "html.parser")
            organization = self._extract_org_name(soup, url)
            return {
                "organization": organization,
                "qualification_rationale": self._extract_page_context(soup),
                "page_country": self._page_country(soup, organization),
            }
        except Exception as e:  # noqa: BLE001 - identity is best-effort
            logger.debug("Could not parse identity from %s (%s)", url, e)
            return {}

    # Second-level labels that are part of the public suffix rather than the
    # company's name. Without these, every .co.uk company shares the label
    # "co.uk" and would look like every other.
    _PUBLIC_SLDS: ClassVar[frozenset[str]] = frozenset(
        {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc"}
    )

    # Path fragments that mark a page as the site owner's own contact or legal
    # page, in the languages this agent meets. An Impressum belongs to whoever
    # runs the site, so an address on it that is NOT theirs is the agency
    # credit guard 2 exists to drop.
    _OWN_PAGE_PATHS: ClassVar[tuple[str, ...]] = (
        "contact",
        "kontakt",
        "contatti",
        "contacto",
        "impressum",
        "imprint",
        "legal",
        "mentions-legales",
        "about",
        "chi-siamo",
        "quienes-somos",
        "ueber-uns",
        "uber-uns",
        "a-propos",
    )

    def _page_is_the_companys_own(self, url: str, parsed) -> bool:
        """Is this page published BY the company whose address we want?

        Guard 2 - "a different mail domain means a web agency credit" - only
        holds when the host is the company. On a directory, an exhibitor
        listing or a trade-press article, a different mail domain is the
        normal case and the whole reason the page is worth reading.

        Defaults to TRUE when it cannot tell, so the stricter behaviour stays
        the default and a page this cannot classify is treated as before.
        """
        path = (parsed.path or "").lower()
        if any(frag in path for frag in self._OWN_PAGE_PATHS):
            return True

        # The title the page gave itself, already parsed - no second request.
        stashed = getattr(self, "_last_identity", None)
        org = ""
        if stashed and stashed.get("url") == url:
            org = (stashed.get("organization") or "").lower()
        if not org:
            return True  # nothing to judge on; keep the old behaviour

        # A legal-page title is the site owner's own page by definition.
        if any(frag in org for frag in ("impressum", "imprint", "legal")):
            return True

        # "Goerlich Pharma" on goerlich-pharma.de is the company's own site;
        # "Nutrivita Srl - Produttore ... | Europages" on europages.co.uk is
        # not. Compared on the squashed brand label so punctuation and legal
        # forms cannot defeat it.
        squashed = re.sub(r"[^a-z0-9]", "", org)
        label = re.sub(r"[^a-z0-9]", "", self._brand_label(parsed.netloc))
        if not label or not squashed:
            return True
        return squashed.startswith(label) or label.startswith(squashed[: len(label)])

    @classmethod
    def _brand_label(cls, host: str) -> str:
        """The company's own label in a hostname: brand.co.uk -> 'brand'."""
        parts = [p for p in (host or "").lower().split(":")[0].split(".") if p]
        if len(parts) >= 3 and parts[-2] in cls._PUBLIC_SLDS:
            return parts[-3]
        if len(parts) >= 2:
            return parts[-2]
        return parts[0] if parts else ""

    @classmethod
    def _is_company_address(cls, email: str, netloc: str) -> bool:
        """True if the address belongs to the company whose site we are on.

        Compares the BRAND LABEL, not the whole domain. The guard exists to
        exclude the web agency credited in an Impressum, and an agency has a
        different NAME - not merely a different TLD. Measured: delthapharma.com
        publishes seven addresses, every one of them @delthapharma.it, and a
        whole-domain comparison rejected all seven, losing the richest site in
        the sample. vitalproducts.de vs memaba-design.de is still rejected,
        which is the case this guard is for.
        """
        mail_host = (email or "").rsplit("@", 1)[-1]
        site_label = cls._brand_label(netloc)
        mail_label = cls._brand_label(mail_host)
        return bool(site_label) and site_label == mail_label

    def _one_per_company(self, contacts: list[dict]) -> list[dict]:
        """Keep the best address per company (guard 3).

        Grouped by BRAND LABEL, not by full domain - the same comparison
        guard 2 uses. A company publishing info@brand.com and einkauf@brand.de
        is one company, and grouping by domain sent it two emails.
        """
        best: dict[str, dict] = {}
        for c in contacts:
            domain = self._brand_label((c.get("email") or "").rsplit("@", 1)[-1])
            if not domain:
                continue
            if domain not in best or self._rank_address(
                c["email"]
            ) < self._rank_address(best[domain]["email"]):
                best[domain] = c
        return list(best.values())

    @classmethod
    def _rank_address(cls, email: str) -> int:
        """Lower is better. A buying mailbox beats a person beats reception.

        The unrecognised case is a NAMED mailbox - m.mueller@, maria.rossi@ -
        and it must outrank info@, which this function's own vocabulary calls
        reception. It did not: "info" is the last entry in BUYING_PREFIXES and
        scored len-1, while anything unrecognised scored len. So a page
        offering both info@ and m.mueller@ labelled "Leiter Einkauf" kept
        reception and discarded the buyer, which is the whole point of ranking
        inverted. That got sharper when info@ stopped being blocked outright.

        Ordering: recognised buying prefix (0..n-2) < named mailbox (n-1) <
        info@ (n). Reception is a real fallback, so it stays last rather than
        being dropped.
        """
        prefix = (email or "").split("@", 1)[0].lower()
        reception = len(cls.BUYING_PREFIXES)
        for i, p in enumerate(cls.BUYING_PREFIXES):
            if prefix == p or prefix.startswith(p):
                # "info" sorts BELOW an unrecognised (named) mailbox, not above.
                return reception if p == "info" else i
        return reception - 1

    def _scrape_page(self, url: str) -> list[dict]:
        """Scrape a single page for contact information.

        The robots.txt check lives HERE, at the single chokepoint every fetch
        passes through, rather than at the call sites - a guard placed in the
        caller is one refactor away from being bypassed, and this one carries
        a compliance obligation (see _robots_allows).
        """
        if not self._robots_allows(url):
            logger.info("Skipping %s - robots.txt disallows it", url)
            return []

        # One fetch per URL per run. A search returns several pages of the same
        # site, and scrape_site probes up to seven contact paths for each - so
        # /contatti was being fetched four times over, 18 requests at one
        # prospect's website with a politeness sleep before each. That is the
        # kind of traffic that gets a sender blocked by the very companies it
        # is trying to reach.
        #
        # A wrapper rather than a check before each return, so a later edit
        # cannot add a return path that quietly bypasses the cache. The stashed
        # identity is restored too: callers read _last_identity after this
        # returns, and a cache hit that skipped it would leave them reading
        # whichever page happened to be fetched last.
        seen = getattr(self, "_fetched_pages", None)
        if seen is None:
            seen = self._fetched_pages = {}
        key = url.rstrip("/")
        if key in seen:
            contacts, identity = seen[key]
            logger.debug("Already fetched %s this run - reusing the result", url)
            self._last_identity = identity
            return [dict(c) for c in contacts]

        contacts = self._fetch_and_parse(url)
        # Copies go into the cache, not the objects being returned. scrape_site
        # mutates what it receives - organization, qualification_rationale,
        # source_url - so storing the same dicts would let the first caller
        # rewrite what every later one sees.
        seen[key] = (
            [dict(c) for c in contacts],
            getattr(self, "_last_identity", None),
        )
        return contacts

    def _fetch_and_parse(self, url: str) -> list[dict]:
        """The actual fetch. Reached only through _scrape_page, which owns the
        robots check and the per-run cache."""
        try:
            response = self._client.get(url)
            response.raise_for_status()
            # Hand BeautifulSoup the raw bytes, not response.text.
            #
            # httpx decodes as UTF-8 when the HTTP header omits a charset, and
            # it ignores the HTML <meta charset>. Passing the resulting str also
            # bypasses bs4's own charset sniffing, so an ISO-8859-1 page that
            # declares its encoding only in <meta> came through as
            # "Gruene Naehrstoffe GmbH ���". That mangled <title>
            # becomes `organization`, which goes straight into the email SUBJECT
            # line - and German, French, Italian and Nordic legacy sites are
            # exactly this project's target market.
            html = response.content
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP {e.response.status_code} for {url}")
            return []
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            # HTTPError covers the transport failures (connect, read, timeout,
            # too many redirects). InvalidURL is separate in httpx and is
            # reachable here because these URLs come from scraped search
            # results, not from us.
            logger.warning(f"Failed to fetch {url}: {e}")
            return []

        # Let BeautifulSoup sniff the encoding from the bytes (including the
        # HTML <meta charset>), then work from ITS decoding everywhere so the
        # regex scan and the parsed tree agree on the text.
        soup = BeautifulSoup(html, "html.parser")
        decoded_html = str(soup)

        # Who this page says the company is, computed while the parsed page is
        # in hand and stashed for _page_identity. Done BEFORE the early return
        # below on purpose: a page with no address is exactly when scrape_site
        # needs this, and recovering it with a second request meant one wasted
        # download per site plus a silent fallback to the CONTACT page's title
        # whenever that request failed.
        org_name = self._extract_org_name(soup, url)
        page_context = self._extract_page_context(soup)
        page = self._page_country(soup, org_name)
        self._last_identity = {
            "url": url,
            "organization": org_name,
            "qualification_rationale": page_context,
            "page_country": page,
        }

        # Extract emails from the decoded HTML
        emails = self._extract_emails(decoded_html)
        if not emails:
            return []

        contacts = []
        for email in emails:
            contact = {
                "email": email,
                "name": None,
                "role": None,
                "organization": org_name,
                "source_url": url,
                "qualification_rationale": (
                    f"Email found on page: {url}. "
                    f"Page context: {page_context[:200] if page_context else 'N/A'}"
                ),
                "page_country": page,
            }

            # Try to find name/role near the email on the page
            name_role = self._find_name_near_email(soup, email)
            if name_role:
                contact["name"] = name_role.get("name")
                contact["role"] = name_role.get("role")

            contacts.append(contact)

        return contacts

    # ─────────────────────────────────────────────────────────────────
    # Email Extraction & Validation
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _deobfuscate(text: str) -> str:
        """Turn bracketed obfuscation into real punctuation, e.g.
        'info [at] company [dot] com' -> 'info@company.com'. Only the bracketed
        forms are touched, so ordinary prose ('meet at noon') is left alone."""
        text = re.sub(r"\s*[\(\[\{]\s*at\s*[\)\]\}]\s*", "@", text, flags=re.IGNORECASE)
        text = re.sub(
            r"\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*", ".", text, flags=re.IGNORECASE
        )
        return text

    @staticmethod
    def _is_certified_mail(domain: str) -> bool:
        """True for an Italian PEC / certified-mail domain (see the constant).

        Compares whole LABELS, so "concert.it", "specialita.it" and
        "pecorino-shop.it" are ordinary prospects, while pec.it,
        pec.aruba.it and postacert.comune.milano.it are refused.
        """
        labels = {p for p in (domain or "").lower().split(".") if p}
        return bool(labels & CERTIFIED_MAIL_LABELS)

    def _is_blocked_prefix(self, local_part: str) -> bool:
        """True if the mailbox is a generic/no-reply address. Matches the prefix
        only at a token boundary, so 'supporter'/'adminaide' are NOT blocked."""
        for p in self.blocked_prefixes:
            if local_part == p or re.match(rf"^{re.escape(p)}[._\-+]", local_part):
                return True
        return False

    def _extract_emails(self, html: str) -> list[str]:
        """Extract and validate real contact emails from a page.

        Ignores <script>/<style>/<head> so config blobs (Sentry DSNs, analytics
        keys) can't masquerade as contacts; reads mailto: links and visible text
        (which decodes HTML entities such as &#64;) and de-obfuscates
        '[at]'/'[dot]'; drops asset filenames and generic mailboxes."""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "head", "noscript", "template"]):
            tag.decompose()

        candidates = []
        # 1. mailto: links (most reliable signal)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip()
                if addr:
                    candidates.append(addr)
        # 2. visible text (entities already decoded), obfuscation undone
        text = self._deobfuscate(soup.get_text(separator=" "))
        candidates.extend(EMAIL_RE.findall(text))

        validated = []
        seen = set()
        for email in candidates:
            email = email.lower().strip().strip(".")
            if not email or "@" not in email or email in seen:
                continue
            seen.add(email)

            local_part, domain = email.split("@", 1)

            # Skip generic/blocked mailbox prefixes (word-boundary match).
            if self._is_blocked_prefix(local_part):
                continue
            # Skip personal email providers.
            if domain in PERSONAL_DOMAINS:
                continue
            # Skip certified mail (PEC) - legal correspondence, not marketing.
            if self._is_certified_mail(domain):
                continue
            # Skip asset filenames masquerading as emails (logo@2x.png).
            if domain.endswith(ASSET_DOMAIN_EXTS):
                continue

            # Validate with email-validator if available.
            if validate_email is not None:
                try:
                    info = validate_email(email, check_deliverability=False)
                    email = info.normalized
                except EmailNotValidError:
                    continue
            elif not EMAIL_RE.fullmatch(email):
                continue

            validated.append(email)

        return validated

    # ─────────────────────────────────────────────────────────────────
    # Context Extraction Helpers
    # ─────────────────────────────────────────────────────────────────

    # Page titles carry the brand on either side of the separator. WordPress
    # and Yoast default to "%%title%% %%sep%% %%sitename%%" - page first, brand
    # last - which is common on exactly the smaller European sites this agent
    # scrapes. Blindly taking the first half turned "Contact Us - Acme
    # Nutrition GmbH" into the organisation "Contact Us", which then went into
    # the relevance gate AND into the AI prompt that writes the opening line of
    # a real email.
    _GENERIC_TITLE_PARTS = frozenset(
        {
            "home",
            "homepage",
            "welcome",
            "contact",
            "contact us",
            "about",
            "about us",
            "products",
            "shop",
            "news",
            "blog",
            "impressum",
            "kontakt",
            "startseite",
            "willkommen",
            "über uns",
            "uber uns",
            "accueil",
            "à propos",
            "a propos",
            "contactez-nous",
            "chi siamo",
            "contatti",
            "prodotti",
            "azienda",
            "inicio",
            "contacto",
            "sobre nosotros",
            "quienes somos",
        }
    )

    @classmethod
    def _pick_company_side(cls, parts: list) -> str:
        """Choose the half of a split title that names the company.

        Prefers the first part, as before, unless it is obviously a page name
        and the last part is not - so the common "Brand | Page" layout is
        untouched and only the inverted convention is corrected.
        """
        first, last = parts[0], parts[-1]
        first_generic = first.strip().lower() in cls._GENERIC_TITLE_PARTS
        last_generic = last.strip().lower() in cls._GENERIC_TITLE_PARTS
        if first_generic and not last_generic:
            return last
        return first

    # Control characters and the Unicode line separators: no company's name
    # holds one, and a <title> written over several lines is common.
    _CONTROL_RUN = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")

    @classmethod
    def _without_controls(cls, text: str) -> str:
        """text, trimmed, with each run of control characters and the spaces
        beside it read as one space."""
        pieces = (piece.strip() for piece in cls._CONTROL_RUN.split(text))
        return " ".join(piece for piece in pieces if piece)

    def _extract_org_name(self, soup: BeautifulSoup, url: str) -> str | None:
        """Extract organization name from page metadata or title.

        A name the page gives is at most 100 characters, with no control
        character: it is stored, matched against the client and competitor
        lists, and written into every email to the company, its subject
        among them.
        """
        # Try meta tags first
        for meta in soup.find_all("meta"):
            if meta.get("property") == "og:site_name":
                # CMS themes often ship this tag unfilled. Returning "" here
                # left the contact nameless, so the client and competitor
                # lists could not match it; fall through to the title.
                content = self._without_controls(str(meta.get("content") or ""))
                if content:
                    return content[:100]

        # Try title tag
        if soup.title and soup.title.string:
            title = self._without_controls(soup.title.string)
            for sep in [" | ", " - ", " — ", " :: ", " » "]:
                if sep in title:
                    parts = [p.strip() for p in title.split(sep) if p.strip()]
                    if parts:
                        return self._pick_company_side(parts)[:100]
            return title[:100]

        # Fallback to domain name
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        return domain.split(".")[0].title()

    def _extract_page_context(self, soup: BeautifulSoup) -> str | None:
        """Extract descriptive context from the page."""
        # Try meta description
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            return meta_desc["content"].strip()

        # Try the first real paragraph. Not simply soup.find("p"): on a
        # European site the first <p> in document order is routinely the
        # cookie banner, so the company's "description" became "Wir verwenden
        # Cookies...". That text then fed the vertical gate - dropping a good
        # supplement company for showing no supplement signal - and went into
        # the AI prompt, which would have opened a cold email by remarking on
        # their cookie policy.
        for p in soup.find_all("p"):
            text = p.get_text(strip=True)
            if not text or self._is_boilerplate(p, text):
                continue
            return text[:300]

        return None

    # Consent-banner markup, and the wording it uses in the four languages
    # this agent scrapes.
    _BOILERPLATE_ATTR = re.compile(r"cookie|consent|gdpr|privacy|cmp", re.IGNORECASE)
    _BOILERPLATE_TEXT = re.compile(
        r"\b(cookies?|einwilligung|zustimmung|consenso|consentement|"
        r"privacy polic|datenschutz|informativa)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _is_boilerplate(cls, node, text: str) -> bool:
        """True if this paragraph is consent/legal furniture, not description."""
        if cls._BOILERPLATE_TEXT.search(text):
            return True
        for parent in [node, *getattr(node, "parents", [])]:
            attrs = getattr(parent, "attrs", None)
            if not attrs:
                continue
            blob = " ".join(
                str(attrs.get(k, "")) for k in ("class", "id", "role", "data-cookie")
            )
            if blob and cls._BOILERPLATE_ATTR.search(blob):
                return True
        return False

    def _find_name_near_email(self, soup: BeautifulSoup, email: str) -> dict | None:
        """
        Try to find a person's name and role near an email address on the page.
        Looks for common patterns like name in parent/sibling elements.
        """
        # Find the text node containing the email.
        #
        # IGNORECASE is required: _extract_emails lowercases every address
        # before storing it, but pages routinely render them with capitals
        # ("Anna.Rossi@Acme.de"). A case-sensitive lookup therefore never
        # matched, role stayed None, and lead_scoring fell back to its default
        # of 5 instead of up to 35 - capping every contact at 70 against a
        # tier-A threshold of 75, so tier A was unreachable for every contact.
        email_elements = soup.find_all(
            string=re.compile(re.escape(email), re.IGNORECASE)
        )

        if not email_elements:
            # Addresses found only in a mailto: href never appear in a text node
            # at all, so fall back to the element carrying the link.
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().startswith("mailto:") and email in href.lower():
                    email_elements = [a]
                    break

        for element in email_elements:
            # Look at parent and sibling elements
            parent = element.parent
            if not parent:
                continue

            # Check grandparent for structured contact cards
            grandparent = parent.parent if parent else None
            search_elements = [parent]
            if grandparent:
                search_elements.append(grandparent)

            for container in search_elements:
                text = container.get_text(separator=" ", strip=True)

                # Look for common name patterns
                name_match = re.search(
                    r"(?:Dr\.?\s+|Prof\.?\s+|Mr\.?\s+|Mrs\.?\s+|Ms\.?\s+)?"
                    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
                    text,
                )

                # Look for role/title patterns.
                #
                # The (?<!\w)/(?!\w) anchors are load-bearing. Without them this
                # matched inside longer words, and `text` is the raw container
                # text around a scraped mailto - which on almost every European
                # site is the footer, next to a cookie banner. "We use cookies
                # to improve your experience. Contact: info@company.com" made
                # re.search return "Coo" (inside "cookies") as the contact's
                # ROLE; "Headquarters" gave "Head", "Contractor" gave "Cto".
                # That fake role was then scored as C-suite, promoting a generic
                # info@ mailbox into the top lead tier.
                #
                # Longest-first ordering matters too: with "Manager" before
                # "General Manager" the alternation returns the shorter match.
                role_match = re.search(
                    r"(?<!\w)(?:Chief\s+\w+\s+Officer|General\s+Manager|"
                    r"CEO|CFO|CTO|COO|Director|Manager|Head|VP|President|"
                    r"Pharmacist|Doctor|Physician|Procurement|Buyer)(?!\w)",
                    text,
                    re.IGNORECASE,
                )

                if name_match or role_match:
                    return {
                        "name": name_match.group(0).strip() if name_match else None,
                        "role": role_match.group(0).strip() if role_match else None,
                    }

        return None

    # ─────────────────────────────────────────────────────────────────
    # URL Filtering
    # ─────────────────────────────────────────────────────────────────

    def _should_skip_url(self, url: str) -> bool:
        """Check if a URL should be skipped (non-HTML, social media, etc.)."""
        parsed = urlparse(url)
        path = parsed.path.lower()

        # Skip file downloads
        for ext in SKIP_EXTENSIONS:
            if path.endswith(ext):
                return True

        # Skip social media (often block scraping)
        skip_domains = {
            "facebook.com",
            "twitter.com",
            "x.com",
            "linkedin.com",
            "instagram.com",
            "youtube.com",
            "tiktok.com",
        }
        domain = parsed.netloc.lower().replace("www.", "")
        return domain in skip_domains
