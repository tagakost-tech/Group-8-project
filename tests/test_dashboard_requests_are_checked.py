"""Every request to the dashboard is checked for where it came from, its size
and its shape, and the page itself runs under a strict policy.

Before:
- a POST with no Origin header passed the cross-origin check (only a
  mismatching one was refused), and /api/agent/stop read no body, so it
  needed no JSON content type;
- a request body of any size was read;
- a number or a list where text belongs crashed the route (500);
- the Content-Security-Policy said only frame-ancestors, the contacts table
  ran inline onclick handlers, and Chart.js came from a floating major
  version (chart.js@4) the CDN could change under the page; Chart.js and
  Font Awesome now come from the dashboard itself, and the policy allows no
  other server;
- the template preview was a same-origin frame a template's script could
  reach out of;
- /track/ answered whether open tracking was on or off, with no limit.
"""

import re
import shutil

import pytest

import main
from config import RUNTIME_EDITABLE
from conftest import TEST_DASHBOARD_ORIGIN, anonymous

REAL_TEMPLATES = main.PROJECT_ROOT / "templates"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
RECIPIENT_PAGES = ("/track/", "/unsubscribe/")


@pytest.fixture
def app(settings_factory):
    return main.create_dashboard_app(settings_factory())


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def copy_of_templates(tmp_path, monkeypatch):
    """The editor writes to a COPY, never the real templates."""
    shutil.copytree(REAL_TEMPLATES, tmp_path / "templates")
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    return tmp_path / "templates"


def _write_urls(app):
    for rule in app.url_map.iter_rules():
        if not rule.methods & WRITE_METHODS or rule.rule.startswith(RECIPIENT_PAGES):
            continue
        url = rule.rule
        for arg in rule.arguments:
            url = re.sub(rf"<(\w+:)?{arg}>", "1", url)
        yield url


# ── Where a write came from ──


def test_a_write_with_no_origin_and_no_referer_is_refused(app):
    """A script or a forged request that names no page is refused on every
    write route; before, a missing Origin passed."""
    stranger = anonymous(app.test_client())
    login = app.test_client().environ_base["HTTP_AUTHORIZATION"]
    urls = list(_write_urls(app))
    assert len(urls) >= 8, urls
    for url in urls:
        resp = stranger.post(url, json={}, headers={"Authorization": login})
        assert resp.status_code == 403, f"{url} -> {resp.status_code}"


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost.evil.example"},
        {"Origin": "http://localhost:8080"},
        {"Origin": "https://localhost"},
        {"Referer": "https://evil.example/page"},
        {"Referer": "http://localhost.evil.example/"},
        {"Referer": "not a url"},
    ],
)
def test_a_write_from_another_origin_is_refused(app, headers):
    client = anonymous(app.test_client())
    login = app.test_client().environ_base["HTTP_AUTHORIZATION"]
    resp = client.post(
        "/api/queries/add",
        json={"query": "q"},
        headers={"Authorization": login, **headers},
    )
    assert resp.status_code == 403


def test_the_origin_wins_over_the_referer(client):
    resp = client.post(
        "/api/queries/add",
        json={"query": "q"},
        headers={
            "Origin": "https://evil.example",
            "Referer": TEST_DASHBOARD_ORIGIN + "/",
        },
    )
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": TEST_DASHBOARD_ORIGIN},
        {"Origin": "HTTP://LOCALHOST"},
        {"Referer": TEST_DASHBOARD_ORIGIN + "/?tab=contacts"},
    ],
)
def test_a_write_from_the_dashboards_own_page_is_accepted(app, headers):
    client = anonymous(app.test_client())
    login = app.test_client().environ_base["HTTP_AUTHORIZATION"]
    resp = client.post(
        "/api/queries/add",
        json={"query": "q"},
        headers={"Authorization": login, **headers},
    )
    assert resp.status_code == 200


def test_reading_needs_no_origin(app):
    client = anonymous(app.test_client())
    login = app.test_client().environ_base["HTTP_AUTHORIZATION"]
    assert client.get("/api/stats", headers={"Authorization": login}).status_code == 200


