"""The contacts table offers Block only where it can do something.

Block on a closed row (replied, client, opted out, bounced, erased) used to
overwrite the reason outreach ended. The server now keeps that status, and
the table leaves Block off rows the contacts list marks closed. A page loaded
before a contact closed can still show it; then the operator is told what the
server did, not what the page assumed, with no Undo when nothing was added.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, _contacts_page, live_dashboard  # noqa: F401

pytestmark = pytest.mark.ui


def _row(cid, email, status, closed):
    return {
        "id": cid,
        "email": email,
        "name": "A",
        "organization": "Brand GmbH",
        "status": status,
        "closed": closed,
        "lead_score": 50,
        "lead_tier": "B",
        "timestamp": "2026-01-01",
    }


def test_closed_rows_offer_no_block(live_dashboard):  # noqa: F811
    body = json.dumps(
        [
            _row(1, "open@brand.de", "pending", False),
            _row(2, "gone@brand.de", "unsubscribed", True),
            _row(3, "said-yes@brand.de", "responded", True),
        ]
    )
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, body=body)

            def blocks(email):
                row = page.locator("#contactsTable tbody tr", has_text=email)
                return row.locator(".action-btn.block").count()

            assert blocks("open@brand.de") == 1
            assert blocks("gone@brand.de") == 0
            assert blocks("said-yes@brand.de") == 0
        finally:
            browser.close()


def test_a_block_the_server_finds_closed_reports_what_it_did(live_dashboard):  # noqa: F811
    body = json.dumps([_row(1, "late@brand.de", "pending", False)])
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, body=body)
            page.route(
                "**/api/contacts/*/action",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "status": "ok",
                            "closed": "responded",
                            "message": "Already closed (responded): the status "
                            "stays, and the address is suppressed.",
                        }
                    ),
                ),
            )
            row = page.locator("#contactsTable tbody tr", has_text="late@brand.de")
            row.locator(".action-btn", has_text="Block").click()

            page.wait_for_selector(".toast.show", timeout=5000)
            assert "Already closed" in page.inner_text(".toast")
            assert page.query_selector(".toast.show .toast-action") is None, (
                "an Undo was offered for a block that added nothing"
            )
        finally:
            browser.close()
