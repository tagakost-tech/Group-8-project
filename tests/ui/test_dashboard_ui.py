"""Browser-based UI tests for the dashboard (Playwright / webapp-testing skill).

Marked ``ui`` and excluded from the default run. Enable with:
    pip install playwright && python -m playwright install chromium
    python -m pytest -m ui
"""

import json
import socket
import threading
from urllib.parse import urlparse

import pytest

# Skip the whole module if Playwright isn't installed.
pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright

from conftest import _THIS_MACHINE, TEST_DASHBOARD_LOGIN, browser_reached, open_tab

pytestmark = pytest.mark.ui


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live_dashboard(tmp_path_factory):
    from werkzeug.serving import make_server

    import main
    from conftest import _browser_dashboard_settings

    folder = tmp_path_factory.mktemp("uidb")
    port = _free_port()  # ephemeral port (safe from Chromium's blocked-port list)
    app = main.create_dashboard_app(_browser_dashboard_settings(folder))
    server = make_server("127.0.0.1", port, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


# Chromium resolves no host name but this machine's, so the page can reach no
# other server by name.
_THIS_MACHINE_ONLY = "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1"


def _leaves_this_machine(url):
    parts = urlparse(url)
    return parts.scheme in ("http", "https", "ws", "wss") and (
        parts.hostname not in _THIS_MACHINE
    )


def _report_requests_to_other_machines(target):
    """Every request the page makes to another machine fails the running test
    (conftest._no_network), even one the page copes without, or one to a bare
    IP address, which the resolver rule does not stop."""

    def seen(request):
        if _leaves_this_machine(request.url):
            browser_reached(f"Chromium {request.method} {request.url}")

    target.on("request", seen)
    return target


def _browser(p):
    try:
        browser = p.chromium.launch(headless=True, args=[_THIS_MACHINE_ONLY])
    except Exception as e:  # noqa: BLE001 - any launch failure means skip
        pytest.skip(f"Chromium not available: {e}")
    new_context, new_page = browser.new_context, browser.new_page
    browser.new_context = lambda *a, **k: _report_requests_to_other_machines(
        new_context(*a, **k)
    )
    browser.new_page = lambda *a, **k: _report_requests_to_other_machines(
        new_page(*a, **k)
    )
    return browser


def test_insights_tab_renders(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.goto(live_dashboard)
            page.wait_for_load_state("networkidle")
            open_tab(page, "tab-insights")
            page.wait_for_selector("#tab-insights.active", timeout=5000)
            # Wait for the CONTENT, not a fixed nap: /api/config queues behind
            # /api/deliverability, which does live DNS lookups (SPF/DMARC/
            # blocklist) server-side. On a machine where DNS answers slowly an
            # 800ms sleep loses that race and reports 0 chips while the data
            # is fine. Waiting on the selector still fails if chips never
            # render - it just stops failing when they render late.
            page.wait_for_selector("#featuresGrid .feature-chip", timeout=15000)
            page.wait_for_selector("#sequenceStats .mini-stat", timeout=15000)

            assert len(page.query_selector_all("#featuresGrid .feature-chip")) >= 8
            assert len(page.query_selector_all("#abTable tbody tr")) >= 1
            assert len(page.query_selector_all("#sequenceStats .mini-stat")) == 4
        finally:
            browser.close()


def test_tab_navigation(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.goto(live_dashboard)
            page.wait_for_load_state("networkidle")
            for tab in [
                "tab-contacts",
                "tab-emails",
                "tab-searches",
                "tab-insights",
                "tab-overview",
            ]:
                open_tab(page, tab)
                page.wait_for_selector(f"#{tab}.active", timeout=5000)
        finally:
            browser.close()


# ── Keyboard navigation ─────────────────────────────────────────────────────
# The sidebar was seven bare <li> with a click handler: no tab stop, no role,
# no state exposed. The whole navigation was mouse-only.


def test_sidebar_is_operable_by_keyboard(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.goto(live_dashboard)
            page.wait_for_load_state("networkidle")

            first = page.locator(".nav-links li[aria-selected='true']")
            assert first.get_attribute("data-target") == "tab-overview"
            first.focus()

            # Review comes right after Overview: it is where people decide
            # what the agent may send, the tab they work in.
            page.keyboard.press("ArrowDown")
            page.wait_for_selector("#tab-review.active", timeout=5000)
            # Selection must carry focus with it, or the next arrow key goes
            # nowhere and the user is stranded on one item.
            focused = page.evaluate("document.activeElement.dataset.target")
            assert focused == "tab-review"

            page.keyboard.press("End")
            page.wait_for_selector("#tab-searches.active", timeout=5000)
            page.keyboard.press("ArrowDown")  # wraps back to the top
            page.wait_for_selector("#tab-overview.active", timeout=5000)
        finally:
            browser.close()


def test_the_tablist_is_a_single_tab_stop(live_dashboard):
    """Roving tabindex: seven separate stops would make Tab useless for
    reaching the panel content behind the nav."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.goto(live_dashboard)
            page.wait_for_load_state("networkidle")
            stops = page.eval_on_selector_all(
                ".nav-links li", "els => els.map(e => e.tabIndex)"
            )
            assert stops.count(0) == 1, f"expected exactly one tab stop, got {stops}"
            assert stops.count(-1) == len(stops) - 1
        finally:
            browser.close()


def test_icon_only_buttons_have_accessible_names(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.goto(live_dashboard)
            page.wait_for_load_state("networkidle")
            unnamed = page.eval_on_selector_all(
                ".btn-icon",
                "els => els.filter(e => !e.getAttribute('aria-label')).length",
            )
            assert unnamed == 0
        finally:
            browser.close()


# ── Table states ────────────────────────────────────────────────────────────
# "Loaded and found nothing", "not loaded yet" and "the fetch failed" all
# rendered as the same blank rectangle.


def _state_text(page, table_id):
    el = page.query_selector(f"#{table_id} .state-cell")
    return el.inner_text() if el else ""


def test_an_empty_table_says_it_is_empty(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/contacts*",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body="[]"
                ),
            )
            page.goto(live_dashboard)
            open_tab(page, "tab-contacts")
            page.wait_for_selector("#contactsTable .empty-state", timeout=5000)
            assert "No contacts" in _state_text(page, "contactsTable")
            assert not page.query_selector("#contactsTable .empty-state.is-error")
        finally:
            browser.close()


def test_a_failed_fetch_is_distinguishable_from_an_empty_table(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route("**/api/contacts*", lambda route: route.abort())
            page.goto(live_dashboard)
            open_tab(page, "tab-contacts")
            page.wait_for_selector("#contactsTable .empty-state.is-error", timeout=5000)
            assert "Could not load" in _state_text(page, "contactsTable")
        finally:
            browser.close()


ONE_CONTACT = (
    '[{"id": 1, "email": "a@b.de", "name": "Ann", "organization": "B",'
    ' "status": "pending", "lead_score": 50, "fit_score": 50, "timestamp": "2026-01-01"}]'
)


def test_a_filter_matching_nothing_explains_itself(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/contacts*",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=ONE_CONTACT
                ),
            )
            page.goto(live_dashboard)
            open_tab(page, "tab-contacts")
            page.wait_for_selector("#contactsTable tbody tr td", timeout=5000)

            search = ".table-search input[data-table='contactsTable']"
            page.fill(search, "zzzznothing")
            page.wait_for_selector("#contactsTableNoMatch", timeout=5000)

            # Clearing it must restore the row and drop the message.
            page.fill(search, "")
            page.wait_for_selector(
                "#contactsTableNoMatch", state="detached", timeout=5000
            )
            visible = page.eval_on_selector_all(
                "#contactsTable tbody tr",
                "els => els.filter(e => e.style.display !== 'none').length",
            )
            assert visible == 1
        finally:
            browser.close()


# ── Endpoints the backend served but nothing consumed ───────────────────────
# /api/activity, /api/retries, /api/blacklist and /api/learning were all live
# and all invisible: the agent collected the data and the dashboard never
# asked for it.

ACTIVITY_ROWS = json.dumps(
    [
        {
            "id": 3,
            "timestamp": "2026-01-01 10:00:03",
            "action": "SMTP_ERROR",
            "details": "451 temporary",
            "status": "error",
        },
        {
            "id": 2,
            "timestamp": "2026-01-01 10:00:02",
            "action": "EMAIL_SENT",
            "details": "a@b.de",
            "status": "success",
        },
        {
            "id": 1,
            "timestamp": "2026-01-01 10:00:01",
            "action": "AGENT_START",
            "details": "run started",
            "status": "info",
        },
    ]
)


def test_the_activity_feed_renders_and_is_ordered_oldest_first(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/activity*",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=ACTIVITY_ROWS
                ),
            )
            page.goto(live_dashboard)
            page.wait_for_selector(".act-row", timeout=8000)

            actions = page.eval_on_selector_all(
                ".act-action", "els => els.map(e => e.textContent)"
            )
            # The API returns newest first; a feed read top-to-bottom wants the
            # log order, or "what happened next" reads backwards.
            assert actions == ["AGENT_START", "EMAIL_SENT", "SMTP_ERROR"]

            # Severity has to be visible without reading every line.
            assert page.query_selector(".act-row.act-error")
            assert page.query_selector(".act-row.act-ok")
        finally:
            browser.close()


def test_an_empty_activity_feed_says_so(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/activity*",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body="[]"
                ),
            )
            page.goto(live_dashboard)
            page.wait_for_selector("#activityFeed .empty-state", timeout=8000)
            assert "No activity yet" in page.inner_text("#activityFeed")
        finally:
            browser.close()


def test_the_retry_queue_is_visible_and_flags_exhausted_sends(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/retries",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "pending": 4,
                            "succeeded": 9,
                            "exhausted": 2,
                            "cancelled": 3,
                            "total": 18,
                        }
                    ),
                ),
            )
            page.goto(live_dashboard)
            open_tab(page, "tab-insights")
            page.wait_for_selector("#retryStats .mini-stat", timeout=8000)

            nums = page.eval_on_selector_all(
                "#retryStats .num", "els => els.map(e => e.textContent)"
            )
            # Cancelled retries have their own count, so the parts add up.
            assert nums == ["4", "9", "2", "3", "18"]
            # Sends that used every attempt are gone for good; that must not
            # render identically to a zero.
            assert page.query_selector("#retryStats .num-bad")
            assert "will not be tried again" in page.inner_text("#retryNote")
        finally:
            browser.close()


def test_a_quiet_retry_queue_does_not_raise_an_alarm(live_dashboard):
    """Guard: the flagging must key off the value, not fire unconditionally."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/retries",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {"pending": 0, "succeeded": 3, "exhausted": 0, "total": 3}
                    ),
                ),
            )
            page.goto(live_dashboard)
            open_tab(page, "tab-insights")
            page.wait_for_selector("#retryStats .mini-stat", timeout=8000)
            assert not page.query_selector("#retryStats .num-bad")
            assert not page.query_selector("#retryStats .num-warn")
            assert "Nothing waiting" in page.inner_text("#retryNote")
        finally:
            browser.close()


def test_the_learning_snapshot_is_visible(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/learning",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "top_verticals": [
                                {
                                    "name": "sports nutrition",
                                    "weight": 0.9,
                                    "verified": 12,
                                    "responded": 3,
                                    "searches": 8,
                                },
                                {
                                    "name": "pharma",
                                    "weight": 0.3,
                                    "verified": 2,
                                    "responded": 0,
                                    "searches": 5,
                                },
                            ],
                            "top_countries": [
                                {
                                    "name": "Germany",
                                    "weight": 0.7,
                                    "verified": 9,
                                    "responded": 2,
                                    "searches": 6,
                                }
                            ],
                            "avoid": ["cosmetics"],
                        }
                    ),
                ),
            )
            page.goto(live_dashboard)
            open_tab(page, "tab-insights")
            page.wait_for_selector("#learningPanel .rank-row", timeout=8000)

            names = page.eval_on_selector_all(
                "#learningPanel .rank-name", "els => els.map(e => e.textContent)"
            )
            assert "sports nutrition" in names
            assert "Germany" in names
            # The bar is scaled against the strongest weight, so the top row
            # fills it and a weaker one does not.
            widths = page.eval_on_selector_all(
                "#learningPanel .rank-fill", "els => els.map(e => e.style.width)"
            )
            assert widths[0] == "100%"
            assert widths[1] != "100%"
            # inner_text applies CSS, and the chips are text-transform:capitalize.
            assert "cosmetics" in page.inner_text(".learning-avoid").lower()
        finally:
            browser.close()


