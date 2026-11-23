"""A carbon passport for each lot Omega ships.

A Tier-1 reporting its suppliers' emissions (Scope 3) needs the footprint of
the parts it buys, ideally from primary data. Omega has that data already:
the steel certificate names the mill, its route and its CO2 per tonne; the
presses count the good parts; the meters give the energy. This module turns
them into a product carbon footprint per part, in the main fields of the
Catena-X PCF rulebook, set against the EU default for the same part from
China. A person approves each passport before it leaves.
"""

import json

from modules import clock
from modules.cbam import _china_default, load_carbon

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS carbon_lots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_code         TEXT NOT NULL UNIQUE,
    created_at       TEXT NOT NULL,
    part_name        TEXT NOT NULL,
    customer         TEXT,
    part_mass_g      REAL NOT NULL,
    steel_mill       TEXT,
    steel_route      TEXT NOT NULL,
    mill_t_co2_per_t REAL,
    wire_kg          REAL NOT NULL,
    good_parts       INTEGER NOT NULL,
    energy_kwh       REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft',
    reviewer         TEXT,
    decided_at       TEXT
);
"""


def footprint(lot: dict, carbon: dict | None = None) -> dict:
    """The lot's footprint per part, its parts, and the comparison."""
    c = carbon or load_carbon()
    route = c["steel_routes"].get(lot["steel_route"]) or c["steel_routes"]["eaf_scrap"]
    from_certificate = lot.get("mill_t_co2_per_t") is not None
    steel_factor = lot["mill_t_co2_per_t"] if from_certificate else route["t_co2_per_t"]
    good = max(1, int(lot["good_parts"]))
    steel_kg = float(lot["wire_kg"]) * steel_factor  # t CO2/t == kg CO2/kg
    energy_kg = float(lot["energy_kwh"]) * c["electricity_kgco2_per_kwh"]
    total_kg = steel_kg + energy_kg
    per_part_g = total_kg / good * 1000
    good_mass_kg = good * float(lot["part_mass_g"]) / 1000
    yield_ = good_mass_kg / float(lot["wire_kg"]) if lot["wire_kg"] else None
    year = int(str(lot.get("created_at") or clock.db_now())[:4])
    china_g = float(lot["part_mass_g"]) * _china_default(c, year)
    return {
        "per_part_g_co2": round(per_part_g, 2),
        "lot_kg_co2": round(total_kg, 1),
        "steel_share": round(steel_kg / total_kg, 3) if total_kg else None,
        "yield": round(yield_, 3) if yield_ else None,
        "china_default_g_co2": round(china_g, 2),
        "times_lower": round(china_g / per_part_g, 1) if per_part_g else None,
        "steel_factor": steel_factor,
        "steel_factor_from": "the mill's certificate"
        if from_certificate
        else f"{route['label']} average",
        "primary_data_share": 1.0 if from_certificate else 0.6,
    }


def pcf_record(lot: dict, fp: dict, carbon: dict | None = None) -> dict:
    """The main fields of a Catena-X product carbon footprint, per piece."""
    c = carbon or load_carbon()
    return {
        "specVersion": "Catena-X PCF Rulebook (main fields; prototype)",
        "productId": lot["lot_code"],
        "productName": lot["part_name"],
        "companyName": "Omega IFS",
        "declaredUnit": "piece",
        "unitaryProductAmount": 1,
        "pcfExcludingBiogenic": round(fp["per_part_g_co2"] / 1000, 6),
        "pcfUnit": "kg CO2e per piece",
        "fossilGhgEmissions": round(fp["per_part_g_co2"] / 1000, 6),
        "primaryDataShare": fp["primary_data_share"],
        "geographyCountry": "IT",
        "referencePeriodStart": str(lot.get("created_at") or "")[:10],
        "crossSectoralStandardsUsed": ["GHG Protocol Product Standard", "ISO 14067"],
        "boundaryProcessesDescription": (
            f"Steel ({fp['steel_factor']} t CO2/t, from {fp['steel_factor_from']}) and the "
            f"electricity to form the lot ({c['electricity_kgco2_per_kwh']} kg CO2/kWh). "
            "Transport, heat treatment and coating not included."
        ),
        "comment": "Prototype figure for a course project; not a verified declaration.",
    }


