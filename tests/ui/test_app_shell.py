"""The new app shell at /: what a person does in it, in a real browser.

Marked ``ui``; run with ``python -m pytest -m ui``."""

import socket
import threading

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright

from conftest import TEST_DASHBOARD_LOGIN

pytestmark = pytest.mark.ui

SCREENS = ["today", "review", "leads", "markets", "network", "setup"]

# A partner's own login: a member of Omega's network, not Omega's staff.
PARTNER_LOGIN = {"username": "brianza", "password": "lathe-room-key-2026"}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def shell(tmp_path):
    """A dashboard with two emails waiting."""
    from werkzeug.security import generate_password_hash
    from werkzeug.serving import make_server

    import main
    from conftest import _browser_dashboard_settings
    from modules import network
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
    partner = network.add_member(
        db,
        {
            "name": "Torneria Brianza",
            "city": "Monza",
            "country": "IT",
            "specialisations": ["turning"],
            "certifications": "ISO 9001",
            "capacity_per_week": 3000,
            "login": PARTNER_LOGIN["username"],
            "password": PARTNER_LOGIN["password"],
        },
        generate_password_hash,
    )
    port = _free_port()
    server = make_server("127.0.0.1", port, app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "db": db,
            "items": items,
            "partner": partner,
        }
    finally:
        server.shutdown()


def _browser_page(shell, login, first_screen):
    """A signed-in browser that fails the test on any console error, a
    refused request included."""
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001 - any launch failure means skip
            pytest.skip(f"Chromium not available: {e}")
        pg = browser.new_page(
            http_credentials=login,
            viewport={"width": 1440, "height": 900},
        )
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.goto(shell["url"] + "/")
        expect(pg.locator(f"#screen-{first_screen}")).to_be_visible()
        yield pg
        browser.close()
        assert errors == [], errors


@pytest.fixture
def page(shell):
    yield from _browser_page(shell, TEST_DASHBOARD_LOGIN, "today")


@pytest.fixture
def partner_page(shell):
    yield from _browser_page(shell, PARTNER_LOGIN, "network")


def _job_from_omega(shell, quantity=800):
    """A turning job Omega drafts and approves for the partner, as the server
    would make it."""
    from modules import network

    omega = network.self_member(shell["db"])
    viewer = network.Viewer(omega["id"], "orchestrator", TEST_DASHBOARD_LOGIN["username"])
    job = network.create_job(
        shell["db"],
        viewer,
        {
            "to_member_id": shell["partner"]["id"],
            "kind": "operation",
            "operation": "turning",
            "part": "M10 shoulder bolt, groove under head",
            "quantity": quantity,
        },
    )
    return network.decide(shell["db"], viewer, job["id"], "approve")


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


def test_the_network_lists_members_with_their_free_capacity(page):
    page.keyboard.press("5")
    expect(page.locator("#screen-network")).to_be_visible()
    card = page.locator(".nw-member", has_text="Torneria Brianza")
    expect(card).to_contain_text("Turning")
    expect(card).to_contain_text("3,000 of 3,000 free")
    expect(card.locator("[data-send-to]")).to_be_visible()
    omega = page.locator(".nw-member.is-self")
    expect(omega).to_contain_text("Omega IFS")
    expect(omega.locator("[data-send-to]")).to_have_count(0)


def test_a_job_sent_from_the_sheet_lands_in_jobs(page, shell):
    page.keyboard.press("5")
    page.click("#nwSendJob")
    page.select_option("#nwOp", "turning")
    expect(page.locator("#nwRecipients input[name=nwTo]")).to_have_count(1)
    expect(page.locator("#nwRecipients input[name=nwTo]")).to_be_checked()
    expect(page.locator("#nwRecipients")).to_contain_text("Fits")
    page.fill("#nwPart", "M8x40 flange bolt, turned groove")
    page.fill("#nwQty", "1000")
    page.click("#sheetOk")
    expect(page.locator("#sheet")).to_be_hidden()
    expect(page.locator("#toast")).to_contain_text("Torneria Brianza")
    row = lambda: tuple(
        shell["db"]
        .conn.execute("SELECT to_member_id, operation, quantity, status, approved_by FROM network_jobs")
        .fetchone()
    )
    assert row() == (shell["partner"]["id"], "turning", 1000, "draft", None)
    expect(page.locator("#nwJobs")).to_be_visible()
    expect(page.locator("#nwJobList [data-job]")).to_have_count(1)
    expect(page.locator("#nwJobList")).to_contain_text("Waiting for approval")
    expect(page.locator("#nwJobDetail h2")).to_have_text("M8x40 flange bolt, turned groove")
    expect(page.locator("#nwJobDetail")).to_contain_text("Not yet: nothing has been sent")
    page.click("#nwJobDetail [data-decide=approve]")
    expect(page.locator("#toast")).to_contain_text("Approved and sent")
    expect(page.locator("#nwJobList")).to_contain_text("Sent")
    login = TEST_DASHBOARD_LOGIN["username"]
    assert row() == (shell["partner"]["id"], "turning", 1000, "requested", login)