def test_the_blocklist_recheck_button_shows_per_list_results(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.route(
                "**/api/blacklist",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "domain": "suppleco.eu",
                            "status": "listed",
                            "listed_on": ["zen.spamhaus.org"],
                            "checked": 2,
                            "total": 3,
                            "results": [
                                {"list": "zen.spamhaus.org", "status": "listed"},
                                {"list": "bl.spamcop.net", "status": "clean"},
                                {"list": "b.barracuda.org", "status": "unavailable"},
                            ],
                        }
                    ),
                ),
            )
            page.goto(live_dashboard)
            open_tab(page, "tab-insights")
            page.wait_for_selector("#recheckBlacklist", timeout=8000)
            page.click("#recheckBlacklist")
            page.wait_for_selector("#blacklistDetail .kv-table", timeout=8000)

            detail = page.inner_text("#blacklistDetail")
            # WHICH list is the problem is the whole point; the cached summary
            # in the panel above only ever said "listed".
            assert "zen.spamhaus.org" in detail
            assert "bl.spamcop.net" in detail
            assert "2 of 3 blocklists answered" in detail
            assert page.query_selector("#blacklistDetail .bad")
        finally:
            browser.close()


# ── Interactivity ───────────────────────────────────────────────────────────
# The tables arrived in one fixed server order with a single text box over
# them, and the only row actions were Block and Send.

