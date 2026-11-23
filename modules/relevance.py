"""
Lead relevance filter for the B2B Outreach Agent.

A lightweight, deterministic gate that keeps outreach on-target *before* an
email is ever composed:

  1. Geo gate      - reject clearly non-European country-code domains.
  2. Negative gate - reject obvious non-target signals (news/media, other
                     industries) found in the org name / page context.
  3. Vertical gate - (optional) require at least one signal of the client's market
                     (food supplement / nutraceutical / vitamin ...).

This is intentionally conservative and cheap. Nuanced "is this really a
manufacturer we want?" judgement is left to the AI relevance classifier
(Phase 2); this filter just stops the obvious junk (cnn.com, economictimes,
mailinator, non-EU country domains, etc.).
"""

import logging
import re
from urllib.parse import urlparse

from modules.client_profile import PROFILE

logger = logging.getLogger(__name__)


# The client's market words (client/<client>.json; overridable via config).
# The vertical gate matches them by SUBSTRING on purpose (see _has_phrase), so
# stems cover their own inflections, in every language the profile lists.
DEFAULT_VERTICALS = PROFILE.verticals

_BUYER_PHRASES = PROFILE.buyer_phrases

# Clearly non-European country-code TLDs -> hard reject when eu_only.
NON_EU_CCTLDS = {
    "us",
    "in",
    "cn",
    "jp",
    "au",
    "ca",
    "br",
    "ru",
    "mx",
    "kr",
    "sg",
    "hk",
    "tw",
    "za",
    "ae",
    "id",
    "th",
    "vn",
    "ph",
    "my",
    "nz",
    "ar",
    "cl",
    "ng",
    "pk",
    "bd",
    "lk",
    "sa",
    "eg",
    "ke",
    "il",
    "ir",
    "iq",
    "qa",
    "kw",
    "om",
    "jo",
    "ma",
    "dz",
    "tn",
    "gh",
    "bo",
    "pe",
    "ve",
    "ec",
    "uy",
    "py",
    "cr",
    "pa",
    "do",
    "gt",
    "hn",
    "sv",
    "ni",
    "cu",
    "kz",
    "uz",
    "az",
    "ge",
    "am",
    "mn",
    "np",
    "kh",
    "la",
    "mm",
    "bn",
}

# European country-code TLDs (used to set the informational 'european' flag).
EUROPEAN_CCTLDS = {
    "uk",
    "gb",
    "ie",
    "de",
    "fr",
    "it",
    "es",
    "pt",
    "nl",
    "be",
    "lu",
    "at",
    "ch",
    "dk",
    "se",
    "no",
    "fi",
    "is",
    "pl",
    "cz",
    "sk",
    "hu",
    "ro",
    "bg",
    "gr",
    "hr",
    "si",
    "ee",
    "lv",
    "lt",
    "mt",
    "cy",
    "eu",
    "li",
    "mc",
    "ad",
    "sm",
    "va",
    "al",
    "rs",
    "ba",
    "mk",
    "me",
    "md",
    "ua",
    "by",
}

# Non-European region tokens that appear in the LOCAL PART of routed mailboxes
# (e.g. sales.thai@, sales.us@, office.au@). Matched only as whole
# dot/dash/underscore-delimited tokens, so ordinary words like "business",
# "august", "info" or "customer" are never affected. European codes
# (uk/de/fr/...) are intentionally absent so European regional mailboxes pass.
NON_EU_REGION_TOKENS = {
    "us",
    "usa",
    "america",
    "americas",
    "au",
    "aus",
    "australia",
    "nz",
    "ca",
    "canada",
    "br",
    "brazil",
    "mx",
    "mexico",
    "latam",
    "jp",
    "japan",
    "kr",
    "korea",
    "cn",
    "china",
    "tw",
    "taiwan",
    "hk",
    "hongkong",
    "sg",
    "singapore",
    "asia",
    "apac",
    "th",
    "thai",
    "thailand",
    "vn",
    "vietnam",
    "ph",
    "malaysia",
    "indonesia",
    "ae",
    "dubai",
    "uae",
    "za",
    "africa",
    "ru",
    "russia",
    "il",
    "israel",
}

# Consumer / ISP mail domains from non-European regions - never a real
# European B2B lead. High-precision blocklist (catches e.g. Taiwan's hinet).
NON_EU_DOMAINS = {
    "hinet.net",
    "163.com",
    "126.com",
    "qq.com",
    "sina.com",
    "sohu.com",
    "naver.com",
    "daum.net",
    "rediffmail.com",
}

