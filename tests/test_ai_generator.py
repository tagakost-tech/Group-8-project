"""Tests that the AI generator degrades safely when unconfigured/unavailable."""
import pytest

from modules.ai_generator import AIGenerator


def test_unconfigured_generator_is_safe():
    ai = AIGenerator()
    assert ai.is_configured is False
    # All AI helpers must fail safe without a configured server (no exceptions).
    assert ai.classify_relevance({"email": "x@y.com", "organization": "Z"}) is None
    assert ai.generate_opening_line({"organization": "Z"}) == ""
    assert ai.generate_companies("pharma companies", 3) == []
    assert ai.suggest_discovery_queries(count=5) == []


def test_own_server_configures_with_an_address_and_a_model():
    ai = AIGenerator(
        provider="openai_compatible",
        base_url="http://127.0.0.1:11434/v1/",
        llm_model="qwen2.5:14b",
    )
    assert ai.provider == "openai_compatible"
    assert ai.is_configured is True
    assert ai.base_url == "http://127.0.0.1:11434/v1"


@pytest.mark.parametrize("provider", ["", "gemini", "groq"])
def test_any_other_provider_is_off_and_safe(provider):
    ai = AIGenerator(
        provider=provider,
        base_url="http://127.0.0.1:11434/v1",
        llm_model="qwen2.5:14b",
        personal_data_allowed=True,
    )
    assert ai.is_configured is False
    assert ai.base_url is None
    # No network calls when unconfigured.
    assert ai.generate_opening_line({"organization": "Z"}) == ""
    assert ai.classify_relevance({"email": "x@y.com"}) is None
    assert ai.suggest_discovery_queries() == []


def test_own_server_safe_without_a_model():
    ai = AIGenerator(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1")
    assert ai.is_configured is False
    assert ai.generate_opening_line({"organization": "Z"}) == ""