CONTACTS = json.dumps(
    [
        {
            "id": 1,
            "email": "low@brand.de",
            "name": "Zoe Adams",
            "organization": "Z Ltd",
            "status": "low_score",
            "lead_score": 12,
            "fit_score": 12,
            "lead_tier": "D",
            "discard_reason": "Score 12 below threshold 50",
            "timestamp": "2026-01-03",
        },
        {
            "id": 2,
            "email": "hot@brand.de",
            "name": "Adam Zeller",
            "organization": "A GmbH",
            "status": "pending",
            "lead_score": 91,
            "fit_score": 91,
            "lead_tier": "A",
            "timestamp": "2026-01-01",
        },
        {
            "id": 3,
            "email": "mid@brand.de",
            "name": "Mia Best",
            "organization": "M SARL",
            "status": "pending",
            "lead_score": 55,
            "fit_score": 55,
            "lead_tier": "B",
            "timestamp": "2026-01-02",
        },
    ]
)


def _contacts_page(browser, live_dashboard, body=CONTACTS):
    """A page whose stubbed API behaves like the real one.

    Two things this must model, or the tests stop describing production:
      * the status chips read their totals from /api/stats, not from the rows
        on screen - so stubbing only /api/contacts leaves the chips empty;
      * /api/contacts filters SERVER-side via ?status=, so a chip click is a
        refetch, not a local sieve.
    """
    rows = json.loads(body)
    counts = {}
    for r in rows:
        counts[r.get("status") or "unknown"] = (
            counts.get(r.get("status") or "unknown", 0) + 1
        )

    # A context passed in carries its own login; only a browser takes one here.
    if hasattr(browser, "new_context"):
        page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
    else:
        page = browser.new_page()
    page.route(
        "**/api/stats",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "total_contacts": len(rows),
                    "emails_sent": 0,
                    "contact_status_counts": counts,
                }
            ),
        ),
    )

    def contacts_route(route):
        from urllib.parse import parse_qs, urlparse

        want = parse_qs(urlparse(route.request.url).query).get("status", [None])[0]
        subset = [r for r in rows if not want or r.get("status") == want]
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(subset)
        )

    page.route("**/api/contacts*", contacts_route)

    # The detail panel's explanation, built from the same rows. Requests are
    # recorded so a test can tell a refetch from a reused panel, and rows can
    # be changed between refreshes through page.contact_rows.
    page.contact_rows = rows
    page.why_requests = []

    def why_route(route):
        cid = int(route.request.url.rstrip("/").split("/")[-2])
        page.why_requests.append(cid)
        row = next((r for r in rows if r["id"] == cid), None)
        if row is None:
            route.fulfill(status=404, content_type="application/json", body="{}")
            return
        facts = [["Status", row.get("status")]]
        if row.get("discard_reason"):
            facts.append(["Reason", row["discard_reason"]])
        if row.get("language") == "it":
            facts.append(["Language", "Italian (its address ends in .it)"])
        elif row.get("language") == "en":
            facts.append(["Language", "English"])
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                row.get("_why")
                or {
                    "id": cid,
                    "status": row.get("status"),
                    "headline": f"Explained: {row.get('status')}",
                    "facts": facts,
                    "timeline": [{"when": "2026-01-01 10:00:00", "what": "Found"}],
                }
            ),
        )

    page.route("**/api/contacts/*/why", why_route)
    page.goto(live_dashboard)
    open_tab(page, "tab-contacts")
    page.wait_for_selector("#contactsTable tbody tr td", timeout=8000)
    return page


