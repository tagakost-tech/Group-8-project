"""How well a company fits Omega, in four parts a person can check.

From the team's lead-finder design: four weighted factors, each scored 0-100
with a reason, and a total. It is shown to the reviewer next to the evidence;
it never decides on its own whether a company is emailed (the lead score and
the screens do that, and a person approves).

    market       30%  is the sector growing, or one Omega already sells into
    need         30%  does the company build things that need parts like Omega's
    fit          25%  can Omega supply it today (certifications it holds or lacks)
    distance     15%  how close the company is to Italy

The sector comes from the client profile (client/<client>.json): the sector
whose buyer phrases the company's own text matches best.
"""

import re

from modules.client_profile import PROFILE

WEIGHTS = {"market": 0.30, "need": 0.30, "fit": 0.25, "distance": 0.15}

# Closeness to Italy, 0-100: delivery time, freight and how often a buyer
# meets Italian suppliers. The team's table, extended to the countries the
# agent searches.
PROXIMITY = {
    "IT": 100,
    "CH": 90,
    "AT": 88,
    "SI": 85,
    "DE": 85,
    "FR": 85,
    "ES": 80,
    "CZ": 78,
    "BE": 75,
    "SK": 72,
    "NL": 72,
    "HU": 70,
    "PL": 68,
    "HR": 70,
    "PT": 65,
    "RO": 60,
    "GB": 60,
    "DK": 58,
    "SE": 55,
    "IE": 50,
    "NO": 45,
    "FI": 45,
    "GR": 55,
    "US": 20,
    "MX": 30,
}
FAR_FROM_ITALY = 40

# What Omega holds (IATF 16949, ISO 9001, ISO 14001) and what some sectors
# ask for on top. A certification Omega lacks is information for the
# reviewer, never a reason to drop the company.
HELD = ("IATF 16949", "ISO 9001", "ISO 14001")
NOT_HELD = {
    "EN 9100": re.compile(r"\b(?:EN|AS)\s?9100\b", re.IGNORECASE),
    "IRIS / ISO 22163": re.compile(r"\bIRIS\b|22163", re.IGNORECASE),
    "ISO 13485": re.compile(r"13485", re.IGNORECASE),
    "AQAP 2110": re.compile(r"AQAP", re.IGNORECASE),
}

_TYPES = (
    (
        "distributor",
        re.compile(
            r"\b(?:distribut|wholesal|grossist|großhandel|grosshandel|händler)",
            re.IGNORECASE,
        ),
    ),
    (
        "Tier-1",
        re.compile(
            r"\btier[\s-]?1\b|\bsystem supplier\b|\bsystemlieferant", re.IGNORECASE
        ),
    ),
    (
        "Tier-2",
        re.compile(r"\btier[\s-]?2\b|\bsub-?supplier\b|\bsubfornitor", re.IGNORECASE),
    ),
    (
        "OEM",
        re.compile(
            r"\boem\b|\bhersteller\b|\bmanufactur|\bproduttore\b|\bproduces\b|\bmakes\b",
            re.IGNORECASE,
        ),
    ),
)


def company_type(text: str) -> str:
    """OEM, Tier-1, Tier-2 or distributor, read from how the company
    describes itself; "unknown" when it does not say."""
    for name, rx in _TYPES:
        if rx.search(text or ""):
            return name
    return "unknown"


# Words every sector's phrases share, which say nothing about the sector.
_GENERIC = {
    "manufacturer",
    "manufacturers",
    "manufacturing",
    "hersteller",
    "herstellung",
    "produttore",
    "produttori",
    "produzione",
    "maker",
    "makers",
    "supplier",
    "suppliers",
    "zulieferer",
    "fornitore",
    "fornitori",
    "company",
    "companies",
    "azienda",
    "aziende",
    "unternehmen",
    "systems",
    "system",
    "components",
    "componenti",
    "komponenten",
    "europe",
    "europa",
    "italy",
    "italia",
    "germany",
    "deutschland",
    "industrial",
    "industriale",
}
_WORD = re.compile(r"[a-zà-ÿß]+")


def _stems(text: str) -> set[str]:
    """Seven-letter stems of the meaningful words: "Wärmepumpen" and
    "Wärmepumpe" meet at "wärmepu"; "instructions" and "instrument" do not."""
    return {
        w[:7]
        for w in _WORD.findall((text or "").lower())
        if len(w) >= 5 and w not in _GENERIC
    }


