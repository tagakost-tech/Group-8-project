"""What every commercial email must carry, in one place.

The legal footer is rendered here rather than typed into each template, so an
edited template, an A/B variant, a follow-up and the built-in fallback all
say the same thing - and a template that drops it can be refused on save.
"""

import hashlib
import html
import ipaddress
import re
from datetime import timedelta
from urllib.parse import urlparse

from modules import clock

# A template without these cannot be sent lawfully: the first carries the
# sender's legal identity and the notice, the second the opt-out.
REQUIRED_TEMPLATE_PLACEHOLDERS = ("{{legal_footer}}", "{{unsubscribe_url}}")

# #4b5563 on white is 7.6:1, above the 4.5:1 minimum for small text; the
# grey this replaced, #9ca3af, was 2.5:1.
_FOOTER_COLOUR = "#4b5563"
_LINK_STYLE = f"color: {_FOOTER_COLOUR}; text-decoration: underline;"
_TEXT_STYLE = (
    f"margin: 12px 0 0 0; font-size: 11px; color: {_FOOTER_COLOUR}; line-height: 1.5;"
)


_ADDRESS_IN_TEXT = re.compile(r"([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_UNSUBSCRIBE_TOKEN = re.compile(r"(/unsubscribe/)[A-Za-z0-9_\-=.]+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# A candidate is masked only once ipaddress accepts it and it has three groups
# or more, so a clock time (10:30:00) or a slice in a traceback (x[1::2]) stays.
_IPV6 = re.compile(
    r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}"
    r"(?:[0-9A-Fa-f]{1,4}|(?:\d{1,3}\.){3}\d{1,3})?(?![\w:]|\.\d)"
)
# Keys and passwords: the own AI server's, a mailbox's, or one a library puts
# in an error message. What names the value stays, so the line still says
# what was there.
_SECRET_VALUES = (
    re.compile(
        r"(?i)\b((?:authorization|(?:x-)?api-key)[\"']?\s*[:=]\s*[\"']?"
        r"(?:(?:bearer|basic|digest|token)\s+)?)[^\s\"',;}]+"
    ),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}"),
    # A signed token on its own: a Microsoft access token or the assertion
    # the agent signs for one. And a SASL XOAUTH2 answer, whose base64 always
    # starts with "user=" encoded and carries the token.
    re.compile(r"\b()eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"),
    re.compile(r"(?i)\b(xoauth2\s+)[A-Za-z0-9+/]{16,}=*"),
    re.compile(r"\b()dXNlcj1[A-Za-z0-9+/]{16,}=*"),
    re.compile(r"\b(sk-)[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(gsk_)[A-Za-z0-9]{16,}"),
    re.compile(r"\b(AIza)[0-9A-Za-z_\-]{30,}"),
    # A password whatever its length, also as a quoted JSON key; a quoted
    # one up to its closing quote, spaces and all.
    re.compile(
        r"(?i)\b([\w.\-]*(?:password|passwd|pwd)(?:[\"']?\s*=\s*|[\"']\s*:\s*)[\"']?)"
        r"(?:(?<=\")[^\"\n]+|(?<=')[^'\n]+|[^\s\"'&,;]+)"
    ),
    # key=, token=, secret= (or a quoted name and a colon, as in JSON or a
    # printed dict): a long value only, so key=status stays readable; never
    # an erasure token, which the erasure is logged by.
    re.compile(
        r"(?i)\b([\w.\-]*(?:key|token|secret)(?:[\"']?\s*=\s*|[\"']\s*:\s*)[\"']?)"
        r"(?!erased_)[A-Za-z0-9+/_.~\-]{16,}=*"
    ),
)


def _mask_ipv6(m: re.Match) -> str:
    text = m.group(0)
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return text
    if sum(1 for group in text.split(":") if group) < 3:
        return text
    return "[ip]"


def _mask_address(m: re.Match) -> str:
    local, domain = m.group(1), m.group(2)
    if local.startswith("erased_") and domain == "redacted.invalid":
        return m.group(0)  # an erasure is logged by its token, on purpose
    return f"{local[0]}***@{domain}"