# Rows the operator can actually see: not the empty/error placeholder, not an
# expanded detail panel, not one hidden by the search box.
_VISIBLE_ROWS_JS = (
    "[...document.querySelectorAll('#contactsTable tbody tr')]"
    "  .filter(r => !r.querySelector('.state-cell')"
    "            && !r.classList.contains('detail-row')"
    "            && r.style.display !== 'none').length"
)


def _visible_rows(page) -> int:
    return page.evaluate(f"() => {_VISIBLE_ROWS_JS}")


def _wait_until(page, predicate, describe, timeout: int = 5000, step: int = 50):
    """Poll a PYTHON-side condition until it holds, then return.

    For assertions about something the browser did asynchronously that is not
    visible in the DOM - a request the route handler recorded, say. A fixed
    wait_for_timeout before such an assert is a guess about how fast the
    machine is: it passes when idle and fails under a full suite, which reads
    as a flaky test rather than as the timing assumption it actually is.
    """
    waited = 0
    while waited < timeout:
        if predicate():
            return
        page.wait_for_timeout(step)
        waited += step
    raise AssertionError(f"timed out after {timeout}ms waiting for {describe}")


def _wait_for_rows(page, expected: int, timeout: int = 5000):
    """Wait for the table to settle on a row count, rather than reading it once.

    Anything that re-fetches - a status chip, a poll - lands asynchronously, so
    a bare assert races the network. This still fails if the count never
    arrives; it just stops failing when the machine is merely busy.
    """
    try:
        page.wait_for_function(
            f"() => ({_VISIBLE_ROWS_JS}) === {expected}", timeout=timeout
        )
    except Exception as e:  # re-raised below with the actual count
        raise AssertionError(
            f"expected {expected} visible row(s), still {_visible_rows(page)} "
            f"after {timeout}ms"
        ) from e


