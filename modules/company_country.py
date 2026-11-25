"""Which country a contact's company is in, from what was collected about it.

the client writes to Italian companies in Italian, and an Italian company does
not always write from a .it address: a .com, or a gmail address, is common.
So the company's country is read from the company itself, deterministic
evidence first: the country its own website's domain shows, then what the
page the address was found on says about who published it.

Only the company's own pages count. On a directory, an exhibitor list or a
trade-press article the domain and the footer are the publisher's: a German
firm listed on europages.it would read as Italian.

Kept apart from compliance.py, which holds legal rules; these are scraping
heuristics. It imports neither the database nor the search, so both can
import it.
"""

import re

from modules.compliance import (
    MANUAL_CHANNELS,
    country_refusal,
    italian_countries,
    recipient_country,
    website_country,
)

# The channels whose source_url is the company's own site (search.scrape_site).
OWN_CHANNELS = ("own_website", "contact_page")

# An Italian VAT number: the label, then exactly eleven digits. Italian law
# (DPR 633/72 art. 35) has a company print it on its website, so it is the
# strongest sign there is. The fiscal code often shares the label
# ("P.IVA/C.F."). A twelfth digit makes it some other number.
_VAT = re.compile(
    r"\b(?:P\.?\s?IVA|Partita\s+IVA)\b[\s:.n°/-]*(?:C\.?\s?F\.?[\s:./-]*)?"
    r"(?:IT\s?)?(\d{11})(?!\d)",
    re.IGNORECASE,
)
# Legal forms only Italy uses: S.p.A. with its dots (a wellness "Spa", "SPA"
# or "spa" never matches) and S.r.l.s. Undotted "SpA" is also Chile's
# sociedad por acciones, so it is only a weak sign below. Not the start of a
# dotted acronym: "S.P.A.R.K. Ltd" is no S.p.A.
_STRONG_FORM = re.compile(
    r"(?<![\w.])(?:S\.\s?p\.\s?A\.?|S\.?r\.?l\.?s\.?)(?!\.?\w)", re.IGNORECASE
)
# Legal forms Italy shares: S.R.L. and S.N.C. are also Romanian, S.A.S. and
# SAS also French. Each is only one weak sign.
_WEAK_FORM = re.compile(
    r"(?<![\w.])(?:(?i:S\.?r\.?l|S\.?n\.?c|S\.?a\.?s)\.?|SpA)(?![\w.])"
)
# Legal forms of other countries. A company whose own name carries one is
# not Italian, whatever its page says: an Italian web agency's credit in its
# footer ("Website: Pixel S.r.l. - P.IVA ...") is the agency's.
_FOREIGN_FORM = re.compile(
    r"(?<![\w.])(?:(?i:GmbH|Ltd|Limited|PLC|LLC|Inc|Corp|SARL)|AG|KG|B\.V|N\.V"
    r"|S\.L|Oy|ApS|A/S|Sp\.\s?z\s?o\.o|s\.r\.o|Kft)\.?(?![\w.])"
)
# An Italian phone number: +39 or 0039, then a landline (0...) or mobile (3...).
_IT_PHONE = re.compile(r"(?<![\w+])(?:\+|00)39[\s./()-]*[03]\d")


def page_country(
    text: str | None, html_lang: str | None = None, organization: str | None = None
) -> str | None:
    """The country a company's own page shows: "IT" when it is Italian, else
    None.

    One strong sign is enough: an Italian VAT number, S.p.A. or S.r.l.s.
    Otherwise two different weak signs are needed: an Italian legal form other
    countries share, a +39 phone number, a page in Italian. One weak sign
    alone is not enough: an Italian email to someone who cannot read it is
    the worse mistake.

    In capitals on a page in another language the form is not counted: a
    spa's "Our S.P.A. (Salus Per Aquam) menu" reads just like it, and an
    Italian company's own name says S.p.A. wherever it is written.

    An organisation named with another country's legal form (GmbH, Ltd...)
    is not Italian: what its page shows of Italy is someone else's."""
    if _FOREIGN_FORM.search(organization or ""):
        return None
    body = text or ""
    lang = (html_lang or "").strip().lower()
    foreign_page = bool(lang) and not lang.startswith("it")
    names = f"{organization or ''} {body}"
    if (
        _VAT.search(body)
        or _STRONG_FORM.search(organization or "")
        or any(
            not (foreign_page and m.group().isupper())
            for m in _STRONG_FORM.finditer(body)
        )
    ):
        return "IT"
    weak = {
        "form": bool(_WEAK_FORM.search(names)),
        "phone": bool(_IT_PHONE.search(body)),
        "lang": lang.startswith("it"),
    }
    return "IT" if sum(weak.values()) >= 2 else None


