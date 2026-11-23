"""The dashboard works under its own Content-Security-Policy, and the policy
stops what it is there to stop.

The policy refuses inline script ('unsafe-inline' is not in script-src), so
the contacts table's row buttons, which ran inline onclick handlers, now go
through one listener in dashboard.js. The template preview is a sandboxed
frame filled through srcdoc. Here a real Chromium, with the policy enforced,
walks the tabs, opens a contact's details, presses its row buttons and
previews a template, and nothing the page does is refused; an injected
inline handler and a script in a template do not run.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, _contacts_page, live_dashboard  # noqa: F401

from conftest import TEST_DASHBOARD_LOGIN, open_tab

pytestmark = pytest.mark.ui

# Records every policy refusal, from before the page's own script runs.
_WATCH = """
window.__refused = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.__refused.push(e.violatedDirective + " " + e.blockedURI);
});
"""

TABS = [
    "tab-overview",
    "tab-insights",
    "tab-contacts",
    "tab-emails",
    "tab-directsend",
    "tab-template",
    "tab-searches",
]


def _watched(browser, live, **kw):
    context = browser.new_context(http_credentials=TEST_DASHBOARD_LOGIN)
    context.add_init_script(_WATCH)
    page = context.new_page()
    page.on("dialog", lambda d: d.dismiss())
    page.goto(live)
    page.wait_for_load_state("networkidle")
    return page


def test_every_tab_renders_with_nothing_refused(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _watched(browser, live_dashboard)
            assert page.evaluate("typeof Chart") == "function", (
                "Chart.js did not load from its pinned address"
            )
            for tab in TABS:
                open_tab(page, tab)
                page.wait_for_selector(f"#{tab}.active", timeout=5000)
            page.wait_for_load_state("networkidle")
            assert page.evaluate("window.__refused") == []
        finally:
            browser.close()


def test_the_row_buttons_work_without_inline_handlers(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            context = browser.new_context(http_credentials=TEST_DASHBOARD_LOGIN)
            context.add_init_script(_WATCH)
            page = _contacts_page(context, live_dashboard)
            posted = []
            page.route(
                "**/api/contacts/*/action",
                lambda route: (
                    posted.append(route.request.post_data_json),
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body='{"status": "ok"}',
                    ),
                ),
            )
            row = page.locator("#contactsTable > tbody > tr", has_text="low@brand.de")
            row.locator(".act-expand").click()
            page.wait_for_selector(".detail-row .why-headline", timeout=5000)
            row.locator("button[data-action='send']").click()
            row = page.locator("#contactsTable > tbody > tr", has_text="mid@brand.de")
            row.locator("button[data-action='block']").click()
            page.wait_for_timeout(500)
            assert {"action": "send_now"} in posted
            assert {"action": "block"} in posted
            assert page.evaluate("window.__refused") == []
        finally:
            browser.close()


BLOCKED_ROW = {
    "id": 4,
    "email": "gone@brand.de",
    "name": "Gus One",
    "organization": "G Srl",
    "status": "blocked",
    "lead_score": 40,
    "lead_tier": "C",
    "timestamp": "2026-01-04",
}


def test_the_client_and_unblock_buttons_reach_the_server(
    live_dashboard,  # noqa: F811
):
    """The two row buttons the test above does not press: each data-action
    must reach its own action, not merely some handler."""
    import json

    from test_dashboard_ui import CONTACTS

    with sync_playwright() as p:
        browser = _browser(p)
        try:
            context = browser.new_context(http_credentials=TEST_DASHBOARD_LOGIN)
            context.add_init_script(_WATCH)
            body = json.dumps(json.loads(CONTACTS) + [BLOCKED_ROW])
            page = _contacts_page(context, live_dashboard, body=body)
            page.on("dialog", lambda d: d.accept())
            posted = []
            page.route(
                "**/api/contacts/*/action",
                lambda route: (
                    posted.append(
                        (route.request.url.split("/")[-2], route.request.post_data_json)
                    ),
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body='{"status": "ok"}',
                    ),
                ),
            )
            row = page.locator("#contactsTable > tbody > tr", has_text="hot@brand.de")
            row.locator("button[data-action='client']").click()
            row = page.locator("#contactsTable > tbody > tr", has_text="gone@brand.de")
            row.locator("button[data-action='unblock']").click()
            page.wait_for_timeout(500)
            assert ("2", {"action": "mark_client"}) in posted
            assert ("4", {"action": "unblock"}) in posted
            assert page.evaluate("window.__refused") == []
        finally:
            browser.close()


def test_an_injected_inline_handler_does_not_run(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _watched(browser, live_dashboard)
            page.evaluate(
                """() => {
                  const div = document.createElement("div");
                  div.innerHTML = '<img src="/no-such-image" '
                    + 'onerror="window.__ran = true">';
                  document.body.appendChild(div);
                }"""
            )
            page.wait_for_timeout(500)
            assert page.evaluate("window.__ran === true") is False
            assert any(
                r.startswith("script-src") for r in page.evaluate("window.__refused")
            )
        finally:
            browser.close()


def test_a_script_in_a_template_does_not_run_in_the_preview(
    live_dashboard,  # noqa: F811
):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _watched(browser, live_dashboard)
            open_tab(page, "tab-template")
            page.wait_for_function(
                "() => !document.getElementById('saveTemplateBtn').disabled",
                timeout=15000,
            )
            page.fill(
                "#templateEditor",
                "<p>Hello preview</p><script>"
                "try { parent.__escaped = true } catch (e) {}"
                "document.body.dataset.ran = 'yes'</script>",
            )
            frame = page.frame_locator("#templatePreview")
            assert "Hello preview" in frame.locator("body").inner_text(timeout=5000)
            page.wait_for_timeout(300)
            assert frame.locator("body").get_attribute("data-ran") is None
            assert page.evaluate("window.__escaped === true") is False
        finally:
            browser.close()