def _scores(page):
    return page.eval_on_selector_all(
        "#contactsTable tbody tr:not(.detail-row)",
        "els => els.filter(e => !e.querySelector('.state-cell'))"
        "        .map(e => e.children[0].textContent.trim())",
    )


def test_columns_sort_and_toggle_direction(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            assert _scores(page) == ["12", "91", "55"], "server order, unsorted"

            score_th = page.locator("#contactsTable thead th").first
            score_th.click()
            # Numeric, not lexicographic: "12" must not sort above "9".
            assert _scores(page) == ["12", "55", "91"]
            assert score_th.get_attribute("aria-sort") == "ascending"

            score_th.click()
            assert _scores(page) == ["91", "55", "12"]
            assert score_th.get_attribute("aria-sort") == "descending"
        finally:
            browser.close()


def test_a_column_can_be_sorted_from_the_keyboard(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            name_th = page.locator("#contactsTable thead th").nth(1)
            name_th.focus()
            page.keyboard.press("Enter")
            names = page.eval_on_selector_all(
                "#contactsTable tbody tr:not(.detail-row)",
                "els => els.filter(e => !e.querySelector('.state-cell'))"
                "        .map(e => e.children[1].textContent.trim())",
            )
            assert names == ["Adam Zeller", "Mia Best", "Zoe Adams"]
        finally:
            browser.close()


def test_the_actions_column_is_not_sortable(live_dashboard):
    """It holds buttons, not a value."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            cls = page.get_attribute("#contactsTable thead th[data-nosort]", "class")
            assert "th-sortable" not in (cls or "")
        finally:
            browser.close()


def test_a_chosen_sort_survives_a_refresh(live_dashboard):
    """Polling re-renders the tbody every few seconds. Losing the order the
    operator picked each time would make sorting useless on a live table."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            page.locator("#contactsTable thead th").first.click()
            assert _scores(page) == ["12", "55", "91"]
            page.click("#refreshContacts")
            page.wait_for_timeout(500)
            assert _scores(page) == ["12", "55", "91"]
        finally:
            browser.close()


def test_status_chips_filter_and_combine_with_the_search_box(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            page.wait_for_selector(".filter-chip", timeout=5000)

            assert _visible_rows(page) == 3

            # A chip click re-FETCHES (/api/contacts?status=...), it does not
            # sieve the DOM locally - so the row count cannot be read on the
            # next line. Asserting immediately passed on an idle machine and
            # failed under the full suite, which is the shape of a test that
            # teaches people to re-run rather than read.
            page.click(".filter-chip[data-status='pending']")
            _wait_for_rows(page, 2)

            # Chip AND search, not chip OR search. The search box filters the
            # rows already on the page, so these need no round-trip.
            search = ".table-search input[data-table='contactsTable']"
            page.fill(search, "mia")
            assert _visible_rows(page) == 1

            # A search that contradicts the chip yields nothing - and says so.
            page.fill(search, "zoe")
            assert _visible_rows(page) == 0
            assert page.query_selector("#contactsTableNoMatch")

            page.fill(search, "")
            page.click(".filter-chip[data-status='pending']")  # toggle off
            _wait_for_rows(page, 3)
        finally:
            browser.close()


def test_a_chip_keeps_focus_when_activated(live_dashboard):
    """Re-rendering the chip markup on click would destroy the element the
    keyboard user just activated, dropping focus to the top of the document."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            page.wait_for_selector(".filter-chip", timeout=5000)
            page.focus(".filter-chip[data-status='pending']")
            page.keyboard.press("Enter")
            page.wait_for_timeout(200)
            assert page.evaluate("document.activeElement.dataset.status") == "pending"
        finally:
            browser.close()


def test_row_detail_shows_why_a_contact_was_discarded(live_dashboard):
    """discard_reason has always been in the API response and was never
    rendered: "low_score" gave the verdict but never the score behind it."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            row = page.locator("#contactsTable tbody tr", has_text="low@brand.de")
            btn = row.locator(".act-expand")
            btn.click()
            page.wait_for_selector(".detail-row", timeout=5000)

            detail = page.inner_text(".detail-row")
            assert "Score 12 below threshold 50" in detail
            assert btn.get_attribute("aria-expanded") == "true"

            btn.click()
            page.wait_for_selector(".detail-row", state="detached", timeout=5000)
            assert btn.get_attribute("aria-expanded") == "false"
        finally:
            browser.close()


LANGUAGE_CONTACTS = json.dumps(
    [
        {
            "id": 11,
            "email": "acquisti@rossi-integratori.it",
            "name": "Maria Rossi",
            "organization": "Rossi Integratori",
            "status": "pending",
            "lead_score": 70,
            "fit_score": 70,
            "language": "it",
            "timestamp": "2026-01-01",
        },
        {
            "id": 12,
            "email": "sales@nordic-wellness.com",
            "name": "Anna Berg",
            "organization": "Nordic Wellness",
            "status": "pending",
            "lead_score": 60,
            "fit_score": 60,
            "language": "en",
            "timestamp": "2026-01-02",
        },
        {
            "id": 13,
            "email": "erased_0f3a9c@erased.invalid",
            "name": None,
            "organization": None,
            "status": "erased",
            "lead_score": 0,
            "fit_score": 0,
            "language": None,
            "closed": True,
            "timestamp": "2026-01-03",
        },
    ]
)


def test_each_contact_shows_the_language_of_its_emails(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, body=LANGUAGE_CONTACTS)

            def badge(email):
                row = page.locator("#contactsTable tbody tr", has_text=email)
                return row.locator(".lang-badge")

            it = badge("rossi-integratori.it")
            assert it.inner_text() == "IT"
            assert it.get_attribute("title") == "Written in Italian"
            en = badge("nordic-wellness.com")
            assert en.inner_text() == "EN"
            assert en.get_attribute("title") == "Written in English"
            assert badge("erased.invalid").count() == 0

            row = page.locator("#contactsTable tbody tr", has_text="rossi-integratori")
            row.locator(".act-expand").click()
            page.wait_for_selector(".detail-row .why-facts", timeout=5000)
            detail = page.inner_text(".detail-row")
            assert "Language" in detail
            assert "Italian (its address ends in .it)" in detail
        finally:
            browser.close()


def test_the_language_badge_keeps_the_email_column_sortable(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard, body=LANGUAGE_CONTACTS)
            page.locator("#contactsTable thead th").nth(2).click()
            emails = page.eval_on_selector_all(
                "#contactsTable tbody tr:not(.detail-row)",
                "els => els.map(e => e.children[2].dataset.sort)",
            )
            assert emails == sorted(emails)
            assert "acquisti@rossi-integratori.it" in emails
        finally:
            browser.close()


def test_blocking_offers_an_undo(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            sent = []
            page.route(
                "**/api/contacts/*/action",
                lambda route: (
                    sent.append(route.request.post_data),
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"status": "ok", "undo": "unblock"}),
                    ),
                )[-1],
            )
            row = page.locator("#contactsTable tbody tr", has_text="hot@brand.de")
            row.locator(".action-btn", has_text="Block").click()

            page.wait_for_selector(".toast.show .toast-action", timeout=5000)
            assert "hot@brand.de" in page.inner_text(".toast")

            page.click(".toast-action")
            page.wait_for_timeout(400)
            assert any('"unblock"' in (s or "") for s in sent), (
                f"Undo did not send an unblock; requests were {sent}"
            )
        finally:
            browser.close()


def test_a_refused_action_is_not_reported_as_success(live_dashboard):
    """The old handler resolved r.json() and toasted success unconditionally,
    so a 409 still announced "Action block executed"."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = _contacts_page(browser, live_dashboard)
            page.route(
                "**/api/contacts/*/action",
                lambda route: route.fulfill(
                    status=409,
                    content_type="application/json",
                    body=json.dumps({"error": "suppressed because of unsubscribe"}),
                ),
            )
            row = page.locator("#contactsTable tbody tr", has_text="hot@brand.de")
            row.locator(".action-btn", has_text="Block").click()

            page.wait_for_selector(".toast.show", timeout=5000)
            cls = page.get_attribute("#toast", "class")
            assert "error" in cls, f"a refusal must not read as success (class={cls})"
            assert "unsubscribe" in page.inner_text(".toast")
        finally:
            browser.close()


# ── Layout ──────────────────────────────────────────────────────────────────


def test_card_headers_stay_inside_their_card(live_dashboard):
    """A negative margin meant to pull a header out to the edge of a PADDED
    card was also hitting the zero-padding table cards, dragging the header
    outside the rounded corner and clipping its title. Enumerating exceptions
    kept failing as new zero-padding cards were added, so the card now carries
    an explicit .card-flush marker; this checks every card, not just one."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            page.goto(live_dashboard)
            page.wait_for_load_state("networkidle")
            probe = """() => [...document.querySelectorAll('.glass-card')]
                .map(c => {
                  const h = c.querySelector(':scope > .card-header');
                  if (!h || c.offsetParent === null) return null;
                  const d = c.getBoundingClientRect().left
                          - h.getBoundingClientRect().left;
                  return d > 0.5 ? `${c.className} overhang=${d.toFixed(1)}px` : null;
                }).filter(Boolean)"""
            for tab in ["tab-overview", "tab-contacts", "tab-insights"]:
                open_tab(page, tab)
                page.wait_for_selector(f"#{tab}.active", timeout=5000)
                page.wait_for_timeout(400)
                bad = page.evaluate(probe)
                assert not bad, f"{tab}: {bad}"
        finally:
            browser.close()


@pytest.mark.parametrize("width,height", [(390, 844), (768, 1024), (1440, 900)])
def test_the_page_never_scrolls_sideways(live_dashboard, width, height):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN, viewport={"width": width, "height": height})
            page.goto(live_dashboard)
            page.wait_for_load_state("networkidle")
            # The Overview's Ready to send card, from the real route: its
            # details carry long file paths.
            page.wait_for_selector("#readinessChecks tr", timeout=15000)
            assert not page.evaluate(
                "document.documentElement.scrollWidth >"
                " document.documentElement.clientWidth"
            ), f"tab-overview overflows sideways at {width}px"
            for tab in ["tab-contacts", "tab-emails", "tab-insights", "tab-template"]:
                if width < 900:
                    page.click("#menuToggle")
                    page.wait_for_timeout(320)
                open_tab(page, tab)
                page.wait_for_selector(f"#{tab}.active", timeout=5000)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth >"
                    " document.documentElement.clientWidth"
                )
                assert not overflow, f"{tab} overflows sideways at {width}px"
        finally:
            browser.close()