def mask_personal(text: str) -> str:
    """Mask what identifies a person in a log line.

    An address keeps its first letter and its domain - delivery problems are
    diagnosed by domain - and loses the rest of the part before the @, which
    is the person. An unsubscribe link's token identifies the address to
    whoever holds the key (and its old form held it readably), and an IP
    address can point to a person, so both are masked, IPv4 and IPv6. An
    erasure token is left readable: the erasure is logged by it. Keys,
    tokens and passwords are masked too, though they name no one: whoever
    reads a log must not be able to send mail or call the AI server with them.
    """
    text = _UNSUBSCRIBE_TOKEN.sub(r"\1[token]", text)
    for pattern in _SECRET_VALUES:
        text = pattern.sub(r"\1[secret]", text)
    text = _ADDRESS_IN_TEXT.sub(_mask_address, text)
    text = _IPV6.sub(_mask_ipv6, text)
    return _IPV4.sub("[ip]", text)


def is_public_base_url(base_url: str) -> bool:
    """True when BASE_URL is a public address a recipient could reach.

    Decides whether the unsubscribe link is a web page or a mailto, and
    whether a pixel could load at all. Loopback, LAN / private, link-local
    and .local addresses count as NOT public.
    """
    parsed = urlparse((base_url or "").strip())
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified
        )
    except ValueError:
        return True  # a real hostname (not a bare IP) -> public


# Where an address came from (decision L1.4) or, for one an operator typed
# in, the channel it came through (L1.5), with the lawful basis each gives.
# Recorded on the contact when it is stored, with RULE_VERSION. The
# developer's proposals, pending counsel.
SOURCE_CHANNELS = {
    "own_website": ("the company's own website", "legitimate_interest"),
    "contact_page": (
        "a contact, legal or about page on the company's own website",
        "legitimate_interest",
    ),
    "third_party": ("a third-party directory or listing", "legitimate_interest"),
    "inbound_request": ("an inbound request from the company", "consent"),
    # A business card alone is not consent (L1.5).
    "trade_fair": ("a trade-fair contact", "legitimate_interest"),
    # Art. 130(4): SUPPLECO's own similar products only, with the right to
    # object offered at collection and in every email.
    "existing_customer": ("an existing customer", "existing_customer"),
    "referral": ("a referral", "legitimate_interest"),
}
MANUAL_CHANNELS = ("inbound_request", "trade_fair", "existing_customer", "referral")
# The version of the rules above, recorded with each contact so a later
# change can tell which contacts were collected under which rules.
RULE_VERSION = "rules-draft-1"


def basis_for(channel: str | None) -> str:
    """The lawful basis a source channel gives; ValueError for no channel."""
    if channel not in SOURCE_CHANNELS:
        raise ValueError(f"unknown or missing source channel: {channel!r}")
    return SOURCE_CHANNELS[channel][1]


def first_contact_overdue(settings, collected_at: str | None) -> bool:
    """True once a contact collected at `collected_at` (contacts.timestamp)
    is past the first-email limit. The first email carries the notice, due
    within a month of collection (Art. 14(3)(a); decision L1.8)."""
    if not collected_at:
        return False
    limit = clock.now() - timedelta(days=settings.first_contact_max_age_days)
    return str(collected_at) < limit.strftime("%Y-%m-%d %H:%M:%S")


def opt_out_route(settings) -> tuple[bool, str]:
    """Whether an opt-out from a recipient would be read, and how.

    A public https BASE_URL gives a web unsubscribe link and the one-click
    unsubscribe that mail apps show (offered over https only). Otherwise the
    route is a mailto to the sending mailbox, which only the IMAP monitor
    reads. doctor reports this and the send check enforces it, from this one
    rule. Whether the monitor actually reads is a rule of its own
    (imap_monitor.inbox_read).
    """
    base_url = (settings.base_url or "").strip()
    if is_public_base_url(base_url) and base_url.lower().startswith("https://"):
        return True, "web unsubscribe link (public https BASE_URL)"
    if settings.imap_enabled:
        return True, "mailto link, read by the IMAP monitor"
    return False, (
        "opt-out emails to the mailto link go unread - set IMAP_ENABLED=true "
        "or publish BASE_URL over https (GDPR Art. 21(3))"
    )


