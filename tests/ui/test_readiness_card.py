"""The Overview's "Ready to send" card, and what the sidebar says with it.

The sidebar read "Agent running" in green while every live send was refused.
These tests stub /api/readiness with a blocked and a dry-run state, and load
the real route once, to check the card renders what the server says.

Marked ui, so it runs with `python -m pytest -m ui`.
"""

import json
import threading

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from test_dashboard_ui import _browser, _free_port

from conftest import TEST_DASHBOARD_LOGIN

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


def _check(label, ok, detail, blocking=True):
    return {"label": label, "ok": ok, "detail": detail, "blocking": blocking}


BLOCKED = {
    "checks": [
        _check(
            "Sender title",
            False,
            "SENDER_TITLE blank for giulia@suppleco.example - live first emails "
            "from it are refused",
        ),
        _check("PDF attachment present", True, "C:\\SUPPLECO\\assets\\profile.pdf"),
        _check(
            "Italian email template present",
            True,
            "templates/email_template_it.html",
            False,
        ),
        _check("Client list file", True, "C:\\SUPPLECO\\clients.xlsx (412 companies)"),
        _check("Competitor list file", None, "none configured", False),
        _check("Inbox read", None, "IMAP off"),
        _check(
            "Country lock",
            False,
            "ENABLED_COUNTRIES is blank - no live email may go out until counsel "
            "enables a country (decision L1.1)",
        ),
        _check("Legal identity (email footer)", True, "SUPPLECO S.r.l."),
        _check("Opt-out route", True, "https://outreach.example.test/unsubscribe"),
        _check("Legal footer in templates", True, "6 template(s) checked"),
    ],
    "can_send_live": False,
    "dry_run": False,
    "mailboxes": [
        {"address": "giulia@suppleco.example", "title": ""},
        {"address": "marco@suppleco.example", "title": "Export Sales"},
    ],
    "italian_followups": {"followup_1_it": True, "followup_2_it": False},
    "competitor": {"state": "off", "count": None},
}


def _page(browser, url, readiness=None):
    page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
    page.route(
        "**/api/deliverability",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"
        ),
    )
    page.readiness_calls = []
    if readiness is not None:

        def fulfil(route):
            page.readiness_calls.append(route.request.method)
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps(readiness)
            )

        page.route("**/api/readiness", fulfil)
    page.goto(url + "/classic")
    page.wait_for_selector("#readinessChecks tr", timeout=15000)
    return page


def _sidebar(page):
    return page.inner_text(".sidebar-footer span")


def test_a_blocked_state_names_the_reason(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, BLOCKED)
            headline = page.inner_text("#readinessHeadline")
            assert "Live first emails are refused" in headline, headline
            assert "SENDER_TITLE blank" in headline, headline

            rows = page.inner_text("#readinessChecks")
            for name in (
                "Sender title",
                "Company Profile PDF",
                "Italian first email",
                "Italian follow-ups 1 and 2",
                "Client list",
                "Competitor list",
                "Inbox read",
                "Enabled countries",
                "Legal identity",
                "Opt-out route",
                "Legal footer in templates",
            ):
                assert name in rows, f"{name} missing from {rows}"
            assert "followup_2_it.html" in rows and "English" in rows
            assert "412 companies" in rows
            assert page.query_selector_all("#readinessChecks .bad")

            box = page.inner_text("#readinessMailboxes")
            assert "giulia@suppleco.example" in box
            untitled = page.locator("#readinessMailboxes tr", has_text="giulia@")
            assert "no title" in untitled.inner_text()
            assert "Export Sales" in box

            page.wait_for_function(
                "() => document.querySelector('.sidebar-footer span').textContent"
                ".includes('Live sending blocked')",
                timeout=15000,
            )
            assert "see Ready to send" in _sidebar(page)
            assert not page.is_visible("#readinessDryRun")
        finally:
            browser.close()


def test_a_dry_run_says_so_on_the_card_and_in_the_sidebar(live):
    state = dict(BLOCKED, dry_run=True)
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, state)
            assert page.is_visible("#readinessDryRun")
            assert "DRY RUN - nothing is sent" in page.inner_text("#readinessDryRun")
            page.wait_for_function(
                "() => document.querySelector('.sidebar-footer span').textContent"
                ".includes('DRY RUN')",
                timeout=15000,
            )
        finally:
            browser.close()


def test_a_ready_state_says_a_live_email_can_go(live):
    state = dict(
        BLOCKED,
        checks=[dict(c, ok=c["ok"] is not False) for c in BLOCKED["checks"]],
        can_send_live=True,
        mailboxes=[{"address": "giulia@suppleco.example", "title": "Export Sales"}],
    )
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, state)
            assert (
                page.inner_text("#readinessHeadline")
                == "A live first email can go out"
            )
            page.wait_for_timeout(300)
            assert "blocked" not in _sidebar(page)
        finally:
            browser.close()