def test_the_actions_column_fits_without_clipping(live_dashboard):
    """Adding a third button pushed Block off the right edge at 1440px, where
    the only way to reach it was to notice the table scrolled sideways."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN, viewport={"width": 1440, "height": 900})
            page.route(
                "**/api/contacts*",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=CONTACTS
                ),
            )
            page.goto(live_dashboard)
            open_tab(page, "tab-contacts")
            page.wait_for_selector("#contactsTable tbody tr td", timeout=8000)
            clipped = page.evaluate(
                """() => {
                  const box = document.querySelector('#tab-contacts .table-responsive');
                  const r = box.getBoundingClientRect();
                  return [...document.querySelectorAll('#contactsTable tbody .action-btn')]
                    .filter(b => b.getBoundingClientRect().right > r.right + 0.5)
                    .map(b => b.getAttribute('aria-label') || b.textContent);
                }"""
            )
            assert not clipped, f"action buttons clipped: {clipped}"
        finally:
            browser.close()


# ── The Contacts chips must count the TABLE, not the page ───────────────────
# /api/contacts returns one page (top 200 by lead score). The chips tallied the
# rendered rows, so they reported the PAGE as though it were the total: the
# chip read "pending 41" while the Overview card read 437. Clicking it then
# filtered client-side, so the other 396 pending contacts were unreachable.

# 200 rows, of which only 41 are pending - the real shape of the bug.
_PAGE = [
    {
        "id": i,
        "email": f"c{i}@brand.de",
        "name": f"N{i}",
        "organization": "Org",
        "status": "pending" if i < 41 else "emailed",
        "lead_score": 90,
        "fit_score": 90,
        "timestamp": "2026-01-01",
    }
    for i in range(200)
]
_TRUE_COUNTS = {"pending": 437, "emailed": 183, "ai_irrelevant": 206}


def _counts_page(page, contacts=None):
    """Serve a page that under-represents 'pending', with honest totals."""
    page.route(
        "**/api/stats",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "total_contacts": 826,
                    "emails_sent": 183,
                    "contact_status_counts": _TRUE_COUNTS,
                }
            ),
        ),
    )
    page.route(
        "**/api/contacts*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(contacts if contacts is not None else _PAGE),
        ),
    )


def test_the_status_chip_shows_the_true_total_not_the_page_count(live_dashboard):
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            _counts_page(page)
            page.goto(live_dashboard)
            open_tab(page, "tab-contacts")
            page.wait_for_selector(".filter-chip[data-status='pending']", timeout=8000)

            chip = page.inner_text(".filter-chip[data-status='pending']")

            assert "437" in chip, (
                f"the chip reads {chip!r} - that is the count of the 200 rows on "
                f"screen, not of the 437 pending contacts in the database"
            )
            assert "41" not in chip.replace("437", "")
        finally:
            browser.close()


def test_clicking_a_status_chip_asks_the_server(live_dashboard):
    """Client-side filtering could only sieve the page already fetched."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            requested = []
            page.route(
                "**/api/stats",
                lambda r: r.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "total_contacts": 826,
                            "emails_sent": 0,
                            "contact_status_counts": _TRUE_COUNTS,
                        }
                    ),
                ),
            )

            def contacts_route(route):
                requested.append(route.request.url)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_PAGE),
                )

            page.route("**/api/contacts*", contacts_route)
            page.goto(live_dashboard)
            open_tab(page, "tab-contacts")
            page.wait_for_selector(".filter-chip[data-status='pending']", timeout=8000)
            requested.clear()

            page.click(".filter-chip[data-status='pending']")
            _wait_until(
                page,
                lambda: any("status=pending" in u for u in requested),
                "a request carrying status=pending",
            )

            assert any("status=pending" in u for u in requested), (
                f"no server request carried the filter; requests were {requested}"
            )
        finally:
            browser.close()