def recipient_country(email: str) -> str | None:
    """The ISO country code an address's country-code domain shows, or None.

    .uk is the United Kingdom (GB). A generic domain - .com, .eu, .swiss -
    shows no country. A country-code domain is only a proxy (a .de domain can
    belong to a Swiss company), which is why the lock built on it refuses by
    default and is interim until the full country table (W2).
    """
    email = (email or "").strip().lower()
    domain = email.rsplit("@", 1)[1] if "@" in email else ""
    return _domain_country(domain)


def _domain_country(domain: str) -> str | None:
    """The ISO country code a domain's last label shows, or None. Shared by
    the address and the company website, so the two read a domain alike."""
    domain = (domain or "").strip().lower().rstrip(".")
    tld = domain.rsplit(".", 1)[1] if "." in domain else ""
    if len(tld) != 2 or not tld.isalpha() or tld == "eu":
        return None
    return "GB" if tld == "uk" else tld.upper()


# Country-code domains sold to anyone as a brand name: a .io or .ai website
# says nothing about where the company is. Read as a country, an Italian
# brand on brand.io would be stored as the British Indian Ocean Territory,
# and a stored country is never asked about again.
_VANITY_TLDS = frozenset({"ai", "co", "fm", "io", "ly", "me", "tv"})


def website_country(url: str | None) -> str | None:
    """The ISO country code a company website's domain shows, or None.

    Read from the website, not the address: the address has its own rule,
    and a company writing from gmail.com still publishes its site on its own
    domain."""
    if not url:
        return None
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return None
    country = _domain_country(host)
    if country is None or country.lower() in _VANITY_TLDS:
        return None
    return country


def enabled_countries(settings) -> set[str]:
    """The countries counsel has enabled (ENABLED_COUNTRIES), as ISO codes."""
    return {
        c.strip().upper()
        for c in (settings.enabled_countries or "").split(",")
        if c.strip()
    }


def italian_countries(settings) -> set[str]:
    """The countries whose recipients are written to in Italian
    (ITALIAN_COUNTRIES), as ISO codes."""
    return {
        c.strip().upper()
        for c in (getattr(settings, "italian_countries", "") or "").split(",")
        if c.strip()
    }


# Where a contact's company country came from: its website's domain, the
# page it was collected from, or the AI's reading of its company.
COUNTRY_SOURCES = ("website", "page", "ai")
LANGUAGE_CHOICES = ("it", "en")
# Why a contact gets its language: the first email it was sent, the
# operator's choice, its address, its company's country (named by where that
# came from), or nothing showing Italian.
LANGUAGE_REASONS = ("sent", "operator", "address", *COUNTRY_SOURCES, "default")


def notice_language(version: str | None) -> str:
    """The language an email was written in, from the notice version recorded
    with it. Every Italian version ends in "-it" (NOTICE_VERSIONS_IT). A row
    with no version was sent before versions were recorded (27 October 2026),
    and Italian emails began on 11 November 2026, so it went in English."""
    return "it" if (version or "").endswith("-it") else "en"


def language_and_reason(
    settings,
    email: str,
    contact: dict | None = None,
    sent_language: str | None = None,
) -> tuple[str, str]:
    """The language of every email to this contact, and why (LANGUAGE_REASONS).

    Once it was sent an email, that email's language: a thread never changes
    language half way. Before that, the operator's choice; then Italian when
    its address, or its company's country, is one ITALIAN_COUNTRIES lists;
    English otherwise. Company evidence only adds Italian: an Italian email
    to someone who cannot read it is the worse mistake, so a .it address
    stays Italian and only good evidence makes anyone else Italian."""
    if sent_language in LANGUAGE_CHOICES:
        return sent_language, "sent"
    contact = contact or {}
    choice = contact.get("language_choice")
    if choice in LANGUAGE_CHOICES:
        return choice, "operator"
    italian = italian_countries(settings)
    if recipient_country(email) in italian:
        return "it", "address"
    source = contact.get("company_country_source")
    if contact.get("company_country") in italian and source in COUNTRY_SOURCES:
        return "it", source
    return "en", "default"


