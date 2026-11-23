"""
Lead Scoring Engine for the B2B Outreach Agent.

Scores contacts 0-100 based on four weighted dimensions:
  - Role relevance   (0-35 pts)
  - Email quality     (0-25 pts)
  - Organization fit  (0-25 pts)
  - Source quality     (0-15 pts)

Contacts are bucketed into tiers: A (75+), B (50-74), C (25-49), D (<25).
"""

import logging
import re

from modules.query_generator import VERTICAL_SEEDS
from modules.relevance import DEFAULT_VERTICALS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scoring lookup tables
# ---------------------------------------------------------------------------

# Role keywords mapped to their point values (checked via substring match)
_ROLE_SCORES: list[tuple[list[str], int]] = [
    # C-suite / VP / Director tier
    (
        [
            "ceo",
            "cfo",
            "coo",
            "cmo",
            "cto",
            "cio",
            "cpo",
            "chief",
            "vice president",
            "vp ",
            "v.p.",
            "director",
            "president",
            "founder",
            "owner",
            "managing director",
            "general manager",
        ],
        35,
    ),
    # Procurement / Buyer tier
    (
        [
            "procurement",
            "buyer",
            "purchasing",
            "supply chain",
            "sourcing",
            "acquisition",
        ],
        30,
    ),
    # Manager / Head tier
    (
        [
            "manager",
            "head of",
            "head,",
            "team lead",
            "supervisor",
            "coordinator",
            "lead ",
        ],
        25,
    ),
]
# No clinical tier. It gave a pharmacist, doctor or nurse 20 points, ahead of
# an unknown role, and none of them buys contract manufacturing for a
# supplement brand.

_DEFAULT_ROLE_SCORE = 5

# Role-based email prefixes (before the @)
_ROLE_BASED_PREFIXES = [
    "ceo",
    "cfo",
    "coo",
    "cmo",
    "cto",
    "cio",
    "director",
    "president",
    "founder",
    "owner",
    "vp",
    "manager",
]

_DEPARTMENT_PREFIXES = [
    "procurement",
    "purchasing",
    "sales",
    "marketing",
    "info",
    "contact",
    "support",
    "admin",
    "hr",
    "office",
    "reception",
    "enquiries",
    "inquiries",
]

# Organization keywords signalling the client's market (client/<client>.json):
# the words the relevance gate accepts and the discovery searches are built
# from, so all three name one market, in the languages its companies write in.
# Matched at the start of a word (see _starts_a_word).
_MARKET_KEYWORDS = tuple(dict.fromkeys([*DEFAULT_VERTICALS, *VERTICAL_SEEDS]))

# The pages the crawler reads an address from (SearchModule's contact paths),
# in the languages it reads them in. Only the English words counted, so an
# address on a German /kontakt or /impressum scored 10 points below the same
# address on an English /contact.
_CONTACT_PAGE_WORDS = (
    "contact",  # also contact-us, contacto, nous-contacter
    "kontakt",
    "contatt",  # contatti, contatto, contattaci
    "impressum",
    "imprint",
    "mentions-legales",
    "aviso-legal",
)
_ABOUT_PAGE_WORDS = (
    "about",
    "team",
    "staff",
    "chi-siamo",
    "ueber-uns",
    "uber-uns",
    "a-propos",
    "quienes-somos",
    "over-ons",
)


def _has_term(haystack: str, term: str) -> bool:
    r"""Whole-word match for a keyword or multi-word phrase.

    A bare ``term in haystack`` scored "Coordinator" as C-suite because
    "coo" is its first three letters, and "Contractor" likewise via "cto" -
    both skipping the 25-point Manager tier to land on 35. On the
    organisation side "lab" matched inside "Nordic Labels & Packaging" and
    "Collaborate Media", scoring unrelated companies as pharma-relevant.

    Anchored with \b at both ends; internal whitespace matches any run of
    space so "general manager" still matches "General  Manager".

    This makes each keyword match WHOLE WORDS ONLY, so a stem meant to
    cover a longer word no longer does - "pharma" stops matching
    "pharmaceutical". The organisation terms are stems, so they are matched
    with _starts_a_word instead.
    """
    if not term:
        return False
    # (?<!\w) / (?!\w) rather than \b on both ends. Several keywords here end
    # in punctuation ("v.p.", "head,"), and \b after a '.' or ',' demands a
    # WORD character next - so \b would have stopped those matching at all,
    # swapping one silent scoring bug for another. These lookarounds express
    # the actual rule: the match must not be glued to further word characters.
    # "coo" therefore stops firing inside "coordinator", while "v.p." still
    # fires in "V.P. Sales".
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in term.split()) + r"(?!\w)"
    return re.search(pattern, haystack, re.IGNORECASE) is not None


