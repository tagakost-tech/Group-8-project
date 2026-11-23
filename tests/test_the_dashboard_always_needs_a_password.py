"""The dashboard always needs a password of 12 characters or more.

It started on 127.0.0.1 with DASHBOARD_PASSWORD blank and answered without
credentials, so any other user or program on the machine (a shared office
PC, a browser extension, a local malware sample) could read every contact
and send mail as SUPPLECO through it; doctor only warned. A short password
fell to guessing in minutes.

Now the dashboard refuses to start, wherever it listens, when the password
is blank or shorter than 12 characters; the app is not even built
(create_dashboard_app raises), so no other entry point can serve it open;
and doctor reports the same rule as a blocking failure. The dashboard never
runs Werkzeug's debugger, whatever DEBUG says, on Flask's development server
(--dev-server) either; waitress, its usual server, has none.
"""

from types import SimpleNamespace

import flask
import pytest
import waitress

import main

LABEL = "Dashboard password"
LOCAL = "http://127.0.0.1:5000"
PUBLIC = "https://out.example.com"
WHERE = [
    {"base_url": LOCAL, "dashboard_host": "127.0.0.1"},
    {"base_url": LOCAL, "dashboard_host": "localhost"},
    {"base_url": LOCAL, "dashboard_host": "::1"},
    {"base_url": LOCAL, "dashboard_host": "0.0.0.0"},
    {"base_url": PUBLIC, "dashboard_host": "127.0.0.1"},
]
REFUSED = ["", "   ", "short-pw", "elevenchars", " " * 12]
ACCEPTED = ["twelve-chars", "Perché2026!x", "a much longer pass phrase"]


def _started(settings, monkeypatch, args=None):
    """The servers started: Flask's (--dev-server) or waitress, by kwargs."""
    runs = []
    monkeypatch.setattr(flask.Flask, "run", lambda self, **k: runs.append(k))
    monkeypatch.setattr(waitress, "serve", lambda app, **k: runs.append(k))
    try:
        main.cmd_dashboard(args, settings)
    except SystemExit as e:
        assert e.code == 1
    return runs


def _doctor(settings):
    return next(
        c for c in main.run_doctor_checks(settings, network=False) if c.label == LABEL
    )


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("password", REFUSED)
def test_a_blank_or_short_password_stops_the_dashboard_anywhere(
    settings_factory, monkeypatch, capsys, where, password
):
    s = settings_factory(DASHBOARD_PASSWORD=password, **where)
    assert _started(s, monkeypatch) == []
    out = capsys.readouterr().out
    assert "Refusing to start" in out and "DASHBOARD_PASSWORD" in out
    assert password.strip() == "" or password not in out


@pytest.mark.parametrize("password", REFUSED)
def test_the_app_is_not_built_without_a_good_password(settings_factory, password):
    with pytest.raises(ValueError, match="DASHBOARD_PASSWORD"):
        main.create_dashboard_app(settings_factory(DASHBOARD_PASSWORD=password))


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("password", ACCEPTED)
def test_a_password_of_twelve_characters_or_more_starts(
    settings_factory, monkeypatch, where, password
):
    s = settings_factory(DASHBOARD_PASSWORD=password, **where)
    assert len(_started(s, monkeypatch)) == 1
    check = _doctor(s)
    assert check.ok is True and check.detail == "set"


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize("password", REFUSED)
def test_doctor_blocks_on_the_same_rule(settings_factory, where, password):
    check = _doctor(settings_factory(DASHBOARD_PASSWORD=password, **where))
    assert check.ok is False and check.blocking
    assert "DASHBOARD_PASSWORD" in check.detail


def test_doctor_prints_a_failure_and_exits(settings_factory, monkeypatch, capsys):
    s = settings_factory(DASHBOARD_PASSWORD="", base_url=LOCAL)
    real = main.run_doctor_checks
    monkeypatch.setattr(
        main,
        "run_doctor_checks",
        lambda *a, **k: [c for c in real(s, network=False) if c.label == LABEL],
    )
    with pytest.raises(SystemExit) as stopped:
        main.cmd_doctor(type("A", (), {"smtp": False})(), s)
    assert stopped.value.code == 1
    assert "[FAIL] Dashboard password" in capsys.readouterr().out


def test_a_rule_that_cannot_decide_still_blocks(settings_factory, monkeypatch):
    """Doctor must report a rule that raised, and neither crash nor pass it."""

    def broken(_settings):
        raise RuntimeError("cannot read the settings")

    monkeypatch.setattr(main, "dashboard_password_refusal", broken)
    check = _doctor(settings_factory())
    assert check.ok is False and check.blocking
    assert "error" in check.detail


def test_the_refusal_names_the_rule_not_the_password(settings_factory):
    refusal = main.dashboard_password_refusal(
        settings_factory(DASHBOARD_PASSWORD="hunter2-abc")
    )
    assert "12 characters" in refusal
    assert "hunter2" not in refusal


# ── Werkzeug's debugger ──


@pytest.mark.parametrize("debug", [True, False])
def test_the_debugger_never_runs_whatever_debug_says(
    settings_factory, monkeypatch, debug
):
    s = settings_factory(debug=debug, dashboard_host="127.0.0.1", base_url=LOCAL)
    (run,) = _started(s, monkeypatch, SimpleNamespace(dev_server=True))
    assert run["debug"] is False
    assert run["use_debugger"] is False
    assert run["use_reloader"] is False


def test_flask_debug_in_the_environment_does_not_switch_it_on(
    settings_factory, monkeypatch
):
    monkeypatch.setenv("FLASK_DEBUG", "1")
    app = main.create_dashboard_app(settings_factory(debug=True))
    assert app.debug is False

    @app.route("/boom")
    def boom():
        raise RuntimeError("secret-traceback-detail")

    resp = app.test_client().get("/boom")
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "secret-traceback-detail" not in body
    assert "Traceback" not in body and "Werkzeug Debugger" not in body