def email_language(
    settings,
    email: str,
    contact: dict | None = None,
    sent_language: str | None = None,
) -> str:
    """The language of every email to this contact (language_and_reason).
    Given an address alone, the address decides, and a domain that shows no
    country (.com, .eu) gets English."""
    return language_and_reason(settings, email, contact, sent_language)[0]


def language_for(
    settings, db, contact: dict | None = None, email: str = ""
) -> tuple[str, str]:
    """The language of a contact as stored, and why: the one answer for the
    first email, a follow-up, a retry, Direct Send, the unsubscribe page and
    the dashboard.

    The row is read again, so a choice the operator made after a batch read
    its contacts counts, and the first email's recorded language decides once
    there is one. An address with no row (a preview of a new address, an
    erased contact's link) is decided by the address and what the caller
    passed. The database is passed in because database.py imports this
    module. A database error is raised: a send must not guess its language."""
    contact = contact or {}
    email = email or contact.get("email") or ""
    cid = contact.get("id")
    stored = db.get_contact_by_id(cid) if cid else None
    if stored is None and email:
        stored = db.get_contact_by_email(email)
    if stored is None:
        return language_and_reason(settings, email, contact)
    sent = db.first_sent_language(stored["id"])
    return language_and_reason(settings, email, stored, sent)


def language_reason_words(
    language: str, reason: str, email: str = "", contact: dict | None = None
) -> str:
    """A contact's language and why, in the operator's words."""
    name = "Italian" if language == "it" else "English"
    if reason == "sent":
        return f"{name} (the language of its first email)"
    if reason == "operator":
        return f"{name} (set by you)"
    if language != "it":
        return "English"
    if reason == "address":
        return f"Italian (its address ends in .{(email or '').rsplit('.', 1)[-1]})"
    if reason == "website":
        host = urlparse((contact or {}).get("source_url") or "").hostname or ""
        return f"Italian (its company website ends in .{host.rsplit('.', 1)[-1]})"
    if reason == "page":
        if (contact or {}).get("source_channel") in MANUAL_CHANNELS:
            return "Italian (its company's name shows an Italian company)"
        return "Italian (its company's page shows an Italian company)"
    if reason == "ai":
        return "Italian (the AI's reading of its company)"
    return "Italian"


def country_refusal(settings, email: str) -> str | None:
    """Why the interim country lock refuses this recipient, or None."""
    country = recipient_country(email)
    if country is None:
        if settings.unknown_country_allowed:
            return None
        return (
            "its domain shows no country, which counts as needing consent "
            "(UNKNOWN_COUNTRY_ALLOWED is off)"
        )
    if country not in enabled_countries(settings):
        return f"{country} is not an enabled country (ENABLED_COUNTRIES)"
    return None


def missing_placeholders(template_html: str) -> list[str]:
    """The required placeholders a template does not contain."""
    return [p for p in REQUIRED_TEMPLATE_PLACEHOLDERS if p not in (template_html or "")]


def _source_label(contact: dict, language: str = "en") -> str:
    """Where the address was found, as GDPR Art. 14(2)(f) requires.

    The host only: the full URL of a contact page is noise in a footer, and
    the host is what lets the recipient recognise their own website. An
    address an operator entered says how it reached SUPPLECO instead.
    """
    host = (
        (urlparse(contact.get("source_url") or "").hostname or "")
        .lower()
        .removeprefix("www.")
    )
    channel = contact.get("send_channel") or contact.get("source_channel")
    if language == "it":
        return host or _MANUAL_SOURCE_IT.get(channel, "fonti pubblicamente accessibili")
    return host or _MANUAL_SOURCE.get(channel, "publicly available business sources")


# The AI uses that see a recipient's data: their opening line, their email,
# their company's site, their reply. Choosing search words
# (ai_strategist_enabled) sees none.
AI_SWITCHES = (
    "ai_personalization_enabled",
    "ai_humanize_emails",
    "ai_relevance_enabled",
    "ai_reply_classification_enabled",
)


def ai_in_use(settings) -> bool:
    return any(getattr(settings, s, False) for s in AI_SWITCHES)


