"""The charts and the icons show with no internet at all.

The page loaded Chart.js and Font Awesome from public CDNs, so on a network
that blocks them the charts said "unavailable" and every icon was an empty
box, and a CDN that was hacked could run its own script in the page. The
dashboard now serves both itself (static/vendor). Here Chromium can reach
nothing but this machine (tests/ui/test_dashboard_ui.py, _browser) and the
test fails if the page even asks for another server; the charts are drawn
by the real Chart.js and the icons drawn in the Font Awesome font.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, live_dashboard  # noqa: F401

from conftest import TEST_DASHBOARD_LOGIN

pytestmark = pytest.mark.ui

_CANVASES = [
    "emailsPerDayChart",
    "leadFunnelChart",
    "contactStatusChart",
    "successRateChart",
]


def _page(browser, live):
    page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
    page.goto(live + "/classic")
    page.wait_for_load_state("networkidle")
    return page


def test_the_real_chart_library_draws_every_chart(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            assert page.evaluate("Chart.version") == "4.4.1"
            drawn = page.evaluate(
                "ids => ids.filter(id => Chart.getChart(document.getElementById(id)))",
                _CANVASES,
            )
            assert drawn == _CANVASES
            assert "Charts unavailable" not in page.text_content(".charts-grid")
        finally:
            browser.close()


def test_the_icons_are_drawn_in_the_icon_font(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            page.evaluate("document.fonts.ready")
            assert page.evaluate(
                "document.fonts.check('900 16px \"Font Awesome 6 Free\"')"
            ), "the solid icon font did not load"
            icon = page.locator("i.fa-solid.fa-users").first
            family = icon.evaluate("el => getComputedStyle(el, '::before').fontFamily")
            assert "Font Awesome 6 Free" in family
            content = icon.evaluate("el => getComputedStyle(el, '::before').content")
            assert content not in ("", "none", "normal"), content
            loaded = page.evaluate(
                "[...document.fonts].filter(f => f.status === 'loaded')"
                ".map(f => f.family)"
            )
            assert any("Font Awesome 6 Free" in f for f in loaded), loaded
        finally:
            browser.close()
