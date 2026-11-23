"""Every AI prompt describes SUPPLECO in SUPPLECO's own words.

Each prompt carried its own description of SUPPLECO, written by us: "an
Italian contract manufacturer / private-label producer of food supplements
and nutraceuticals (vitamins, probiotics, sports nutrition, herbal and
beauty-from-within supplements) for other brands". SUPPLECO sent its own value
proposition on 10 November 2026, and the model was never shown it.

Now the value proposition lives once, verbatim, in modules/suppleco_profile.py,
and every prompt that says what SUPPLECO is or sells quotes it. The prompts'
answer formats are unchanged, and are asserted on the prompt that reaches the
(faked) model, after scrubbing.
"""

import hashlib
import inspect

import pytest

import modules.ai_generator
from modules.ai_generator import AIGenerator
from modules.client_profile import VALUE_PROPOSITION_PHRASES, value_proposition

# sha256 of each section-2 line of SUPPLECO's document, as written. The document
# itself stays out of the repository.
IT_SHA256 = "864c6e3f7fd4cb7a35e6d1fc3473b449e9ddf811ad08d4154b76b1e7febd86f7"
EN_SHA256 = "9d7798026ee51a8183d6cdb0c1cfc1c17e2d7cb1295698c579b79d502596361a"

OUR_OLD_WORDS = [
    "contract manufacturer / private-label producer",
    "beauty-from-within supplements) for other brands",
    "offering contract manufacturing of food supplements",
    "supplement/nutraceutical contract manufacturer",
]


# ── The text ─────────────────────────────────────────────────────────────────


def test_the_value_proposition_is_supplecos_verbatim():
    assert hashlib.sha256(value_proposition("it").encode()).hexdigest() == IT_SHA256
    assert hashlib.sha256(value_proposition("en").encode()).hexdigest() == EN_SHA256


@pytest.mark.parametrize(
    "lang,first,last",
    [
        (
            "it",
            "CDMO specializzata in integratori alimentari",
            "supporto dalla formulazione alla commercializzazione",
        ),
        (
            "en",
            "specialized CDMO for food supplements",
            "support from formulation to commercialization",
        ),
    ],
)
def test_the_phrases_are_kept_in_supplecos_order(lang, first, last):
    phrases = VALUE_PROPOSITION_PHRASES[lang]
    assert len(phrases) == 29
    assert (phrases[0], phrases[-1]) == (first, last)


def test_italian_when_asked_english_otherwise():
    assert value_proposition("it") != value_proposition("en")
    assert value_proposition("it").startswith("CDMO specializzata")
    assert value_proposition("de") == value_proposition("en")
    assert value_proposition() == value_proposition("en")


# ── The prompts ──────────────────────────────────────────────────────────────


def _ai(answer):
    ai = AIGenerator(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1", llm_model="test-model", personal_data_allowed=True)
    ai.prompts = []

    def complete(prompt, temperature=None):
        ai.prompts.append(prompt)
        return answer

    ai._complete = complete
    return ai


CONTACT = {
    "organization": "Brandco",
    "email": "info@brandco.de",
    "qualification_rationale": "Makes vitamins and probiotics",
}

CALLS = {
    "opening": (
        "We admired your probiotic range.",
        lambda ai: ai.generate_opening_line(CONTACT),
        "Output ONLY the sentence, nothing else.",
        "We admired your probiotic range.",
    ),
    "body": (
        "Your probiotic range caught my eye. We could produce it. Open to a call?",
        lambda ai: ai.generate_email_body(CONTACT, "Marco", "SUPPLECO"),
        "Output ONLY the body sentences.",
        "Your probiotic range caught my eye. We could produce it. Open to a call?",
    ),
    "relevance": (
        "YES",
        lambda ai: ai.classify_relevance(CONTACT),
        "Answer with exactly one word: YES or NO.",
        True,
    ),
    "reply": (
        "NO",
        lambda ai: ai.classify_reply_intent("Re: hello", "Not for us, thanks."),
        "Answer with exactly one word: YES or NO.",
        False,
    ),
    "strategist": (
        "q1\nq2",
        lambda ai: ai.suggest_discovery_queries(count=2),
        "- Output ONLY the queries, one per line. No numbering, no extra text.",
        ["q1", "q2"],
    ),
}


@pytest.mark.parametrize("call", sorted(CALLS))
def test_every_prompt_describes_suppleco_in_its_own_words(call):
    answer, run, _fmt, _parsed = CALLS[call]
    ai = _ai(answer)
    run(ai)
    prompt = ai.prompts[0]
    assert value_proposition("en") in prompt
    left = [w for w in OUR_OLD_WORDS if w in prompt]
    assert not left, f"the {call} prompt still describes SUPPLECO as {left}"


@pytest.mark.parametrize("call", sorted(CALLS))
def test_the_answer_format_and_its_parsing_are_unchanged(call):
    answer, run, fmt, parsed = CALLS[call]
    ai = _ai(answer)
    assert run(ai) == parsed
    assert fmt in ai.prompts[0]


def test_the_rules_that_keep_competitors_out_are_kept():
    ai = _ai("YES")
    ai.classify_relevance(CONTACT)
    assert (
        "- it is a CONTRACT or private-label MANUFACTURER of supplements or "
        "medicines: it offers to make products FOR other companies' brands, so it "
        "is a competitor, not a buyer;" in ai.prompts[0]
    )
    ai = _ai("q1")
    ai.suggest_discovery_queries(count=1)
    assert 'Do NOT use "manufacturer" or "contract manufacturer"' in ai.prompts[0]


def test_the_strategist_drops_a_query_that_searches_for_what_suppleco_is():
    """The strategist is shown SUPPLECO's value proposition, which is made of
    supplier words, and a model that echoed it queued searches for rivals."""
    ai = _ai(
        "CDMO nutraceuticals Germany\n"
        "private label supplements RTM products France\n"
        "probiotic brand Germany\n"
        "vitamin retailer Dortmund"
    )
    assert ai.suggest_discovery_queries(count=5) == [
        "probiotic brand Germany",
        "vitamin retailer Dortmund",
    ]


def test_the_strategist_is_told_supplecos_buyer_words_and_its_supplier_words():
    ai = _ai("q1")
    ai.suggest_discovery_queries(count=1)
    prompt = ai.prompts[0]
    assert "nutraceutical brand, supplement brand owner" in prompt
    assert "azienda nutraceutica, brand di integratori alimentari" in prompt
    assert "Do NOT use the words SUPPLECO uses for itself either (CDMO" in prompt


def test_the_opening_line_still_does_not_name_suppleco():
    ai = _ai("We admired your probiotic range.")
    ai.generate_opening_line(CONTACT)
    assert "- Do NOT mention SUPPLECO by name." in ai.prompts[0]


def test_no_description_of_our_own_is_left_in_the_prompts():
    source = inspect.getsource(modules.ai_generator)
    assert "Italian contract manufacturer" not in source
    assert "private-label producer" not in source
