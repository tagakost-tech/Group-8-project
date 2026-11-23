"""A slow website cannot hold a page fetch for longer than its time limit.

The httpx fallback (used when curl_cffi is unavailable) had only httpx's
timeout, which counts each wait for data on its own: a server sending one
byte every second never tripped it, and could hold the agent's loop thread
for as long as it liked. curl, the normal path, stops a request after 15 s
in total. Now the whole httpx fetch has the same limit, however slowly the
headers or the body arrive.

These tests run a real server on this machine (loopback only), named
slow.test through a fake resolver; the address check is relaxed for it.
"""

import ipaddress
import socket
import threading
import time

import httpx
import pytest

import modules.search as search_mod
from modules.search import _HttpxFetcher

LIMIT = 1.0
TRICKLE_GAP = 0.1
SERVER_GIVES_UP = 6.0


class _Server:
    """Answers each connection with `answer(conn)` on its own thread."""

    def __init__(self, answer):
        self._answer = answer
        self.stop = threading.Event()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                data = conn.recv(4096)
                if not data:
                    return
                request += data
            try:
                self._answer(conn, self.stop)
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self._sock.close()
        self._thread.join(2)


def _trickle(conn, stop, head=b""):
    conn.sendall(head)
    started = time.monotonic()
    while not stop.is_set() and time.monotonic() - started < SERVER_GIVES_UP:
        conn.sendall(b"a")
        time.sleep(TRICKLE_GAP)


def _slow_body(conn, stop):
    _trickle(conn, stop, b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")


def _slow_headers(conn, stop):
    _trickle(conn, stop, b"HTTP/1.1 200 OK\r\nX-Slow: ")


def _quick(conn, stop):
    conn.sendall(
        b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\nContent-Type: text/html\r\n\r\n"
        b"<p>hello</p>"
    )


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.setattr(
        search_mod, "resolve_host", lambda host: [ipaddress.ip_address("127.0.0.1")]
    )
    monkeypatch.setattr(search_mod, "_special_kind", lambda ip: "")
    servers = []

    def start(answer):
        server = _Server(answer)
        servers.append(server)
        return f"http://slow.test:{server.port}/"

    yield start
    for server in servers:
        server.close()


def _timed_fetch(fetcher, url):
    started = time.monotonic()
    try:
        fetcher.get(url)
    except httpx.HTTPError as e:
        return time.monotonic() - started, e
    return time.monotonic() - started, None


@pytest.mark.parametrize("answer", [_slow_body, _slow_headers], ids=["body", "headers"])
def test_a_trickling_answer_is_cut_off_at_the_time_limit(serve, answer):
    f = _HttpxFetcher({}, timeout=LIMIT)
    try:
        elapsed, error = _timed_fetch(f, serve(answer))
    finally:
        f.close()
    assert isinstance(error, httpx.TimeoutException), error
    assert elapsed < LIMIT + 2.0, elapsed


def test_a_quick_page_still_arrives_and_the_limit_starts_again(serve):
    f = _HttpxFetcher({}, timeout=LIMIT)
    try:
        _elapsed, slow_error = _timed_fetch(f, serve(_slow_body))
        page = f.get(serve(_quick))
        time.sleep(LIMIT)
        again = f.get(serve(_quick))
    finally:
        f.close()
    assert isinstance(slow_error, httpx.TimeoutException)
    assert page.status_code == 200 and page.content == b"<p>hello</p>"
    assert again.content == b"<p>hello</p>"


def test_the_shared_agent_fetcher_has_the_limit_too(serve, db, monkeypatch):
    monkeypatch.setattr(search_mod, "cffi_requests", None)
    sm = search_mod.SearchModule(db, rate_limit=0.0)
    try:
        assert isinstance(sm._client, _HttpxFetcher)
        sm._client._timeout = LIMIT
        elapsed, error = _timed_fetch(sm._client, serve(_slow_body))
    finally:
        sm.close()
    assert isinstance(error, httpx.TimeoutException), error
    assert elapsed < LIMIT + 2.0, elapsed
