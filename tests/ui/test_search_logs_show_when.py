"""The Search Logs table says when each search was run.

Every search is stored with its time (search_log.timestamp, returned by GET
/api/searches), but the table showed only Query, Source, Results, Scraped and
Status, so nothing said whether a search ran this morning or last month. A
Date column now comes first, sortable like its neighbours.

Nothing here leaves this machine: the chart library and the icon font are
answered locally, the deliverability card is refused, and the searches are a
stub.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json
import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, live_dashboard  # noqa: F401

from conftest import TEST_DASHBOARD_LOGIN, open_tab

pytestmark = pytest.mark.ui

_CHART_STAND_IN = (
    "window.Chart = class { constructor(el, cfg) { this.data = cfg.data; }"
    " update() {} resize() {} destroy() {} };"
    " window.Chart.defaults = { font: {} };"
)

# Newest first, as GET /api/searches returns them.
SEARCHES = [
    {
        "id": 2,
        "timestamp": "2026-11-12 10:15:03",
        "search_query": "integratori alimentari private label italia",
        "source": "google",
        "results_count": 10,
        "urls_scraped": 8,
        "status": "completed",
    },
    {
        "id": 1,
        "timestamp": "2026-10-30 16:42:09",
        "search_query": "nutraceutical brand germany",
        "source": "bing",
        "results_count": 7,
        "urls_scraped": 5,
        "status": "completed",
    },
]


def _answer_locally(route):
    if route.request.url.endswith(".js"):
        route.fulfill(content_type="application/javascript", body=_CHART_STAND_IN)
    else:
        route.fulfill(content_type="text/css", body="")


def open_search_logs(page, dashboard, narrow=False):
    page.route(re.compile(r"^https?://(?!127\.0\.0\.1[:/])"), _answer_locally)
    page.route("**/api/deliverability", lambda route: route.abort())
    page.route(
        "**/api/searches*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(SEARCHES)
        ),
    )
    page.goto(dashboard + "/classic")
    page.wait_for_load_state("networkidle")
    if narrow:
        page.click("#menuToggle")
        page.wait_for_timeout(320)
    open_tab(page, "tab-searches")
    page.wait_for_selector("#tab-searches.active", timeout=5000)
    page.wait_for_selector("#searchesTable tbody tr td", timeout=8000)


def _first_column(page):
    return page.eval_on_selector_all(
        "#searchesTable tbody tr", "rows => rows.map(r => r.cells[0].innerText)"
    )


def test_the_first_column_is_when_the_search_ran(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN, viewport={"width": 1440, "height": 900})
            open_search_logs(page, live_dashboard)
            headers = page.eval_on_selector_all(
                "#searchesTable thead th", "ths => ths.map(t => t.textContent.trim())"
            )
            assert headers[0] == "Date", headers
            assert _first_column(page) == [
                "2026-11-12 10:15:03",
                "2026-10-30 16:42:09",
            ]
            # The query still sits beside it, in its own column.
            assert page.eval_on_selector(
                "#searchesTable tbody tr", "r => r.cells[1].innerText"
            ) == ("integratori alimentari private label italia")

            page.click("#searchesTable thead th:first-child")
            assert _first_column(page) == [
                "2026-10-30 16:42:09",
                "2026-11-12 10:15:03",
            ]
            assert (
                page.get_attribute("#searchesTable thead th:first-child", "aria-sort")
                == "ascending"
            )
        finally:
            browser.close()


def test_the_page_does_not_scroll_sideways_at_390(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN, viewport={"width": 390, "height": 844})
            open_search_logs(page, live_dashboard, narrow=True)
            assert _first_column(page)[0] == "2026-11-12 10:15:03"
            assert not page.evaluate(
                "document.documentElement.scrollWidth >"
                " document.documentElement.clientWidth"
            ), "tab-searches overflows sideways at 390px"
        finally:
            browser.close()
