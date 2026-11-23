"""LLM_PROVIDER means the provider it names, however it is written.

The setting is stripped and lowercased once, when it is loaded. The only
provider is openai_compatible, the AI model on SUPPLECO's own server; blank
means no AI. Gemini and Groq were removed (no outside AI company may receive
data), so their names are refused at startup, as is any other name. doctor
builds the generator the agent builds, and names its model.
"""

import pytest
from pydantic import ValidationError

CONFIGURED = "qwen2.5:14b"


def _own_settings(settings_factory, written):
    return settings_factory(
        llm_provider=written,
        LLM_BASE_URL="http://127.0.0.1:11434/v1",
        LLM_MODEL=CONFIGURED,
    )


@pytest.mark.parametrize(
    "written", ["openai_compatible", "OpenAI_Compatible", " OPENAI_COMPATIBLE "]
)
def test_the_own_server_however_written_runs_on_the_configured_model(
    settings_factory, written
):
    from agent import OutreachAgent

    ai = OutreachAgent(_own_settings(settings_factory, written)).ai_gen
    assert (ai.provider, ai.MODEL_NAME) == ("openai_compatible", CONFIGURED)
    assert ai.is_configured


@pytest.mark.parametrize("written", ["", "  "])
def test_a_blank_provider_means_no_ai(settings_factory, written):
    from agent import OutreachAgent

    settings = settings_factory(llm_provider=written)
    assert settings.llm_provider == ""
    assert not OutreachAgent(settings).ai_gen.is_configured


@pytest.mark.parametrize(
    "written",
    ["gemini", "Gemini", "groq", " GROQ ", "openai", "ollama", "gemini-2.0-flash"],
)
def test_a_cloud_or_unknown_provider_is_refused_at_startup(settings_factory, written):
    with pytest.raises(ValidationError, match="must be openai_compatible"):
        settings_factory(llm_provider=written)


def test_doctor_checks_the_generator_the_agent_builds(settings_factory):
    from main import run_doctor_checks

    checks = run_doctor_checks(
        _own_settings(settings_factory, "OpenAI_Compatible"), network=False
    )
    ai = next(c for c in checks if c.label.startswith("AI generator"))
    assert ai.ok is True
    assert ai.detail.startswith("openai_compatible: configured") and CONFIGURED in ai.detail
