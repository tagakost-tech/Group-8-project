"""Two dashboard controls promised what the product does not do.

Send on a filtered row toasted "Queued for the next send", and the next pass
discarded the contact again. These tests press the button on a real row of a
real server: a stubbed reply would only test the stub.

The Spam / inbox test card could never send. It posts no channel, a live Direct
Send needs one, mail-tester.com shows no country, and if it did go through it
would store mail-tester's address as a prospect. So the card is off and says why.
Its send is stubbed here, so that a broken build cannot send anything.

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
    port = _free_port()
    server = make_server("127.0.0.1", port, main.create_dashboard_app(settings))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", main.Database(settings.get_abs_db_path())
    finally:
        server.shutdown()


def _open_tab(browser, url, tab):
    page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
    # This panel does live DNS lookups, which no test here is about.
    page.route(
        "**/api/deliverability",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"
        ),
    )
    page.goto(url)
    open_tab(page, tab)
    page.wait_for_selector(f"#{tab}.active", timeout=5000)
    return page


def _row_with_status(db, email, status):
    cid = db.add_contact(db.log_search("q"), email, organization="Brand GmbH")
    db.update_contact_status(cid, status)
    return cid


def _press_send(page, email):
    row = page.locator("#contactsTable tbody tr", has_text=email)
    row.locator(".action-btn.send").click()
    # A real round trip, queued behind everything the page asks for on load.
    page.wait_for_selector(".toast.show", timeout=15000)
    return page.inner_text(".toast")


@pytest.mark.parametrize(
    "status", ["low_score", "irrelevant", "ai_irrelevant", "unverified"]
)
def test_send_on_a_filtered_row_says_the_filters_run_again(live, status):
    url, db = live
    email = f"{status.replace('_', '-')}@brand.de"
    cid = _row_with_status(db, email, status)
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open_tab(browser, url, "tab-contacts")
            toast = _press_send(page, email)

            assert "the filters run again before anything is sent" in toast, toast
            assert "next send" not in toast, toast
            assert db.get_contact_by_id(cid)["status"] == "pending"
        finally:
            browser.close()


def test_send_on_a_failed_row_still_says_it_is_queued(live):
    """The control: no filter verdict stands against this row."""
    url, db = live
    _row_with_status(db, "failed@brand.de", "failed")
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open_tab(browser, url, "tab-contacts")
            assert _press_send(page, "failed@brand.de") == "Queued for the next send"
        finally:
            browser.close()


def test_the_spam_test_card_is_disabled_and_says_why(live):
    url, _db = live
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open_tab(browser, url, "tab-directsend")

            for control in ("#stOpenBtn", "#stAddr", "#stSendBtn"):
                assert page.is_disabled(control), (
                    f"{control} still offers a test that can never send"
                )
            page.wait_for_selector("#stDisabledReason", timeout=5000)
            reason = page.inner_text("#stDisabledReason")
            for why in ("a channel", "an enabled country", "stored as a contact"):
                assert why in reason, reason
            card = page.locator(".manual-card", has=page.locator("#spamTestForm"))
            assert "Sends a real message" not in card.inner_text()
            assert page.is_enabled("#msSendBtn"), (
                "only the card is off, not Direct Send"
            )
        finally:
            browser.close()


def test_the_address_box_does_not_look_ready_to_type_in(live):
    """A button dims when disabled; an input styled by this page did not."""
    url, _db = live
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open_tab(browser, url, "tab-directsend")
            opacity = page.eval_on_selector(
                "#stAddr", "e => getComputedStyle(e).opacity"
            )
            assert float(opacity) < 1, "a dead address box looks like a live one"
        finally:
            browser.close()


def test_pressing_the_disabled_send_posts_nothing(live):
    """With an address in the box the card's own handler would post at once."""
    url, _db = live
    posted = []

    def refuse(route):
        posted.append(route.request.post_data)
        route.fulfill(status=400, content_type="application/json", body="{}")

    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open_tab(browser, url, "tab-directsend")
            page.route("**/api/send-manual", refuse)
            page.eval_on_selector(
                "#stAddr", "e => { e.value = 'test-abc123@srv1.mail-tester.com'; }"
            )
            page.click("#stSendBtn", force=True)
            page.wait_for_timeout(250)

            assert posted == [], f"the disabled card still posted: {posted}"
        finally:
            browser.close()


