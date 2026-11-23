"""The contact detail panel: why a contact was or wasn't emailed.

It shows the explanation from /api/contacts/<id>/why, and it has to survive
the things the table does to its rows: sorting moved a panel away from its
contact (and its text turned the score sort into a text sort), the search box
matched and hid panels as if they were rows, and every 5-15 s refresh closed
every open panel. Every value it shows came from a scraped page or a server
reply, so none of it may run as markup.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

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


def _open(page, email):
    row = page.locator(_ROW, has_text=email)
    row.locator(".act-expand").click()
    page.wait_for_selector(".detail-row .why-headline", timeout=5000)
    return row


def _order(page):
    """Each direct row in order, as its email or 'panel:<id>'."""
    return page.evaluate(
        """() => [...document.querySelectorAll('#contactsTable > tbody > tr')]
              .map(r => r.classList.contains('detail-row')
                  ? 'panel:' + r.dataset.for
                  : (r.children[2] ? r.children[2].textContent : ''))"""
    )


def test_the_panel_shows_the_explanation(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            _open(page, "low@brand.de")
            text = page.inner_text(".detail-row")
            assert "Explained: low_score" in text
            assert "Score 12 below threshold 50" in text
            assert "Found" in text
        finally:
            browser.close()


def test_sorting_keeps_each_panel_under_its_contact(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            _open(page, "low@brand.de")
            page.click("#contactsTable thead th:first-child")  # score, ascending
            order = _order(page)
            assert order.index("panel:1") == order.index("low@brand.de") + 1, order
            scores = page.evaluate(
                """() => [...document.querySelectorAll('#contactsTable > tbody > tr')]
                      .filter(r => !r.classList.contains('detail-row'))
                      .map(r => Number(r.children[0].dataset.sort))"""
            )
            assert scores == sorted(scores), "the score column sorted as text"
        finally:
            browser.close()


def test_the_search_hides_a_panel_with_its_contact(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            _open(page, "low@brand.de")
            page.fill('.table-search input[data-table="contactsTable"]', "mid@brand")
            assert not page.is_visible(".detail-row")
            # Text that appears only inside the panel matches no contact.
            page.fill('.table-search input[data-table="contactsTable"]', "Explained")
            assert not page.is_visible(".detail-row")
            assert page.is_visible("#contactsTableNoMatch")
        finally:
            browser.close()


def test_an_open_panel_survives_a_refresh_without_refetching(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            row = _open(page, "low@brand.de")
            page.click("#refreshContacts")
            page.wait_for_timeout(600)
            assert page.is_visible(".detail-row .why-headline")
            assert row.locator(".act-expand").get_attribute("aria-expanded") == "true"
            assert page.why_requests == [1]
        finally:
            browser.close()


def test_a_status_change_refetches_the_explanation(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            _open(page, "low@brand.de")
            page.contact_rows[0]["status"] = "blocked"
            page.click("#refreshContacts")
            _wait_until(page, lambda: page.why_requests == [1, 1], "a refetch")
            page.wait_for_selector("text=Explained: blocked", timeout=5000)
        finally:
            browser.close()


def test_hostile_text_in_the_explanation_is_only_text(live_dashboard):  # noqa: F811
    hostile = '<img src=x onerror="window.__pwned=true">'
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            page.contact_rows[0]["_why"] = {
                "id": 1,
                "status": "low_score",
                "headline": hostile,
                "facts": [[hostile, hostile]],
                "timeline": [{"when": hostile, "what": hostile}],
            }
            page.contact_rows[0]["source_url"] = "javascript:window.__pwned=true"
            page.click("#refreshContacts")  # the panel reads the reloaded row
            page.wait_for_function(
                "() => document.querySelector('#contactsTable').textContent"
                ".includes('low@brand.de')"
            )
            page.wait_for_timeout(300)
            _open(page, "low@brand.de")
            page.wait_for_timeout(300)
            assert page.evaluate("() => window.__pwned === true") is False
            assert hostile in page.inner_text(".detail-row .why-headline")
            # Shown as text, never as a link that runs when clicked.
            assert "javascript:window.__pwned=true" in page.inner_text(".detail-row")
            assert page.locator(".detail-row a[href^='javascript']").count() == 0
        finally:
            browser.close()