# What no prompt may carry, whatever the call site (step G10): an address,
# a phone number (9 to 15 digits; a date or a year is not one), or a URL's
# query string, where tracking ids and addresses travel.
_PHONE = re.compile(r"(?<![\w.])\+?\d(?:[\s()./-]?\d){8,14}(?!\w)")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")


def scrub_for_ai(text: str) -> str:
    text = _ADDRESS_IN_TEXT.sub("[email]", text or "")
    text = _URL_QUERY.sub(r"\1", text)
    return _PHONE.sub("[phone]", text)


# The first layer of the notice (decision L3.2): one text per lawful basis,
# so a contact who consented is not told they are emailed on legitimate
# interest. The developer's draft, pending counsel. Changing any of these
# words needs a new version in NOTICE_VERSIONS - a test holds each version's
# fingerprint - so the version recorded with each email names what it said.
_BASIS_SENTENCE = {
    "legitimate_interest": (
        "We rely on legitimate interest (GDPR Art. 6(1)(f)): offering our "
        "services to businesses that may need them."
    ),
    "consent": (
        "We rely on the consent you gave us (GDPR Art. 6(1)(a)), which you may "
        "withdraw at any time."
    ),
    "existing_customer": (
        "We write to you as our customer, about products like those you bought "
        "from us (Art. 130(4) of the Italian privacy code)."
    ),
}
_MANUAL_SOURCE = {
    "inbound_request": "your company's request to us",
    "trade_fair": "a trade fair",
    "existing_customer": "our dealings with your company",
    "referral": "a referral",
}
_NOTICE_TEXT = (
    "This is a commercial communication from {company}. We hold your name, job "
    "title and business email address, obtained from {source}, to offer our "
    "contract manufacturing of food supplements to your company. "
    "{basis_sentence} Our email provider receives them to deliver our "
    "emails{ai}{keep}. You may ask us for access, rectification or erasure, or "
    "to restrict their use, and you may complain to a data protection "
    "authority - in Italy, the Garante. Privacy contact: {contact}. Full "
    "notice: {privacy}."
)
# The AI is a model on SUPPLECO's own server (version 2): version 1 said "an
# AI service", which reads as an outside company receiving the data.
_AI_TEXT = (
    ", and an AI model running on our own server helps us write our emails "
    "and read replies, so no outside AI company receives them"
)
_KEEP_TEXT = "; we erase them {days} days after our last email if you do not reply"
# Art. 21(4): the right to object, presented clearly and apart from the rest.
_OBJECTION_TEXT = (
    'You may object to these emails at any time: <a href="{unsub}" '
    'style="{link}">unsubscribe</a>, or reply "unsubscribe".'
)
NOTICE_VERSIONS = {
    "legitimate_interest": "notice-li-2",
    "consent": "notice-consent-2",
    "existing_customer": "notice-customer-2",
}

