"""The Template Editor opens and saves the Italian texts too.

Italian prospects read email_template_it.html and the two Italian follow-ups,
and the editor could open only the English first email. /api/template is
stubbed for both reads and saves, so no test here writes the repository's
own templates.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, _free_port

from conftest import TEST_DASHBOARD_LOGIN, open_tab

pytestmark = pytest.mark.ui

TEXTS = {
    "email_template": "<p>Dear {{name}}</p>{{legal_footer}}{{unsubscribe_url}}",
    "email_template_it": "<p>Gentile {{name}}</p>{{legal_footer}}{{unsubscribe_url}}",
    "followup_1_it": "<p>Faccio seguito</p>{{legal_footer}}{{unsubscribe_url}}",
}


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


def _editor(browser, url, save_status=200, save_body=None):
    page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
    page.gets, page.posts = [], []

    def template(route):
        req = route.request
        if req.method == "POST":
            page.posts.append(json.loads(req.post_data))
            route.fulfill(
                status=save_status,
                content_type="application/json",
                body=json.dumps(save_body or {"status": "ok"}),
            )
            return
        name = parse_qs(urlparse(req.url).query).get("name", ["email_template"])[0]
        page.gets.append(name)
        body = (
            {"content": TEXTS[name]}
            if name in TEXTS
            else {"content": "", "missing": True}
        )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/template*", template)
    page.route(
        "**/api/deliverability",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"
        ),
    )
    page.goto(url)
    open_tab(page, "tab-template")
    page.wait_for_function(
        "() => document.getElementById('templateEditor').value.includes('Dear')",
        timeout=15000,
    )
    return page


def _choose(page, name):
    page.select_option("#templateName", name)
    page.wait_for_function(
        "n => !document.getElementById('saveTemplateBtn').disabled",
        arg=name,
        timeout=15000,
    )


def test_choosing_the_italian_first_email_opens_it(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _editor(browser, live)
            assert page.gets[-1] == "email_template"
            _choose(page, "email_template_it")
            page.wait_for_function(
                "() => document.getElementById('templateEditor').value.includes('Gentile')",
                timeout=15000,
            )
            assert page.gets[-1] == "email_template_it"
            assert page.input_value("#templateEditor") == TEXTS["email_template_it"]
            assert page.inner_text("#templateNote") == ""
        finally:
            browser.close()


def test_save_posts_the_chosen_text(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _editor(browser, live)
            _choose(page, "email_template_it")
            page.wait_for_function(
                "() => document.getElementById('templateEditor').value.includes('Gentile')",
                timeout=15000,
            )
            edited = TEXTS["email_template_it"].replace("Gentile", "Egregio")
            page.fill("#templateEditor", edited)
            page.click("#saveTemplateBtn")
            page.wait_for_selector(".toast.show", timeout=15000)
            assert page.posts == [{"name": "email_template_it", "content": edited}]
            assert "First email - Italian saved" in page.inner_text(".toast")
        finally:
            browser.close()


def test_a_refused_save_shows_the_server_s_reason(live):
    reason = (
        "Refusing to save: the template must keep {{unsubscribe_url}} - they "
        "carry the legally required sender identity and the opt-out link."
    )
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _editor(browser, live, save_status=400, save_body={"error": reason})
            _choose(page, "followup_1_it")
            page.wait_for_function(
                "() => document.getElementById('templateEditor').value.includes('Faccio')",
                timeout=15000,
            )
            page.fill("#templateEditor", "<p>Faccio seguito</p>{{legal_footer}}")
            page.click("#saveTemplateBtn")
            page.wait_for_selector(".toast.show", timeout=15000)
            assert "error" in page.get_attribute("#toast", "class")
            assert page.inner_text(".toast") == reason
            assert page.posts[-1]["name"] == "followup_1_it"
        finally:
            browser.close()


def test_unsaved_edits_are_asked_about_before_another_text_opens(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _editor(browser, live)
            page.fill("#templateEditor", TEXTS["email_template"] + "<p>draft</p>")
            asked = []

            def dismiss(dialog):
                asked.append(dialog.message)
                dialog.dismiss()

            page.once("dialog", dismiss)
            page.select_option("#templateName", "followup_1_it")
            page.wait_for_timeout(400)
            assert asked and "not saved" in asked[0]
            assert page.input_value("#templateName") == "email_template"
            assert "<p>draft</p>" in page.input_value("#templateEditor")
            assert page.gets == ["email_template"]

            page.once("dialog", lambda dialog: dialog.accept())
            _choose(page, "followup_1_it")
            page.wait_for_function(
                "() => document.getElementById('templateEditor').value.includes('Faccio')",
                timeout=15000,
            )
        finally:
            browser.close()


def test_a_missing_italian_text_opens_empty_and_says_so(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _editor(browser, live)
            _choose(page, "followup_2_it")
            page.wait_for_function(
                "() => document.getElementById('templateNote').textContent.length > 0",
                timeout=15000,
            )
            assert page.input_value("#templateEditor") == ""
            assert (
                page.inner_text("#templateNote")
                == "Missing - Italian recipients get the English text"
            )
            placeholder = page.get_attribute("#templateEditor", "placeholder")
            assert "Loading" not in placeholder
            assert "save to create it" in placeholder
        finally:
            browser.close()
