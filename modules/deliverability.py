"""
Deliverability linter for outreach email templates.

Flags common issues that hurt inbox placement: spam-trigger words, too many
links, image-heavy / text-light layouts, ALL-CAPS shouting, missing
unsubscribe link, and exclamation-mark overuse. Advisory only - it returns a
list of human-readable warnings and never blocks sending.
"""

import re

# Well-known spam-trigger phrases (lowercased).
#
# Bare "free" is deliberately absent. It fires on ordinary polite English and
# on this industry's own product language: the shipped followup_1.html was
# flagged for "please feel free to", and "gluten-free", "sugar-free" and
# "lactose-free" are what SUPPLECO's customers put on their labels. A linter that
# cries wolf on the opt-out sentence teaches the operator to ignore it, which
# costs more than the warning was ever worth. The commercial uses of the word
# are listed explicitly instead.
SPAM_WORDS = [
    "free trial",
    "free gift",
    "for free",
    "guarantee",
    "guaranteed",
    "act now",
    "limited time",
    "click here",
    "buy now",
    "order now",
    "risk-free",
    "100%",
    "cash",
    "cheap",
    "discount",
    "earn money",
    "make money",
    "no cost",
    "no obligation",
    "offer expires",
    "urgent",
    "winner",
    "congratulations",
    "$$$",
    "credit card",
    "investment",
    "viagra",
    "casino",
    "lottery",
    "prize",
    "double your",
    "extra income",
]

# Whole-term matching, never substring. `w in text_lower` matched "cash" inside
# "cashew" and "prize" inside "prized" - both ordinary words in supplement copy.
# The boundary excludes hyphens as well as word characters, so "risk-free"
# matches as itself while "gluten-free" does not match a bare "free"-style
# entry. Lookarounds rather than \b because several entries end in a non-word
# character ("100%", "$$$"), where \b asserts the opposite of what is meant.
_SPAM_RE = [
    (w, re.compile(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])")) for w in SPAM_WORDS
]


class DeliverabilityLinter:
    """Static advisory linter for email HTML/plain content."""

    @staticmethod
    def _strip_html(html: str) -> str:
        return re.sub(r"<[^>]+>", " ", html or "")

    @classmethod
    def lint(cls, html: str, plain: str = "") -> list:
        """Return a list of deliverability warning strings (empty == clean)."""
        issues = []
        visible = (plain or cls._strip_html(html)).strip()
        text_lower = visible.lower()

        found = sorted({w for w, rx in _SPAM_RE if rx.search(text_lower)})
        if found:
            issues.append("Spam-trigger words present: " + ", ".join(found[:8]))

        link_count = len(re.findall(r"href\s*=", html or "", re.IGNORECASE))
        if link_count > 5:
            issues.append(
                f"Many links ({link_count}) - keep them minimal for deliverability"
            )

        image_count = len(re.findall(r"<img", html or "", re.IGNORECASE))
        word_count = len(visible.split())
        if image_count and word_count < 40:
            issues.append(
                f"Image-heavy with little text ({word_count} words, {image_count} images)"
            )

        caps = re.findall(r"\b[A-Z]{5,}\b", cls._strip_html(html))
        if len(caps) > 3:
            issues.append(f"Excessive ALL-CAPS words ({len(caps)})")

        if "unsubscribe" not in (html or "").lower():
            issues.append(
                "No 'unsubscribe' link found (hurts compliance & deliverability)"
            )

        if visible.count("!") > 3:
            issues.append("Too many exclamation marks")

        return issues