def approve(db, lot_id: int, reviewer: str) -> dict:
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise ValueError("a passport needs a named reviewer")
    with db._transaction() as conn:
        n = conn.execute(
            """UPDATE carbon_lots SET status = 'approved', reviewer = ?, decided_at = ?
               WHERE id = ? AND status = 'draft'""",
            (reviewer, clock.db_now(), lot_id),
        ).rowcount
    if not n:
        raise ValueError("this passport is not a draft waiting for approval")
    db.log_activity(
        "PASSPORT_APPROVED",
        f"carbon passport {lot_id} approved by {reviewer}",
        status="success",
    )
    return get(db, lot_id)


def get(db, lot_id: int) -> dict | None:
    row = db.conn.execute(
        "SELECT * FROM carbon_lots WHERE id = ?", (lot_id,)
    ).fetchone()
    if row is None:
        return None
    lot = dict(row)
    lot["footprint"] = footprint(lot)
    lot["pcf"] = pcf_record(lot, lot["footprint"])
    return lot


def listing(db) -> list[dict]:
    rows = db.conn.execute(
        "SELECT * FROM carbon_lots ORDER BY created_at DESC, id DESC"
    ).fetchall()
    out = []
    for r in rows:
        lot = dict(r)
        lot["footprint"] = footprint(lot)
        out.append(lot)
    return out


def add(db, **lot) -> int:
    with db._transaction() as conn:
        cur = conn.execute(
            """INSERT INTO carbon_lots (lot_code, created_at, part_name, customer, part_mass_g,
                   steel_mill, steel_route, mill_t_co2_per_t, wire_kg, good_parts, energy_kwh)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lot["lot_code"],
                lot.get("created_at") or clock.db_now(),
                lot["part_name"],
                lot.get("customer"),
                lot["part_mass_g"],
                lot.get("steel_mill"),
                lot.get("steel_route", "eaf_scrap"),
                lot.get("mill_t_co2_per_t"),
                lot["wire_kg"],
                lot["good_parts"],
                lot["energy_kwh"],
            ),
        )
        return cur.lastrowid


def passport_pdf(lot: dict) -> bytes:
    """One page a buyer can file: the figure, how it was made, the comparison."""
    import fitz  # PyMuPDF

    fp, pcf = lot["footprint"], lot["pcf"]
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    ink, soft = (0.08, 0.1, 0.12), (0.42, 0.45, 0.5)
    y = 72

    def line(text, size=10, color=ink, gap=16):
        nonlocal y
        page.insert_text((56, y), text, fontsize=size, color=color)
        y += gap

    line("Omega IFS - product carbon passport", 18, gap=30)
    line(f"Lot {lot['lot_code']}   {lot['part_name']}", 12, gap=22)
    line(
        f"Customer: {lot.get('customer') or '-'}   Approved by: {lot.get('reviewer') or 'not yet approved'}",
        9,
        soft,
        28,
    )
    line(f"{fp['per_part_g_co2']} g CO2e per part", 26, gap=34)
    line(
        f"{fp['times_lower']}x lower than the EU default for the same part made in China "
        f"({fp['china_default_g_co2']} g)",
        10,
        gap=26,
    )
    for k, v in (
        (
            "Steel",
            f"{lot.get('steel_mill') or '-'}, {fp['steel_factor']} t CO2/t ({fp['steel_factor_from']})",
        ),
        (
            "Good parts",
            f"{lot['good_parts']:,} from {lot['wire_kg']} kg of wire (yield {fp['yield']})",
        ),
        ("Energy", f"{lot['energy_kwh']} kWh"),
        ("Lot total", f"{fp['lot_kg_co2']} kg CO2e"),
        ("Primary data", f"{round(fp['primary_data_share'] * 100)}%"),
        (
            "Method",
            ", ".join(pcf["crossSectoralStandardsUsed"]) + "; Catena-X PCF fields",
        ),
    ):
        page.insert_text((56, y), k, fontsize=9, color=soft)
        page.insert_text((170, y), str(v), fontsize=9, color=ink)
        y += 18
    y += 10
    page.insert_textbox(
        fitz.Rect(56, y, 540, y + 80),
        pcf["boundaryProcessesDescription"],
        fontsize=8,
        color=soft,
    )
    page.insert_textbox(
        fitz.Rect(56, 780, 540, 810), pcf["comment"], fontsize=7, color=soft
    )
    data = doc.tobytes()
    doc.close()
    return data


def pcf_json(lot: dict) -> str:
    return json.dumps(lot["pcf"], indent=2)
