"""The Template Editor opens and saves the English follow-ups too.

A walk-through found that only the first emails and the Italian follow-ups
could be opened: the list had no English follow-up, and /api/template refused
followup_1 and followup_2 as unknown. All six texts now open and save, under
the same footer guard. Against the real routes, over a copy of the
repository's templates, so no test here writes the real ones.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import shutil
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, _free_port

from conftest import TEST_DASHBOARD_LOGIN, open_tab

pytestmark = pytest.mark.ui

ALL_SIX = [
    "email_template",
    "followup_1",
    "followup_2",
    "email_template_it",
    "followup_1_it",
    "followup_2_it",
]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    from werkzeug.serving import make_server

    import main
    from conftest import _browser_dashboard_settings

    root = tmp_path_factory.mktemp("root")
    shutil.copytree(Path(main.__file__).resolve().parent / "templates", root / "templates")
    settings = _browser_dashboard_settings(tmp_path_factory.mktemp("uidb"))
    port = _free_port()
    # The app takes its page and scripts from the repository; only the
    # template route, which reads PROJECT_ROOT per request, sees the copy.
    server = make_server("127.0.0.1", port, main.create_dashboard_app(settings))
    real_root = main.PROJECT_ROOT
    main.PROJECT_ROOT = root
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", root / "templates"
    finally:
        server.shutdown()
        main.PROJECT_ROOT = real_root


def _open(browser, url):
    page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
    page.route(
        "**/api/deliverability",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"
        ),
    )
    page.goto(url)
    open_tab(page, "tab-template")
    page.wait_for_function(
        "() => document.getElementById('templateEditor').value.length > 0",
        timeout=15000,
    )
    return page


def _as_in_a_textarea(path):
    """The file's text as the editor holds it: a textarea's line ends are
    LF, and the copied file may have CR LF."""
    return "\n".join(path.read_text(encoding="utf-8").splitlines())


def _choose(page, name, folder):
    page.select_option("#templateName", name)
    page.wait_for_function(
        "t => !document.getElementById('saveTemplateBtn').disabled"
        " && document.getElementById('templateEditor').value.trimEnd() === t",
        arg=_as_in_a_textarea(folder / f"{name}.html").rstrip(),
        timeout=15000,
    )


def test_all_six_texts_are_in_the_list(live):
    url, _folder = live
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open(browser, url)
            options = page.eval_on_selector_all(
                "#templateName option", "els => els.map(e => e.value)"
            )
            assert sorted(options) == sorted(ALL_SIX)
        finally:
            browser.close()


@pytest.mark.parametrize("name", ["followup_1", "followup_2"])
def test_an_english_follow_up_opens_and_saves(live, name):
    url, folder = live
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open(browser, url)
            _choose(page, name, folder)
            assert page.inner_text("#templateNote") == ""
            edited = page.input_value("#templateEditor") + "<p>edited</p>"
            page.fill("#templateEditor", edited)
            page.click("#saveTemplateBtn")
            page.wait_for_selector(".toast.show", timeout=15000)
            assert "error" not in page.get_attribute("#toast", "class")
            assert (folder / f"{name}.html").read_text(encoding="utf-8") == edited
        finally:
            browser.close()


def test_an_english_follow_up_keeps_the_footer_guard(live):
    url, folder = live
    before = (folder / "followup_2.html").read_bytes()
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open(browser, url)
            _choose(page, "followup_2", folder)
            text = page.input_value("#templateEditor")
            page.fill("#templateEditor", text.replace("{{legal_footer}}", ""))
            page.click("#saveTemplateBtn")
            page.wait_for_selector(".toast.show", timeout=15000)
            assert "error" in page.get_attribute("#toast", "class")
            assert "{{legal_footer}}" in page.inner_text(".toast")
            assert (folder / "followup_2.html").read_bytes() == before
        finally:
            browser.close()
