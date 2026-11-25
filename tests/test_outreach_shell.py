"""The outreach app: its front page, the outcome funnel, CSV lead import,
who may control the agent, and the host's health check."""

import base64
from typing import ClassVar

import main
from conftest import sent_to
from modules import outcomes
from modules.database import Database
from modules.review_queue import ReviewQueue


def _client(tmp_path, settings_factory, **extra):
    s = settings_factory(
        db_path=str(tmp_path / "dash.db"), blacklist_check_enabled=False, **extra
    )
    return main.create_dashboard_app(s).test_client(), Database(s.db_path)


def _as(name, password):
    token = base64.b64encode(f"{name}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_the_app_shell_is_the_front_page_and_runs_under_the_csp(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    page = client.get("/")
    html = page.get_data(as_text=True)
    assert 'id="screen-leads"' in html and 'href="/classic"' in html
    assert "<script>" not in html and 'src="/static/app/app.js"' in html
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert client.get("/static/app/app.js").status_code == 200
    assert 'id="tab-review"' in client.get("/classic").get_data(as_text=True)


def test_no_carbon_or_quote_feature_is_left(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    html = client.get("/").get_data(as_text=True).lower()
    assert "carbon" not in html and "quote" not in html.replace("blockquote", "")
    for path in ("/api/carbon/summary", "/api/quotes", "/api/memory/search?q=x"):
        assert client.get(path).status_code == 404, path


def _contact(db, email, org):
    sid = db.log_search("q")
    return db.add_contact(sid, email, organization=org)


def test_the_funnel_counts_companies_and_each_stage_implies_the_one_before(
    tmp_path, settings_factory
):
    client, db = _client(tmp_path, settings_factory)
    a = _contact(db, "a@one.example", "One")
    b = _contact(db, "b@two.example", "Two")
    _contact(db, "c@three.example", "Three")
    sent_to(db, "a@one.example")
    sent_to(db, "a@one.example")  # a follow-up: still one company
    sent_to(db, "b@two.example")

    assert client.post(f"/api/contacts/{a}/outcome", json={"stage": "won"}).status_code == 200
    assert client.post(f"/api/contacts/{b}/outcome", json={"stage": "interested"}).status_code == 200

    f = client.get("/api/funnel").get_json()
    counts = {s["key"]: s["count"] for s in f["stages"]}
    assert counts == {
        "found": 3, "emailed": 2, "replied": 2, "interested": 2, "meeting": 1, "won": 1,
    }
    assert f["rates"]["reply"] == 100.0 and f["rates"]["meeting"] == 50.0
    widths = [s["count"] for s in f["stages"]]
    assert widths == sorted(widths, reverse=True)


def test_an_outcome_is_recorded_with_who_set_it_and_can_be_cleared(tmp_path, settings_factory):
    client, db = _client(tmp_path, settings_factory)
    cid = _contact(db, "m@heat.example", "Heat GmbH")
    client.post(f"/api/contacts/{cid}/outcome", json={"stage": "meeting"})
    row = db.conn.execute("SELECT stage, updated_by FROM outcomes").fetchone()
    assert tuple(row) == ("meeting", "admin")
    assert [c["outcome"] for c in client.get("/api/contacts").get_json()] == ["meeting"]
    client.post(f"/api/contacts/{cid}/outcome", json={"stage": None})
    assert outcomes.stage_of(db, cid) is None


def test_an_unknown_stage_or_contact_is_refused(tmp_path, settings_factory):
    client, db = _client(tmp_path, settings_factory)
    cid = _contact(db, "m@heat.example", "Heat GmbH")
    assert client.post(f"/api/contacts/{cid}/outcome", json={"stage": "maybe"}).status_code == 400
    assert client.post("/api/contacts/999/outcome", json={"stage": "won"}).status_code == 404


def test_a_review_item_shows_its_companys_outcome(tmp_path, settings_factory):
    client, db = _client(tmp_path, settings_factory)
    cid = _contact(db, "m@heat.example", "Heat GmbH")
    sent_to(db, "m@heat.example")
    ReviewQueue(db).propose(cid, "reply", "Re: parts", "<p>Thanks.</p>{{legal_footer}}")
    outcomes.set_stage(db, cid, "interested", "maria")
    items = client.get("/api/review?view=waiting").get_json()["items"]
    assert items[0]["outcome"] == "interested"


CSV = (
    "Azienda;E-mail;Paese;Settore\n"
    "Heat GmbH;m.weber@heat.example;DE;heat pumps\n"
    "Heat GmbH;M.Weber@heat.example;DE;heat pumps\n"
    "Lock BV;sales@lock.example;nl;locks\n"
    "Broken;not-an-address;IT;\n"
    "Known;known@old.example;IT;\n"
)


def test_a_csv_is_previewed_then_imported_and_screened_like_found_leads(
    tmp_path, settings_factory
):
    client, db = _client(tmp_path, settings_factory)
    _contact(db, "known@old.example", "Known")

    preview = client.post(
        "/api/leads/import", json={"csv": CSV, "filename": "fair.csv", "channel": "trade_fair"}
    ).get_json()
    assert preview["would_add"] == 2 and preview["added"] == 0
    assert preview["skipped"]["twice in the file"] == 1
    assert preview["skipped"]["already in the leads"] == 1
    assert preview["skipped"]["not an email address"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1

    done = client.post(
        "/api/leads/import",
        json={"csv": CSV, "filename": "fair.csv", "channel": "trade_fair", "dry_run": False},
    ).get_json()
    assert done["added"] == 2
    heat = db.get_contact_by_email("m.weber@heat.example")
    assert heat["organization"] == "Heat GmbH" and heat["source_channel"] == "trade_fair"
    assert heat["company_country"] == "DE" and heat["company_country_source"] == "list"
    lock = db.get_contact_by_email("sales@lock.example")
    assert lock["company_country"] == "NL"


def test_a_suppressed_address_is_not_imported(tmp_path, settings_factory):
    client, db = _client(tmp_path, settings_factory)
    db.add_to_suppression("m.weber@heat.example", "unsubscribed")
    r = client.post(
        "/api/leads/import",
        json={"csv": "email\nm.weber@heat.example\n", "channel": "referral", "dry_run": False},
    ).get_json()
    assert r["added"] == 0 and r["skipped"]["on the do-not-contact list"] == 1


def test_a_csv_without_an_email_column_or_a_source_is_refused(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    no_email = client.post("/api/leads/import", json={"csv": "company\nHeat\n", "channel": "referral"})
    assert no_email.status_code == 400
    agent_channel = client.post(
        "/api/leads/import", json={"csv": "email\na@b.example\n", "channel": "own_website"}
    )
    assert agent_channel.status_code == 400


def test_a_reviewer_decides_on_proposals_but_cannot_control_the_agent(
    tmp_path, settings_factory
):
    client, db = _client(
        tmp_path, settings_factory, dashboard_reviewers="maria:reviewer-pass-123"
    )
    maria = _as("maria", "reviewer-pass-123")
    cid = _contact(db, "m@heat.example", "Heat GmbH")
    for path, body in [
        ("/api/agent/start", {"mode": "dry_run"}),
        ("/api/send-manual", {"email": "x@y.example"}),
        ("/api/config", {}),
        ("/api/leads/import", {"csv": "email\na@b.example\n", "channel": "referral"}),
    ]:
        r = client.post(path, json=body, headers=maria)
        assert r.status_code == 403, path
        assert "administrator" in r.get_json()["error"]
    ok = client.post(f"/api/contacts/{cid}/outcome", json={"stage": "meeting"}, headers=maria)
    assert ok.status_code == 200
    assert outcomes.stage_of(db, cid) == "meeting"


def test_the_health_check_needs_no_login_and_says_nothing_personal(
    tmp_path, settings_factory
):
    from conftest import anonymous

    client, _db = _client(tmp_path, settings_factory)
    r = anonymous(client).get("/healthz")
    assert r.status_code == 200 and r.get_json() == {"status": "ok"}
    assert anonymous(client).get("/api/contacts").status_code == 401


def test_https_responses_tell_the_browser_to_stay_on_https(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    plain = client.get("/healthz")
    assert "Strict-Transport-Security" not in plain.headers
    secure = client.get("/healthz", headers={"X-Forwarded-Proto": "https"})
    assert secure.headers["Strict-Transport-Security"].startswith("max-age=31536000")


class _Dkim:
    """Stands in for dns.resolver.Resolver: ``keys`` maps a name to its TXT."""

    keys: ClassVar[dict] = {}
    down = False

    def __init__(self, *_a, **_k):
        self.timeout = self.lifetime = None

    def resolve(self, name, rdtype, *_a, **_k):
        from types import SimpleNamespace

        import dns.resolver

        if self.down:
            raise dns.resolver.LifetimeTimeout(timeout=3.0, errors=[])
        if name not in self.keys:
            raise dns.resolver.NXDOMAIN()
        return [SimpleNamespace(strings=[self.keys[name]])]


def test_dkim_is_found_under_the_providers_selector_or_said_to_be_missing(monkeypatch):
    import dns.resolver

    monkeypatch.setattr(dns.resolver, "Resolver", _Dkim)
    _Dkim.down = False
    _Dkim.keys = {"selector1._domainkey.omega.example": b"v=DKIM1; k=rsa; p=MIGf"}
    assert main._lookup_dkim("omega.example") == (True, "selector1")
    _Dkim.keys = {}
    found, _ = main._lookup_dkim("omega.example")
    assert found is False and "selector" in main.dkim_words(False, None)
    _Dkim.down = True
    assert main._lookup_dkim("omega.example") == (None, None)


def test_the_dkim_answer_has_its_own_route(tmp_path, settings_factory, monkeypatch):
    import dns.resolver

    monkeypatch.setattr(dns.resolver, "Resolver", _Dkim)
    _Dkim.down = False
    _Dkim.keys = {"google._domainkey.omega.example": b"v=DKIM1; p=MIIB"}
    client, _db = _client(tmp_path, settings_factory, smtp_username="sales@omega.example")
    r = client.get("/api/deliverability/dkim").get_json()
    assert r["dkim"] is True and r["selector"] == "google"


# ── Sign-in page and sessions ──
HTML = {"Accept": "text/html,application/xhtml+xml"}


def _signed_in(client, name, password, **headers):
    from conftest import TEST_DASHBOARD_ORIGIN, anonymous

    browser = anonymous(client)
    r = browser.post(
        "/login",
        data={"username": name, "password": password, "next": "/"},
        headers={"Origin": TEST_DASHBOARD_ORIGIN, **headers},
    )
    return browser, r


def test_a_browser_without_a_session_gets_the_sign_in_page_not_a_pop_up(
    tmp_path, settings_factory
):
    from conftest import anonymous

    client, _db = _client(tmp_path, settings_factory)
    page = anonymous(client).get("/", headers=HTML)
    assert page.status_code == 401
    assert "WWW-Authenticate" not in page.headers
    assert 'action="/login"' in page.get_data(as_text=True)
    api = anonymous(client).get("/api/contacts", headers=HTML)
    assert api.status_code == 401 and "WWW-Authenticate" in api.headers


def test_signing_in_gives_a_session_that_records_who_acted(tmp_path, settings_factory):
    from conftest import TEST_DASHBOARD_PASSWORD, TEST_DASHBOARD_USERNAME

    client, db = _client(tmp_path, settings_factory, dashboard_reviewers="maria:reviewer-pass-123")
    browser, r = _signed_in(client, "maria", "reviewer-pass-123")
    assert r.status_code == 302 and r.headers["Location"] == "/"
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie
    cid = _contact(db, "m@heat.example", "Heat GmbH")
    from conftest import TEST_DASHBOARD_ORIGIN

    ok = browser.post(
        f"/api/contacts/{cid}/outcome",
        json={"stage": "meeting"},
        headers={"Origin": TEST_DASHBOARD_ORIGIN},
    )
    assert ok.status_code == 200
    assert db.conn.execute("SELECT updated_by FROM outcomes").fetchone()[0] == "maria"
    refused = browser.post(
        "/api/agent/start", json={"mode": "dry_run"}, headers={"Origin": TEST_DASHBOARD_ORIGIN}
    )
    assert refused.status_code == 403
    admin, r = _signed_in(client, TEST_DASHBOARD_USERNAME, TEST_DASHBOARD_PASSWORD)
    assert admin.get("/api/funnel").status_code == 200


def test_a_wrong_password_is_refused_and_counted(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    browser, r = _signed_in(client, "admin", "not-the-password")
    assert r.status_code == 401 and "do not match" in r.get_data(as_text=True)
    assert browser.get("/api/funnel").status_code == 401


def test_signing_in_never_sends_the_browser_to_another_site(tmp_path, settings_factory):
    from conftest import (
        TEST_DASHBOARD_ORIGIN,
        TEST_DASHBOARD_PASSWORD,
        TEST_DASHBOARD_USERNAME,
        anonymous,
    )

    client, _db = _client(tmp_path, settings_factory)
    r = anonymous(client).post(
        "/login",
        data={
            "username": TEST_DASHBOARD_USERNAME,
            "password": TEST_DASHBOARD_PASSWORD,
            "next": "https://evil.example/",
        },
        headers={"Origin": TEST_DASHBOARD_ORIGIN},
    )
    assert r.headers["Location"] == "/"


def test_signing_out_ends_the_session(tmp_path, settings_factory):
    from conftest import (
        TEST_DASHBOARD_ORIGIN,
        TEST_DASHBOARD_PASSWORD,
        TEST_DASHBOARD_USERNAME,
    )

    client, _db = _client(tmp_path, settings_factory)
    browser, _r = _signed_in(client, TEST_DASHBOARD_USERNAME, TEST_DASHBOARD_PASSWORD)
    assert browser.get("/api/funnel").status_code == 200
    browser.post("/logout", headers={"Origin": TEST_DASHBOARD_ORIGIN})
    assert browser.get("/api/funnel").status_code == 401


def test_a_changed_password_ends_every_session(tmp_path, settings_factory):
    from conftest import anonymous

    client, _db = _client(tmp_path, settings_factory, dashboard_reviewers="maria:reviewer-pass-123")
    _browser, r = _signed_in(client, "maria", "reviewer-pass-123")
    cookie = r.headers["Set-Cookie"].split(";")[0]
    later, _db2 = _client(tmp_path, settings_factory, dashboard_reviewers="maria:a-new-password-456")
    stale = anonymous(later).get("/api/funnel", headers={"Cookie": cookie})
    assert stale.status_code == 401


def test_the_session_cookie_is_secure_over_https(tmp_path, settings_factory):
    from conftest import TEST_DASHBOARD_PASSWORD, TEST_DASHBOARD_USERNAME

    client, _db = _client(tmp_path, settings_factory)
    _browser, r = _signed_in(
        client, TEST_DASHBOARD_USERNAME, TEST_DASHBOARD_PASSWORD, **{"X-Forwarded-Proto": "https"}
    )
    assert "Secure" in r.headers["Set-Cookie"]


def test_pages_keep_their_origin_on_form_posts(tmp_path, settings_factory):
    """Under no-referrer Chrome posts a form with Origin: null, which the
    cross-origin check refuses: signing in failed in a real browser."""
    from conftest import anonymous

    client, _db = _client(tmp_path, settings_factory)
    page = anonymous(client).get("/login")
    assert page.headers["Referrer-Policy"] == "same-origin"
    assert 'action="/login"' in page.get_data(as_text=True)


def test_setup_lists_what_is_left_before_a_live_email(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory, smtp_username="", smtp_password="")
    r = client.get("/api/setup").get_json()
    steps = {st["key"]: st for st in r["steps"]}
    assert set(steps) >= {"mailbox", "domain", "legal", "optout", "customers", "countries", "ai"}
    assert steps["mailbox"]["status"] == "todo" and steps["mailbox"]["how"]
    assert steps["domain"]["status"] == "todo"
    assert steps["ai"]["required"] is False
    assert r["done"] < r["required"] and r["can_send_live"] is False


def test_the_fallback_email_describes_the_client_in_its_own_words():
    """With the AI writer on but silent, the body once said the sender made
    food supplements, whatever the client: Omega's buyers would have read it."""
    from types import SimpleNamespace

    from modules.client_profile import PROFILE
    from modules.dispatch_pipeline import DispatchPipeline

    agent = SimpleNamespace(
        settings=SimpleNamespace(ai_humanize_emails=True, sender_name="G", company_name="Omega IFS"),
        ai_gen=SimpleNamespace(is_configured=True, generate_email_body=lambda *a: ""),
        _stats={},
        _body_to_html=lambda text: text,
    )
    body = DispatchPipeline(agent).build_email_body({"organization": "Heat GmbH"})
    assert PROFILE.value_proposition_phrases["en"][0] in body
    assert "Omega IFS" in body and "Heat GmbH" in body
    assert "private-label" not in body and "probiotics" not in body
    assert agent._stats["ai_body_fallback"] == 1
