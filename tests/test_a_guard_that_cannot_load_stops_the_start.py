"""A guard that cannot be loaded stops the start. It does not switch itself off.

The agent imported each of its components inside try/except ImportError, and
on a failure logged one warning and ran without it. For a guard - the daily
sending limit, the limit per company, the relevance filter, the email
verifier, lead scoring's minimum score, the unsubscribe handler, the inbox
monitor that reads opt-outs and bounces, the blocklist check, the AI
relevance check - that meant a broken install (a package missing after an
update, a file lost) sent mail with the guard off, and every send path read
the missing guard as "switched off in the settings".

Now a guard the settings switch on that cannot be imported raises
GuardUnavailable: no agent is built, `main.py run` and the dashboard's Start
and Send say which component is missing, and nothing is sent. An extra (the
retry queue, follow-ups, open tracking, the AI that words the emails,
discovery, learning) only adds mail or a record; the agent still runs without
it, and says so where the dashboard's error panel shows it.
"""

import importlib
import sqlite3
import sys

import pytest

import main
from agent import GuardUnavailable, OutreachAgent
from modules.database import Database


def _settings(settings_factory, tmp_path, **kw):
    kw.setdefault("blacklist_check_enabled", False)
    return settings_factory(db_path=str(tmp_path / "g.db"), **kw)


def _lose(monkeypatch, module):
    """As after a broken install: importing the module fails."""
    monkeypatch.setitem(sys.modules, module, None)


PARTS = (
    "dns.resolver",
    "modules.ai_generator",
    "modules.analytics",
    "modules.blacklist",
    "modules.domain_throttle",
    "modules.imap_monitor",
    "modules.lead_scoring",
    "modules.learning",
    "modules.query_generator",
    "modules.relevance",
    "modules.reply_intent",
    "modules.retry_queue",
    "modules.sequences",
    "modules.unsubscribe",
    "modules.verification",
    "modules.warmup",
)


@pytest.fixture(autouse=True)
def every_part_is_imported_first():
    """Each case loses the one module it names and nothing else. The parts
    that import that module are loaded before it goes, or which of them
    refuses first would depend on the tests that ran earlier: run alone, the
    blocklist case was refused by the email verifier, and the discovery case
    by lead scoring."""
    for name in PARTS:
        importlib.import_module(name)


# The blocklist check looks the sending domain up, so it needs one.
SENDER = {"smtp_username": "me@corp.example", "smtp_password": "not-a-real-password"}

# The AI relevance check asks the model on the company's own server.
OWN_AI = {
    "llm_provider": "openai_compatible",
    "LLM_BASE_URL": "http://127.0.0.1:11434/v1",
    "LLM_MODEL": "test-model",
}

# module, the settings that switch the guard on, words the refusal must hold
GUARDS = [
    ("modules.warmup", {"warmup_enabled": True}, "warm-up scheduler"),
    ("modules.domain_throttle", {}, "domain throttle"),
    ("modules.relevance", {"relevance_filter_enabled": True}, "relevance filter"),
    ("modules.verification", {"verify_emails": True}, "email verifier"),
    ("modules.lead_scoring", {"lead_scoring_enabled": True}, "Lead scoring"),
    ("modules.unsubscribe", {}, "unsubscribe handler"),
    ("modules.imap_monitor", {"imap_enabled": True}, "inbox monitor"),
    ("modules.reply_intent", {"imap_enabled": True}, "inbox monitor"),
    (
        "modules.blacklist",
        {"blacklist_check_enabled": True, **SENDER},
        "blocklist check",
    ),
    ("dns.resolver", {"blacklist_check_enabled": True, **SENDER}, "blocklist check"),
    ("modules.ai_generator", {"ai_relevance_enabled": True, **OWN_AI}, "AI relevance"),
]


@pytest.mark.parametrize("module, switched_on, named", GUARDS)
def test_a_guard_that_cannot_be_imported_stops_the_agent_being_built(
    module, switched_on, named, settings_factory, tmp_path, monkeypatch
):
    # Lead scoring imports the relevance module; keep the two cases apart.
    switched_on = {"lead_scoring_enabled": False, **switched_on}
    s = _settings(settings_factory, tmp_path, **switched_on)
    _lose(monkeypatch, module)

    with pytest.raises(GuardUnavailable) as refused:
        OutreachAgent(s)

    message = str(refused.value)
    assert named in message
    assert module in message, "the refusal must say what could not be imported"
    assert "Nothing is sent without it" in message


OFF = [
    ("modules.warmup", {"warmup_enabled": False}, "warmup"),
    ("modules.relevance", {"relevance_filter_enabled": False}, "relevance_filter"),
    ("modules.verification", {"verify_emails": False}, "email_verifier"),
    ("modules.lead_scoring", {"lead_scoring_enabled": False}, "lead_scorer"),
    ("modules.imap_monitor", {"imap_enabled": False}, "imap_monitor"),
    ("modules.blacklist", {"blacklist_check_enabled": False, **SENDER}, "_bl_checker"),
]


