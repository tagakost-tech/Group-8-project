"""The charts and the warm-up figure are asked for once a minute, not at every poll.

The poll runs every 5 s while the agent runs and every 15 s while it is idle.
It asked /api/charts every time, which reads every contact and email and wrote
"Generated chart data" to the log at each poll, for figures that move slowly.
Now they are asked when the page opens and then at most once a minute, as the
readiness card is; the page shows the same figures.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json
import threading
from urllib.parse import urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, _free_port

from conftest import TEST_DASHBOARD_LOGIN

pytestmark = pytest.mark.ui


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


def _charts(opened):
    return {
        "emails_per_day": {"labels": ["2026-11-18"], "data": [4]},
        "success_rate_trend": {"labels": ["2026-11-18"], "data": [100.0]},
        "contacts_by_status": {"labels": ["emailed"], "data": [4]},
        "lead_funnel": {
            "labels": ["Found", "Emailed", "Opened", "Replied"],
            "data": [9, 4, opened, 0],
        },
    }


def test_the_poll_asks_for_the_charts_at_most_once_a_minute(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.clock.install()
            answers = {
                "/api/charts": _charts(opened=3),
                "/api/warmup": {
                    "day_number": 4,
                    "sent_today": 2,
                    "daily_allowance": 14,
                },
            }
            asked = []

            def fulfil(route):
                path = urlparse(route.request.url).path
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(answers[path]),
                )

            for path in answers:
                page.route(f"**{path}", fulfil)
            page.on("request", lambda r: asked.append(urlparse(r.url).path))

            def count(path):
                return asked.count(path)

            page.goto(live + "/classic")
            page.locator("#statWarmup", has_text="Day 4 (2/14)").wait_for(timeout=15000)
            assert page.inner_text("#statOpens") == "3"
            assert (count("/api/charts"), count("/api/warmup")) == (1, 1)
            status_before = count("/api/agent/status")

            answers["/api/charts"] = _charts(opened=5)
            answers["/api/warmup"] = {
                "day_number": 5,
                "sent_today": 7,
                "daily_allowance": 17,
            }
            page.clock.run_for(46_000)  # three idle polls
            page.wait_for_timeout(300)
            assert count("/api/agent/status") >= status_before + 3, (
                "the poll itself stopped"
            )
            assert (count("/api/charts"), count("/api/warmup")) == (1, 1), (
                "the 15 s poll asked for the charts again"
            )
            assert page.inner_text("#statWarmup") == "Day 4 (2/14)"

            page.clock.run_for(30_000)
            page.locator("#statWarmup", has_text="Day 5 (7/17)").wait_for(timeout=5000)
            assert page.inner_text("#statOpens") == "5"
            assert (count("/api/charts"), count("/api/warmup")) == (2, 2), (
                "a minute passed and the charts were not asked again"
            )
        finally:
            browser.close()
