"""The Template Editor's preview shows the email as it will read.

A walk-through found "My name is Jane Smith and I am responsible for
{{sender_title}} at Suppleco ... Acme Corp" in the preview, and Italian
follow-up 1 opening on "{{greeting}}". Against the real routes and the
repository's own texts (only read here), every text now previews with this
installation's sender, a sample recipient and no raw placeholder.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import threading

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, _free_port

from conftest import TEST_DASHBOARD_LOGIN, open_tab

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    from werkzeug.serving import make_server

    import main
    from conftest import _browser_dashboard_settings

    settings = _browser_dashboard_settings(tmp_path_factory.mktemp("uidb"))
    settings = settings.model_copy(
        update={
            "sender_name": "Giulia Bianchi",
            "sender_title": "",
            "company_name": "SUPPLECO",
            "sender_signature": "",
        }
    )
    port = _free_port()
    server = make_server("127.0.0.1", port, main.create_dashboard_app(settings))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def _preview_text(page, name):
    page.select_option("#templateName", name)
    page.wait_for_function(
        "() => !document.getElementById('saveTemplateBtn').disabled",
        timeout=15000,
    )
    frame = page.frame_locator("#templatePreview")
    return frame.locator("body").inner_text(timeout=15000)


@pytest.mark.parametrize(
    "name, greeting",
    [
        ("email_template", None),
        ("email_template_it", None),
        ("followup_1_it", "Buongiorno,"),
        ("followup_2_it", "Buongiorno,"),
    ],
)
def test_each_text_previews_with_the_real_sender_and_no_placeholder(
    live, name, greeting
):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/deliverability",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body="{}"
                ),
            )
            page.goto(live + "/classic")
            open_tab(page, "tab-template")
            text = _preview_text(page, name)
            assert "{{" not in text
            assert "Jane Smith" not in text and "Acme" not in text
            assert "Your Company Ltd" not in text
            assert "Giulia Bianchi" in text
            assert "Sample Brand S.r.l." in text
            # The send drops the template's full stop after a name that ends
            # in one; the preview must read the same.
            assert "S.r.l.." not in text
            assert "[Legal footer" in text
            if name.startswith("email_template"):
                assert "[sender title not set]" in text
            if greeting:
                assert greeting in text
        finally:
            browser.close()
