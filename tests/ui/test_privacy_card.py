"""The Overview's "Privacy and inbox - to act on" card.

Data requests have a one-month legal deadline and nothing on the page showed
the register; unfinished erasures, held opt-outs and inbox gaps appeared only
through doctor on the command line. Both routes are stubbed here.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json
import threading

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, _free_port
from test_readiness_card import BLOCKED

from conftest import TEST_DASHBOARD_LOGIN

pytestmark = pytest.mark.ui

ERASURE_REMEDY = (
    "1 erasure(s) with files not changed yet: the journal, an access answer or a "
    "held opt-out may still name them. Another program may hold the file, or "
    "the disk may be full (the log says why) - once it is free, run `main.py backup`"
)

READINESS = dict(
    BLOCKED,
    privacy=[
        {"label": "Erasures finished", "ok": False, "detail": ERASURE_REMEDY, "blocking": False},
        {
            "label": "Held opt-outs",
            "ok": False,
            "detail": "2 opt-out(s) held while the database was locked - applied at "
            "the next start; those addresses are refused meanwhile",
            "blocking": False,
        },
        {
            "label": "Data-subject requests",
            "ok": False,
            "detail": "overdue: #7 - answer them now (`main.py dsr list`)",
            "blocking": False,
        },
        {"label": "Inbox gap", "ok": None, "detail": "IMAP off", "blocking": False},
    ],
)

REQUESTS = [
    {
        "id": 7,
        "kind": "erasure",
        "channel": "reply",
        "received_at": "2026-09-28T09:12:00+02:00",
        "due_at": "2026-10-28T09:12:00+01:00",
        "days_left": -15,
        "overdue": True,
    },
    {
        "id": 9,
        "kind": "access",
        "channel": "manual",
        "received_at": "2026-11-02T16:40:00+01:00",
        "due_at": "2026-12-02T16:40:00+01:00",
        "days_left": 20,
        "overdue": False,
    },
]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    from werkzeug.serving import make_server

    import main
    from conftest import _browser_dashboard_settings

    settings = _browser_dashboard_settings(tmp_path_factory.mktemp("uidb"))
    port = _free_port()
    server = make_server("127.0.0.1", port, main.create_dashboard_app(settings))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def _stub(page, pattern, body):
    page.route(
        pattern,
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)
        ),
    )


def _page(browser, url, requests):
    page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
    _stub(page, "**/api/deliverability", {})
    _stub(page, "**/api/readiness", READINESS)
    _stub(page, "**/api/data-requests", requests)
    page.goto(url + "/classic")
    page.wait_for_selector("#privacyChecks tr", timeout=15000)
    return page


def test_the_card_shows_what_to_act_on(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, REQUESTS)
            checks = page.inner_text("#privacyChecks")
            for name in (
                "Erasures finished",
                "Held opt-outs",
                "Data-subject requests",
                "Inbox gap",
            ):
                assert name in checks
            erasures = page.locator("#privacyChecks tr", has_text="Erasures finished")
            assert "run `main.py backup`" in erasures.inner_text()
            assert "Answer with main.py dsr" in page.inner_text("#privacyCard")
            assert not page.query_selector("#privacyCard button")
        finally:
            browser.close()


def test_an_overdue_request_is_marked_in_the_failed_colour(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, REQUESTS)
            page.wait_for_selector("#dsrTable tbody tr", timeout=15000)
            late = page.locator("#dsrTable tbody tr", has_text="#7")
            badge = late.locator(".badge-failed")
            assert badge.count() == 1
            assert "overdue" in badge.text_content()
            assert "15 days" in badge.text_content()
            assert "2026-10-28" in late.inner_text()
            on_time = page.locator("#dsrTable tbody tr", has_text="#9")
            assert on_time.locator(".badge-failed").count() == 0
            assert "20" in on_time.inner_text()
            rows = page.eval_on_selector_all(
                "#dsrTable tbody tr", "els => els.map(e => e.children[0].textContent)"
            )
            assert rows == ["#7", "#9"]
        finally:
            browser.close()


def test_an_empty_register_says_so(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, [])
            page.wait_for_function(
                "() => document.getElementById('dsrTable').textContent.includes('No open')",
                timeout=15000,
            )
            assert page.inner_text("#dsrTable") == "No open data requests"
        finally:
            browser.close()


def test_the_real_routes_render_the_card(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            _stub(page, "**/api/deliverability", {})
            page.goto(live + "/classic")
            page.wait_for_selector("#privacyChecks tr", timeout=15000)
            page.wait_for_function(
                "() => document.getElementById('dsrTable').textContent.length > 0",
                timeout=15000,
            )
            assert len(page.query_selector_all("#privacyChecks tr")) == 4
            assert not page.query_selector("#privacyCard .is-error")
        finally:
            browser.close()


@pytest.mark.parametrize("width", [390, 1222, 1440])
def test_the_deadline_columns_are_on_screen(live, width):
    """At 390px the register ran to 600px inside a 332px card, so Due and
    Days left - the columns that say a request is late - sat off-screen
    behind a sideways scroll. The deadline must be visible without one."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, REQUESTS)
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_selector("#dsrTable tbody tr", timeout=15000)
            geometry = page.evaluate(
                """() => {
                    const box = document.getElementById('dsrTable');
                    const edge = box.getBoundingClientRect().right;
                    const shown = [...box.querySelectorAll('th')]
                        .filter(h => h.getBoundingClientRect().width > 0);
                    return {
                        scrolls: box.scrollWidth > box.clientWidth,
                        names: shown.map(h => h.textContent),
                        outside: shown
                            .filter(h => h.getBoundingClientRect().right > edge + 1)
                            .map(h => h.textContent),
                    };
                }"""
            )
            assert not geometry["scrolls"]
            assert geometry["outside"] == []
            assert {"#", "Kind", "Due", "Days left"} <= set(geometry["names"])
            if width >= 1222:
                assert "Received" in geometry["names"]
        finally:
            browser.close()