def _starts_a_word(haystack: str, term: str) -> bool:
    """``term`` at the start of a word, however the word ends.

    The organisation terms are the relevance gate's, written as stems for its
    substring match: "nutraceutic" for Nutraceutica, "nahrungsergänzung" for
    Nahrungsergänzungsmittel. As whole words they would miss every plural and
    compound ("Vitalis Supplements"); as bare substrings they could match
    inside an unrelated word, as "lab" once did in "Labels". Anchored on the
    left only, like the competitor gate's stems.
    """
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(w) for w in term.split())
    return re.search(pattern, haystack, re.IGNORECASE) is not None


class LeadScorer:
    """Scores individual contacts and assigns quality tiers."""

    def score_contact(self, contact: dict) -> dict:
        """
        Score a contact dictionary and return the result.

        Parameters
        ----------
        contact : dict
            Must contain at least ``email``. Optional keys:
            ``role``, ``organization``, ``source_url``.

        Returns
        -------
        dict
            ``{score: int, breakdown: dict, tier: str}``
        """
        breakdown = {
            "role_relevance": self._score_role(contact.get("role")),
            "email_quality": self._score_email(contact.get("email", "")),
            "organization_signals": self._score_organization(
                contact.get("organization")
            ),
            "source_quality": self._score_source(contact.get("source_url")),
        }

        total = sum(breakdown.values())
        total = max(0, min(100, total))  # clamp

        tier = self._tier_label(total)

        logger.debug(
            "Scored contact '%s' -> %d (tier %s)",
            contact.get("email", "?"),
            total,
            tier,
        )

        return {"score": total, "breakdown": breakdown, "tier": tier}

    # ------------------------------------------------------------------
    # Private scoring helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_role(role: str | None) -> int:
        """Score based on the contact's job title / role."""
        if not role:
            return _DEFAULT_ROLE_SCORE

        role_lower = role.lower().strip()
        if not role_lower:
            return _DEFAULT_ROLE_SCORE

        for keywords, points in _ROLE_SCORES:
            for kw in keywords:
                if _has_term(role_lower, kw):
                    return points

        return _DEFAULT_ROLE_SCORE

    @staticmethod
    def _score_email(email: str) -> int:
        """Score based on email address pattern quality."""
        if not email or "@" not in email:
            return 0

        local_part = email.split("@")[0].lower()

        # Personal business pattern: firstname.lastname or first_last, etc.
        if re.match(r"^[a-z]+[._][a-z]+", local_part):
            return 25

        # Role-based email (ceo@, director@, etc.)
        for prefix in _ROLE_BASED_PREFIXES:
            if local_part == prefix or local_part.startswith(prefix + "."):
                return 15

        # Department email (procurement@, sales@, etc.)
        for prefix in _DEPARTMENT_PREFIXES:
            if local_part == prefix or local_part.startswith(prefix + "."):
                return 10

        return 0

    @staticmethod
    def _score_organization(org: str | None) -> int:
        """Score based on organization name signals."""
        if not org:
            return 5

        org_lower = org.lower().strip()
        if not org_lower:
            return 5

        for kw in _MARKET_KEYWORDS:
            if _starts_a_word(org_lower, kw):
                return 25

        # Has some organization name but not one in the client's market
        return 10

    @staticmethod
    def _score_source(source_url: str | None) -> int:
        """Score based on the page where the contact was found."""
        if not source_url:
            return 5

        url_lower = source_url.lower()

        if any(word in url_lower for word in _CONTACT_PAGE_WORDS):
            return 15
        if any(word in url_lower for word in _ABOUT_PAGE_WORDS):
            return 12
        if "directory" in url_lower or "listing" in url_lower or "members" in url_lower:
            return 10

        return 5

    @staticmethod
    def _tier_label(score: int) -> str:
        """Map a numeric score to a tier letter."""
        if score >= 75:
            return "A"
        if score >= 50:
            return "B"
        if score >= 25:
            return "C"
        return "D"
