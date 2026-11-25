"""The new app shell at /: what a person does in it, in a real browser.

Marked ``ui``; run with ``python -m pytest -m ui``."""

import socket
import threading

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

from conftest import TEST_DASHBOARD_LOGIN

pytestmark = pytest.mark.ui

SCREENS = ["today", "review", "leads", "markets", "setup"]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def shell(tmp_path):
    """A dashboard with two emails waiting."""
    from werkzeug.serving import make_server

    import main
    from conftest import _browser_dashboard_settings
    from modules.database import Database
    from modules.review_queue import ReviewQueue

    settings = _browser_dashboard_settings(tmp_path)
    app = main.create_dashboard_app(settings)
    db = Database(settings.get_abs_db_path())
    sid = db.log_search("q")
    items = []
    for email, org in (
        ("buyer@doors.example", "Door Systems GmbH, Tier-1 automotive supplier"),
        ("buyer@hinges.example", "Hinge Works S.r.l."),
    ):
        cid = db.add_contact(
            sid,
            email,
            organization=org,
            company_country="DE",
            company_country_source="page",
        )
        items.append(
            ReviewQueue(db).propose(
                cid,
                "first_email",
                "Parts to your drawing",
                "<p>Dear buyer,</p><p>We make cold-formed parts.</p><p>Best regards,</p>{{legal_footer}}",
            )
        )
    port = _free_port()
    server = make_server("127.0.0.1", port, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {"url": f"http://127.0.0.1:{port}", "db": db, "items": items}
    finally:
        server.shutdown()


@pytest.fixture
def page(shell):
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001 - any launch failure means skip
            pytest.skip(f"Chromium not available: {e}")
        pg = browser.new_page(
            http_credentials=TEST_DASHBOARD_LOGIN,
            viewport={"width": 1440, "height": 900},
        )
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.goto(shell["url"] + "/")
        expect(pg.locator("#screen-today")).to_be_visible()
        yield pg
        browser.close()
        assert errors == [], errors


def _status(shell, item_id):
    return (
        shell["db"]
        .conn.execute("SELECT status FROM review_queue WHERE id = ?", (item_id,))
        .fetchone()[0]
    )


def test_the_number_keys_move_between_screens(page):
    for n, name in enumerate(SCREENS, start=1):
        page.keyboard.press(str(n))
        expect(page.locator(f"#screen-{name}")).to_be_visible()
        expect(page.locator(f".nav a[data-screen={name}]")).to_have_attribute(
            "aria-current", "page"
        )
    expect(page.locator("#screen-today")).to_be_hidden()


def test_the_classic_dashboard_is_one_link_away(page, shell):
    page.click(".classic-link")
    expect(page).to_have_url(shell["url"] + "/classic")
    expect(page.locator("#tab-overview")).to_be_attached()


def test_an_email_is_approved_with_its_edit_by_keyboard(page, shell):
    page.keyboard.press("2")
    expect(page.locator("#reviewList [data-id]")).to_have_count(2)
    page.keyboard.press("j")
    expect(page.locator("#rvText")).to_be_visible()
    page.fill("#rvSubject", "Parts to your drawing, from Omega")
    page.keyboard.press("Control+Enter")
    expect(page.locator("#toast")).to_contain_text("Approved")
    row = (
        shell["db"]
        .conn.execute(
            "SELECT status, final_subject, reviewer FROM review_queue WHERE id = ?",
            (shell["items"][0],),
        )
        .fetchone()
    )
    assert tuple(row) == (
        "approved",
        "Parts to your drawing, from Omega",
        TEST_DASHBOARD_LOGIN["username"],
    )


def test_a_rejection_needs_a_reason(page, shell):
    page.keyboard.press("2")
    page.click(f"#reviewList [data-id='{shell['items'][1]}']")
    page.click("#rvReject")
    page.click("#sheetOk")
    expect(page.locator("#sheetError")).to_contain_text("reason")
    assert _status(shell, shell["items"][1]) == "pending"
    page.fill("#rejectReason", "a distributor, not a maker")
    page.click("#sheetOk")
    expect(page.locator("#sheet")).to_be_hidden()
    assert _status(shell, shell["items"][1]) == "rejected"


def test_an_outcome_set_in_review_reaches_the_funnel_on_today(page, shell):
    page.keyboard.press("2")
    expect(page.locator("#reviewList [data-id]")).to_have_count(2)
    page.keyboard.press("j")
    page.select_option("#reviewDetail select[data-outcome]", "meeting")
    expect(page.locator("#toast")).to_contain_text("Meeting booked")
    page.keyboard.press("1")
    expect(page.locator("#todayFunnel")).to_contain_text("Meeting booked")
    expect(page.locator("#todayTiles")).to_contain_text("Meetings booked")
    assert (
        shell["db"].conn.execute("SELECT stage FROM outcomes").fetchone()[0] == "meeting"
    )


def test_a_csv_is_previewed_before_it_is_imported(page, shell, tmp_path):
    csv = tmp_path / "fair.csv"
    csv.write_text("company,email\nLock BV,sales@lock.example\nDup,buyer@doors.example\n")
    page.keyboard.press("3")
    page.click("#importLeads")
    page.set_input_files("#csvFile", str(csv))
    page.click("#sheetOk")
    expect(page.locator("#csvPreview")).to_contain_text("1 new company")
    expect(page.locator("#csvPreview")).to_contain_text("already in the leads")
    assert shell["db"].get_contact_by_email("sales@lock.example") is None
    page.click("#sheetOk")
    expect(page.locator("#sheet")).to_be_hidden()
    expect(page.locator("#leadTable")).to_contain_text("Lock BV")


def test_the_palette_finds_a_screen(page):
    page.keyboard.press("Control+k")
    expect(page.locator("#palette")).to_be_visible()
    page.keyboard.type("markets")
    page.keyboard.press("Enter")
    expect(page.locator("#screen-markets")).to_be_visible()
    expect(page.locator("#palette")).to_be_hidden()


def test_no_screen_scrolls_sideways_on_a_phone(page):
    page.set_viewport_size({"width": 390, "height": 844})
    for name in SCREENS:
        page.goto(page.url.split("#")[0] + f"#{name}")
        expect(page.locator(f"#screen-{name}")).to_be_visible()
        page.wait_for_load_state("networkidle")
        assert page.evaluate("document.documentElement.scrollWidth") <= 390, name
    expect(page.locator(".sidebar")).to_have_css("position", "fixed")