# The same first layer in Italian, for recipients written to in Italian
# (ITALIAN_COUNTRIES), sentence for sentence. The developer's draft for
# counsel, as the English one is, and versioned apart from it: the version
# recorded with a send says which language it was in. Changing any of these
# words needs a new version in NOTICE_VERSIONS_IT.
_BASIS_SENTENCE_IT = {
    "legitimate_interest": (
        "Il trattamento si basa sul legittimo interesse (art. 6, par. 1, lett. f), "
        "del GDPR): offrire i nostri servizi alle aziende che potrebbero averne "
        "bisogno."
    ),
    "consent": (
        "Il trattamento si basa sul consenso che ci ha fornito (art. 6, par. 1, "
        "lett. a), del GDPR), che può revocare in qualsiasi momento."
    ),
    "existing_customer": (
        "Le scriviamo in quanto azienda nostra cliente, su prodotti analoghi a "
        "quelli che ha acquistato da noi (art. 130, comma 4, del D.Lgs. 196/2003)."
    ),
}
_MANUAL_SOURCE_IT = {
    "inbound_request": "una richiesta della Sua azienda",
    "trade_fair": "una fiera",
    "existing_customer": "i nostri rapporti con la Sua azienda",
    "referral": "una segnalazione",
}
_NOTICE_TEXT_IT = (
    "Questa è una comunicazione commerciale di {company}. Trattiamo il Suo "
    "nome, il Suo ruolo e il Suo indirizzo email aziendale, ottenuti tramite "
    "{source}, per proporre alla Sua azienda la nostra produzione conto terzi "
    "di integratori alimentari. {basis_sentence} Il nostro fornitore di posta "
    "elettronica riceve tali dati per recapitare le nostre email{ai}{keep}. "
    "Può chiederci l'accesso, la rettifica o la cancellazione dei Suoi dati, o "
    "la limitazione del trattamento, e può proporre reclamo a un'autorità di "
    "controllo: in Italia, il Garante per la protezione dei dati personali. "
    "Contatto privacy: {contact}. "
    "Informativa completa: {privacy}."
)
_AI_TEXT_IT = (
    ", e un modello di intelligenza artificiale in esecuzione sul nostro server "
    "ci aiuta a scrivere le nostre email e a leggere le risposte, senza che "
    "alcuna società esterna di intelligenza artificiale riceva tali dati"
)
_KEEP_TEXT_IT = (
    "; li cancelliamo {days} giorni dopo la nostra ultima email, se non risponde"
)
# The word to reply with is one modules/reply_intent reads as an opt-out.
_OBJECTION_TEXT_IT = (
    'Può opporsi a queste email in qualsiasi momento: <a href="{unsub}" '
    'style="{link}">si cancelli qui</a>, oppure risponda "cancellami".'
)
NOTICE_VERSIONS_IT = {
    "legitimate_interest": "notice-li-2-it",
    "consent": "notice-consent-2-it",
    "existing_customer": "notice-customer-2-it",
}


def _notice_parts(language: str) -> dict:
    """The words of the first layer in one language: English unless Italian
    is asked for."""
    if language == "it":
        return {
            "notice": _NOTICE_TEXT_IT,
            "basis": _BASIS_SENTENCE_IT,
            "ai": _AI_TEXT_IT,
            "keep": _KEEP_TEXT_IT,
            "objection": _OBJECTION_TEXT_IT,
            "sources": _MANUAL_SOURCE_IT,
            "versions": NOTICE_VERSIONS_IT,
        }
    return {
        "notice": _NOTICE_TEXT,
        "basis": _BASIS_SENTENCE,
        "ai": _AI_TEXT,
        "keep": _KEEP_TEXT,
        "objection": _OBJECTION_TEXT,
        "sources": _MANUAL_SOURCE,
        "versions": NOTICE_VERSIONS,
    }


def notice_version(basis: str | None, language: str = "en") -> str:
    """The version of the footer a send on this basis, in this language,
    carries."""
    versions = _notice_parts(language)["versions"]
    return versions.get(basis, versions["legitimate_interest"])


def notice_fingerprint(basis: str, language: str = "en") -> str:
    """A digest of every word the footer for a basis can carry."""
    parts = _notice_parts(language)
    words = "\n".join(
        (
            parts["notice"],
            parts["basis"][basis],
            parts["ai"],
            parts["keep"],
            parts["objection"],
            *sorted(parts["sources"].values()),
        )
    )
    return hashlib.sha256(words.encode("utf-8")).hexdigest()[:16]