def _sector_for(text: str) -> dict | None:
    """The profile sector sharing the most meaningful words with the
    company's own text: its name and buyer phrases in every language."""
    found = _stems(text)
    best, best_hits = None, 0
    for sector in PROFILE.sectors:
        keywords = sector.get("search_keywords") or {}
        words = (
            keywords
            if isinstance(keywords, list)
            else [k for lang in keywords.values() for k in lang]
        )
        vocabulary = _stems(" ".join(map(str, [sector.get("name", ""), *words])))
        hits = len(found & vocabulary)
        if hits > best_hits:
            best, best_hits = sector, hits
    return best


def certifications_needed(sector: dict | None, text: str) -> list[str]:
    barriers = sector.get("barriers", "") if sector else ""
    if isinstance(barriers, list):
        barriers = " ".join(map(str, barriers))
    haystack = f"{barriers} {text or ''}"
    return [name for name, rx in NOT_HELD.items() if rx.search(haystack)]


def assess(contact: dict) -> dict:
    """The fit score of one contact, with every part explained."""
    text = " ".join(
        str(contact.get(k) or "") for k in ("organization", "qualification_rationale")
    )
    sector = _sector_for(text)
    kind = company_type(text)
    certs = certifications_needed(sector, text)
    country = (contact.get("company_country") or "").upper()

    if sector is None:
        market = (40, "no sector in Omega's profile matches what the company says")
    elif sector.get("type") == "blue_ocean":
        market = (80, f"{sector['name']}: a new sector with growing demand")
    else:
        market = (65, f"{sector['name']}: a sector Omega already supplies")

    need = {
        "OEM": (85, "builds its own products, so it buys parts"),
        "Tier-1": (90, "a Tier-1 supplier: assemblies that need formed parts"),
        "Tier-2": (80, "a Tier-2 supplier: buys parts for sub-assemblies"),
        "distributor": (35, "a distributor: resells, rarely buys to drawing"),
    }.get(kind, (55, "its type is not stated on its page"))

    if certs:
        fit = (
            40,
            "may require " + ", ".join(certs) + ", which Omega does not hold yet",
        )
    else:
        fit = (90, "Omega's certifications (" + ", ".join(HELD) + ") cover it")

    prox = PROXIMITY.get(country)
    if prox is None:
        distance = (50, "country not known")
    elif prox < FAR_FROM_ITALY:
        distance = (prox, f"{country}: far from Italy")
    else:
        distance = (prox, f"{country}: {prox}/100 close to Italy")

    parts = {"market": market, "need": need, "fit": fit, "distance": distance}
    total = sum(WEIGHTS[k] * v[0] for k, v in parts.items())
    return {
        "total": max(0, min(100, round(total))),
        "parts": [
            {"name": k, "weight": round(WEIGHTS[k] * 100), "score": v[0], "why": v[1]}
            for k, v in parts.items()
        ],
        "sector_id": sector.get("id") if sector else None,
        "sector": sector.get("name") if sector else None,
        "company_type": kind,
        "certifications_needed": certs,
        "carbon_pressure": carbon_pressure(text, kind, sector, country),
    }


EU = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split()  # noqa: SIM905
)
_CLIMATE_WORDS = (
    "csrd",
    "scope 3",
    "net zero",
    "net-zero",
    "sbti",
    "science based",
    "carbon footprint",
    "carbon neutral",
    "decarbon",
    "low-carbon",
    "co2",
    "climate",
    "sustainab",
    "esg",
)
_SOURCING_WORDS = (
    "import",
    "asia",
    "china",
    "chinese",
    "global sourcing",
    "low-cost countr",
)


def carbon_pressure(text: str, kind: str, sector: dict | None, country: str) -> dict:
    """Why a buyer may care about a low-carbon supplier: not scored, shown.

    CBAM charges EU importers of fasteners; CSRD makes large EU companies
    report their suppliers' emissions; carmakers pass that down to Tier-1s."""
    low = text.lower()
    reasons = []
    if country in EU:
        reasons.append("in the EU: CBAM applies to its fastener imports")
    if kind in ("OEM", "Tier-1") and country in EU:
        reasons.append(
            "a large EU buyer: likely to report suppliers' emissions (CSRD, Scope 3)"
        )
    if sector and "automot" in (sector.get("id", "") + sector.get("name", "")).lower():
        reasons.append(
            "automotive: carmakers ask suppliers for a carbon footprint per part"
        )
    if any(w in low for w in _CLIMATE_WORDS):
        reasons.append("its own page talks about climate targets")
    if any(w in low for w in _SOURCING_WORDS):
        reasons.append("mentions sourcing from Asia")
    level = "high" if len(reasons) >= 3 else "some" if reasons else "none"
    return {"level": level, "reasons": reasons}


def band(total: int) -> str:
    """good (70+), mid (45-69) or low (under 45)."""
    return "good" if total >= 70 else "mid" if total >= 45 else "low"
