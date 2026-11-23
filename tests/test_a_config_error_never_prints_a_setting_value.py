"""A configuration error names the setting, never the value it holds.

pydantic quoted the refused value in its error (input_value=...), and
start-up printed that error to the console, the task's output and the Docker
log. A misspelt key holding a password (SMTP_PASWORD=...) was refused as an
extra input, and the refusal printed the password; a model-level refusal
quoted every setting at once. The validators' own messages quoted values too.

SUPPLECO's AI now runs only on its own server, so an old settings file that
still holds a cloud AI key (GEMINI_API_KEY, GROQ_API_KEY) or GROQ_MODEL is
refused at start-up with a message naming the line to delete - and never the
key itself. Every probe here is a fake value that must appear nowhere in what
is printed or logged.
"""

import ipaddress
import logging

import pytest
from pydantic import ValidationError

import config
import main
from config import Settings, config_error_text, effective_settings

SECRET = "fake-secret-7Hq2ZpW9"


def _settings_file(tmp_path, *lines):
    # Not named .env: the probe is an ordinary dotenv file read by path.
    path = tmp_path / "probe-settings.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _refusal(tmp_path, *lines):
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=str(_settings_file(tmp_path, *lines)))
    return refused.value


def _printed_at_start_up(monkeypatch, capsys, tmp_path, *lines):
    """What `main.py status` prints when its settings file holds lines."""
    path = _settings_file(tmp_path, *lines)
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(main, "load_settings", lambda: Settings(_env_file=str(path)))
    monkeypatch.setattr("sys.argv", ["main.py", "status"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1
    captured = capsys.readouterr()
    return captured.out + captured.err


@pytest.mark.parametrize("key", ["GEMINI_API_KEY", "GROQ_API_KEY", "GROQ_MODEL"])
@pytest.mark.parametrize("written", [str.upper, str.lower])
def test_a_cloud_ai_setting_stops_start_up_naming_the_line_to_delete(
    tmp_path, key, written
):
    error = _refusal(tmp_path, f"{written(key)}={SECRET}")
    text = config_error_text(error)
    assert key in text
    assert f"Delete the line that starts {key}= from .env" in text
    assert "SUPPLECO's own server" in text
    assert SECRET not in text
    assert SECRET not in str(error)


def test_both_cloud_keys_are_named_in_one_refusal(tmp_path):
    text = config_error_text(
        _refusal(tmp_path, f"GEMINI_API_KEY={SECRET}", f"GROQ_API_KEY={SECRET}x")
    )
    assert "the line that starts GEMINI_API_KEY=" in text
    assert "the line that starts GROQ_API_KEY=" in text
    assert SECRET not in text


def test_the_cli_refuses_a_cloud_key_without_printing_it(
    monkeypatch, capsys, tmp_path
):
    out = _printed_at_start_up(
        monkeypatch,
        capsys,
        tmp_path,
        "SMTP_PASSWORD=" + SECRET + "-smtp",
        f"GEMINI_API_KEY={SECRET}",
    )
    assert "Configuration error" in out
    assert "Delete the line that starts GEMINI_API_KEY= from .env" in out
    assert SECRET not in out


@pytest.mark.parametrize(
    "line",
    [
        f"SMTP_PASWORD={SECRET}",
        f"IMAP_PASSWROD={SECRET}",
        f"OPENAI_API_KEY={SECRET}",
    ],
)
def test_a_misspelt_key_is_refused_without_printing_its_value(
    monkeypatch, capsys, tmp_path, line
):
    error = _refusal(tmp_path, line)
    assert SECRET not in str(error)
    assert SECRET not in config_error_text(error)
    out = _printed_at_start_up(monkeypatch, capsys, tmp_path, line)
    assert "Configuration error" in out and line.split("=")[0] in out
    assert SECRET not in out


@pytest.fixture
def no_dns(monkeypatch):
    def resolve(host):
        if host == "ai.internal":
            return [ipaddress.ip_address("10.0.0.7")]
        if host.endswith(".example.com"):
            return [ipaddress.ip_address("93.184.215.14")]
        raise OSError("no such name")

    monkeypatch.setattr(config, "resolve_host", resolve)


OWN_AI = {
    "llm_provider": "openai_compatible",
    "LLM_BASE_URL": "http://ai.internal:11434/v1",
    "LLM_MODEL": "qwen2.5:14b",
}


@pytest.mark.parametrize(
    "probe,value",
    [
        ("privacy_notice_url", f"http://{SECRET}.example/privacy"),
        ("privacy_contact_email", SECRET),
        ("legal_single_member", SECRET),
        ("ENABLED_COUNTRIES", f"IT,{SECRET}"),
        ("ITALIAN_COUNTRIES", SECRET),
        ("llm_provider", SECRET),
        ("send_window_days", f"1,{SECRET}"),
        ("max_bounce_rate", 0.987654),
        ("max_domain_bounce_rate", 0.987654),
        ("bounce_guard_min_sends", 987654),
        ("domain_bounce_min_sends", 987654),
        ("warmup_max_daily", -987654),
        ("warmup_increment", -987654),
        ("delay_between_emails", -987.654),
        ("send_jitter_seconds", -987.654),
        ("retention_days_contacted", 23),
        ("smtp_port", SECRET),
    ],
)
def test_no_validator_quotes_the_refused_value(settings_factory, no_dns, probe, value):
    with pytest.raises(ValidationError) as refused:
        settings_factory(**{probe: value})
    for text in (str(refused.value), config_error_text(refused.value)):
        assert str(value) not in text, text


@pytest.mark.parametrize(
    "probe,value",
    [
        ("LLM_REASONING_EFFORT", SECRET),
        ("LLM_TIMEOUT_SECONDS", 987654),
        ("LLM_BASE_URL", f"http://{SECRET}.example.com/v1"),
        ("LLM_BASE_URL", f"http://admin:{SECRET}@10.0.0.7/v1"),
    ],
)
def test_no_own_server_refusal_quotes_the_value(settings_factory, no_dns, probe, value):
    with pytest.raises(ValidationError) as refused:
        settings_factory(**{**OWN_AI, probe: value})
    for text in (str(refused.value), config_error_text(refused.value)):
        assert str(value) not in text, text
        assert SECRET not in text, text


def test_the_refusal_of_a_name_that_does_not_resolve_does_not_quote_it(no_dns):
    """Doctor and every AI call refuse a name that cannot be looked up; the
    host is part of a setting, so their message must not repeat it."""
    problem = config.own_server_host_problem(f"{SECRET}.nowhere.test")
    assert "does not resolve" in problem
    assert SECRET not in problem


def test_a_send_window_that_never_opens_names_no_hour(settings_factory):
    with pytest.raises(ValidationError) as refused:
        settings_factory(send_window_start_hour=17, send_window_end_hour=11)
    text = config_error_text(refused.value)
    assert "send_window_end_hour must be greater than" in text
    assert "17" not in text and "11" not in text


def test_a_rejected_dashboard_override_is_logged_without_its_value(
    settings_factory, db, caplog
):
    db.set_runtime_override("max_bounce_rate", 0.987654)
    base = settings_factory()
    with caplog.at_level(logging.ERROR, logger="config"):
        kept = effective_settings(base, db)
    assert kept.max_bounce_rate == base.max_bounce_rate
    assert "Rejecting config override max_bounce_rate" in caplog.text
    assert "0.987654" not in caplog.text


def test_the_dashboard_refuses_a_value_without_quoting_it(settings_factory, tmp_path):
    app = main.create_dashboard_app(
        settings_factory(db_path=str(tmp_path / "dash.db"), blacklist_check_enabled=False)
    )
    answer = app.test_client().post(
        "/api/config", json={"settings": {"max_bounce_rate": 0.987654}}
    )
    body = answer.get_data(as_text=True)
    assert answer.status_code == 400, body
    assert "max_bounce_rate" in body and "must be a fraction" in body
    assert "0.987654" not in body


def test_a_half_filled_mailbox_slot_is_logged_without_its_address(
    settings_factory, caplog
):
    with caplog.at_level(logging.ERROR, logger="config"):
        settings_factory(sender_2_email=f"{SECRET}@example.com").get_sender_accounts()
    assert "SENDER_2_EMAIL is set but SENDER_2_PASSWORD is empty" in caplog.text
    assert SECRET not in caplog.text


def test_a_broken_sender_accounts_list_is_logged_without_its_values(
    settings_factory, caplog
):
    broken = f'[{{"username": "x@example.com", "smtp_port": "{SECRET}"}}]'
    with caplog.at_level(logging.ERROR, logger="config"):
        settings_factory(sender_accounts=broken).get_sender_accounts()
    assert "SENDER_ACCOUNTS could not be parsed" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize("key", ["GEMINI_API_KEY", "GROQ_API_KEY", "GROQ_MODEL"])
def test_a_cloud_ai_setting_in_the_environment_stops_start_up(monkeypatch, key):
    """Docker's env_file turns .env into environment variables, and
    pydantic-settings passes on only the names it knows from there: in the
    container a cloud key in .env was neither refused nor seen."""
    monkeypatch.setenv(key, SECRET)
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None)
    text = config_error_text(refused.value)
    assert f"Delete the line that starts {key}= from .env" in text
    assert f"{key} is also set in the environment" in text
    assert SECRET not in text
    assert SECRET not in str(refused.value)


def test_the_cli_refuses_a_cloud_key_from_dockers_env_file(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    # Were the key let through, start-up would go on to set up the logging.
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    out = _printed_at_start_up(monkeypatch, capsys, tmp_path, "DRY_RUN=true")
    assert "Delete the line that starts GROQ_API_KEY= from .env" in out
    assert "also set in the environment" in out
    assert SECRET not in out
