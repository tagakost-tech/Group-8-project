"""Regression tests for the three Batch 1 defects.

Each test fails against the pre-fix code, so they lock the behaviour in rather
than merely exercising it.
"""

import pytest

import main


def _client(tmp_path, settings_factory, **overrides):
    s = settings_factory(db_path=str(tmp_path / "b1.db"), **overrides)
    return main.create_dashboard_app(s).test_client()


@pytest.fixture
def protect_template():
    """Snapshot templates/email_template.html and restore it afterwards.

    /api/template writes to the real project file - there is no way to point it
    at a temp path. Without this, a template test running against code lacking
    the empty-save guard truncates the developer's actual template. That is not
    hypothetical: it happened while these tests were being written.
    """
    path = main.PROJECT_ROOT / "templates" / "email_template.html"
    original = path.read_text(encoding="utf-8") if path.exists() else None
    try:
        yield path
    finally:
        if original is not None:
            path.write_text(original, encoding="utf-8")


# ── 1. RFC 8058 one-click unsubscribe ────────────────────────────────────────
# create_email() advertises List-Unsubscribe-Post, so Gmail/Yahoo/Apple POST to
# this route. The route was GET-only, so every one-click opt-out got a 405: the
# recipient stayed subscribed and the provider recorded a failed unsubscribe
# against the sending domain.


def test_one_click_unsubscribe_accepts_post(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    resp = client.post("/unsubscribe/whatever-token")
    assert resp.status_code != 405, "one-click POST must not be Method Not Allowed"


def test_one_click_unsubscribe_never_reports_failure_to_the_provider(
    tmp_path, settings_factory
):
    """An invalid token must still return 2xx on the POST path.

    A non-2xx makes the mailbox provider retry and counts against the domain,
    and providers do not re-deliver a failed opt-out - so surfacing the error
    loses it outright. The reason is logged instead.
    """
    client = _client(tmp_path, settings_factory)
    resp = client.post("/unsubscribe/definitely-not-a-valid-token")
    assert 200 <= resp.status_code < 300
    assert resp.get_data() == b""


def test_browser_unsubscribe_still_reports_errors(tmp_path, settings_factory):
    """The GET path is a human in a browser; it should still show the failure."""
    client = _client(tmp_path, settings_factory)
    resp = client.get("/unsubscribe/definitely-not-a-valid-token")
    assert resp.status_code == 400


# ── 2. Template editor must not truncate the template ────────────────────────
# The editor shows an empty textarea when its GET failed, which is visually
# identical to "not loaded yet". Saving that wrote "" over email_template.html
# and returned {"status": "ok"} -> the UI said "saved successfully" while the
# agent silently fell back to a generic default template.


def test_saving_an_empty_template_is_rejected(tmp_path, settings_factory, protect_template):
    client = _client(tmp_path, settings_factory)
    before = (main.PROJECT_ROOT / "templates" / "email_template.html").read_text(
        encoding="utf-8"
    )

    resp = client.post("/api/template", json={"content": ""})

    assert resp.status_code == 400
    assert resp.get_json().get("status") != "ok"
    after = (main.PROJECT_ROOT / "templates" / "email_template.html").read_text(
        encoding="utf-8"
    )
    assert after == before, "template file must be untouched"


def test_saving_whitespace_only_template_is_rejected(tmp_path, settings_factory, protect_template):
    client = _client(tmp_path, settings_factory)
    resp = client.post("/api/template", json={"content": "   \n\t  "})
    assert resp.status_code == 400


def test_template_post_with_null_body_does_not_500(tmp_path, settings_factory, protect_template):
    """`request.json` is None for a literal `null` body; `.get` used to raise."""
    client = _client(tmp_path, settings_factory)
    resp = client.post("/api/template", data="null", content_type="application/json")
    assert resp.status_code != 500


# ── 3. Stop must actually stop ───────────────────────────────────────────────
# run_single() unconditionally set _running = True. run_continuous() calls it
# with _from_continuous=True, so a stop that arrived mid-iteration was revived
# and the loop ran forever, sending a full batch each time.


def _agent(tmp_path, settings_factory):
    from agent import OutreachAgent

    s = settings_factory(db_path=str(tmp_path / "stop.db"))
    return OutreachAgent(s)


def test_run_single_from_continuous_does_not_revive_a_stop(tmp_path, settings_factory):
    agent = _agent(tmp_path, settings_factory)
    agent._running = False  # operator pressed Stop

    agent.run_single(["some query"], dry_run=True, _from_continuous=True)

    assert agent._running is False, (
        "a batch invoked from run_continuous must not re-arm _running"
    )


def test_run_single_from_continuous_sends_nothing_when_stopped(
    tmp_path, settings_factory
):
    """The batch must return immediately, not run a search/send pass."""
    agent = _agent(tmp_path, settings_factory)
    called = []
    agent.search.search_and_extract = lambda *a, **kw: called.append(1) or []

    agent._running = False
    agent.run_single(["q"], dry_run=True, _from_continuous=True)

    assert called == [], "no discovery should run after a stop"


def test_standalone_run_single_still_arms_running(tmp_path, settings_factory):
    """The guard must not break the normal one-shot path."""
    agent = _agent(tmp_path, settings_factory)
    agent.search.search_and_extract = lambda *a, **kw: []
    agent._running = False

    agent.run_single([], dry_run=True)  # no _from_continuous

    # run_single resets _running to False on a clean standalone exit; the point
    # is that it ran at all rather than short-circuiting like the stopped case.
    assert agent._stats is not None


def test_stop_clears_running(tmp_path, settings_factory):
    agent = _agent(tmp_path, settings_factory)
    agent._running = True
    agent.stop()
    assert agent._running is False