def collected_country(
    source_url: str | None, source_channel: str | None, page: str | None
) -> tuple[str | None, str | None]:
    """The company's country and where it was read, from the page an address
    was collected on: its website's domain first, then the page's own
    evidence (page_country). (None, None) when neither shows one, or when
    the page is not the company's own."""
    if source_channel not in OWN_CHANNELS:
        return None, None
    country = website_country(source_url)
    if country:
        return country, "website"
    if page:
        return page, "page"
    return None, None


def stored_evidence(contact: dict) -> tuple[str | None, str | None]:
    """collected_country for a contact already stored. Only its organisation
    and the page description it was qualified on are kept, and not the
    page's language, so only a strong sign or a phone number with a shared
    legal form can show it is Italian.

    A contact the operator entered (a Direct Send) has no page: the
    organisation they typed is the company's own name, so its legal form
    counts on its own."""
    if contact.get("source_channel") in MANUAL_CHANNELS:
        if page_country(None, None, contact.get("organization")):
            return "IT", "page"
        return None, None
    page = page_country(
        contact.get("qualification_rationale"), None, contact.get("organization")
    )
    return collected_country(
        contact.get("source_url"), contact.get("source_channel"), page
    )


# An answer is one code in capitals, and nothing else: "Italy", "I think IT"
# or "IT (Italy)" are a model not doing as asked, and a guess read out of
# them is no evidence.
_ANSWER = re.compile(r"[A-Z]{2}")
_EU_CODES = {"UK": "GB", "EL": "GR"}


def parse_country_answer(text: str | None) -> str:
    """The country code the AI answered, or "" when it named none.

    UK and EL are the codes the European Union itself writes for the United
    Kingdom and Greece, so a model may answer with them; read as GB and GR,
    the ISO codes every other rule compares against."""
    answer = (text or "").strip()
    if not _ANSWER.fullmatch(answer) or answer == "XX":
        return ""
    return _EU_CODES.get(answer, answer)


def settle_company_country(
    settings, db, contact: dict, ai_gen=None, write: bool = True
) -> tuple[str | None, str | None]:
    """Work out and record a stored contact's company country, once.

    What was stored about it comes first (stored_evidence). The AI is asked
    only when that shows nothing, COMPANY_COUNTRY_AI is on, a configured
    client is given, its address does not already make it Italian and the
    operator has not chosen its language; and only when write is True, so a
    dry run costs nothing. An answer naming no country is recorded too, so
    the contact is not paid for again; a failed call records nothing and
    never raises. Returns the (country, source) recorded or, with write
    False, found; (None, None) for nothing. The contact dict is updated, so
    a send in the same pass reads it."""
    if contact.get("company_country_source"):
        return None, None
    country, source = stored_evidence(contact)
    if source is None and write and _may_ask_ai(settings, contact, ai_gen):
        answer = ai_gen.classify_company_country(contact)
        if answer is None:
            return None, None
        country, source = answer or None, "ai"
    if source is None:
        return None, None
    if write:
        if not db.set_company_country(contact["id"], country, source):
            return None, None
        contact["company_country"] = country
        contact["company_country_source"] = source
    return country, source


def _may_ask_ai(settings, contact: dict, ai_gen) -> bool:
    """Whether the AI may be asked: a client, the setting, an answer that
    could change the contact's language, and an address the country lock
    lets a live email reach. A contact the lock refuses is never written
    to, so its company is not sent to the provider for nothing."""
    italian = italian_countries(settings)
    email = contact.get("email") or ""
    return (
        ai_gen is not None
        and getattr(ai_gen, "is_configured", False)
        and settings.company_country_ai
        and bool(italian)
        and recipient_country(email) not in italian
        and not contact.get("language_choice")
        and country_refusal(settings, email) is None
    )
