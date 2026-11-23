"""The dashboard keeps working when Chart.js does not load.

templates/dashboard.html loaded Chart.js from cdn.jsdelivr.net, and an office
network can block that: a corporate proxy, TLS inspection. Without Chart.js
the dashboard's script threw "Chart is not defined" at the chart theme,
before it had set up the contacts table, its row actions or the refresh
buttons, so the operator saw no contacts and nothing said why. The dashboard
now serves Chart.js itself (static/vendor), but the file can still fail to
load: deleted, or held by antivirus. Here the request for it is aborted, and
so is every request that leaves this machine.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json
from urllib.parse import urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from test_dashboard_ui import (  # noqa: F401
    _browser,
    _contacts_page,
    _wait_for_rows,
    live_dashboard,
)

from conftest import _THIS_MACHINE, TEST_DASHBOARD_LOGIN, open_tab

pytestmark = pytest.mark.ui

_CANVASES = [
    "emailsPerDayChart",
    "leadFunnelChart",
    "contactStatusChart",
    "successRateChart",
]

# Plays Chart.js, so the test of the charts needs no internet either: it
# records each chart the dashboard makes and holds its data, as the library
# does.
_STAND_IN_CHART = """
window.__charts = [];
window.Chart = function (canvas, config) {
  window.__charts.push(canvas.id);
  this.data = config.data;
  this.update = function () {};
  this.resize = function () {};
};
window.Chart.defaults = { font: {} };
"""

# What /api/charts returns once the agent has run: four contacts opened.
CHARTS = {
    "emails_per_day": {"labels": ["2026-01-01", "2026-01-02"], "data": [3, 5]},
    "success_rate_trend": {"labels": ["2026-01-01", "2026-01-02"], "data": [100, 80]},
    "contacts_by_status": {"labels": ["pending", "emailed"], "data": [12, 7]},
    "lead_funnel": {
        "labels": ["Found", "Emailed", "Opened", "Replied"],
        "data": [19, 7, 4, 1],
    },
}


def _leaves_this_machine(url):
    parts = urlparse(url)
    return parts.scheme in ("http", "https") and parts.hostname not in _THIS_MACHINE


def _without_the_cdns(browser, url, *, charts=None, stand_in=None):
    """The contacts page, on a PC where Chart.js and every request to another
    machine fail.

    The routes and the stand-in go on a context, which _contacts_page opens
    its page from, so they are in place before the page loads. What the page
    threw is kept in page.errors, its console warnings in page.warnings.
    """
    context = browser.new_context(http_credentials=TEST_DASHBOARD_LOGIN)
    errors, warnings = [], []

    def warned(message):
        if message.type == "warning":
            warnings.append(message.text)

    context.on("weberror", lambda e: errors.append(e.error.message))
    context.on("console", warned)
    context.route(_leaves_this_machine, lambda route: route.abort())
    context.route("**/static/vendor/chartjs/**", lambda route: route.abort())
    if charts is not None:
        context.route(
            "**/api/charts",
            lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(charts)
            ),
        )
    if stand_in:
        context.add_init_script(stand_in)
    try:
        page = _contacts_page(context, url)
    except PlaywrightTimeoutError:
        raise AssertionError(
            f"the contacts table never showed a row; the page threw {errors}"
        ) from None
    if not stand_in:
        # Had the library got through, these tests would not be testing the
        # dashboard without it.
        assert page.evaluate("typeof Chart") == "undefined", "Chart.js was loaded"
    page.errors, page.warnings = errors, warnings
    return page


def test_the_contacts_table_shows_its_rows(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _without_the_cdns(browser, live_dashboard)
            _wait_for_rows(page, 3)
            emails = page.eval_on_selector_all(
                "#contactsTable tbody tr td:nth-child(3)",
                "els => els.map(e => e.textContent.trim())",
            )
            assert sorted(emails) == ["hot@brand.de", "low@brand.de", "mid@brand.de"]
            assert page.errors == []
        finally:
            browser.close()


def test_a_row_action_still_works(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _without_the_cdns(browser, live_dashboard)
            row = page.locator("#contactsTable > tbody > tr", has_text="low@brand.de")
            row.locator(".act-expand").click()
            page.wait_for_selector(".detail-row .why-headline", timeout=5000)
            assert "Score 12 below threshold 50" in page.inner_text(".detail-row")
        finally:
            browser.close()


def test_each_chart_box_says_the_charts_are_unavailable(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _without_the_cdns(browser, live_dashboard)
            open_tab(page, "tab-overview")
            page.wait_for_selector("#tab-overview.active", timeout=5000)
            boxes = page.locator(".charts-grid .chart-container")
            texts = boxes.all_inner_texts()
            assert len(texts) == 4
            assert all("Charts unavailable" in t for t in texts), texts
        finally:
            browser.close()


def test_the_opens_figure_still_updates(live_dashboard):  # noqa: F811
    """The figure is filled after the charts, in the same response handler,
    so it stays at 0 if filling a chart that is not there throws."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _without_the_cdns(browser, live_dashboard, charts=CHARTS)
            try:
                page.wait_for_function(
                    "() => document.getElementById('statOpens').textContent === '4'",
                    timeout=5000,
                )
            except PlaywrightTimeoutError:
                raise AssertionError(
                    f"Opens still reads {page.inner_text('#statOpens')!r}; "
                    f"console warnings: {page.warnings}"
                ) from None
        finally:
            browser.close()


def test_the_charts_are_drawn_when_the_library_loads(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _without_the_cdns(browser, live_dashboard, stand_in=_STAND_IN_CHART)
            assert page.evaluate("window.__charts") == _CANVASES
            assert page.evaluate("typeof Chart.defaults.color") == "string"
            assert "Charts unavailable" not in page.text_content(".charts-grid")
            assert page.errors == []
        finally:
            browser.close()
