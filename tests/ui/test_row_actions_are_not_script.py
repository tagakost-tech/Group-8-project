"""A company name from a scraped page must never become code.

Every value in the contacts table is attacker-controlled. modules/search.py
takes `organization` from the page's own og:site_name or <title>, and `email`
from its body - so whoever owns a site the agent crawls chooses exactly what
lands in these cells.

The table escaped those values for HTML, which is correct where they are text.
Two buttons additionally spliced them into an onclick="" attribute, and HTML
escaping does not survive that: the browser decodes character references while
tokenising the attribute, so &quot; becomes a real quote INSIDE the JavaScript
and closes the string argument early. Everything after it is a new statement,
running with the operator's session on a page with no script-src CSP.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, _contacts_page, live_dashboard  # noqa: F401

pytestmark = pytest.mark.ui

# Closes markClient's string argument, ends the call, runs a statement of its
# own, then comments out the rest of the attribute so nothing is left dangling.
BREAKOUT = 'Acme"); window.__pwned = true; //'

HOSTILE = json.dumps(
    [
        {
            "id": 1,
            "email": "a@brand.de",
            "name": "A",
            "organization": BREAKOUT,
            "status": "pending",
            "lead_score": 50,
            "lead_tier": "B",
            "timestamp": "2026-01-01",
        }
    ]
)

HOSTILE_EMAIL = json.dumps(
    [
        {
            "id": 1,
            "email": 'x@y.de"); window.__pwned = true; //',
            "name": "A",
            "organization": "Legit GmbH",
            "status": "pending",
            "lead_score": 50,
            "lead_tier": "B",
            "timestamp": "2026-01-01",
        }
    ]
)


def _fired(page, selector):
    """True if the injected statement executed."""
    page.on("dialog", lambda d: d.dismiss())
    page.click(selector)
    page.wait_for_timeout(250)
    return page.evaluate("() => window.__pwned === true")


def test_a_hostile_company_name_does_not_execute(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, body=HOSTILE)
            assert not _fired(page, "button.action-btn.client"), (
                "a scraped company name executed as JavaScript in the "
                "operator's session - it can call any /api route they can"
            )
        finally:
            browser.close()


def test_a_hostile_email_does_not_execute(live_dashboard):  # noqa: F811
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, body=HOSTILE_EMAIL)
            assert not _fired(page, "button.action-btn.block"), (
                "a scraped email address executed as JavaScript"
            )
        finally:
            browser.close()


def test_a_quote_in_a_real_company_name_still_works(live_dashboard):  # noqa: F811
    """Not every quote is an attack. `The "Fix-It" Shop` is a legal name, and
    under the old markup its button threw a syntax error and did nothing -
    silently, so the operator would believe they had marked the company."""
    body = json.dumps(
        [
            {
                "id": 7,
                "email": "hello@fixit.de",
                "name": "R",
                "organization": 'The "Fix-It" Shop GmbH',
                "status": "pending",
                "lead_score": 50,
                "lead_tier": "B",
                "timestamp": "2026-01-01",
            }
        ]
    )
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, body=body)
            asked = []

            def _capture(d):
                asked.append(d.message)
                d.dismiss()

            page.on("dialog", _capture)
            page.click("button.action-btn.client")
            page.wait_for_timeout(250)
            assert asked, "the button did nothing - a quote broke the handler"
            assert "Fix-It" in asked[0], (
                f"the company name reached the operator mangled: {asked[0]!r}"
            )
        finally:
            browser.close()


def test_the_name_is_still_shown_as_text(live_dashboard):  # noqa: F811
    """Guard: hardening the buttons must not stop the cell showing the real
    name. textContent, not inner_text - the latter applies CSS."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, body=HOSTILE)
            cell = page.eval_on_selector(
                "#contactsTable tbody tr td:nth-child(4)", "e => e.textContent"
            )
            assert cell.strip() == BREAKOUT
        finally:
            browser.close()
