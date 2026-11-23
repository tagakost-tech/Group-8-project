"""From a drawing to a quote a person approves.

A buyer sends a drawing; the estimator needs to know, fast, whether the part
can be cold-formed, how, at what price, and what it saves against machining.
This module does the arithmetic. It never decides: every figure comes back
with the assumption it rests on, the estimator corrects what the drawing
reader got wrong, and a quote is sent only once a person approves it.

The part is described as Omega's parts are drawn: a head on a shank, with an
optional collar and a rolled thread. Rates and emission factors live in
client/omega_costing.json, marked as estimates until Omega replaces them.
"""

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

COSTING_PATH = Path(__file__).resolve().parent.parent / "client" / "omega_costing.json"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quotes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    contact_id    INTEGER,
    drawing_name  TEXT,
    part          TEXT NOT NULL,
    estimate      TEXT NOT NULL,
    language      TEXT,
    status        TEXT NOT NULL DEFAULT 'draft',
    quote_text    TEXT,
    reviewer      TEXT,
    decided_at    TEXT,
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);
"""


def load_costing(path: Path | None = None) -> dict:
    return json.loads(Path(path or COSTING_PATH).read_text(encoding="utf-8"))


@dataclass
class Part:
    """A headed part, in millimetres. Zero means "not on this part"."""

    length: float = 0.0  # overall length
    shank_d: float = 0.0  # shank diameter
    head_d: float = 0.0
    head_h: float = 0.0
    collar_d: float = 0.0
    collar_h: float = 0.0
    thread: str = ""  # e.g. "M6x1"
    thread_len: float = 0.0
    material: str = "carbon_steel"
    tolerance_mm: float = 0.0  # tightest tolerance on the drawing
    annual_volume: int = 0
    notes: list = field(default_factory=list)  # what the reader could not settle


# ── reading a drawing ────────────────────────────────────────────────────

_NUM = r"(\d+(?:[.,]\d+)?)"


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def read_drawing_text(text: str, costing: dict | None = None) -> Part:
    """Fill a Part from the text layer of a drawing (a CAD PDF keeps its
    dimensions as text). It reads what is labelled; what it cannot tell is
    left at zero and named in notes, for the estimator to fill in."""
    costing = costing or load_costing()
    t = " ".join((text or "").split())
    part = Part()

    diams = [_f(m) for m in re.findall(r"[ØøΦ⌀]\s*" + _NUM, t)]
    thread = re.search(r"\bM\s?(\d+(?:[.,]\d+)?)(?:\s?[x×]\s?(\d+(?:[.,]\d+)?))?", t)
    if thread:
        major = _f(thread.group(1))
        part.thread = f"M{thread.group(1)}" + (
            f"x{thread.group(2)}" if thread.group(2) else ""
        )
        diams.append(major)
    if diams:
        part.head_d = max(diams)
        smaller = sorted({d for d in diams if d < part.head_d})
        part.shank_d = smaller[0] if smaller else part.head_d
        if len(smaller) >= 2:
            part.collar_d = smaller[-1]
    m = re.search(r"\b(?:L|length|lunghezza|Länge)\s*[=:]?\s*" + _NUM, t, re.IGNORECASE)
    if m:
        part.length = _f(m.group(1))
    m = re.search(
        r"\b(?:head height|altezza testa|Kopfhöhe|H|k)\s*[=:]\s*" + _NUM,
        t,
        re.IGNORECASE,
    )
    if m:
        part.head_h = _f(m.group(1))
    m = re.search(
        r"\b(?:thread length|lunghezza filetto|Gewindelänge|b)\s*[=:]\s*" + _NUM,
        t,
        re.IGNORECASE,
    )
    if m:
        part.thread_len = _f(m.group(1))
    m = re.search(r"\b(?:collar|collare|Bund)\b\D{0,12}?" + _NUM + r"\s*mm", t, re.IGNORECASE)
    if m:
        part.collar_h = _f(m.group(1))
    elif part.collar_d:
        part.notes.append("collar height not found on the drawing")
    tols = [_f(x) for x in re.findall(r"±\s*" + _NUM, t)]
    if tols:
        part.tolerance_mm = min(tols)
    m = re.search(
        r"\b(?:annual|yearly|volume|pcs/year|pezzi/anno|Stk/Jahr)\D{0,20}?(\d[\d.,' ]{2,})",
        t,
        re.IGNORECASE,
    )
    if m:
        part.annual_volume = int(re.sub(r"\D", "", m.group(1)) or 0)

    low = t.lower()
    for key, mat in costing["materials"].items():
        if any(alias.lower() in low for alias in mat["aliases"]):
            part.material = key
            break
    else:
        part.notes.append("material not found on the drawing: carbon steel assumed")

    for name, value in (("length", part.length), ("shank diameter", part.shank_d)):
        if not value:
            part.notes.append(f"{name} not found on the drawing")
    if part.head_d and not part.head_h:
        part.head_h = round(part.head_d * 0.6, 1)
        part.notes.append("head height not found: 0.6 x head diameter assumed")
    if part.thread and not part.thread_len:
        part.thread_len = round(part.length * 0.6, 1) if part.length else 0
        part.notes.append("thread length not found: 60% of the length assumed")
    return part


def read_drawing_pdf(data: bytes, costing: dict | None = None) -> Part:
    import fitz  # PyMuPDF

    with fitz.open(stream=data, filetype="pdf") as doc:
        text = " ".join(page.get_text() for page in doc)
    return read_drawing_text(text, costing)


# ── geometry ─────────────────────────────────────────────────────────────


def _cyl(d: float, h: float) -> float:
    return math.pi * (d / 2) ** 2 * h


def volume_mm3(p: Part) -> float:
    head = _cyl(p.head_d, p.head_h) if p.head_d and p.head_h else 0
    collar = _cyl(p.collar_d, p.collar_h) if p.collar_d and p.collar_h else 0
    shank_len = max(0.0, p.length - p.head_h - p.collar_h)
    return head + collar + _cyl(p.shank_d, shank_len)


# ── can it be cold-formed, and how ───────────────────────────────────────


def formability(p: Part, costing: dict) -> dict:
    """The verdict and its reasons, in the terms a header setter uses: the
    wire, how much material the head gathers (upset length over wire
    diameter), and what forming cannot reach."""
    reasons, secondary = [], []
    wire = p.shank_d
    lo, hi = costing["wire_mm"]["min"], costing["wire_mm"]["max"]
    if not wire:
        return {
            "verdict": "unknown",
            "stations": None,
            "wire_mm": None,
            "reasons": ["the shank diameter is needed to judge the part"],
            "secondary": [],
        }
    if not (lo <= wire <= hi):
        return {
            "verdict": "not formable",
            "stations": None,
            "wire_mm": wire,
            "reasons": [f"wire {wire} mm is outside Omega's {lo}-{hi} mm range"],
            "secondary": [],
        }
    if p.material not in costing["materials"]:
        reasons.append("material not in the costing table")

    gathered = sum(
        _cyl(d, h)
        for d, h in ((p.head_d, p.head_h), (p.collar_d, p.collar_h))
        if d > wire and h
    )
    upset_ratio = gathered / _cyl(wire, 1) / wire if gathered else 0
    if upset_ratio <= 2.3:
        stations = 1 if upset_ratio <= 1.2 else 2
    elif upset_ratio <= 4.5:
        stations = 3
    elif upset_ratio <= 6.5:
        stations = 4
    elif upset_ratio <= 9:
        stations = 6
    else:
        return {
            "verdict": "not formable",
            "stations": None,
            "wire_mm": wire,
            "reasons": [
                f"the head gathers {upset_ratio:.1f} wire diameters of material, beyond six stations"
            ],
            "secondary": [],
        }
    reasons.append(
        f"the head gathers {upset_ratio:.1f} wire diameters: {stations} forming station(s)"
    )
    if p.collar_d:
        stations = min(6, stations + 1)
        reasons.append("a collar adds a station")
    if p.head_d and p.head_d / wire > 3.2:
        reasons.append(
            f"head diameter is {p.head_d / wire:.1f}x the wire: possible, check for cracks"
        )
    if p.length and p.length / wire > 12:
        reasons.append(
            f"length is {p.length / wire:.0f}x the wire: long for a header, check the machine"
        )
    if p.thread:
        secondary.append("thread rolling")
    if p.tolerance_mm and p.tolerance_mm < 0.02:
        secondary.append(f"grinding or turning for the {p.tolerance_mm} mm tolerance")
    verdict = (
        "formable"
        if not secondary or secondary == ["thread rolling"]
        else "formable with secondary operations"
    )
    return {
        "verdict": verdict,
        "stations": stations,
        "wire_mm": wire,
        "reasons": reasons,
        "secondary": secondary,
    }


# ── cost and carbon ──────────────────────────────────────────────────────


def estimate(p: Part, costing: dict | None = None) -> dict:
    """Formability, price per 1,000 and the carbon of cold forming against
    machining the same part from bar, with every assumption named."""
    costing = costing or load_costing()
    form = formability(p, costing)
    mat = costing["materials"].get(p.material, costing["materials"]["carbon_steel"])
    vol = volume_mm3(p)
    mass_g = vol / 1000 * mat["density_g_cm3"]
    out = {
        "part": asdict(p),
        "formability": form,
        "material": mat["label"],
        "mass_g": round(mass_g, 2),
        "cost": None,
        "carbon": None,
    }
    if not vol or form["stations"] is None:
        return out

    f = costing["forming"]
    elec = costing["electricity_kgco2_per_kwh"]
    wire_kg_1000 = mass_g / f["yield"]  # g per part = kg per 1,000
    material = wire_kg_1000 * mat["price_eur_kg"]
    rate = f["parts_per_minute_by_stations"][str(form["stations"])]
    machine = 1000 / rate / 60 * f["machine_eur_per_hour"]
    extra = f["sorting_eur_per_1000"] + (
        f["thread_rolling_eur_per_1000"] if p.thread else 0
    )
    tooling = form["stations"] * f["tooling_eur_per_station"] + f["setup_eur"]
    per_1000_tooling = tooling / (p.annual_volume / 1000) if p.annual_volume else None
    base = material + machine + extra + (per_1000_tooling or 0)
    out["cost"] = {
        "per_1000_eur": round(base * (1 + f["margin"]), 2),
        "breakdown": {
            "material": round(material, 2),
            "forming": round(machine, 2),
            "rolling and sorting": round(extra, 2),
            "tooling spread over a year": round(per_1000_tooling, 2)
            if per_1000_tooling
            else None,
            "margin": f["margin"],
        },
        "tooling_once_eur": round(tooling),
        "parts_per_minute": rate,
        "assumptions": "indicative rates in client/omega_costing.json, to be replaced by Omega's",
    }

    # machining the same part from bar of its largest diameter
    m = costing["machining"]
    bar_d = max(p.head_d, p.collar_d, p.shank_d)
    bar_mass_g = (
        _cyl(bar_d, p.length + m["parting_allowance_mm"]) / 1000 * mat["density_g_cm3"]
    )
    removed_kg = max(0.0, bar_mass_g - mass_g) / 1000
    formed_kg = mass_g / 1000 / f["yield"]
    formed_co2 = formed_kg * mat["kgco2_per_kg"] + formed_kg * f["kwh_per_kg"] * elec
    machined_co2 = (
        bar_mass_g / 1000 * mat["kgco2_per_kg"]
        + removed_kg * m["kwh_per_kg_removed"] * elec
    )
    out["carbon"] = {
        "formed_g_co2_per_part": round(formed_co2 * 1000, 2),
        "machined_g_co2_per_part": round(machined_co2 * 1000, 2),
        "saving_pct": round((1 - formed_co2 / machined_co2) * 100)
        if machined_co2
        else None,
        "material_used_formed_g": round(formed_kg * 1000, 2),
        "material_used_machined_g": round(bar_mass_g, 2),
        "per_year_kg_co2_saved": (
            round((machined_co2 - formed_co2) * p.annual_volume)
            if p.annual_volume
            else None
        ),
        "assumptions": "steel and electricity factors in client/omega_costing.json (estimates)",
    }
    return out


def quote_text(est: dict, language: str = "en") -> str:
    """The paragraph that goes into an answer, once a person approves it."""
    p, c, k, f = est["part"], est["cost"], est["carbon"], est["formability"]
    if not c:
        return ""
    if language == "it":
        return (
            f"Per il particolare indicato ({est['material']}, {p['length']} mm, filo {f['wire_mm']} mm) "
            f"la nostra stima è di {c['per_1000_eur']:.2f} EUR ogni 1.000 pezzi, con un'attrezzatura "
            f"una tantum di circa {c['tooling_once_eur']} EUR. Stampato a freddo usa {k['material_used_formed_g']} g "
            f"di materiale per pezzo invece di {k['material_used_machined_g']} g da barra: circa il "
            f"{k['saving_pct']}% di CO2 in meno. Prezzo da confermare con il disegno definitivo."
        )
    return (
        f"For the part you sent ({est['material']}, {p['length']} mm, wire {f['wire_mm']} mm) our estimate "
        f"is EUR {c['per_1000_eur']:.2f} per 1,000 parts, with one-off tooling of about EUR "
        f"{c['tooling_once_eur']}. Cold-formed, it uses {k['material_used_formed_g']} g of material per part "
        f"instead of {k['material_used_machined_g']} g from bar: about {k['saving_pct']}% less CO2. "
        f"Price to be confirmed on the final drawing."
    )