@pytest.mark.parametrize("module, switched_off, attribute", OFF)
def test_a_guard_the_settings_switch_off_is_not_needed(
    module, switched_off, attribute, settings_factory, tmp_path, monkeypatch
):
    switched_off = {"lead_scoring_enabled": False, **switched_off}
    s = _settings(settings_factory, tmp_path, **switched_off)
    _lose(monkeypatch, module)

    agent = OutreachAgent(s)

    assert getattr(agent, attribute) is None


def test_a_sound_install_holds_every_guard_the_settings_switch_on(
    settings_factory, tmp_path
):
    agent = OutreachAgent(
        _settings(
            settings_factory,
            tmp_path,
            relevance_filter_enabled=True,
            imap_enabled=True,
            blacklist_check_enabled=True,
            **SENDER,
        )
    )

    assert agent.guards_not_loaded() == []
    assert agent.warmup and agent.domain_throttle and agent.relevance_filter
    assert agent.email_verifier and agent.lead_scorer and agent.unsubscribe_handler
    assert len(agent.imap_monitors) == 1


def test_no_agent_is_built_with_a_guard_missing_however_it_went_missing(
    settings_factory, tmp_path, monkeypatch
):
    """Not only a failed import: the check at the end of the build refuses
    any guard the settings switch on that the agent does not hold."""
    import modules.warmup

    monkeypatch.setattr(modules.warmup, "WarmupScheduler", lambda **_kw: None)

    with pytest.raises(GuardUnavailable) as refused:
        OutreachAgent(_settings(settings_factory, tmp_path))

    assert "the warm-up scheduler" in str(refused.value)


def test_a_lost_module_that_a_guard_imports_stops_the_start_too(
    settings_factory, tmp_path, monkeypatch
):
    """In a process that starts on the broken install nothing is loaded yet.
    The search-word lists are only an extra where discovery reads them, but
    lead scoring reads them too, so without them the agent does not start."""
    monkeypatch.delitem(sys.modules, "modules.lead_scoring")
    _lose(monkeypatch, "modules.query_generator")

    with pytest.raises(GuardUnavailable) as refused:
        OutreachAgent(_settings(settings_factory, tmp_path))

    assert "Lead scoring" in str(refused.value)
    assert "modules.query_generator" in str(refused.value)


# ── The extras ───────────────────────────────────────────────────────

EXTRAS = [
    ("modules.retry_queue", {"retry_enabled": True}, "retry_queue", "retry queue"),
    ("modules.sequences", {"sequences_enabled": True}, "sequences", "Follow-up"),
    (
        "modules.analytics",
        {"OPEN_TRACKING_ENABLED": True},
        "analytics",
        "Open tracking",
    ),
    ("modules.ai_generator", {}, "ai_gen", "AI generator"),
    # The relevance check is on, but no AI server is set: the AI is off.
    (
        "modules.ai_generator",
        {"ai_relevance_enabled": True},
        "ai_gen",
        "AI generator",
    ),
    # An AI server is set, but only to word the emails.
    (
        "modules.ai_generator",
        {"ai_humanize_emails": True, **OWN_AI},
        "ai_gen",
        "AI generator",
    ),
    (
        "modules.query_generator",
        {"heuristic_discovery_enabled": True, "learning_enabled": False},
        "query_generator",
        "Heuristic discovery",
    ),
    ("modules.learning", {"learning_enabled": True}, "learner", "learning loop"),
]


@pytest.mark.parametrize("module, switched_on, attribute, named", EXTRAS)
def test_an_extra_that_cannot_be_imported_is_reported_and_the_agent_runs(
    module, switched_on, attribute, named, settings_factory, tmp_path, monkeypatch
):
    s = _settings(settings_factory, tmp_path, **switched_on)
    _lose(monkeypatch, module)

    agent = OutreachAgent(s)

    assert getattr(agent, attribute) is None
    assert agent.guards_not_loaded() == []
    shown = main.create_dashboard_app(s).test_client().get("/api/errors").get_json()
    missing = [r for r in shown if r["action"] == "COMPONENT_MISSING"]
    assert len(missing) == 1, "the dashboard does not say an extra is missing"
    assert named in missing[0]["details"] and module in missing[0]["details"]


def test_a_locked_database_does_not_stop_the_report_of_a_missing_extra(
    settings_factory, tmp_path, monkeypatch, caplog
):
    write = Database.log_activity

    def locked(self, event, *args, **kwargs):
        if event == "COMPONENT_MISSING":
            raise sqlite3.OperationalError("database is locked")
        return write(self, event, *args, **kwargs)

    monkeypatch.setattr(Database, "log_activity", locked)
    _lose(monkeypatch, "modules.retry_queue")

    agent = OutreachAgent(_settings(settings_factory, tmp_path))

    assert agent.retry_queue is None
    assert "The retry queue could not be loaded" in caplog.text


# ── How each way of starting says it ─────────────────────────────────


def test_main_py_run_refuses_in_words_and_exits(
    settings_factory, tmp_path, monkeypatch, capsys
):
    s = _settings(settings_factory, tmp_path)
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", ["main.py", "run", "--dry-run"])
    _lose(monkeypatch, "modules.warmup")

    with pytest.raises(SystemExit) as stopped:
        main.main()

    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "Refusing to start - The warm-up scheduler (the daily sending limit)" in out
    assert "pip install -r requirements.txt" in out
    assert "Traceback" not in out


