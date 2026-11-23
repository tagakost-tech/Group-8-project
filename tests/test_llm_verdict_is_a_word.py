"""The model's YES/NO verdict is read as a word, not found as a substring.

Both classifiers asked for exactly one word and then tested `"NO" in text`.
"UNKNOWN", "CANNOT DETERMINE" and "NOT SURE" all contain "NO", so a model
that could not decide was read as a firm no. For relevance that is terminal:
the contact is marked ai_irrelevant and never emailed. The callers treat
None as "undetermined - do not filter", which is what an unclear answer is.
"""

import pytest

from modules.ai_generator import AIGenerator


@pytest.fixture
def ai(monkeypatch):
    gen = AIGenerator(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1", llm_model="test-model", personal_data_allowed=True)
    gen.answer = ""
    monkeypatch.setattr(gen, "_complete_with_retry", lambda *a, **k: gen.answer)
    return gen


def _both(ai, answer):
    ai.answer = answer
    return (
        ai.classify_relevance({"organization": "Brand", "email": "info@brand.de"}),
        ai.classify_reply_intent("Re: hello", "some reply"),
    )


@pytest.mark.parametrize("answer", ["UNKNOWN", "Not sure", "I cannot determine this"])
def test_an_undecided_answer_is_not_a_no(ai, answer):
    assert _both(ai, answer) == (None, None)


@pytest.mark.parametrize("answer", ["NO", "No.", "no - it is a news site"])
def test_a_real_no_is_still_a_no(ai, answer):
    assert _both(ai, answer) == (False, False)


@pytest.mark.parametrize("answer", ["YES", "Yes.", "yes, a supplement brand"])
def test_a_yes_is_a_yes(ai, answer):
    assert _both(ai, answer) == (True, True)
