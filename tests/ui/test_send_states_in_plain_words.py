"""Campaign Logs show send states in plain words.

getBadge showed preparing, sent_unconfirmed, rejected and dry_run with the
same blue 'pending' badge and the raw code: an email that may have reached the
person looked like one waiting in the queue.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json
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
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def _email(n, status, **kw):
    row = {
        "id": n,
        "timestamp": f"2026-11-0{n} 10:00:00",
        "recipient_email": f"buyer{n}@brand{n}.de",
        "sender_account": "giulia@suppleco.example",
        "subject": f"Subject {n}",
        "delivery_status": status,
        "notice_version": None,
        "has_attachment": 0,
        "attachment_filename": None,
        "smtp_response": None,
        "error_message": None,
        "opened_at": None,
    }
    row.update(kw)
    return row


EMAILS = [
    _email(
        1,
        "sent",
        notice_version="notice-li-1-it",
        has_attachment=1,
        attachment_filename="Suppleco Company Profile 2026.pdf",
    ),
    _email(
        2,
        "sent_unconfirmed",
        notice_version="notice-li-1",
        error_message="Connection lost after the server accepted the message",
    ),
    _email(3, "preparing"),
    _email(4, "rejected", smtp_response="550 5.1.1 <buyer4@brand4.de>: no such user"),
    _email(5, "dry_run"),
    _email(6, "failed", error_message="SMTP timeout"),
]

EXPECTED = {
    "Subject 1": ("sent", "badge-emailed"),
    "Subject 2": ("Sent, not confirmed - not repeated", "badge-emailed"),
    "Subject 3": ("Sending - not settled", "badge-pending"),
    "Subject 4": ("Refused by the server", "badge-failed"),
    "Subject 5": ("Preview only - not sent", "badge-pending"),
    "Subject 6": ("failed", "badge-failed"),
}


def _stub(page, pattern, body):
    page.route(
        pattern,
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)
        ),
    )


def _emails_page(browser, url):
    page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
    _stub(page, "**/api/deliverability", {})
    _stub(page, "**/api/emails*", EMAILS)
    page.goto(url + "/classic")
    open_tab(page, "tab-emails")
    page.wait_for_selector("#emailsTable tbody .status-badge", timeout=15000)
    return page


def _row(page, subject):
    return page.locator("#emailsTable tbody tr", has_text=subject)


def test_each_send_state_has_its_own_words_and_colour(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _emails_page(browser, live)
            for subject, (label, cls) in EXPECTED.items():
                badge = _row(page, subject).locator(".status-badge")
                assert badge.text_content() == label, subject
                assert cls in badge.get_attribute("class"), subject
            cells = page.eval_on_selector_all(
                "#emailsTable tbody td", "els => els.map(e => e.textContent)"
            )
            for code in ("sent_unconfirmed", "preparing", "rejected", "dry_run"):
                assert not any(code in c for c in cells), code
        finally:
            browser.close()


def test_the_badge_title_carries_the_server_s_words(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _emails_page(browser, live)
            title = _row(page, "Subject 4").locator(".status-badge").get_attribute(
                "title"
            )
            assert title == "550 5.1.1 <buyer4@brand4.de>: no such user"
            title = _row(page, "Subject 2").locator(".status-badge").get_attribute(
                "title"
            )
            assert title == "Connection lost after the server accepted the message"
            assert (
                _row(page, "Subject 3").locator(".status-badge").get_attribute("title")
                is None
            )
        finally:
            browser.close()


def test_the_subject_says_the_language_and_the_attachment(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _emails_page(browser, live)
            first = _row(page, "Subject 1")
            assert first.locator(".lang-badge").text_content() == "IT"
            pdf = first.locator(".file-badge")
            assert pdf.text_content() == "PDF"
            assert pdf.get_attribute("title") == "Suppleco Company Profile 2026.pdf"
            assert _row(page, "Subject 2").locator(".lang-badge").text_content() == "EN"
            assert _row(page, "Subject 3").locator(".lang-badge").count() == 0
            assert _row(page, "Subject 3").locator(".file-badge").count() == 0
            assert len(page.query_selector_all("#emailsTable thead th")) == 6
        finally:
            browser.close()


def test_contact_badges_for_existing_statuses_are_unchanged(live):
    contacts = [
        {"id": i, "email": f"c{i}@brand.de", "status": s, "lead_score": 50}
        for i, s in enumerate(
            ["pending", "emailed", "failed", "blocked", "low_score", "bounced"], 1
        )
    ]
    before = {
        "pending": "badge-pending",
        "emailed": "badge-emailed",
        "failed": "badge-failed",
        "blocked": "badge-failed",
        "low_score": "badge-pending",
        "bounced": "badge-pending",
    }
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            _stub(page, "**/api/deliverability", {})
            _stub(page, "**/api/contacts*", contacts)
            page.goto(live + "/classic")
            open_tab(page, "tab-contacts")
            page.wait_for_selector("#contactsTable tbody .status-badge", timeout=15000)
            for c in contacts:
                badge = page.locator(
                    "#contactsTable tbody tr", has_text=c["email"]
                ).locator(".status-badge")
                assert badge.text_content() == c["status"]
                assert before[c["status"]] in badge.get_attribute("class")
        finally:
            browser.close()
