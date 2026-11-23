"""Who the agent works for, in one file the client can read and correct.

Every word the agent uses to describe the client, to find its buyers and to
recognise its rivals used to be written into the modules for one client. It now
lives in a JSON profile (``client/omega.json`` by default, or the file named by
the ``CLIENT_PROFILE_PATH`` environment variable), so the same agent serves a
different client by swapping one file, and the client can check the words the
agent uses in its name without reading code.

The profile is read once, at import, because the modules that use it build
their search vocabulary and regular expressions at import too. A missing or
incomplete profile stops the agent at start-up with the missing key named,
rather than letting it search, filter or write on a blank vocabulary.
"""

import json
import os
import re
from pathlib import Path

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent.parent / "client" / "omega.json"

_REQUIRED = (
    "name",
    "value_proposition",
    "buyer_seeds",
    "phrase_countries",
    "vertical_seeds",
    "modifiers",
    "supplier_words_pattern",
    "verticals",
    "competitor_keywords",
    "competitor_self_description_keywords",
    "competitor_core_stems",
    "competitor_qualifiers",
    "negative_keywords",
    "rival_label",
    "market_label",
    "prompts",
)

_REQUIRED_PROMPTS = (
    "opening_context",
    "relevance_intro",
    "relevance_no",
    "relevance_yes",
    "relevance_judge_note",
    "country_context",
    "strategist_targets",
    "strategist_verticals",
    "strategist_query_rule",
)


class ProfileError(ValueError):
    """The client profile is missing, unreadable or incomplete."""


class ClientProfile:
    """The client's own words, read from its profile file."""

    def __init__(self, data: dict, source: str = "<dict>"):
        missing = [k for k in _REQUIRED if k not in data]
        missing += [
            f"prompts.{k}"
            for k in _REQUIRED_PROMPTS
            if k not in data.get("prompts", {})
        ]
        if missing:
            raise ProfileError(f"client profile {source} lacks: {', '.join(missing)}")
        if not data["value_proposition"].get("en"):
            raise ProfileError(
                f"client profile {source} has no English value proposition"
            )
        self.source = source
        self.name: str = data["name"]
        self.value_proposition_phrases: dict = {
            lang: tuple(phrases) for lang, phrases in data["value_proposition"].items()
        }
        self.buyer_seeds: dict = {
            lang: list(phrases) for lang, phrases in data["buyer_seeds"].items()
        }
        self.phrase_countries: dict = dict(data["phrase_countries"])
        self.vertical_seeds: list = list(data["vertical_seeds"])
        self.modifiers: list = list(data["modifiers"])
        self.supplier_words_re = re.compile(
            data["supplier_words_pattern"], re.IGNORECASE
        )
        self.verticals: list = list(data["verticals"])
        self.competitor_keywords: list = list(data["competitor_keywords"])
        self.competitor_self_description_keywords: list = list(
            data["competitor_self_description_keywords"]
        )
        self.competitor_core_stems: list = list(data["competitor_core_stems"])
        self.competitor_qualifiers: list = list(data["competitor_qualifiers"])
        self.negative_keywords: list = list(data["negative_keywords"])
        self.rival_label: str = data["rival_label"]
        self.market_label: str = data["market_label"]
        self.prompts: dict = {
            k: _named(v, self.name) for k, v in data["prompts"].items()
        }
        self.sectors: list = list(data.get("sectors", []))
        self.reply_suggestions: dict = dict(data.get("reply_suggestion", {}))
        # Descriptions of the kinds of company that buy from the client today,
        # for the lookalike score (modules/lookalike.py). Optional.
        self.best_customers: list = list(data.get("best_customers", []))

    def value_proposition(self, language: str = "en") -> str:
        """The client's value proposition as it wrote it, in the language asked
        for when the profile has it, else English."""
        phrases = (
            self.value_proposition_phrases.get(language)
            or (self.value_proposition_phrases["en"])
        )
        return ", ".join(phrases)

    def reply_suggestion(self, language: str = "en") -> str:
        """The suggested answer to a reply, for a person to edit. Its legal
        footer is filled when it is sent, and must stay."""
        text = self.reply_suggestions.get(language) or self.reply_suggestions.get("en")
        if not text:
            text = (
                "<p>Thank you for your reply.</p>"
                "<p>[Answer their question here.]</p>"
                "<p>{{sender_signature}}</p>"
            )
        return text if "{{legal_footer}}" in text else text + "\n{{legal_footer}}"

    @property
    def buyer_phrases(self) -> list:
        return [p for ps in self.buyer_seeds.values() for p in ps]


def _named(value, name: str):
    """Put the client's name where the profile wrote ``{name}``. A plain
    replace, not str.format, so braces elsewhere in a prompt stay as written."""
    if isinstance(value, str):
        return value.replace("{name}", name)
    if isinstance(value, list):
        return [_named(v, name) for v in value]
    return value


def profile_path() -> Path:
    return Path(os.environ.get("CLIENT_PROFILE_PATH") or DEFAULT_PROFILE_PATH)


def load_profile(path: Path | str | None = None) -> ClientProfile:
    path = Path(path) if path else profile_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProfileError(f"cannot read client profile {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProfileError(f"client profile {path} is not valid JSON: {exc}") from exc
    return ClientProfile(data, str(path))


PROFILE = load_profile()


def value_proposition(language: str = "en") -> str:
    return PROFILE.value_proposition(language)


# The value proposition phrases themselves, per language, for callers that
# check a text against them.
VALUE_PROPOSITION_PHRASES = PROFILE.value_proposition_phrases
