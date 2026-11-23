"""The dashboard, the settings and the container image hold nothing open.

The dashboard started on any address with no password, and with Flask's
debugger, which runs any code it is sent, wherever DEBUG was on. It answered
whatever Host a request named. Settings printed their secrets in any repr.
The page fetched its font from Google, sending each viewer's address there.
The Docker image ran as root and carried the client list, and an image built
without that list switched the client screen off with nothing but a log line.
Nothing kept exports, backups or the client list out of git.

Now (step G15, tightened by the 2026-11 security hardening):
- the dashboard refuses to start without a password of 12 characters or
  more, wherever it listens, and never runs the debugger, whatever DEBUG
  says: waitress serves it, and Flask's own server runs only on request
  (--dev-server) and on this computer; doctor fails on the same rule (see also
  tests/test_the_dashboard_always_needs_a_password.py);
- requests naming a host the dashboard does not serve are refused
  (TRUSTED_HOSTS, Flask 3.1);
- secrets are left out of a settings repr;
- no third-party font;
- live sends are refused, and doctor fails, while the client screen is on
  but its list never loaded;
- the image runs as a user other than root and leaves out the client list
  and the data folder; git ignores the client list, exports and backups.
"""

import re
from types import SimpleNamespace

import flask
import pytest
import waitress

import main
from agent import OutreachAgent
from main import PROJECT_ROOT as ROOT
from main import run_doctor_checks

LOCAL = "http://127.0.0.1:5000"
GOOD = "a-good-dashboard-password"


def _runs(settings_factory, tmp_path, monkeypatch, args=None, **kw):
    """The servers started: Flask's (--dev-server) or waitress, by kwargs."""
    started = []
    monkeypatch.setattr(flask.Flask, "run", lambda self, **k: started.append(k))
    monkeypatch.setattr(waitress, "serve", lambda app, **k: started.append(k))
    s = settings_factory(db_path=str(tmp_path / "d.db"), **kw)
    try:
        main.cmd_dashboard(args, s)
    except SystemExit as e:
        assert e.code == 1
    return started


def _start(settings_factory, tmp_path, monkeypatch, **kw):
    return bool(_runs(settings_factory, tmp_path, monkeypatch, **kw))


@pytest.mark.parametrize(
    "kw,starts",
    [
        ({"dashboard_host": "127.0.0.1", "base_url": LOCAL}, False),
        ({"dashboard_host": "0.0.0.0", "base_url": LOCAL}, False),
        ({"dashboard_host": "127.0.0.1"}, False),  # public BASE_URL: a tunnel
        ({"dashboard_host": "0.0.0.0", "DASHBOARD_PASSWORD": "pw"}, False),
        ({"dashboard_host": "127.0.0.1", "base_url": LOCAL, "DASHBOARD_PASSWORD": GOOD}, True),
        ({"dashboard_host": "0.0.0.0", "DASHBOARD_PASSWORD": GOOD}, True),
    ],
)
def test_a_dashboard_needs_a_password_wherever_it_listens(
    settings_factory, tmp_path, monkeypatch, kw, starts
):
    kw.setdefault("DASHBOARD_PASSWORD", "")
    assert _start(settings_factory, tmp_path, monkeypatch, **kw) is starts


def test_the_debugger_never_faces_the_network(settings_factory, tmp_path, monkeypatch):
    """On 0.0.0.0 only waitress serves, which has no debugger; Flask's server
    is refused there, and on this computer runs without its debugger."""
    common = {"DASHBOARD_PASSWORD": GOOD, "debug": True, "base_url": LOCAL}
    dev = SimpleNamespace(dev_server=True)
    (served,) = _runs(settings_factory, tmp_path, monkeypatch, dashboard_host="0.0.0.0", **common)
    assert "debug" not in served and served["max_request_body_size"] == main.MAX_REQUEST_BYTES
    assert _runs(settings_factory, tmp_path, monkeypatch, dev, dashboard_host="0.0.0.0", **common) == []
    (run,) = _runs(settings_factory, tmp_path, monkeypatch, dev, dashboard_host="127.0.0.1", **common)
    assert run["debug"] is False and run["use_debugger"] is False


def _check(settings_factory, tmp_path, label, **kw):
    s = settings_factory(db_path=str(tmp_path / "doc.db"), **kw)
    return next(c for c in run_doctor_checks(s, network=False) if c.label == label)


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1"])
def test_doctor_blocks_a_dashboard_without_a_password(settings_factory, tmp_path, host):
    check = _check(
        settings_factory,
        tmp_path,
        "Dashboard password",
        dashboard_host=host,
        DASHBOARD_PASSWORD="",
    )
    assert check.ok is False and check.blocking


def test_a_request_for_another_host_is_refused(settings_factory, tmp_path):
    client = main.create_dashboard_app(
        settings_factory(db_path=str(tmp_path / "h.db"))
    ).test_client()
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "outreach.example.test"}).status_code != 400
    assert client.get("/", headers={"Host": "localhost:5000"}).status_code != 400


def test_secrets_stay_out_of_a_settings_repr(settings_factory):
    secrets = {
        "smtp_password": "S3cret-smtp",
        "sender_2_password": "S3cret-two",
        "imap_password": "S3cret-imap",
        "unsubscribe_secret": "S3cret-unsub",
        "DASHBOARD_PASSWORD": "S3cret-dash",
        "LLM_API_KEY": "S3cret-llm",
        "ERASURE_KEY": "S3cret-erasure",
    }
    s = settings_factory(**secrets)
    for value in secrets.values():
        assert value not in repr(s) and value not in str(s), value


def test_the_page_fetches_no_third_party_font():
    for path in [*ROOT.glob("templates/*.html"), *ROOT.glob("static/**/*.css")]:
        text = path.read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in text, path.name
        assert "fonts.gstatic.com" not in text, path.name


def test_flask_is_pinned_where_trusted_hosts_exist():
    line = next(
        line
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.lower().startswith("flask")
    )
    assert re.search(r">=\s*3\.1\b", line), line


# ── The client screen without its list ──


def test_live_sends_stop_while_the_client_list_never_loaded(settings_factory, tmp_path):
    agent = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            client_filter_enabled=True,
            client_list_path="no-such-list.xlsx",
        )
    )
    assert agent.client_register is None
    reason = agent._send_blocker(False)
    assert reason and "client list" in reason
    assert agent._send_blocker(True) is None, "a dry run still previews"


def test_doctor_blocks_on_a_missing_client_list(settings_factory, tmp_path):
    check = _check(
        settings_factory, tmp_path, "Client list file", client_list_path="nope.xlsx"
    )
    assert check.ok is False and check.blocking


# ── The image and the repository ──


def test_the_image_runs_as_a_user_other_than_root():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    users = re.findall(r"^USER\s+(\S+)", dockerfile, flags=re.MULTILINE)
    assert users and users[-1] not in ("root", "0")


def test_the_image_leaves_out_the_client_list_and_the_data():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "**/*.xlsx" in ignored and "data/" in ignored


def test_git_ignores_client_lists_exports_and_backups():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("*.xlsx", "data/exports/", "data/backups/", "data/spool/"):
        assert pattern in ignored, pattern