def test_the_table_says_it_is_showing_one_page(live_dashboard):
    """A table that silently shows 200 of 826 invites exactly the conclusion
    that the 200 ARE the 826."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            _counts_page(page)
            page.goto(live_dashboard)
            open_tab(page, "tab-contacts")
            page.wait_for_selector("#contactsPageNote", timeout=8000)
            # The note needs BOTH fetches: rows from /api/contacts and the
            # total from /api/stats. Waiting a fixed 600ms guessed at how fast
            # the second one lands.
            _wait_until(
                page,
                lambda: "826" in page.inner_text("#contactsPageNote"),
                "the page note to show the real total",
            )

            note = page.inner_text("#contactsPageNote")
            assert "200" in note and "826" in note, f"note reads {note!r}"
        finally:
            browser.close()


def test_a_complete_page_is_not_described_as_partial(live_dashboard):
    """Guard: when everything fits, the note must not imply more exists."""
    with sync_playwright() as p:
        browser = _browser(p)
        try:
            page = browser.new_page(http_credentials=TEST_DASHBOARD_LOGIN)
            small = _PAGE[:3]
            page.route(
                "**/api/stats",
                lambda r: r.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "total_contacts": 3,
                            "emails_sent": 0,
                            "contact_status_counts": {"pending": 3},
                        }
                    ),
                ),
            )
            page.route(
                "**/api/contacts*",
                lambda r: r.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(small),
                ),
            )
            page.goto(live_dashboard)
            open_tab(page, "tab-contacts")
            page.wait_for_selector("#contactsPageNote", timeout=8000)
            _wait_until(
                page,
                lambda: "3" in page.inner_text("#contactsPageNote"),
                "the page note to reflect the loaded rows",
            )

            note = page.inner_text("#contactsPageNote")
            assert "top" not in note.lower(), f"note implies truncation: {note!r}"
            assert "3" in note
        finally:
            browser.close()
