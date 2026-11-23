"""Regression tests for the Batch 4 web-security defects."""

import main

# Routes that require a JSON body. /api/agent/stop reads none, but requires
# the header all the same, so a cross-origin form cannot reach it either.
JSON_BODY_ROUTES = [
    ("/api/agent/start", {"mode": "single"}),
    ("/api/agent/stop", {"why": "form"}),
    ("/api/queries/add", {"query": "test query"}),
    ("/api/send-manual", {"email": "x@y.de"}),
]


def _client(tmp_path, settings_factory, **overrides):
    s = settings_factory(db_path=str(tmp_path / "b4.db"), **overrides)
    return main.create_dashboard_app(s).test_client()


# ── 1. CSRF: a cross-origin form must not reach these routes ─────────────────
# get_json(force=True) parsed the body whatever the Content-Type, so a plain
# HTML form on another origin could POST here. enctype="text/plain" is a
# CORS-simple request, so no preflight fires and the browser attaches the
# operator's HTTP Basic credentials automatically - meaning an attacker's page
# could start a campaign or send mail from the operator's own mailbox.


def test_form_encoded_bodies_are_refused(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    for path, payload in JSON_BODY_ROUTES:
        resp = client.post(path, data=payload)  # form encoding, no JSON header
        assert resp.status_code == 415, (
            f"{path} accepted a form-encoded body ({resp.status_code}) - "
            "reachable by a cross-origin form"
        )


def test_text_plain_bodies_are_refused(tmp_path, settings_factory):
    """enctype="text/plain" is the specific CORS-simple vector."""
    client = _client(tmp_path, settings_factory)
    for path, _payload in JSON_BODY_ROUTES:
        resp = client.post(path, data='{"mode":"single"}', content_type="text/plain")
        assert resp.status_code == 415, f"{path} accepted text/plain"


def test_proper_json_is_still_accepted(tmp_path, settings_factory):
    """The over-correction guard: the real dashboard must keep working."""
    client = _client(tmp_path, settings_factory)
    resp = client.post("/api/queries/add", json={"query": "still works"})
    assert resp.status_code == 200
    assert resp.get_json().get("status") == "ok"


def test_cross_origin_writes_are_rejected(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    resp = client.post(
        "/api/queries/add",
        json={"query": "q"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_same_origin_writes_are_allowed(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    resp = client.post(
        "/api/queries/add", json={"query": "q"}, headers={"Origin": "http://localhost"}
    )
    assert resp.status_code == 200


def test_one_click_unsubscribe_survives_the_origin_check(tmp_path, settings_factory):
    """Mailbox providers POST server-to-server, with no login and no Origin;
    this must not be blocked."""
    from conftest import anonymous

    client = anonymous(_client(tmp_path, settings_factory))
    resp = client.post("/unsubscribe/sometoken")
    assert resp.status_code != 403
    assert 200 <= resp.status_code < 300


# ── 2. Clickjacking headers ──────────────────────────────────────────────────


def test_security_headers_present(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    resp = client.get("/api/stats")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in resp.headers.get("Content-Security-Policy", "")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


# ── 3. limit=-1 dumped the entire prospect database ──────────────────────────
# type=int rejects non-numeric, but -1 passed through to SQLite LIMIT -1, which
# means unlimited.


def test_negative_limit_does_not_dump_everything(tmp_path, settings_factory):
    """`?limit=-1` must behave like the default, not like "unlimited".

    Deliberately derives the route's default instead of hardcoding it. An
    earlier version asserted `<= 50` against a route that actually defaults to
    200, so with fewer rows than the default the clamped and unclamped results
    were identical and the test passed while proving nothing. The seeding is
    also validated below, so if this test ever loses the ability to tell the
    two apart it FAILS rather than going quietly green.
    """
    s = settings_factory(db_path=str(tmp_path / "lim.db"))
    app = main.create_dashboard_app(s)
    from modules.database import Database

    db = Database(str(tmp_path / "lim.db"))
    sid = db.log_search("q", source="t")
    total = 250
    for i in range(total):
        db.add_contact(sid, f"c{i}@brand.de")

    client = app.test_client()
    default_rows = len(client.get("/api/contacts").get_json())

    # Self-check: unless the table holds more rows than the route's default,
    # a clamped and an unclamped query return the same thing.
    assert total > default_rows, (
        f"test cannot discriminate: seeded {total} rows but the route default "
        f"returns {default_rows}. Seed more rows."
    )

    unbounded = client.get("/api/contacts?limit=-1").get_json()
    assert len(unbounded) == default_rows, (
        f"limit=-1 returned {len(unbounded)} rows against a default of "
        f"{default_rows} - LIMIT -1 means unlimited in SQLite"
    )


def test_absurd_limit_is_capped(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    resp = client.get("/api/contacts?limit=99999999")
    assert resp.status_code == 200


def test_normal_limit_still_honoured(tmp_path, settings_factory):
    """Guard: clamping must not break ordinary paging."""
    s = settings_factory(db_path=str(tmp_path / "lim2.db"))
    app = main.create_dashboard_app(s)
    from modules.database import Database

    db = Database(str(tmp_path / "lim2.db"))
    sid = db.log_search("q", source="t")
    for i in range(10):
        db.add_contact(sid, f"n{i}@brand.de")

    rows = app.test_client().get("/api/contacts?limit=3").get_json()
    assert len(rows) == 3


# ── 4. Raw exception text must not reach the client ──────────────────────────
# str(e) leaked absolute filesystem paths from read/write failures, and SMTP
# exceptions routinely embed the server banner and the authenticating username.


def test_template_read_failure_does_not_leak_the_path(
    tmp_path, settings_factory, monkeypatch
):
    client = _client(tmp_path, settings_factory)

    def _boom(*a, **kw):
        raise OSError(r"C:\Users\secret\path\email_template.html is unreadable")

    monkeypatch.setattr("pathlib.Path.read_text", _boom)
    resp = client.get("/api/template")

    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "secret" not in body, f"filesystem path leaked: {body}"
    assert r"C:\Users" not in body


def test_stop_is_protected_cross_origin(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    resp = client.post(
        "/api/agent/stop", json={}, headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403


# ── Coverage: every state-changing route must be CSRF-protected ──────────────
# The hand-written JSON_BODY_ROUTES list above got /api/agent/stop wrong (it
# parses no body). Enumerating the URL map instead means a newly added
# unprotected route fails this test rather than waiting to be noticed.


def _concrete_url(rule):
    """Build a callable URL from a rule, substituting dummy path params."""
    url = rule.rule
    for arg in rule.arguments:
        converter = rule._converters[arg].__class__.__name__.lower()
        dummy = "1" if "integer" in converter else "x"
        url = url.replace(f"<{arg}>", dummy)
        for conv in ("int", "string", "path", "float"):
            url = url.replace(f"<{conv}:{arg}>", dummy)
    return url


def test_every_state_changing_route_rejects_cross_origin(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "cov.db"))
    app = main.create_dashboard_app(s)
    client = app.test_client()

    exempt = ("/track/", "/unsubscribe/")  # recipient-facing, must stay public
    checked, unprotected = [], []

    for rule in app.url_map.iter_rules():
        methods = rule.methods - {"HEAD", "OPTIONS"}
        if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        url = _concrete_url(rule)
        if any(url.startswith(p) for p in exempt):
            continue
        checked.append(url)
        resp = client.post(
            url, json={}, headers={"Origin": "https://evil.example"}
        )
        if resp.status_code != 403:
            unprotected.append(f"{url} -> {resp.status_code}")

    assert checked, "no state-changing routes discovered - the scan is broken"
    assert not unprotected, (
        "these state-changing routes accept cross-origin requests: "
        + ", ".join(unprotected)
    )
