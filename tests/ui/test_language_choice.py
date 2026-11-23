"""The contacts page says why a contact gets its language, and lets the
operator choose it.

The language badge's title carries the server's reason ("Italian (its
company website ends in .it)"), and the detail panel offers Automatic,
Italian or English for a contact not yet emailed. A contact already emailed
keeps its first email's language, so it is told that instead of being
offered a control the server would refuse. The reason came from a scraped
page's evidence and a server reply, so none of it may run as markup, and the
control carries its contact as a number in a data attribute, not in script.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import (  # noqa: F401
    _browser,
    _contacts_page,
    _wait_until,
    live_dashboard,
)

pytestmark = pytest.mark.ui

_ROW = "#contactsTable > tbody > tr"
HOSTILE = '<img src=x onerror="window.__pwned=1">'

ROWS = json.dumps(
    [
        {
            "id": 1,
            "email": "acquisti@rossi-integratori.com",
            "name": "Maria Rossi",
            "organization": "Rossi Integratori",
            "status": "pending",
            "lead_score": 70,
            "timestamp": "2026-01-01",
            "language": "it",
            "language_reason": "Italian (its company website ends in .it)",
            "language_locked": False,
            "language_choice": None,
        },
        {
            "id": 2,
            "email": "einkauf@brand.de",
            "name": "Jan Weber",
            "organization": "Brand GmbH",
            "status": "emailed",
            "lead_score": 60,
            "timestamp": "2026-01-02",
            "language": "en",
            "language_reason": "English (the language of its first email)",
            "language_locked": True,
            "language_choice": None,
        },
        {
            "id": 3,
            "email": "erased-token",
            "name": None,
            "organization": None,
            "status": "erased",
            "lead_score": 0,
            "timestamp": "2026-01-03",
            "language": None,
            "language_reason": None,
            "language_locked": False,
        },
        {
            "id": 4,
            "email": "info@evil.com",
            "name": "Eve",
            "organization": "Evil",
            "status": "pending",
            "lead_score": 50,
            "timestamp": "2026-01-04",
            "language": "en",
            "language_reason": HOSTILE,
            "language_locked": False,
            "language_choice": "en",
        },
    ]
)


def _page(browser, url, answer=None):
    page = _contacts_page(browser, url, ROWS)
    page.language_posts = []

    def language_route(route):
        page.language_posts.append(
            (
                route.request.url,
                route.request.headers.get("content-type"),
                route.request.post_data,
            )
        )
        status, body = answer or (
            200,
            {"ok": True, "language": "en", "reason": "English (set by you)"},
        )
        route.fulfill(
            status=status, content_type="application/json", body=json.dumps(body)
        )

    page.route("**/api/contacts/*/language", language_route)
    return page


def _open(page, email):
    row = page.locator(_ROW, has_text=email)
    row.locator(".act-expand").click()
    page.wait_for_selector(".detail-row .why-headline", timeout=5000)
    return page.locator(".detail-row")


def test_the_badge_says_why(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            badge = page.locator(_ROW, has_text="rossi-integratori").locator(
                ".lang-badge"
            )
            assert badge.get_attribute("title") == (
                "Written in Italian: Italian (its company website ends in .it)"
            )
        finally:
            browser.close()


def test_an_unlocked_contact_offers_the_choice(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            panel = _open(page, "rossi-integratori")
            select = panel.locator("select.lang-choice")
            assert select.get_attribute("data-id") == "1"
            assert select.input_value() == "auto"
            assert [o.strip() for o in select.locator("option").all_inner_texts()] == [
                "Automatic",
                "Italian",
                "English",
            ]
        finally:
            browser.close()


def test_a_locked_contact_is_told_not_offered(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            panel = _open(page, "einkauf@brand.de")
            assert panel.locator("select.lang-choice").count() == 0
            assert "Language fixed by the first email" in panel.inner_text()
        finally:
            browser.close()


def test_an_erased_contact_gets_no_control(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            panel = _open(page, "erased-token")
            assert panel.locator("select.lang-choice").count() == 0
            assert "Language fixed" not in panel.inner_text()
        finally:
            browser.close()


def test_choosing_english_posts_english(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            panel = _open(page, "rossi-integratori")
            panel.locator("select.lang-choice").select_option("en")
            _wait_until(page, lambda: page.language_posts, "the POST")
            url, content_type, body = page.language_posts[0]
            assert url.endswith("/api/contacts/1/language")
            assert content_type == "application/json"
            assert json.loads(body) == {"language": "en"}
            page.wait_for_selector("text=English (set by you)", timeout=5000)
        finally:
            browser.close()


def test_a_refusal_is_shown_and_the_choice_put_back(live_dashboard):  # noqa: F811
    refusal = (
        409,
        {
            "error": "Already emailed in Italian: a contact keeps the language "
            "of its first email."
        },
    )
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard, answer=refusal)
            panel = _open(page, "rossi-integratori")
            select = panel.locator("select.lang-choice")
            select.select_option("it")
            page.wait_for_selector("text=Already emailed in Italian", timeout=5000)
            assert select.input_value() == "auto"
        finally:
            browser.close()


def test_a_hostile_reason_is_text(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            badge = page.locator(_ROW, has_text="info@evil.com").locator(".lang-badge")
            assert badge.get_attribute("title") == "Written in English: " + HOSTILE
            panel = _open(page, "info@evil.com")
            assert panel.locator("select.lang-choice").input_value() == "en"
            assert page.locator("#contactsTable img").count() == 0
            assert page.evaluate("() => window.__pwned") is None
        finally:
            browser.close()


def _control_style(page):
    return page.evaluate(
        """() => {
            const s = document.querySelector('.detail-row select.lang-choice');
            const cs = getComputedStyle(s);
            const r = s.getBoundingClientRect();
            const twin = document.querySelector('.editor-toolbar select');
            const t = getComputedStyle(twin);
            return {
                bg: cs.backgroundColor, twinBg: t.backgroundColor,
                color: cs.color, twinColor: t.color,
                font: cs.fontFamily, twinFont: t.fontFamily,
                border: cs.borderTopColor,
                height: r.height, right: r.right,
                width: document.documentElement.clientWidth,
                ring: cs.boxShadow,
                focused: document.activeElement === s,
            };
        }"""
    )


@pytest.mark.parametrize("width", [390, 1222, 1440])
def test_the_control_looks_like_the_pages_other_controls(
    live_dashboard,  # noqa: F811
    width,
):
    """A browser-default white box with black Arial text sat on the dark
    panel; the control must wear the same dress as the Template Editor's
    select, be tall enough to tap, and fit the phone screen."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            page.set_viewport_size({"width": width, "height": 900})
            _open(page, "rossi-integratori")
            style = _control_style(page)
            assert style["bg"] == style["twinBg"] != "rgb(255, 255, 255)"
            assert style["color"] == style["twinColor"]
            assert style["font"] == style["twinFont"]
            assert style["height"] >= 28
            assert style["right"] <= style["width"]
        finally:
            browser.close()


def test_a_chosen_language_is_marked_and_focus_is_visible(live_dashboard):  # noqa: F811
    """Automatic is the rule; a choice the operator made overrides it, so
    the control says so with the accent edge, and a keyboard user sees
    where focus is."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live_dashboard)
            _open(page, "rossi-integratori")
            automatic = _control_style(page)["border"]
            panels = _open(page, "info@evil.com")
            assert panels.count() == 2
            chosen = page.evaluate(
                "() => getComputedStyle(document.querySelectorAll("
                "'.detail-row select.lang-choice')[1]).borderTopColor"
            )
            assert chosen != automatic
            page.locator(".detail-row select.lang-choice").first.focus()
            page.keyboard.press("Shift+Tab")
            page.keyboard.press("Tab")
            style = _control_style(page)
            assert style["focused"]
            assert style["ring"] != "none"
        finally:
            browser.close()