def test_an_order_split_from_the_sheet_waits_for_approval(page, shell):
    page.keyboard.press("5")
    page.click("#nwSplitOrder")
    page.fill("#soPart", "M10x60 flange bolt")
    lines = page.locator("#soLines .nw-line")
    expect(lines).to_have_count(2)
    # The forming line finds no other former here; drop it and let Omega form all.
    lines.nth(0).locator("[data-remove]").click()
    page.fill("#soSelf", "1000")
    expect(page.locator("#soSum")).to_contain_text("= 1,000 of 1,000")
    expect(page.locator("#soLines [data-to]")).to_have_value(str(shell["partner"]["id"]))
    page.click("#sheetOk")
    expect(page.locator("#sheet")).to_be_hidden()
    expect(page.locator("#toast")).to_contain_text("waits for a named person")
    expect(page.locator("#nwJobList")).to_contain_text("Order 1")
    rows = shell["db"].conn.execute(
        "SELECT order_id, operation, quantity, status FROM network_jobs"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(1, "turning", 1000, "draft")]


def test_a_partner_sees_only_the_network_and_takes_the_job(shell, partner_page):
    job = _job_from_omega(shell)
    pg = partner_page
    expect(pg.locator("#networkEyebrow")).to_have_text("Your work in Omega's network")
    expect(pg.locator(".nav a:visible")).to_have_count(1)
    expect(pg.locator(".nav a[data-screen=today]")).to_be_hidden()
    pg.reload()
    expect(pg.locator("#nwJobsCount")).to_have_text("1")
    pg.click("#nwTabJobs")
    pg.click(f"#nwJobList [data-job='{job['id']}']")
    pg.click("#nwJobDetail [data-decide=accept]")
    expect(pg.locator("#toast")).to_contain_text("Accepted")
    expect(pg.locator("#nwJobList")).to_contain_text("Accepted")
    db = shell["db"]
    assert db.conn.execute("SELECT status FROM network_jobs").fetchone()[0] == "accepted"
    assert (
        db.conn.execute(
            "SELECT booked FROM network_members WHERE id = ?", (shell["partner"]["id"],)
        ).fetchone()[0]
        == 800
    )
    pg.fill("#nwMsg", "Groove at 2.5 mm, as drawn? Ships Friday.")
    pg.click("#nwMsgSend")
    expect(pg.locator("#nwThread .nw-msg.mine")).to_contain_text("Ships Friday")
    expect(pg.locator("#nwThread")).to_contain_text(PARTNER_LOGIN["username"])
    assert db.conn.execute("SELECT body FROM network_messages").fetchone()[0].endswith(
        "Ships Friday."
    )
    pg.keyboard.press("Control+k")
    expect(pg.locator("#paletteList")).not_to_contain_text("Leads")


def test_the_network_does_not_scroll_sideways_on_a_phone(shell, page):
    _job_from_omega(shell)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(page.url.split("#")[0] + "#network")
    expect(page.locator(".nw-member")).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    page.click("#nwTabJobs")
    page.click("#nwJobList [data-job]")
    expect(page.locator("#nwThread")).to_contain_text("No messages yet")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    page.click("#nwSendJob")
    expect(page.locator("#nwRecipients input[name=nwTo]")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_the_administrator_adds_a_partner_from_the_sheet(page, shell):
    page.keyboard.press("5")
    page.click("#nwAddMember")
    page.fill("#amName", "Zincatura Lambro S.r.l.")
    page.fill("#amCity", "Monza")
    page.check(".nw-specs input[value=plating]")
    page.fill("#amCap", "30000")
    page.fill("#amLogin", "zincatura")
    page.fill("#amPass", "plating-password-1")
    page.click("#sheetOk")
    expect(page.locator("#toast")).to_contain_text("Zincatura Lambro S.r.l. joined")
    expect(page.locator("#nwMemberGrid")).to_contain_text("Zincatura Lambro S.r.l.")
    row = shell["db"].conn.execute(
        "SELECT specialisations, capacity_per_week FROM network_members WHERE login = 'zincatura'"
    ).fetchone()
    assert tuple(row) == ("plating", 30000)
