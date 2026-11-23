"""DRY_RUN=true holds on the dashboard, as it does for `main.py run`.

Only `main.py run` read the setting. The dashboard's Start button posts mode
"continuous", and api_agent_start ran it live, as it did every mode but the
exact string "dry_run"; Direct Send read only the request's own flag. Someone
rehearsing the pilot with DRY_RUN=true sent real email from both buttons. A
mistyped mode ("dry-run") started a live run too, and the reply said it had
started in the mode asked for.

Now both runs are simulations while DRY_RUN is on, an unknown mode starts
nothing, Direct Send's Send is only a preview and answers as a refusal that
says why, and the status the page polls says whether DRY_RUN is on.
"""

import argparse
import threading

import pytest

import main
from modules.email_sender import EmailSender


def _settings(settings_factory, tmp_path, **overrides):
    return settings_factory(
        db_path=str(tmp_path / "d.db"),
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        warmup_enabled=False,
        attach_profile=False,
        **overrides,
    )


def _dashboard(settings_factory, tmp_path, **overrides):
    s = _settings(settings_factory, tmp_path, **overrides)
    return main.create_dashboard_app(s).test_client()


class _Runs:
    """Every agent the dashboard builds, and every run it starts."""

    def __init__(self):
        self.built = 0
        self.made = []
        self.started = threading.Event()

    def record(self, kind, dry_run):
        self.made.append((kind, dry_run))
        self.started.set()


@pytest.fixture
def runs(monkeypatch):
    """Stands in for OutreachAgent, so a run searches and sends nothing."""
    seen = _Runs()

    class _Agent:
        def __init__(self, settings):
            seen.built += 1

        def run_continuous(self, queries=None, dry_run=False, loop_delay=300.0):
            seen.record("continuous", dry_run)

        def run_single(self, queries=None, dry_run=False, **kw):
            seen.record("single", dry_run)

        def stop(self):
            pass

    monkeypatch.setattr(main, "OutreachAgent", _Agent)
    return seen


def _start(client, runs, mode):
    """POST Start; the run itself happens on the dashboard's own thread."""
    resp = client.post("/api/agent/start", json={"mode": mode})
    if resp.status_code == 200:
        assert runs.started.wait(5), "the dashboard said started, but nothing ran"
    return resp


@pytest.fixture
def wire(monkeypatch):
    """Every message handed to SMTP. Nothing leaves the machine."""
    sent = []

    def _deliver(self, msg, blocked=None):
        sent.append(msg)
        return True, "250 2.0.0 Ok", False, False

    monkeypatch.setattr(EmailSender, "_deliver", _deliver)
    return sent


def _direct_send(client, **extra):
    return client.post(
        "/api/send-manual",
        json={
            "email": "anna@brand.de",
            "organization": "Brand",
            "channel": "referral",
            **extra,
        },
    )


def test_start_simulates_like_the_cli_when_dry_run_is_on(
    tmp_path, settings_factory, runs
):
    s = _settings(settings_factory, tmp_path, dry_run=True)
    main.cmd_run(
        argparse.Namespace(
            dry_run=False, query=None, continuous=False, loop_delay=None
        ),
        s,
    )
    client = main.create_dashboard_app(s).test_client()

    assert _start(client, runs, "continuous").status_code == 200
    assert runs.made == [("single", True), ("continuous", True)], (
        "the CLI simulated, the dashboard's Start button ran live"
    )


@pytest.mark.parametrize(
    "mode, run",
    [("continuous", "continuous"), ("single", "single"), ("dry_run", "single")],
)
def test_every_start_mode_is_a_dry_run_when_dry_run_is_on(
    tmp_path, settings_factory, runs, mode, run
):
    client = _dashboard(settings_factory, tmp_path, dry_run=True)

    assert _start(client, runs, mode).status_code == 200
    assert runs.made == [(run, True)]


@pytest.mark.parametrize(
    "mode, made",
    [
        ("continuous", ("continuous", False)),
        ("single", ("single", False)),
        ("dry_run", ("single", True)),
    ],
)
def test_with_dry_run_off_only_the_dry_run_mode_simulates(
    tmp_path, settings_factory, runs, mode, made
):
    """The control: DRY_RUN off must not turn every run into a simulation."""
    client = _dashboard(settings_factory, tmp_path)

    assert _start(client, runs, mode).status_code == 200
    assert runs.made == [made]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("mode", ["dry-run", "DRY_RUN", "Continuous", None])
def test_an_unknown_mode_is_refused_and_starts_nothing(
    tmp_path, settings_factory, runs, mode, dry_run
):
    client = _dashboard(settings_factory, tmp_path, dry_run=dry_run)

    resp = _start(client, runs, mode)

    assert resp.status_code == 400, (
        f"mode {mode!r} answered {resp.status_code} {resp.get_json()} "
        f"and ran {runs.made}"
    )
    assert resp.get_json().get("error")
    assert runs.built == 0 and runs.made == []


def test_direct_send_is_only_a_preview_when_dry_run_is_on(
    tmp_path, settings_factory, wire
):
    client = _dashboard(settings_factory, tmp_path, dry_run=True)

    resp = _direct_send(client)
    body = resp.get_json()

    assert wire == [], f"DRY_RUN is on, yet SMTP got the message: {body}"
    assert "would send to anna@brand.de" in body["message"], "no preview ran"
    assert "Only a preview: DRY_RUN is on" in body["message"], body["message"]
    # Nothing went out, so the page must not show it as sent: the Spam /
    # inbox test card says "Sent." on any ok answer, and the form clears.
    assert body["ok"] is False and resp.status_code == 400, body


def test_a_preview_asked_for_still_succeeds_when_dry_run_is_on(
    tmp_path, settings_factory, wire
):
    client = _dashboard(settings_factory, tmp_path, dry_run=True)

    resp = _direct_send(client, dry_run=True)
    body = resp.get_json()

    assert wire == []
    assert resp.status_code == 200 and body["ok"] is True, body
    assert "DRY_RUN" not in body["message"], "the operator asked for this preview"


def test_direct_send_still_sends_when_dry_run_is_off(tmp_path, settings_factory, wire):
    """The control: these settings do send, so the preview above is DRY_RUN's."""
    client = _dashboard(settings_factory, tmp_path)

    resp = _direct_send(client)

    assert len(wire) == 1, resp.get_json()
    assert resp.status_code == 200
    message = resp.get_json()["message"]
    assert message.startswith("Email sent to anna@brand.de. Written in English."), (
        message
    )


@pytest.mark.parametrize("dry_run", [True, False])
def test_the_status_says_whether_dry_run_is_on(tmp_path, settings_factory, dry_run):
    client = _dashboard(settings_factory, tmp_path, dry_run=dry_run)

    assert client.get("/api/agent/status").get_json().get("dry_run") is dry_run