def render_legal_footer(
    settings,
    contact: dict,
    unsubscribe_url: str,
    basis: str = "legitimate_interest",
    language: str = "en",
) -> str:
    """The legal footer for one email: identity, the notice, the objection,
    in the recipient's language (Italian for "it", English otherwise)."""
    contact = contact or {}
    parts = _notice_parts(language)
    if basis not in parts["basis"]:
        basis = "legitimate_interest"

    def field(name: str) -> str:
        value = (getattr(settings, name, "") or "").strip()
        # Visible in a dry-run preview; a live send is refused before this
        # could ever reach a recipient.
        return html.escape(value) if value else f"[set {name.upper()}]"

    identity = " &middot; ".join(
        field(n)
        for n in (
            "legal_entity_name",
            "legal_registered_office",
            "legal_company_registration",
        )
    )
    capital = (getattr(settings, "legal_share_capital", "") or "").strip()
    if capital:
        identity += f" &middot; Cap. soc. {html.escape(capital)}"
    if (getattr(settings, "legal_single_member", "") or "") == "yes":
        identity += " &middot; Socio unico"

    privacy_url = (getattr(settings, "privacy_notice_url", "") or "").strip()
    privacy = (
        f'<a href="{html.escape(privacy_url)}" style="{_LINK_STYLE}">'
        f"{html.escape(privacy_url)}</a>"
        if privacy_url
        else "[set PRIVACY_NOTICE_URL]"
    )
    # GDPR Art. 14(2)(a): say how long. Only when the retention job actually
    # enforces it - an unenforced period would be a false statement.
    keep = (
        parts["keep"].format(days=int(settings.retention_days_contacted))
        if getattr(settings, "retention_enabled", False)
        else ""
    )
    # The notice ends its first sentence after the name; a name with a full
    # stop of its own ("Suppleco S.r.l.") lends it to the sentence, so the
    # footer reads "S.r.l." and not "S.r.l..". The notice words stay as
    # versioned.
    company = field("legal_entity_name")
    if company.endswith(".") and "{company}." in parts["notice"]:
        company = company[:-1]
    notice = parts["notice"].format(
        company=company,
        source=html.escape(_source_label(contact, language)),
        basis_sentence=parts["basis"][basis],
        ai=parts["ai"] if ai_in_use(settings) else "",
        keep=keep,
        contact=field("privacy_contact_email"),
        privacy=privacy,
    )
    objection = parts["objection"].format(
        unsub=html.escape(unsubscribe_url or ""), link=_LINK_STYLE
    )
    return "".join(
        f'<p style="{_TEXT_STYLE}">{part}</p>' for part in (identity, notice, objection)
    )


