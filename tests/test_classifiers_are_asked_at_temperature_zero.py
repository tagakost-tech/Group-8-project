"""The three classifier questions are asked at temperature 0.

Every question went at temperature 0.7, so the same company could get
another verdict each time it was asked. On 13 November 2026 a small model on
the company's own server, asked nine invented cases five times each, gave
one case in nine two different verdicts at 0.7 and none at 0.0. A verdict
that flips can be neither trusted nor benchmarked, so relevance, country and
reply intent are now asked at 0 on the own server, the only AI there is.
The writing questions (opening line, body, discovery queries, company list)
keep the temperature they had: 0.7.
"""

import json

import httpx
import pytest

from modules.ai_generator import AIGenerator

CONTACT = {
    "organization": "Vitalis Nordica",
    "qualification_rationale": "Sells its own vitamin brand in Sweden.",
    "email": "buyer@vitalis-nordica.example",
}

CLASSIFIERS = {
    "classify_relevance": lambda ai: ai.classify_relevance(CONTACT),
    "classify_company_country": lambda ai: ai.classify_company_country(CONTACT),
    "classify_reply_intent": lambda ai: ai.classify_reply_intent(
        "Re: supplements", "Could you send prices?"
    ),
}

WRITERS = {
    "generate_opening_line": lambda ai: ai.generate_opening_line(CONTACT),
    "generate_email_body": lambda ai: ai.generate_email_body(CONTACT, "Anna", "SUPPLECO"),
    "suggest_discovery_queries": lambda ai: ai.suggest_discovery_queries(),
    "generate_companies": lambda ai: ai.generate_companies("supplement brands"),
}


@pytest.fixture
def bodies(monkeypatch):
    """The JSON body of every chat-completions request, answered with YES."""
    real_client = httpx.Client
    seen = []

    def answer(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "YES"}}]})

    class Client(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    return seen


def _own():
    return AIGenerator(
        provider="openai_compatible",
        base_url="http://127.0.0.1:11434/v1",
        llm_model="qwen3.5:0.8b",
        personal_data_allowed=True,
    )


@pytest.mark.parametrize("question", list(CLASSIFIERS))
def test_a_classifier_question_is_sent_at_temperature_zero(bodies, question):
    CLASSIFIERS[question](_own())
    assert [b["temperature"] for b in bodies] == [0.0]


@pytest.mark.parametrize("question", list(WRITERS))
def test_a_writing_question_keeps_temperature_0_7(bodies, question):
    WRITERS[question](_own())
    assert [b["temperature"] for b in bodies] == [0.7]


def test_doctors_question_keeps_temperature_0_7(bodies):
    _own().ask_once("Reply with the single word YES.")
    assert [b["temperature"] for b in bodies] == [0.7]
