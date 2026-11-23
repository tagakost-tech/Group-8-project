"""Two threads sharing one page fetcher each connect where they checked.

The curl fetcher pins a checked host to its checked address with a RESOLVE
entry on the shared curl session, read when the request starts. Two
threads fetching through one SearchModule could interleave: thread A sets
its pin, thread B replaces it with its own, and A's request starts with
B's entry, so curl resolved A's host itself again (the DNS-rebinding gap
the pin closes). Now a lock holds each pin from being set until its
request has started.
"""

import ipaddress
import threading

import httpx
import pytest

import modules.search as search_mod
from modules.search import _ImpersonatingFetcher

DNS = {"brand.test": "93.184.216.34", "other.test": "151.101.1.69"}


class _Answer:
    def __init__(self):
        self.status_code = 200
        self.headers = {}

    def iter_content(self):
        yield b"ok"

    def close(self):
        pass


def test_a_pin_set_by_another_thread_never_reaches_this_request(monkeypatch):
    if search_mod.cffi_requests is None:
        pytest.skip("curl_cffi not installed")
    from curl_cffi import CurlOpt

    monkeypatch.setattr(
        search_mod,
        "resolve_host",
        lambda host: [ipaddress.ip_address(DNS[host])],
    )
    f = _ImpersonatingFetcher({}, timeout=5.0)
    pinned = threading.Event()
    first_request_started = threading.Event()

    class Options(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            pinned.set()

    f._session.curl_options = Options()
    pins = {}

    def get(url, **kwargs):
        host = httpx.URL(url).host
        if host == "brand.test":
            pinned.clear()
            first_request_started.set()
            # The other thread's pin would land here, unless held back.
            pinned.wait(1.0)
        pins[host] = list(f._session.curl_options[CurlOpt.RESOLVE])
        return _Answer()

    monkeypatch.setattr(f._session, "get", get)
    errors = []

    def fetch(url, wait_for=None):
        try:
            if wait_for is not None:
                wait_for.wait(5)
            f.get(url)
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [
        threading.Thread(target=fetch, args=("https://brand.test/",)),
        threading.Thread(
            target=fetch, args=("https://other.test/", first_request_started)
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    f.close()

    assert errors == []
    assert pins == {
        "brand.test": ["brand.test:443:93.184.216.34"],
        "other.test": ["other.test:443:151.101.1.69"],
    }
