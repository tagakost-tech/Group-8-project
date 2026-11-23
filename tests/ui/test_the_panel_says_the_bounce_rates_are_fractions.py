"""The settings panel says the two bounce rates are fractions.

The labels read "Pause all sending above this bounce rate" with no unit, so an
operator who meant 1% typed 1, which the server accepted and which switched
the breaker off (tests/test_the_bounce_guards_cannot_be_switched_off.py).

Nothing here leaves this machine: the chart library and the icon font are
answered locally, and the deliverability card, whose server side asks DNS, is
refused in the browser. The panel under test needs neither.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, live_dashboard  # noqa: F401

from conftest import TEST_DASHBOARD_LOGIN, open_tab

pytestmark = pytest.mark.ui

# What dashboard.js asks of Chart.js before the settings panel loads.
_CHART_STAND_IN = (
    "window.Chart = class { constructor(el, cfg) { this.data = cfg.data; }"
    " update() {} resize() {} };"
    " window.Chart.defaults = { font: {} };"
)


def _answer_locally(route):
    if route.request.url.endswith(".js"):
        route.fulfill(content_type="application/javascript", body=_CHART_STAND_IN)
    else:
        route.fulfill(content_type="text/css", body="")


def _labels(page, dashboard) -> dict:
    page.route(re.compile(r"^https?://(?!127\.0\.0\.1[:/])"), _answer_locally)
    page.route("**/api/deliverability", lambda route: route.abort())
    page.goto(dashboard + "/classic")
    open_tab(page, "tab-insights")
    page.wait_for_selector("#configForm .config-row", timeout=15000)
    return page.eval_on_selector_all(
        "#configForm .config-row",
        "rows => Object.fromEntries(rows.map(r => ["
        "r.querySelector('input').dataset.key,"
        " r.querySelector('.config-label').innerText]))",
    )


@pytest.mark.parametrize("key", ["max_bounce_rate", "max_domain_bounce_rate"])
def test_a_bounce_rate_label_gives_its_unit(live_dashboard, key):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            labels = _labels(browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN), live_dashboard)
            assert "as a fraction" in labels[key], (
                f"{labels[key]!r} gives no unit, so 1 meant as 1% is typed "
                f"as a rate of 100%"
            )
            assert "%" in labels[key], labels[key]
        finally:
            browser.close()