PREVIEW = (
    "Draft looks good \u2014 would send to acquisti@rossi-integratori.it. Written in "
    "Italian. The Company Profile (Suppleco Company Profile 2026.pdf) is attached."
)


@pytest.mark.parametrize("width", [390, 1440])
def test_the_dry_run_shows_the_language_and_the_attachment(live, width):
    """The reply is shown whole: the language and the attachment sentence are
    what the operator, and in a demo the client, reads before a live send."""
    url, _db = live
    import json

    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN, viewport={"width": width, "height": 900})
            page.route(
                "**/api/deliverability",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body="{}"
                ),
            )
            page.route(
                "**/api/send-manual",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "message": PREVIEW}),
                ),
            )
            page.goto(url)
            if width < 900:
                page.click("#menuToggle")
                page.wait_for_timeout(320)
            open_tab(page, "tab-directsend")
            page.wait_for_selector("#tab-directsend.active", timeout=5000)
            page.fill("#msEmail", "acquisti@rossi-integratori.it")
            page.click("#msPreviewBtn")
            page.wait_for_function(
                "() => document.getElementById('msResult').textContent.includes('Draft')",
                timeout=15000,
            )
            assert page.inner_text("#msResult") == PREVIEW
            assert "ok" in page.get_attribute("#msResult", "class")
            assert not page.evaluate(
                "document.documentElement.scrollWidth >"
                " document.documentElement.clientWidth"
            ), f"the reply pushes the page sideways at {width}px"
        finally:
            browser.close()


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_a_second_email_is_asked_about_and_sent_only_on_a_yes(live, answer):
    """The route refuses a repeat with 409; the page asks the operator and
    sends again, with the confirmation, only when they say yes."""
    url, _db = live
    import json

    posted, asked = [], []

    def reply(route):
        body = json.loads(route.request.post_data)
        posted.append(body)
        if body.get("confirm_repeat"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "message": "Email sent again."}),
            )
        else:
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ok": False,
                        "already_emailed": True,
                        "last_sent_on": "10 November 2026",
                        "message": "Already emailed - nothing was sent now.",
                    }
                ),
            )

    def respond(dialog):
        asked.append(dialog.message)
        dialog.accept() if answer == "yes" else dialog.dismiss()

    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open_tab(browser, url, "tab-directsend")
            page.route("**/api/send-manual", reply)
            page.on("dialog", respond)
            page.fill("#msEmail", "acquisti@rossi-integratori.it")
            page.click("#msSendBtn")
            expected = "Email sent again." if answer == "yes" else "nothing was sent"
            page.wait_for_function(
                f"() => document.getElementById('msResult').textContent"
                f".includes({json.dumps(expected)})",
                timeout=15000,
            )

            question = (
                "This address was already emailed on 10 November 2026. "
                "Send another email?"
            )
            assert asked == [question]
            assert [b.get("confirm_repeat") for b in posted] == (
                [False, True] if answer == "yes" else [False]
            )
            assert page.is_enabled("#msSendBtn")
        finally:
            browser.close()


def test_a_preview_of_an_emailed_address_asks_nothing(live):
    """A dry run is never held back, so the page has nothing to ask."""
    url, _db = live
    import json

    asked = []
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _open_tab(browser, url, "tab-directsend")
            page.route(
                "**/api/send-manual",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "message": "Draft looks good."}),
                ),
            )
            page.on("dialog", lambda d: asked.append(d.message) or d.dismiss())
            page.fill("#msEmail", "acquisti@rossi-integratori.it")
            page.click("#msPreviewBtn")
            page.wait_for_function(
                "() => document.getElementById('msResult').textContent.includes('Draft')",
                timeout=15000,
            )
            assert asked == []
        finally:
            browser.close()