# Obvious placeholder / example addresses scraped from template pages -
# these are never real leads.
PLACEHOLDER_DOMAINS = {
    "domain.com",
    "example.com",
    "example.org",
    "example.net",
    "email.com",
    "yourcompany.com",
    "company.com",
    "test.com",
    "mydomain.com",
    "yourdomain.com",
    "sentry.io",
    "wixpress.com",
}
PLACEHOLDER_LOCALPARTS = {
    "user",
    "username",
    "youremail",
    "email",
    "name",
    "firstname",
    "lastname",
    "example",
    "test",
    "demo",
    "someone",
    "yourname",
}

# Signals the company IS a contract manufacturer - i.e. a COMPETITOR, not a
# buyer. Without this gate they sail through the vertical check (they match every
# supplement keyword) and we email a rival offering to manufacture for them.
# The AI classifier is asked to catch this too, but it fails OPEN by design, so
# a rate limit or outage would let them through; this gate is deterministic.
#
# Deliberately matches self-description as a producer-for-others. A brand that
# merely mentions using a contract manufacturer will also be rejected - that
# trade-off is intentional: a missed prospect costs one email, contacting a
# competitor is embarrassing and useless. Every rejection is logged with a reason.
COMPETITOR_KEYWORDS = PROFILE.competitor_keywords

# Qualifiers that, when they appear alongside "manufactur", mean the company
# Competitor terms read from what a company says about ITSELF - its name and
# its own description - but never from the source URL, and never when the
# sentence is a want-ad.
#
# "cdmo" is one short token, unlike every other competitor entry, which are
# multi-word phrases. That makes it match in two places the phrases never
# could:
#
#   the URL      .../exhibitors/cdmo-zone/brandx   - a listing, not a claim
#   a want-ad    "Buscamos un CDMO para fabricar nuestros complementos"
#
# The second is the expensive one: that company is asking for exactly what
# SUPPLECO sells, and it was being discarded as a rival with a reason that reads
# like the filter working.
#
# It cannot simply move to name-only matching. A live discovery run found
# naturacare.com, whose own site says "a European CDMO and pharmaceutical
# dietary supplement manufacturer" - the signal is genuinely in the prose, and
# dropping it lets a direct competitor through. So the rule is about VOICE, not
# location: self-description counts, solicitation does not.
COMPETITOR_SELF_DESCRIPTION_KEYWORDS = PROFILE.competitor_self_description_keywords

# Phrases that turn the rest of the sentence into something the company WANTS
# rather than something it IS. Checked immediately before the keyword, in the
# five languages this agent actually meets.
#
# Three ways a rival's own sentence used to read as a want-ad, each found by
# review: a want-ad word inside a longer one ("ricerchiamo e sviluppiamo",
# research), the Italian "cercare di" (to TRY to: "sto cercando di
# presentarvi la nostra azienda, produzione conto terzi"), and a want-ad
# opening followed by the company saying what it is ("Stiamo cercando nuovi
# brand: siamo contract manufacturer"). A want-ad's object is what the company
# lacks; once it says "siamo" / "we are", the rest is a claim.
_SEEKING_RE = re.compile(
    r"\b(?:buscamos|buscando|en busca de|necesitamos|"
    # "cerco" is how SUPPLECO's own keyword list phrases a buyer's need
    # ("cerco CDMO integratori"); without it that buyer read as a rival.
    r"(?:cerco|sto cercando|stiamo cercando|cerchiamo)(?!\s+di\b)|"
    r"in cerca di|alla ricerca di|"
    r"wir suchen|auf der suche nach|"
    r"nous recherchons|a la recherche|"
    r"looking for|seeking|in search of|searching for|"
    # "partner with a CDMO" asks for one; "partner with us", "partner with
    # NutraPro" or "partner with brands" is a rival describing itself.
    r"we need|we require|partner with an?\b)"
    r"(?:(?!\b(?:siamo|we are|we're|somos|nous sommes|wir sind)\b)[^.!?]){0,60}$",
    re.IGNORECASE,
)

# manufactures FOR OTHERS - i.e. a competitor. Kept separate from the phrases
# above so intervening words can't defeat the match, as in
# "Private Label Nutraceutical Manufacturer".
# Stems that, next to a qualifier, mean the company makes what the client
# makes, for others (SUPPLECO: "manufactur" + "private label").
COMPETITOR_CORE_STEMS = PROFILE.competitor_core_stems
COMPETITOR_QUALIFIERS = PROFILE.competitor_qualifiers

# Signals that the target is not a manufacturer we want to contact.
NEGATIVE_KEYWORDS = PROFILE.negative_keywords