def test_a_start_refused_on_the_command_line_is_shown_on_the_dashboard(
    settings_factory, tmp_path, monkeypatch, caplog
):
    """An agent started by a scheduled task or in a container prints where
    no one reads. The dashboard, a process of its own, showed an agent that
    was simply not running."""
    s = _settings(settings_factory, tmp_path)
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", ["main.py", "run", "--continuous"])
    _lose(monkeypatch, "modules.domain_throttle")

    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        main.main()

    assert "Refusing to start - The domain throttle" in caplog.text
    shown = main.create_dashboard_app(s).test_client().get("/api/errors").get_json()
    refusals = [r for r in shown if r["action"] == "GUARD_UNAVAILABLE"]
    assert len(refusals) == 1, "the dashboard does not say why no agent runs"
    assert "domain throttle" in refusals[0]["details"]


def test_a_refusal_that_cannot_be_recorded_is_still_said_in_words(
    settings_factory, tmp_path, monkeypatch, capsys
):
    s = _settings(settings_factory, tmp_path)
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", ["main.py", "run"])
    _lose(monkeypatch, "modules.warmup")

    def locked(self, *_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "log_activity", locked)

    with pytest.raises(SystemExit) as stopped:
        main.main()

    assert stopped.value.code == 1
    assert "Refusing to start - The warm-up scheduler" in capsys.readouterr().out


@pytest.fixture
def dashboard(settings_factory, tmp_path, monkeypatch):
    """A dashboard on an install that has lost the domain throttle, with
    every agent it manages to build recorded."""
    s = _settings(settings_factory, tmp_path)
    client = main.create_dashboard_app(s).test_client()
    built = []
    real = OutreachAgent.__init__

    def init(self, settings):
        real(self, settings)
        built.append(self)

    monkeypatch.setattr(OutreachAgent, "__init__", init)
    # Should an agent be built after all, it must not run a campaign here.
    monkeypatch.setattr(OutreachAgent, "run_single", lambda self, **_kw: None)
    monkeypatch.setattr(OutreachAgent, "run_continuous", lambda self, **_kw: None)
    _lose(monkeypatch, "modules.domain_throttle")
    return client, Database(s.get_abs_db_path()), built


def _refusals(db):
    return [r for r in db.get_recent_errors() if r["action"] == "GUARD_UNAVAILABLE"]


@pytest.mark.parametrize("mode", ["single", "continuous", "dry_run"])
def test_the_dashboards_start_refuses_and_starts_nothing(dashboard, mode):
    client, db, built = dashboard

    resp = client.post("/api/agent/start", json={"mode": mode})

    assert resp.status_code == 503
    error = resp.get_json()["error"]
    assert "Nothing was started or sent" in error and "domain throttle" in error
    assert built == []
    assert len(_refusals(db)) == 1 and "domain throttle" in _refusals(db)[0]["details"]
    assert client.get("/api/agent/status").get_json()["running"] is False


def test_the_dashboards_send_refuses_and_sends_nothing(dashboard):
    client, db, built = dashboard

    resp = client.post(
        "/api/send-manual", json={"email": "anna@brand.de", "name": "Anna"}
    )

    assert resp.status_code == 503
    body = resp.get_json()
    assert body["ok"] is False
    assert "Nothing was started or sent" in body["message"]
    assert "domain throttle" in body["message"]
    assert built == []
    assert db.get_contact_by_email("anna@brand.de") is None
    assert len(_refusals(db)) == 1


def test_the_start_works_again_once_the_install_is_mended(dashboard, monkeypatch):
    client, _db, built = dashboard
    assert client.post("/api/agent/start", json={"mode": "dry_run"}).status_code == 503

    monkeypatch.delitem(sys.modules, "modules.domain_throttle")
    resp = client.post("/api/agent/start", json={"mode": "dry_run"})

    assert resp.status_code == 200
    assert len(built) == 1 and built[0].domain_throttle is not None


# ── A checker lost while the agent runs ──────────────────────────────


def test_a_blocklist_check_lost_mid_run_pauses_sending(
    settings_factory, tmp_path, monkeypatch
):
    """The start refuses without it, so this is one lost since. It used to
    read as "not listed", and sending went on unchecked."""
    agent = OutreachAgent(
        _settings(settings_factory, tmp_path, blacklist_check_enabled=True, **SENDER)
    )
    _lose(monkeypatch, "modules.blacklist")

    assert agent._blacklist_guard_tripped() is True
    assert agent._blacklist_guard_tripped() is True
    paused = [
        r for r in agent.db.get_recent_errors() if r["action"] == "BLACKLIST_GUARD"
    ]
    assert len(paused) == 1, "reported once per episode, and as an error"
    assert "cannot be loaded" in paused[0]["details"]

    agent._sending_paused, agent._pause_reason = True, "blacklist"
    assert agent._safe_to_resume() is False
