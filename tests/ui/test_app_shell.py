"""The new app shell at /: what a person does in it, in a real browser.

Marked ``ui``; run with ``python -m pytest -m ui``."""

import re
import socket
import threading

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

from conftest import TEST_DASHBOARD_LOGIN

pytestmark = pytest.mark.ui

SCREENS = ["today", "review", "quotes", "carbon", "leads", "markets"]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def shell(tmp_path):
    """A dashboard with two emails waiting and one draft passport."""
    from werkzeug.serving import make_server

    import main
    from conftest import _browser_dashboard_settings
    from modules import carbon_passport as cp
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
    lot = cp.add(
        db,
        lot_code="L-TEST-1",
        part_name="Stepped pin",
        part_mass_g=11.9,
        steel_route="eaf_scrap",
        mill_t_co2_per_t=0.62,
        wire_kg=1250,
        good_parts=102000,
        energy_kwh=310,
    )
    port = _free_port()
    server = make_server("127.0.0.1", port, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {"url": f"http://127.0.0.1:{port}", "db": db, "items": items, "lot": lot}
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


def test_the_carbon_paragraph_waits_for_an_approved_passport(page, shell):
    page.keyboard.press("2")
    expect(page.locator("#reviewList [data-id]")).to_have_count(2)
    page.keyboard.press("j")
    page.click("#rvCarbon")
    expect(page.locator("#toast")).to_contain_text("No approved passport")
    shell["db"].conn.execute(
        "UPDATE carbon_lots SET status = 'approved', reviewer = 'x'"
    )
    shell["db"].conn.commit()
    page.click("#rvCarbon")
    expect(page.locator("#rvText")).to_have_value(
        re.compile(r"electric-arc steel.*\n\nBest regards", re.DOTALL)
    )


def test_a_quote_from_the_sample_drawing_shows_what_was_made_before(page):
    page.keyboard.press("3")
    page.click("#useSample")
    expect(page.locator("#estimateCard")).to_contain_text("formable")
    expect(page.locator("#madeBefore li").first).to_contain_text(
        "Stepped pin with collar"
    )
    page.fill("#qImport", "40")
    expect(page.locator("#qCbamOut table tr")).to_have_count(4)


def test_a_passport_is_approved_before_it_can_be_downloaded(page, shell):
    page.keyboard.press("4")
    page.click(f"#lotList [data-lot='{shell['lot']}']")
    expect(page.locator("#lotDetail .figure")).to_contain_text("8.36")
    expect(page.locator("#lotDetail a:has-text('Passport PDF')")).not_to_have_attribute(
        "href", re.compile(".")
    )
    page.click("#approveLot")
    pdf = page.locator("#lotDetail a:has-text('Passport PDF')")
    expect(pdf).to_have_attribute(
        "href", f"/api/carbon/lots/{shell['lot']}/passport.pdf"
    )
    assert (
        shell["db"].conn.execute("SELECT status FROM carbon_lots").fetchone()[0]
        == "approved"
    )


def test_the_cbam_view_shows_every_year_and_its_sources(page):
    page.keyboard.press("4")
    page.click("#carbonViews [data-view=cbam]")
    expect(page.locator("#cbamTable tbody tr")).to_have_count(9)
    expect(page.locator("#cbamVerdict")).to_contain_text("2034")
    page.fill("#cbamForm [name=mass_g]", "250")
    page.fill("#cbamForm [name=competitor]", "600")
    page.fill("#cbamForm [name=omega]", "700")
    expect(page.locator("#cbamVerdict")).to_contain_text("From 2031")
    expect(page.locator("#cbamChart svg path.import")).to_be_attached()


def test_the_palette_finds_a_screen(page):
    page.keyboard.press("Control+k")
    expect(page.locator("#palette")).to_be_visible()
    page.keyboard.type("cbam")
    page.keyboard.press("Enter")
    expect(page.locator("#carbonCbam")).to_be_visible()
    expect(page.locator("#palette")).to_be_hidden()


def test_no_screen_scrolls_sideways_on_a_phone(page):
    page.set_viewport_size({"width": 390, "height": 844})
    for name in SCREENS:
        page.goto(page.url.split("#")[0] + f"#{name}")
        expect(page.locator(f"#screen-{name}")).to_be_visible()
        page.wait_for_load_state("networkidle")
        assert page.evaluate("document.documentElement.scrollWidth") <= 390, name
    expect(page.locator(".sidebar")).to_have_css("position", "fixed")