class RelevanceFilter:
    """Deterministic pre-send relevance gate."""

    def __init__(
        self,
        verticals: list | None = None,
        require_vertical: bool = True,
        eu_only: bool = True,
    ):
        """``verticals`` replace the built-in market words, so an operator can
        narrow the gate (keep "wellness" spas out, say). SUPPLECO's own words for
        its buyers are always added: the agent searches for them, and a company
        found by one must not then be rejected for lacking a market signal.
        Each is the whole phrase, so it widens a narrowed list only by the
        companies SUPPLECO itself named."""
        self.verticals = [
            v.lower().strip()
            for v in dict.fromkeys(
                [*(verticals or DEFAULT_VERTICALS), *_BUYER_PHRASES]
            )
            if v.strip()
        ]
        self.require_vertical = require_vertical
        self.eu_only = eu_only

    @staticmethod
    def _domain(email: str) -> str:
        email = (email or "").lower().strip()
        return email.rsplit("@", 1)[-1] if "@" in email else ""

    @staticmethod
    def _localpart(email: str) -> str:
        email = (email or "").lower().strip()
        return email.rsplit("@", 1)[0] if "@" in email else email

    @staticmethod
    def _tokens(text: str) -> set:
        """Split on ./-/_/+ into whole tokens (for exact region matching)."""
        return {t for t in re.split(r"[._\-+]", text or "") if t}

    @staticmethod
    def _has_phrase(haystack: str, phrase: str) -> bool:
        """Whole-word match for a keyword or multi-word phrase.

        The rejection gates used a bare substring test, so the negative keyword
        "news" fired on "newsletter" - and practically every company site says
        "Subscribe to our newsletter" in its meta description or first <p>.
        Qualified leads were therefore discarded wholesale, and logged as an
        ordinary off-target rejection, so it read like the filter working.

        Only the REJECTION gates use this. The vertical gate keeps substring
        matching on purpose: \\b anchoring there would stop "supplement" from
        matching "supplements" and reject good leads instead.
        """
        return re.search(rf"\b{re.escape(phrase)}\b", haystack) is not None

    @classmethod
    def _self_described_competitor(cls, organization, rationale) -> str | None:
        """A single-token competitor term, but only where it is a CLAIM.

        Matched against the company's name and its own description - never the
        source URL, where the token turns up in listing paths like
        /exhibitors/cdmo-zone/ that say nothing about the company.

        Suppressed when the sentence is a want-ad. "Buscamos un CDMO para
        fabricar nuestros complementos" is a brand asking for exactly what
        SUPPLECO sells; reading that as a rival throws away the best kind of
        lead there is, and logs it as an ordinary competitor rejection so
        nobody looks.

        Name and description are checked SEPARATELY rather than joined, so a
        seeking phrase at the end of one cannot reach across into the other.
        """
        return next(
            (
                kw
                for kw in COMPETITOR_SELF_DESCRIPTION_KEYWORDS
                if cls._claimed((organization, rationale), rf"\b{re.escape(kw)}")
            ),
            None,
        )

    @staticmethod
    def _claimed(fields, pattern: str) -> bool:
        """True if a field matches `pattern` as a CLAIM, not inside a want-ad.

        "We are a contract manufacturer" is a claim; "we are looking for a
        contract manufacturer" is a prospect asking for SUPPLECO. Fields are
        checked separately, so a seeking phrase at the end of one cannot reach
        across into the next.
        """
        for field in fields:
            text = (field or "").lower()
            for m in re.finditer(pattern, text):
                if not _SEEKING_RE.search(text[: m.start()]):
                    return True
                # A rival's own copy often opens with a question put to
                # buyers: "Looking for a contract manufacturer? We produce
                # for brands." A want-ad states a need, with a subject ("We
                # are looking for..."); a subjectless question is a hook.
                start = max(text.rfind(c, 0, m.start()) for c in ".!?") + 1
                end = re.search(r"[.!?]", text[m.end() :])
                if (
                    end
                    and end.group() == "?"
                    and _SEEKING_RE.match(text[start : m.start()].lstrip())
                ):
                    return True
        return False

    @classmethod
    def _tld(cls, email: str) -> str:
        domain = cls._domain(email)
        return domain.rsplit(".", 1)[-1] if "." in domain else ""

    def assess(self, contact: dict) -> dict:
        """
        Assess a contact's relevance.

        Returns a dict: ``{relevant, reason, european, vertical_match}``.
        """
        email = (contact.get("email") or "").lower()
        tld = self._tld(email)
        domain = self._domain(email)
        localpart = self._localpart(email)
        european = tld in EUROPEAN_CCTLDS or tld not in NON_EU_CCTLDS

        haystack = " ".join(
            filter(
                None,
                [
                    contact.get("organization"),
                    contact.get("qualification_rationale"),
                    contact.get("source_url"),
                    domain,
                ],
            )
        ).lower()
        # The REJECTION gates read the source URL's host but not its path.
        # Where an address was found says nothing about the company: a brand
        # found on /news/... is not a news site, and one listed under a
        # directory's /contract-manufacturing/ section is not a rival. The
        # vertical gate below is a positive signal and keeps the full URL.
        url_host = urlparse(contact.get("source_url") or "").hostname or ""
        reject_haystack = " ".join(
            filter(
                None,
                [
                    contact.get("organization"),
                    contact.get("qualification_rationale"),
                    url_host,
                    domain,
                ],
            )
        ).lower()

        # 0. Junk / placeholder gate (always on - never real leads). Matches
        #    placeholder domains incl. subdomains (sentry.wixpress.com), the
        #    placeholder local parts, and machine-generated hex mailbox IDs
        #    (e.g. Sentry event ids scraped from a site's error-tracking config).
        if (
            localpart in PLACEHOLDER_LOCALPARTS
            or any(domain == d or domain.endswith("." + d) for d in PLACEHOLDER_DOMAINS)
            or re.fullmatch(r"[0-9a-f]{16,}", localpart)
        ):
            return {
                "relevant": False,
                "european": False,
                "vertical_match": False,
                "reason": "placeholder/machine-generated address",
            }

        # 1. Geo gate - non-European country-code TLD.
        if self.eu_only and tld in NON_EU_CCTLDS:
            return {
                "relevant": False,
                "european": False,
                "vertical_match": False,
                "reason": f"non-European domain (.{tld})",
            }

        # 1a. Geo gate - non-European consumer/ISP domain (e.g. Taiwan hinet.net),
        #     matching subdomains too (msa.hinet.net -> hinet.net).
        if self.eu_only and any(
            domain == d or domain.endswith("." + d) for d in NON_EU_DOMAINS
        ):
            return {
                "relevant": False,
                "european": False,
                "vertical_match": False,
                "reason": f"non-European mail domain ({domain})",
            }

        # 1b. Geo gate - non-European region encoded in the mailbox local part
        #     (e.g. sales.thai@, sales.us@, office.au@ on a generic .com domain).
        if self.eu_only:
            region = next(
                (t for t in self._tokens(localpart) if t in NON_EU_REGION_TOKENS), None
            )
            if region:
                return {
                    "relevant": False,
                    "european": False,
                    "vertical_match": False,
                    "reason": f"non-European regional mailbox ('{region}')",
                }

        # 2a. Competitor gate - checked BEFORE the vertical gate, because a rival
        # contract manufacturer matches every supplement keyword we look for.
        # Without this we email competitors offering to manufacture for them.
        # Every term must be a CLAIM, not part of a want-ad: "Cerchiamo un
        # contract manufacturer" is a brand asking for exactly what SUPPLECO
        # sells. Only "cdmo" had this; the other terms binned the best lead
        # there is as a routine competitor rejection.
        #
        # Left boundary only (\b before, none after): entries such as "white
        # label produc" deliberately end on a stem, and a trailing \b would
        # never match "producer" - which once let a white-label PRODUCER
        # through as an ordinary lead.
        claim_fields = (
            contact.get("organization"),
            contact.get("qualification_rationale"),
            url_host,
            domain,
        )
        comp = next(
            (
                kw
                for kw in COMPETITOR_KEYWORDS
                if self._claimed(claim_fields, rf"\b{re.escape(kw)}")
            ),
            None,
        )
        if not comp:
            comp = self._self_described_competitor(
                contact.get("organization"), contact.get("qualification_rationale")
            )
        if not comp and any(
            self._claimed(claim_fields, stem) for stem in COMPETITOR_CORE_STEMS
        ):
            # Catches the same idea with words in between, e.g.
            # "Private Label Nutraceutical Manufacturer".
            # Deliberately still a substring test: "manufactur" is a stem meant
            # to match manufacturer/manufacturing/manufactured.
            comp = next(
                (
                    q
                    for q in COMPETITOR_QUALIFIERS
                    if self._has_phrase(reject_haystack, q)
                ),
                None,
            )
        if comp:
            return {
                "relevant": False,
                "european": european,
                "vertical_match": False,
                "reason": f"competitor - {PROFILE.rival_label} ('{comp}')",
            }

        # 2b. Negative gate
        neg = next(
            (kw for kw in NEGATIVE_KEYWORDS if self._has_phrase(reject_haystack, kw)),
            None,
        )
        if neg:
            return {
                "relevant": False,
                "european": european,
                "vertical_match": False,
                "reason": f"non-target signal: '{neg}'",
            }

        # 3. Vertical gate
        vertical_match = any(kw in haystack for kw in self.verticals)
        if self.require_vertical and not vertical_match:
            return {
                "relevant": False,
                "european": european,
                "vertical_match": False,
                "reason": f"no {PROFILE.name} vertical signal ({PROFILE.market_label})",
            }

        return {
            "relevant": True,
            "european": european,
            "vertical_match": vertical_match,
            "reason": "ok",
        }
