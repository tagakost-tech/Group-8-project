"""Probes for the network guard in conftest.py.

The name does not start with test_, so the suite never collects this file:
tests/test_no_test_reaches_the_network.py runs it in a child pytest. Each probe
reaches for another machine the way the suite used to - a web search, an SMTP
session, a DNS lookup - through one of the ways out the guard keeps shut, and
then hides the error the way product code that degrades gracefully does. The
guard must fail every one of them anyway, and must leave this machine's own
connections alone.

Beneath the guard sits a floor, laid when this module is imported, which is
before the guard goes in: the guard is installed when the first test starts.
The floor refuses the same calls and writes each one that reaches it to
NETWORK_FLOOR_LOG. A guard that works leaves the floor untouched, and a broken
one is caught without a packet leaving this machine.
"""

import asyncio
import contextlib
import os
import pathlib
import socket
import sys

import pytest

# TEST-NET-1 (RFC 5737): reserved for documentation, never this machine.
ELSEWHERE = ("192.0.2.1", 9)
URL = "https://example.com/"
# Reserved (RFC 2606): no resolver anywhere answers for it.
UNRESOLVABLE = "https://dashboard-cdn.invalid/chart.js"
_HTTP_VERBS = ("request", "get", "head", "options", "delete", "post", "put", "patch")


def _floor_reached(what):
    log = os.environ.get("NETWORK_FLOOR_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{what}\n")
    raise ConnectionRefusedError(f"the floor beneath the guard refused {what}")


def _lay_the_floor():
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    sendto = socket.socket.sendto
    getaddrinfo = socket.getaddrinfo

    def local(address):
        return not isinstance(address, tuple) or address[0] in ("127.0.0.1", "::1")

    def floor_connect(sock, address):
        if local(address):
            return connect(sock, address)
        return _floor_reached(f"connect {address!r}")

    def floor_connect_ex(sock, address):
        if local(address):
            return connect_ex(sock, address)
        return _floor_reached(f"connect_ex {address!r}")

    def floor_sendto(sock, data, *flags_and_address):
        if local(flags_and_address[-1]):
            return sendto(sock, data, *flags_and_address)
        return _floor_reached(f"sendto {flags_and_address[-1]!r}")

    def floor_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        # A numeric host is parsed here, never looked up.
        if host in ("127.0.0.1", "::1", "localhost") or flags & socket.AI_NUMERICHOST:
            return getaddrinfo(host, port, family, type, proto, flags)
        return _floor_reached(f"getaddrinfo {host!r}")

    socket.socket.connect = floor_connect
    socket.socket.connect_ex = floor_connect_ex
    socket.socket.sendto = floor_sendto
    socket.getaddrinfo = floor_getaddrinfo

    def refused(what):
        return lambda *args, **kwargs: _floor_reached(what)

    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        pass
    else:
        for session in (curl_requests.Session, curl_requests.AsyncSession):
            session.request = refused(f"curl_cffi {session.__name__}.request")
    try:
        import primp
    except ImportError:
        pass
    else:
        for verb in _HTTP_VERBS:
            for client in (primp.Client, primp.AsyncClient):
                setattr(client, verb, refused(f"primp {client.__name__}.{verb}"))
            setattr(primp, verb, refused(f"primp.{verb}"))


_lay_the_floor()


# ── Ways out: each must fail its test ──────────────────────────────────────


def test_a_tcp_connection():
    with socket.socket() as sock, contextlib.suppress(OSError):
        sock.connect(ELSEWHERE)


def test_a_tcp_connection_that_returns_an_error_code():
    with socket.socket() as sock:
        sock.connect_ex(ELSEWHERE)


def test_a_udp_datagram():
    with socket.socket(type=socket.SOCK_DGRAM) as sock, contextlib.suppress(OSError):
        sock.sendto(b"probe", (ELSEWHERE[0], 53))


def test_a_name_lookup():
    with contextlib.suppress(OSError):
        socket.getaddrinfo("example.com", 443)


def test_a_curl_cffi_request():
    curl_requests = pytest.importorskip("curl_cffi.requests")
    with curl_requests.Session() as session, contextlib.suppress(OSError):
        session.get(URL)


def test_a_curl_cffi_async_request():
    curl_requests = pytest.importorskip("curl_cffi.requests")

    async def fetch():
        async with curl_requests.AsyncSession() as session:
            await session.get(URL)

    with contextlib.suppress(OSError):
        asyncio.run(fetch())


def test_a_primp_request():
    primp = pytest.importorskip("primp")
    with contextlib.suppress(OSError):
        primp.Client().get(URL)


def test_a_primp_async_request():
    primp = pytest.importorskip("primp")

    async def fetch():
        await primp.AsyncClient().get(URL)

    with contextlib.suppress(OSError):
        asyncio.run(fetch())


def test_a_primp_request_without_a_client():
    primp = pytest.importorskip("primp")
    with contextlib.suppress(OSError):
        primp.get(URL)


# ── This machine: must pass ────────────────────────────────────────────────


def test_this_machine_can_still_be_reached():
    """The SMTP sink and the dashboard the browser tests drive listen on
    127.0.0.1, and Python's own socketpair connects there too."""
    with (
        socket.create_server(("127.0.0.1", 0)) as server,
        socket.create_connection(server.getsockname()[:2]),
    ):
        pass
    with (
        socket.socket(type=socket.SOCK_DGRAM) as receiver,
        socket.socket(type=socket.SOCK_DGRAM) as sender,
    ):
        receiver.bind(("127.0.0.1", 0))
        sender.sendto(b"probe", receiver.getsockname())
    assert socket.getaddrinfo("localhost", 25)


# ── The browser tests (run alone with -m ui): must fail ────────────────────


@pytest.mark.ui
def test_a_browser_test_is_guarded_too():
    """The live dashboard runs in the test's own process: its lookups are
    guarded as any test's are."""
    with contextlib.suppress(OSError):
        socket.getaddrinfo("example.com", 443)


@pytest.mark.ui
def test_chromium_in_a_browser_test_reaches_no_other_server():
    """Chromium is a process of its own, past the guard: the browser tests'
    _browser stops its lookups and reports each request it makes. The name
    is reserved (RFC 2606), so not even a broken resolver rule sends this
    request anywhere; a page that did load is written to the floor."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "ui"))
    from test_dashboard_ui import _browser

    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page()
            with contextlib.suppress(Exception):
                page.goto(UNRESOLVABLE, timeout=15000)
                _floor_reached(f"Chromium loaded {UNRESOLVABLE}")
        finally:
            browser.close()
