"""The dashboard is served by waitress, a production web server.

It ran on Flask's own development server, which is built for one developer
on one computer and is not hardened against slow or malformed requests, nor
against a body of any size: it reads what it is sent. Now `main.py
dashboard` serves the same checked app with waitress, which refuses a body
over the dashboard's 1 MB limit before reading it. Flask's server runs only
with --dev-server, and only when nothing outside this computer can reach the
dashboard: not when DASHBOARD_HOST listens beyond it, and not behind a public
BASE_URL (a tunnel or proxy). Without waitress the dashboard refuses to
start, rather than fall back to the development server unnoticed.

Here the app waitress is handed is the one create_dashboard_app built, and a
real waitress, started with the same settings, applies every check: the
password and its lockout, the host names, the origin check, the policy and
the size limit.
"""

import base64
import http.client
import socket
import sys
import threading
from types import SimpleNamespace

import flask
import pytest

import main
from conftest import TEST_DASHBOARD_LOGIN, anonymous

waitress = pytest.importorskip("waitress")

LOCAL = "http://127.0.0.1:5000"
DEV = SimpleNamespace(dev_server=True)


@pytest.fixture
def servers(monkeypatch):
    """Which server cmd_dashboard started, with what, and the app it built."""
    started = []
    built = []
    real_build = main.create_dashboard_app

    def build(settings):
        app = real_build(settings)
        built.append(app)
        return app

    monkeypatch.setattr(main, "create_dashboard_app", build)
    monkeypatch.setattr(
        waitress, "serve", lambda app, **k: started.append(("waitress", app, k))
    )
    monkeypatch.setattr(
        flask.Flask, "run", lambda self, **k: started.append(("flask", self, k))
    )
    return SimpleNamespace(started=started, built=built)


def _dashboard(settings, args=None):
    try:
        main.cmd_dashboard(args, settings)
    except SystemExit as e:
        assert e.code == 1
        return False
    return True


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "::1"])
def test_waitress_serves_the_app_create_dashboard_app_checked(
    settings_factory, servers, host
):
    assert _dashboard(settings_factory(dashboard_host=host, dashboard_port=5123))
    ((server, app, kwargs),) = servers.started
    assert server == "waitress"
    assert app is servers.built[0]
    assert kwargs["host"] == host and kwargs["port"] == 5123
    assert kwargs["max_request_body_size"] == main.MAX_REQUEST_BYTES
    assert "trusted_proxy" not in kwargs
    assert anonymous(app.test_client()).get("/").status_code == 401


def test_the_parser_offers_the_development_server_only_by_name():
    parser = main.build_parser()
    assert parser.parse_args(["dashboard"]).dev_server is False
    assert parser.parse_args(["dashboard", "--dev-server"]).dev_server is True


@pytest.mark.parametrize(
    "where",
    [
        {"dashboard_host": "0.0.0.0", "base_url": LOCAL},
        {"dashboard_host": "::", "base_url": LOCAL},
        {"dashboard_host": "192.168.1.20", "base_url": LOCAL},
        {"dashboard_host": "127.0.0.1", "base_url": "https://out.example.com"},
    ],
)
def test_the_development_server_is_refused_when_others_can_reach_it(
    settings_factory, servers, capsys, where
):
    assert not _dashboard(settings_factory(**where), DEV)
    assert servers.started == [] and servers.built == []
    out = capsys.readouterr().out
    assert "Refusing to start" in out and "--dev-server" in out


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_the_development_server_runs_on_this_computer_only_when_asked(
    settings_factory, servers, host
):
    assert _dashboard(settings_factory(dashboard_host=host, base_url=LOCAL), DEV)
    ((server, app, kwargs),) = servers.started
    assert server == "flask" and app is servers.built[0]
    assert kwargs["debug"] is False and kwargs["use_debugger"] is False


def test_without_waitress_the_dashboard_refuses_rather_than_fall_back(
    settings_factory, servers, monkeypatch, capsys
):
    monkeypatch.setitem(sys.modules, "waitress", None)
    assert not _dashboard(settings_factory(dashboard_host="127.0.0.1", base_url=LOCAL))
    assert servers.started == []
    assert "waitress" in capsys.readouterr().out


# ── A real waitress, with the settings cmd_dashboard gives it ──


@pytest.fixture
def live(settings_factory, servers):
    """The app and settings cmd_dashboard handed waitress, served by a real
    waitress on a free port of this computer."""
    assert _dashboard(settings_factory(dashboard_host="127.0.0.1"))
    ((_, app, kwargs),) = servers.started
    kwargs = {k: v for k, v in kwargs.items() if k not in ("host", "port")}
    server = waitress.create_server(app, host="127.0.0.1", port=0, **kwargs)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        yield server.effective_port
    finally:
        server.close()
        thread.join(timeout=10)


_LOGIN = "Basic " + base64.b64encode(
    f"{TEST_DASHBOARD_LOGIN['username']}:{TEST_DASHBOARD_LOGIN['password']}".encode()
).decode()


def _ask(port, method="GET", path="/", headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def test_waitress_asks_for_the_password_and_sends_the_policy(live):
    status, headers, _ = _ask(live)
    assert status == 401
    status, headers, _ = _ask(live, headers={"Authorization": _LOGIN})
    assert status == 200
    assert headers["Content-Security-Policy"] == main.DASHBOARD_CSP
    assert headers["X-Frame-Options"] == "DENY"


def test_waitress_refuses_a_host_name_the_dashboard_does_not_answer_to(live):
    status, _, _ = _ask(live, headers={"Authorization": _LOGIN, "Host": "evil.example"})
    assert status == 400


def test_waitress_refuses_a_write_from_another_site(live):
    status, _, _ = _ask(
        live,
        "POST",
        "/api/agent/stop",
        headers={
            "Authorization": _LOGIN,
            "Origin": "http://evil.example",
            "Content-Type": "application/json",
        },
        body=b"{}",
    )
    assert status == 403


def test_waitress_locks_out_a_guesser_whatever_address_it_claims(live):
    for n in range(main.LOGIN_FAILURES_ALLOWED):
        wrong = "Basic " + base64.b64encode(f"admin:wrong-{n}".encode()).decode()
        headers = {"Authorization": wrong, "X-Forwarded-For": f"203.0.113.{n}"}
        assert _ask(live, headers=headers)[0] == 401
    status, _, _ = _ask(live, headers={"Authorization": _LOGIN})
    assert status == 429


def test_waitress_refuses_a_large_body_before_reading_it(live):
    """The body is never sent: a server that waited to read it would time
    out here, as it would hold a 1 GB upload in a temporary file."""
    with socket.create_connection(("127.0.0.1", live), timeout=5) as sock:
        sock.sendall(
            b"POST /api/template HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {main.MAX_REQUEST_BYTES + 1}\r\n\r\n".encode()
        )
        try:
            answer = sock.recv(200)
        except TimeoutError:
            answer = b""
    assert answer.startswith(b"HTTP/1.1 413"), answer