def test_stop_needs_json_like_every_other_write(client):
    assert client.post("/api/agent/stop").status_code == 415
    assert (
        client.post("/api/agent/stop", data="{}", content_type="text/plain").status_code
        == 415
    )
    assert client.post("/api/agent/stop", json={}).status_code == 404  # none running


# ── Size ──


def test_a_request_over_the_limit_gets_a_clear_413(client, copy_of_templates):
    before = (copy_of_templates / "email_template.html").read_bytes()
    big = "x" * (main.MAX_REQUEST_BYTES + 1)
    resp = client.post("/api/template", json={"name": "email_template", "content": big})
    assert resp.status_code == 413
    assert "1 MB" in resp.get_json()["error"]
    assert (copy_of_templates / "email_template.html").read_bytes() == before


def test_the_largest_real_body_still_fits(client, copy_of_templates):
    """A template save is the largest body the dashboard takes; a template
    a hundred times the size of the largest shipped one still saves."""
    largest = max(p.stat().st_size for p in REAL_TEMPLATES.glob("*.html") if "dash" not in p.name)
    assert main.MAX_REQUEST_BYTES >= 100 * largest
    content = (copy_of_templates / "email_template.html").read_text(encoding="utf-8")
    content += "<!-- " + "padding " * ((main.MAX_REQUEST_BYTES - 4096) // 8) + "-->"
    resp = client.post(
        "/api/template", json={"name": "email_template", "content": content}
    )
    assert resp.status_code == 200, resp.get_json()


# ── Shape ──

WRONG_TYPES = [0, 7, 1.5, True, False, [], ["x"], {}, {"a": "b"}]
TEXT_FIELDS = [
    ("/api/send-manual", {"email": "x@brand.de"}, "email"),
    ("/api/send-manual", {"email": "x@brand.de"}, "name"),
    ("/api/send-manual", {"email": "x@brand.de"}, "organization"),
    ("/api/send-manual", {"email": "x@brand.de"}, "role"),
    ("/api/send-manual", {"email": "x@brand.de"}, "channel"),
    ("/api/queries/add", {"query": "q"}, "query"),
    ("/api/template", {"name": "email_template", "content": "x"}, "name"),
    ("/api/template", {"name": "email_template", "content": "x"}, "content"),
    ("/api/agent/start", {"mode": "dry_run"}, "mode"),
    ("/api/contacts/1/action", {"action": "block"}, "action"),
    ("/api/contacts/1/language", {"language": "it"}, "language"),
]


@pytest.mark.parametrize("url,body,field", TEXT_FIELDS)
@pytest.mark.parametrize("value", WRONG_TYPES, ids=repr)
def test_a_wrong_type_is_a_400_never_a_500(
    client, copy_of_templates, url, body, field, value
):
    resp = client.post(url, json={**body, field: value})
    assert resp.status_code == 400, (url, field, value, resp.get_data(as_text=True))


@pytest.mark.parametrize("key", sorted(RUNTIME_EDITABLE))
@pytest.mark.parametrize("value", [[], ["1"], {}, {"a": 1}, "not a number"], ids=repr)
def test_a_setting_of_the_wrong_type_is_a_400(client, key, value):
    resp = client.post("/api/config", json={"settings": {key: value}})
    assert resp.status_code == 400, (key, value, resp.get_data(as_text=True))


@pytest.mark.parametrize("body", [[1, 2], "text", 5, None, True])
def test_a_body_that_is_not_an_object_is_never_a_500(client, body):
    for url in ("/api/send-manual", "/api/queries/add", "/api/config", "/api/template"):
        resp = client.post(url, json=body)
        assert resp.status_code in (400, 415), (url, body, resp.status_code)


# ── The page's own policy ──


def _csp(resp):
    directives = {}
    for part in resp.headers["Content-Security-Policy"].split(";"):
        name, *values = part.split()
        directives[name] = values
    return directives


def test_the_policy_is_strict(client):
    csp = _csp(client.get("/"))
    assert csp["default-src"] == ["'self'"]
    assert csp["object-src"] == ["'none'"]
    assert csp["base-uri"] == ["'none'"]
    assert csp["form-action"] == ["'self'"]
    assert csp["frame-ancestors"] == ["'none'"]
    for directive in ("script-src", "style-src", "font-src"):
        for source in csp[directive]:
            assert source != "*" and not source.endswith("://*"), (directive, source)
    assert "'unsafe-inline'" not in csp["script-src"]
    assert "'unsafe-eval'" not in csp["script-src"]
    assert csp["script-src"] == ["'self'"]
    assert csp["style-src"] == ["'self'", "'unsafe-inline'"]
    assert csp["font-src"] == ["'self'"]


def test_every_api_answer_carries_the_policy(client):
    for path in ("/api/stats", "/api/config", "/no-such-page"):
        assert "default-src 'self'" in client.get(path).headers["Content-Security-Policy"]


def test_the_page_loads_nothing_from_another_server(client):
    """Scripts and stylesheets come from the dashboard itself (links the
    operator clicks are not loaded); see
    tests/test_the_dashboard_serves_its_own_libraries.py."""
    html = client.get("/").get_data(as_text=True)
    scripts = re.findall(r'<script[^>]*src="([^"]+)"', html)
    styles = re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', html)
    assert scripts == ["/static/vendor/chartjs/chart.umd.js", "/static/js/dashboard.js"]
    assert styles == [
        "/static/vendor/fontawesome/css/all.min.css",
        "/static/css/dashboard.css",
    ]


def test_the_page_has_no_inline_script():
    """No inline <script> body and no on...= handler in the page, nor in the
    markup its script builds: under the policy they would not run."""
    page = (REAL_TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page)
    assert not re.search(r"\son[a-z]+\s*=", page, flags=re.IGNORECASE)
    script = (main.PROJECT_ROOT / "static" / "js" / "dashboard.js").read_text(
        encoding="utf-8"
    )
    code = re.sub(r"^\s*//.*$", "", script, flags=re.MULTILINE)
    handler = re.search(r"""\son[a-z]{3,}=\\?["']""", code)
    assert not handler, f"markup with a handler: {handler and handler.group(0)}"
    assert "javascript:" not in code


def test_the_template_preview_is_sandboxed(client):
    html = client.get("/").get_data(as_text=True)
    frame = re.search(r'<iframe[^>]*id="templatePreview"[^>]*>', html, flags=re.DOTALL)
    assert frame, "no preview frame"
    sandbox = re.search(r'sandbox="([^"]*)"', frame.group(0))
    assert sandbox is not None, "the preview frame has no sandbox"
    assert sandbox.group(1).split() == [], "the sandbox allows something"


# ── /track/ ──


def test_track_is_not_served_while_open_tracking_is_off(settings_factory):
    app = main.create_dashboard_app(settings_factory())
    assert not [r for r in app.url_map.iter_rules() if r.rule.startswith("/track/")]
    assert anonymous(app.test_client()).get("/track/anything").status_code == 404


def test_track_has_its_own_limit_when_on(settings_factory, monkeypatch):
    from modules.analytics import Analytics

    recorded = []
    monkeypatch.setattr(Analytics, "record_open", lambda self, tid: recorded.append(tid))
    s = settings_factory(OPEN_TRACKING_ENABLED=True)
    client = anonymous(main.create_dashboard_app(s).test_client())
    for i in range(main.TRACK_REQUESTS_PER_MINUTE):
        assert client.get(f"/track/t{i}").status_code == 200
    over = client.get("/track/one-too-many")
    assert over.status_code == 429
    assert int(over.headers["Retry-After"]) >= 1
    assert "one-too-many" not in recorded
    assert len(recorded) == main.TRACK_REQUESTS_PER_MINUTE
    other = client.get("/track/elsewhere", environ_base={"REMOTE_ADDR": "198.51.100.9"})
    assert other.status_code == 200
