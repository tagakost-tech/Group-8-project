"""Every Live Activity entry says which day it happened, not only the time.

The feed cut each timestamp down to "11:28:22", so an entry from yesterday or
last week read exactly like one from this morning. It now shows the date and
time the way the Contacts and Campaign Logs tables do.

Nothing here leaves this machine: the chart library and the icon font are
answered locally, the deliverability card is refused, and the feed is a stub.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json
import re

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, live_dashboard  # noqa: F401

from conftest import TEST_DASHBOARD_LOGIN

pytestmark = pytest.mark.ui

_CHART_STAND_IN = (
    "window.Chart = class { constructor(el, cfg) { this.data = cfg.data; }"
    " update() {} resize() {} destroy() {} };"
    " window.Chart.defaults = { font: {} };"
)

# Newest first, as GET /api/activity returns them; two different days.
ACTIVITY = [
    {
        "timestamp": "2026-11-12 11:28:22",
        "action": "search_completed",
        "details": "today's entry",
        "status": "success",
    },
    {
        "timestamp": "2026-11-05 09:04:51",
        "action": "email_sent",
        "details": "last week's entry",
        "status": "info",
    },
]


def _answer_locally(route):
    if route.request.url.endswith(".js"):
        route.fulfill(content_type="application/javascript", body=_CHART_STAND_IN)
    else:
        route.fulfill(content_type="text/css", body="")


def open_feed(page, dashboard):
    page.route(re.compile(r"^https?://(?!127\.0\.0\.1[:/])"), _answer_locally)
    page.route("**/api/deliverability", lambda route: route.abort())
    page.route(
        "**/api/activity*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(ACTIVITY)
        ),
    )
    page.goto(dashboard + "/classic")
    page.wait_for_selector("#activityFeed .act-row", timeout=15000)


@pytest.mark.parametrize("width", [1440, 1200])
def test_each_entry_shows_its_date_and_time(live_dashboard, width):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN, viewport={"width": width, "height": 900})
            open_feed(page, live_dashboard)
            times = page.eval_on_selector_all(
                "#activityFeed .act-time", "els => els.map(e => e.innerText)"
            )
            # Oldest first, like a log.
            assert times == ["2026-11-05 09:04:51", "2026-11-12 11:28:22"], times
            # The date and time sit on one line, as they do in the tables: one
            # line box per cell, counted from the text itself, since a grid
            # stretches every cell in a row to the tallest one.
            wrapped = page.eval_on_selector_all(
                "#activityFeed .act-time",
                "els => els.filter(e => { const r = document.createRange();"
                " r.selectNodeContents(e); return r.getClientRects().length > 1;"
                " }).length",
            )
            assert wrapped == 0, f"the date and time wrap at {width}px"
        finally:
            browser.close()
