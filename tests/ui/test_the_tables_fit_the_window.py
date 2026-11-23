"""Every column of the Contacts and Campaigns tables is on screen at the
owner's window widths.

At a client demo the owner's 1222 px window showed Contacts without Status,
Date Found and Actions, and Campaigns without Subject, Status and Opens: the
long cells (an address, a company name, a subject line) set the table wider
than its card, and the rest could only be reached by noticing the table
scrolled sideways. The long cells now shrink with an ellipsis and keep the
whole text in their title.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import (  # noqa: F401
    _browser,
    _contacts_page,
    live_dashboard,
)

from conftest import TEST_DASHBOARD_LOGIN, open_tab

pytestmark = pytest.mark.ui

WIDTHS = [(1222, 780), (1440, 900)]

LONG_EMAIL = "francesca.marchetti-dellavalle@laboratori-integratori-naturali.it"
LONG_ORG = "Laboratorio Nutraceutico Integratori Naturali del Benessere S.r.l."
LONG_SUBJECT = (
    "Produzione conto terzi di integratori alimentari per il vostro brand: "
    "capsule, compresse, polveri e liquidi"
)
LONG_SENDER = "commerciale.estero@suppleco-integratori.example"

CONTACTS = json.dumps(
    [
        {
            "id": 1,
            "email": LONG_EMAIL,
            "name": "Francesca Marchetti Dellavalle",
            "organization": LONG_ORG,
            "status": "pending",
            "lead_score": 88,
            "lead_tier": "A",
            "timestamp": "2026-11-12 09:41:23",
            "language": "it",
            "language_reason": "Italian (its company website ends in .it)",
            "language_locked": False,
            "language_choice": None,
        },
        {
            "id": 2,
            "email": "purchasing.department@wellness-supplements-europe.co.uk",
            "name": "Jonathan Whitfield-Saunders",
            "organization": "Wellness Supplements Europe Holdings Limited",
            "status": "ai_irrelevant",
            "lead_score": 12,
            "discard_reason": "Not a supplement brand",
            "timestamp": "2026-11-11 17:02:45",
            "language": "en",
            "language_reason": "English (its company website ends in .uk)",
        },
    ]
)

EMAILS = json.dumps(
    [
        {
            "timestamp": "2026-11-12 10:15:07",
            "recipient_email": LONG_EMAIL,
            "sender_account": LONG_SENDER,
            "subject": LONG_SUBJECT,
            "notice_version": "2026-11-it",
            "has_attachment": True,
            "attachment_filename": "SUPPLECO-presentazione.pdf",
            "delivery_status": "delivered",
            "opened_at": "2026-11-12 11:00:00",
        },
        {
            "timestamp": "2026-11-12 10:16:44",
            "recipient_email": "purchasing.department@wellness-supplements-europe.co.uk",
            "sender_account": LONG_SENDER,
            "subject": "Contract manufacturing of food supplements for your brand",
            "notice_version": "2026-11-en",
            "has_attachment": False,
            "delivery_status": "failed",
            "error_message": "550 5.1.1 mailbox unavailable",
            "opened_at": None,
        },
    ]
)

# Each header whose right edge lies past its table's visible box, by name.
_OFF_SCREEN_JS = """(tableId) => {
  const table = document.getElementById(tableId);
  const box = table.closest('.table-responsive').getBoundingClientRect();
  return [...table.querySelectorAll('thead th')]
    .filter(th => th.getBoundingClientRect().right > box.right + 0.5)
    .map(th => th.textContent.trim());
}"""


def _emails_page(browser, base_url, width, height):
    page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN, viewport={"width": width, "height": height})
    page.route(
        "**/api/emails*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=EMAILS
        ),
    )
    page.goto(base_url + "/classic")
    open_tab(page, "tab-emails")
    page.wait_for_selector("#emailsTable tbody tr td .status-badge", timeout=8000)
    return page


@pytest.mark.parametrize("width,height", WIDTHS)
def test_every_contacts_column_is_on_screen(live_dashboard, width, height):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, CONTACTS)
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(100)
            off = page.evaluate(_OFF_SCREEN_JS, "contactsTable")
            assert not off, f"Contacts headers off screen at {width}px: {off}"
        finally:
            browser.close()


@pytest.mark.parametrize("width,height", WIDTHS)
def test_every_campaigns_column_is_on_screen(live_dashboard, width, height):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _emails_page(browser, live_dashboard, width, height)
            off = page.evaluate(_OFF_SCREEN_JS, "emailsTable")
            assert not off, f"Campaigns headers off screen at {width}px: {off}"
        finally:
            browser.close()


_COLUMN_WIDTHS_JS = """(tableId) => Object.fromEntries(
  [...document.getElementById(tableId).querySelectorAll('thead th')]
    .map(th => [th.textContent.trim(), th.getBoundingClientRect().width]))"""


def test_the_address_has_room_to_be_read_at_the_owners_width(live_dashboard):  # noqa: F811
    """Fitting every column left the address about three letters wide at
    1222px, next to a date and time on one 150px line. The date now sits
    above the time and the address gets what that frees."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, CONTACTS)
            page.set_viewport_size({"width": 1222, "height": 780})
            page.wait_for_timeout(100)
            widths = page.evaluate(_COLUMN_WIDTHS_JS, "contactsTable")
            assert widths["Email"] >= 140, widths
            # The sector column is clipped to its own width, so the address keeps
            # the room it needs.
            assert widths["Sector"] <= 180, widths
            page.close()

            page = _emails_page(browser, live_dashboard, 1222, 780)
            widths = page.evaluate(_COLUMN_WIDTHS_JS, "emailsTable")
            assert widths["Date"] <= 100, widths
        finally:
            browser.close()


def test_a_shortened_cell_keeps_its_whole_text_in_its_title(live_dashboard):  # noqa: F811
    """An ellipsis that hides the end of an address with no way to read it
    would trade one missing column for missing text."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, CONTACTS)
            page.set_viewport_size({"width": 1222, "height": 780})
            titles = page.evaluate(
                """() => [...document.querySelectorAll(
                     '#contactsTable tbody tr:first-child td')]
                   .map(td => td.getAttribute('title'))"""
            )
            assert LONG_EMAIL in titles
            assert LONG_ORG in titles
            page.close()

            page = _emails_page(browser, live_dashboard, 1222, 780)
            titles = page.evaluate(
                """() => [...document.querySelectorAll(
                     '#emailsTable tbody tr:first-child td')]
                   .map(td => td.getAttribute('title'))"""
            )
            assert LONG_EMAIL in titles
            assert LONG_SENDER in titles
            assert LONG_SUBJECT in titles
        finally:
            browser.close()


def test_an_opened_contact_shows_its_whole_panel(live_dashboard):  # noqa: F811
    """The detail row spans the table: while the table was wider than its
    card, opening a pending contact scrolled the explanation and the
    language control off the left edge."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, CONTACTS)
            page.set_viewport_size({"width": 1222, "height": 780})
            page.click("#contactsTable tbody tr:first-child .act-expand")
            page.wait_for_selector("select.lang-choice", timeout=5000)
            page.wait_for_timeout(200)
            outside = page.evaluate(
                """() => {
                  const r = document.querySelector('#tab-contacts .table-responsive')
                    .getBoundingClientRect();
                  return ['.detail-body', 'select.lang-choice']
                    .filter(sel => {
                      const b = document.querySelector(sel).getBoundingClientRect();
                      return b.left < r.left - 0.5 || b.right > r.right + 0.5;
                    });
                }"""
            )
            assert not outside, f"outside the visible table: {outside}"
        finally:
            browser.close()
