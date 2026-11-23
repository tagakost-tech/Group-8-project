"""What a Chinese fastener really costs in the EU once CBAM applies.

CBAM (Regulation (EU) 2023/956) covers screws and bolts (CN 7318). An EU
importer of Chinese fasteners pays, per tonne of product, the embedded
emissions times the CBAM certificate price times the share of the carbon cost
charged that year (rising as free allocation is phased out, to 100% in 2034).
Without verified supplier data, the importer must use the EU's default value,
which is high for China (blast-furnace steel).

This module compares that landed price with Omega's quote, year by year. It
is a sales argument a person checks: every rate comes from
client/omega_carbon.json with its source.
"""

import json
from pathlib import Path

CARBON_PATH = Path(__file__).resolve().parent.parent / "client" / "omega_carbon.json"


def load_carbon(path: Path | None = None) -> dict:
    return json.loads(Path(path or CARBON_PATH).read_text(encoding="utf-8"))


def _china_default(c: dict, year: int) -> float:
    table = c["cbam"]["china_default_t_per_t"]
    return float(table.get(str(year), table["later"]))


def compare(
    part_mass_g: float,
    competitor_eur_per_1000: float,
    omega_eur_per_1000: float,
    *,
    certificate_eur_per_t: float | None = None,
    embedded_t_per_t: float | None = None,
    years: range = range(2026, 2035),
    carbon: dict | None = None,
) -> dict:
    """The competitor's price with CBAM added, against Omega's, per year.

    ``embedded_t_per_t`` overrides the EU default (a supplier with verified
    data); ``certificate_eur_per_t`` overrides the price (a scenario)."""
    c = carbon or load_carbon()
    price = (
        c["cbam"]["certificate_eur_per_t"]
        if certificate_eur_per_t is None
        else certificate_eur_per_t
    )
    tonnes_per_1000 = part_mass_g / 1000  # 1,000 parts of g grams weigh g kg, g/1000 t
    rows = []
    for year in years:
        share = c["cbam"]["charged_share_by_year"].get(str(year), 1.0)
        intensity = (
            _china_default(c, year) if embedded_t_per_t is None else embedded_t_per_t
        )
        cbam_cost = tonnes_per_1000 * intensity * price * share
        landed = competitor_eur_per_1000 + cbam_cost
        rows.append(
            {
                "year": year,
                "charged_share": share,
                "cbam_eur_per_1000": round(cbam_cost, 2),
                "competitor_landed_eur_per_1000": round(landed, 2),
                "omega_eur_per_1000": round(omega_eur_per_1000, 2),
                "gap_eur_per_1000": round(omega_eur_per_1000 - landed, 2),
            }
        )
    crossover = next((r["year"] for r in rows if r["gap_eur_per_1000"] <= 0), None)
    return {
        "rows": rows,
        "crossover_year": crossover,
        "certificate_eur_per_t": price,
        "assumptions": [
            c["cbam"]["covers_source"],
            c["cbam"]["certificate_source"],
            c["cbam"]["charged_share_source"],
            c["cbam"]["china_default_source"],
            c["cbam"]["de_minimis_source"],
        ],
    }


def omega_part_co2_kg(
    part_mass_g: float,
    yield_: float = 0.97,
    route: str = "eaf_scrap",
    carbon: dict | None = None,
) -> float:
    """CO2 of one Omega part: steel by its route plus forming energy."""
    c = carbon or load_carbon()
    steel_kg = part_mass_g / 1000 / yield_
    return (
        steel_kg * c["steel_routes"][route]["t_co2_per_t"]
        + steel_kg * c["forming_kwh_per_kg"] * c["electricity_kgco2_per_kwh"]
    )


def china_part_co2_kg(
    part_mass_g: float, year: int = 2026, carbon: dict | None = None
) -> float:
    """CO2 of the same part at the EU default for Chinese screws and bolts."""
    c = carbon or load_carbon()
    return part_mass_g / 1000 * _china_default(c, year)