# What an erasure does with each column of the schema (step G18, decision
# L5.2), as modules.export.gdpr_erase and anonymise_contact_rows do it. A
# column added without an entry fails a test, so nothing personal can be
# stored where an erasure does not reach.
ERASURE_TREATMENTS = {
    "token": "the address replaced by its keyed token",
    "cleared": "set to NULL",
    "redacted": "free text: the address becomes the token, the name and "
    "company '[erased]'",
    "marked": "replaced by '[erased]' (the column may not be NULL)",
    "deleted": "the row is deleted",
    "pseudonymous": "kept, attached only to the token",
    "none": "holds nothing about a person",
}
_NONE, _PSEUDO = "none", "pseudonymous"
PERSONAL_COLUMNS = {
    "contacts": {
        "id": _NONE,
        "timestamp": _PSEUDO,
        "search_id": _NONE,
        "name": "cleared",
        "role": "cleared",
        "organization": "cleared",
        "email": "token",
        "source_url": "cleared",
        "qualification_rationale": "cleared",
        "status": _PSEUDO,
        "discard_reason": _PSEUDO,
        "lead_score": _PSEUDO,
        "lead_tier": _PSEUDO,
        "is_verified": _PSEUDO,
        "verification_details": "cleared",
        "last_reply_at": _PSEUDO,
        "source_channel": _PSEUDO,
        "basis": _PSEUDO,
        "rule_version": _NONE,
        "qualified_at": _PSEUDO,
        # Not about a person, but nothing emails an erased contact again, so
        # nothing needs them.
        "company_country": "cleared",
        "company_country_source": "cleared",
        "language_choice": "cleared",
    },
    "email_log": {
        "id": _NONE,
        "timestamp": _PSEUDO,
        "contact_id": _PSEUDO,
        "recipient_email": "token",
        "subject": "marked",
        "body_preview": "marked",
        "has_attachment": _NONE,
        "attachment_filename": _NONE,
        "delivery_status": _PSEUDO,
        "smtp_response": "redacted",
        "error_message": "redacted",
        "tracking_id": _PSEUDO,
        "opened_at": _PSEUDO,
        "retry_count": _NONE,
        "sequence_step_id": _NONE,
        "notice_version": _NONE,
        "ab_variant": _NONE,
        "sender_account": _NONE,
        "message_id": _PSEUDO,
    },
    "open_tracking": {
        "id": _NONE,
        "tracking_id": _PSEUDO,
        "email_log_id": _PSEUDO,
        "opened_at": _PSEUDO,
        "ip_address": "cleared",
        "user_agent": "cleared",
    },
    # A proposal's text names the person; the decision about it is the
    # record of human oversight, so the row stays, attached to the token.
    "review_queue": {
        "id": _NONE,
        "created_at": _PSEUDO,
        "stage": _NONE,
        "contact_id": _PSEUDO,
        "sequence_step_id": _NONE,
        "status": _PSEUDO,
        "language": "cleared",
        "proposed_subject": "marked",
        "proposed_body": "marked",
        "evidence": "cleared",
        "ai_written": _NONE,
        "final_subject": "cleared",
        "final_body": "cleared",
        "approved_hash": "cleared",
        "edited": _NONE,
        "opened_at": _PSEUDO,
        "opened_by": _NONE,
        "decided_at": _PSEUDO,
        "reviewer": _NONE,
        "reason": "cleared",
        "sent_at": _PSEUDO,
    },
    "retry_queue": {
        "id": _NONE,
        "email_log_id": _PSEUDO,
        "contact_id": _PSEUDO,
        "attempt": _NONE,
        "max_attempts": _NONE,
        "last_error": "redacted",
        "next_retry_at": _NONE,
        "status": _NONE,
        "created_at": _NONE,
    },
    "lead_alerts": {
        c: "deleted"
        for c in ("id", "email", "notified_to", "reply_excerpt", "timestamp")
    },
    "consent_log": {
        c: "deleted"
        for c in (
            "id",
            "email",
            "legal_basis",
            "details",
            "contact_id",
            "channel",
            "rule_version",
            "timestamp",
        )
    },
    "suppression_list": {
        "id": _NONE,
        "email": "token",
        "reason": _PSEUDO,
        "timestamp": _PSEUDO,
    },
    "suppressed_domains": {
        "id": _NONE,
        "domain": "token",
        "reason": _PSEUDO,
        "timestamp": _PSEUDO,
    },
    "dsr_requests": {
        "id": _NONE,
        "received_at": _PSEUDO,
        "due_at": _PSEUDO,
        "email": "token",
        "kind": _PSEUDO,
        "channel": _PSEUDO,
        "status": _PSEUDO,
        "closed_at": _PSEUDO,
        "outcome": "redacted",
        # The keyed token of the message it came in, never its Message-ID.
        "source_ref": _PSEUDO,
    },
    "activity_log": {
        "id": _NONE,
        "timestamp": _NONE,
        "action": _NONE,
        "details": "redacted",
        "status": _NONE,
    },
    # The company's own rows are deleted; other companies' are not about her.
    "domain_send_counts": {
        "id": _NONE,
        "date": _NONE,
        "domain": "deleted",
        "count": _NONE,
    },
    "daily_send_counts": {c: _NONE for c in ("id", "date", "count", "allowance")},
    "search_log": {
        c: _NONE
        for c in (
            "id",
            "timestamp",
            "search_query",
            "source",
            "results_count",
            "urls_scraped",
            "status",
            "notes",
        )
    },
    "sequence_enrollments": {
        "id": _NONE,
        "contact_id": _PSEUDO,
        "sequence_id": _NONE,
        "current_step": _PSEUDO,
        "status": _PSEUDO,
        "enrolled_at": _PSEUDO,
        "last_sent_at": _PSEUDO,
        "completed_at": _PSEUDO,
        "cancel_reason": _NONE,
    },
    "sequence_steps": {
        c: _NONE
        for c in (
            "id",
            "sequence_id",
            "step_number",
            "delay_days",
            "subject",
            "template_name",
        )
    },
    "sequences": {c: _NONE for c in ("id", "name", "created_at")},
    "agent_state": {c: _NONE for c in ("key", "value", "updated_at")},
    "unsubscribe_tokens": {c: "deleted" for c in ("token", "email", "created_at")},
    # A send restored from the journal for an address the database does not
    # hold: never the address, only its keyed token.
    "prior_sends": {
        "id": _NONE,
        "token": _PSEUDO,
        "message_id": _PSEUDO,
        "sent_at": _PSEUDO,
        "delivery_status": _PSEUDO,
        "sequence_step_id": _NONE,
        "sender_account": _NONE,
        "notice_version": _NONE,
    },
}