def test_a_partial_problem_is_named_without_saying_refused(live):
    """One titled mailbox among several, or one language whose text cannot
    go: first emails still go out, and the card says what is held back."""
    state = dict(
        BLOCKED,
        checks=[
            dict(c, ok=c["ok"] is not False, blocking=c["blocking"])
            for c in BLOCKED["checks"]
            if c["label"] != "Sender title"
        ]
        + [
            _check(
                "Sender title",
                False,
                "SENDER_TITLE blank for giulia@suppleco.example - first emails go "
                "out through marco@suppleco.example instead",
                False,
            )
        ],
        can_send_live=True,
        held_back=["English first emails are refused"],
    )
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, state)
            headline = page.inner_text("#readinessHeadline")
            assert headline == (
                "A live first email can go out, but English first emails are refused"
            ), headline
            assert "warn" in page.get_attribute("#readinessHeadline", "class")
            untitled = page.locator("#readinessMailboxes tr", has_text="giulia@")
            text = untitled.inner_text()
            assert "first emails go through a mailbox with one" in text, text
            assert "refused" not in text, text
            page.wait_for_timeout(300)
            assert "blocked" not in _sidebar(page)
        finally:
            browser.close()


def test_an_unreadable_competitor_list_is_the_refusal_named(live):
    """A configured competitor list that cannot be read refuses live sends,
    as the client list does, and the card says so once."""
    detail = "rivals.xlsx cannot be read - no competitor is screened by name"
    state = dict(
        BLOCKED,
        checks=[
            dict(c, ok=True if c["ok"] is False else c["ok"])
            for c in BLOCKED["checks"]
            if c["label"] != "Competitor list file"
        ]
        + [_check("Competitor list file", False, detail)],
        can_send_live=False,
        mailboxes=[{"address": "giulia@suppleco.example", "title": "Export Sales"}],
        competitor={"state": "unreadable", "count": None},
    )
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, state)
            headline = page.inner_text("#readinessHeadline")
            assert headline == (
                "Live first emails are refused: Competitor list: " + detail
            ), headline
            row = page.locator("#readinessChecks tr", has_text="Competitor list")
            assert row.locator(".bad").count() == 1
            assert row.inner_text().count("cannot be read") == 1, row.inner_text()
        finally:
            browser.close()


def test_check_again_asks_the_server(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live, BLOCKED)
            first = len(page.readiness_calls)
            page.click("#checkReadiness")
            page.wait_for_timeout(800)
            assert len(page.readiness_calls) == first + 1
        finally:
            browser.close()


def test_the_poll_asks_at_most_once_a_minute(live):
    """The status poll runs every 5 to 15 s; the doctor runs at most every 60."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.clock.install()
            calls = []

            def fulfil(route):
                calls.append(1)
                route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(BLOCKED)
                )

            page.route("**/api/readiness", fulfil)
            page.route(
                "**/api/deliverability",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body="{}"
                ),
            )
            page.goto(live + "/classic")
            page.wait_for_selector("#readinessChecks tr", timeout=15000)
            assert len(calls) == 1
            page.clock.run_for(46_000)  # three idle polls
            page.wait_for_timeout(300)
            assert len(calls) == 1, "the 15 s poll ran the doctor"
            page.clock.run_for(30_000)
            page.wait_for_timeout(300)
            assert len(calls) == 2, "a minute passed and nothing asked again"
        finally:
            browser.close()


def test_the_real_route_renders_the_card(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _page(browser, live)
            headline = page.inner_text("#readinessHeadline")
            assert headline.startswith(
                ("A live first email can go out", "Live first emails are refused")
            ), headline
            assert not page.query_selector("#readinessCard .is-error")
            assert len(page.query_selector_all("#readinessChecks tr")) == 12
        finally:
            browser.close()


def test_a_failed_check_is_shown_as_an_error_not_as_ready(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/readiness",
                lambda route: route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"error": "Could not run the checks just now"}),
                ),
            )
            page.goto(live + "/classic")
            page.wait_for_selector("#readinessCard .is-error", timeout=15000)
            assert "Could not run the checks" in page.inner_text("#readinessCard")
            assert "can go out" not in page.inner_text("#readinessHeadline")
        finally:
            browser.close()


def _sidebar_settles(page):
    page.wait_for_function(
        "() => document.querySelector('.sidebar-footer span').textContent.trim() !== ''",
        timeout=15000,
    )
    page.wait_for_timeout(500)
    return _sidebar(page), page.get_attribute(".status-indicator", "class")


def test_a_failed_check_never_turns_the_sidebar_green(live):
    """A 503 from /api/readiness set the state to null, which the sidebar
    read as nothing blocking: green, "System Online", while sending was
    still blocked."""
    answers = [(200, BLOCKED), (503, {"error": "Could not run the checks"})]

    def fulfil(route):
        status, body = answers[0] if len(answers) == 1 else answers.pop(0)
        route.fulfill(
            status=status, content_type="application/json", body=json.dumps(body)
        )

    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route("**/api/readiness", fulfil)
            page.goto(live + "/classic")
            page.wait_for_function(
                "() => document.querySelector('.sidebar-footer span').textContent"
                ".includes('Live sending blocked')",
                timeout=15000,
            )
            page.click("#checkReadiness")
            page.wait_for_selector("#readinessCard .is-error", timeout=15000)
            label, dot = _sidebar_settles(page)
            assert "Live sending blocked" in label, label
            assert "paused" in dot and "online" not in dot.split(), dot
        finally:
            browser.close()


def test_a_check_that_never_answered_says_so_in_the_sidebar(live):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/readiness",
                lambda route: route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"error": "Could not run the checks"}),
                ),
            )
            page.goto(live + "/classic")
            page.wait_for_selector("#readinessCard .is-error", timeout=15000)
            label, dot = _sidebar_settles(page)
            assert "System Online" not in label and "Agent running" not in label
            assert "could not be checked" in label, label
            assert "paused" in dot and "online" not in dot.split(), dot
        finally:
            browser.close()
