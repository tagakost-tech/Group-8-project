"""Carbon passports, the CBAM comparison, and what Omega made before.

The figures are a sales argument a person checks, so these pin the
arithmetic to hand-worked values and check that a passport only leaves once
someone approved it."""

import json

import pytest

import main
from modules import carbon_passport as cp
from modules.cbam import china_part_co2_kg, compare, load_carbon, omega_part_co2_kg
from modules.database import Database
from modules.job_memory import _shape_closeness, search, similar

LOT = {
    "lot_code": "L-26-0417",
    "created_at": "2026-11-02 08:00:00",
    "part_name": "Stepped pin",
    "customer": "Example Tier-1",
    "part_mass_g": 11.9,
    "steel_mill": "Example EAF mill",
    "steel_route": "eaf_scrap",
    "mill_t_co2_per_t": 0.62,
    "wire_kg": 1250,
    "good_parts": 102000,
    "energy_kwh": 310,
}


def test_the_passport_footprint_matches_the_hand_worked_figure():
    fp = cp.footprint(LOT)
    # steel 1250 kg x 0.62 = 775 kg; energy 310 kWh x 0.25 = 77.5 kg
    assert fp["lot_kg_co2"] == pytest.approx(852.5)
    assert fp["per_part_g_co2"] == pytest.approx(852.5 / 102000 * 1000, abs=0.01)
    assert fp["yield"] == pytest.approx(102000 * 11.9 / 1000 / 1250, abs=0.001)
    assert fp["china_default_g_co2"] == pytest.approx(11.9 * 7.01, abs=0.01)
    assert fp["steel_factor_from"] == "the mill's certificate"
    assert fp["primary_data_share"] == 1.0


def test_without_a_mill_figure_the_route_average_is_used_and_said():
    fp = cp.footprint({**LOT, "mill_t_co2_per_t": None})
    assert fp["steel_factor"] == 0.69
    assert "average" in fp["steel_factor_from"]
    assert fp["primary_data_share"] < 1


def test_the_china_default_rises_with_the_year_of_the_lot():
    later = cp.footprint({**LOT, "created_at": "2028-03-01 08:00:00"})
    assert later["china_default_g_co2"] == pytest.approx(11.9 * 8.29, abs=0.01)


def test_cbam_adds_the_charged_share_of_the_carbon_price_each_year():
    r = compare(11.9, 60.0, 87.0)
    rows = {row["year"]: row for row in r["rows"]}
    # 1,000 parts of 11.9 g = 0.0119 t; x 7.01 t/t x EUR 82.32 x 2.5%
    assert rows[2026]["cbam_eur_per_1000"] == pytest.approx(
        0.0119 * 7.01 * 82.32 * 0.025, abs=0.01
    )
    assert rows[2034]["cbam_eur_per_1000"] == pytest.approx(
        0.0119 * 8.29 * 82.32, abs=0.01
    )
    assert [row["year"] for row in r["rows"]] == list(range(2026, 2035))
    shares = [row["charged_share"] for row in r["rows"]]
    assert shares == sorted(shares) and shares[-1] == 1.0
    assert r["crossover_year"] is None  # a small pin: CBAM does not close a EUR 27 gap
    assert len(r["assumptions"]) == 5


def test_a_zero_carbon_price_adds_nothing():
    r = compare(11.9, 60.0, 87.0, certificate_eur_per_t=0)
    assert all(row["cbam_eur_per_1000"] == 0 for row in r["rows"])
    assert all(row["gap_eur_per_1000"] == 27.0 for row in r["rows"])


def test_a_heavy_part_crosses_over_once_enough_carbon_is_charged():
    r = compare(250, 600.0, 700.0)
    assert r["crossover_year"] is not None
    first = next(row for row in r["rows"] if row["year"] == r["crossover_year"])
    assert first["gap_eur_per_1000"] <= 0


def test_verified_supplier_data_replaces_the_default():
    default = compare(100, 0, 0)["rows"][-1]["cbam_eur_per_1000"]
    verified = compare(100, 0, 0, embedded_t_per_t=2.0)["rows"][-1]["cbam_eur_per_1000"]
    assert verified < default


def test_an_omega_part_is_several_times_lower_than_the_china_default():
    c = load_carbon()
    assert china_part_co2_kg(11.9, carbon=c) / omega_part_co2_kg(11.9, carbon=c) > 5


def test_the_nearest_past_job_is_the_one_shaped_like_the_part():
    part = {
        "length": 40,
        "shank_d": 6,
        "head_d": 12,
        "collar_d": 8,
        "thread": "M6x1",
        "material": "carbon_steel",
    }
    top = similar(part, "stepped pin with collar")
    assert top[0]["ref"] == "J-1998-014"
    assert top[0]["closeness"] > top[1]["closeness"] >= top[2]["closeness"]
    assert top[0]["notes"]


def test_identical_sizes_are_fully_close_and_different_ones_less():
    a = {"length": 40, "shank_d": 6, "head_d": 12, "collar_d": 8}
    assert _shape_closeness(a, a) == 1.0
    assert (
        _shape_closeness(a, {"length": 80, "shank_d": 12, "head_d": 24, "collar_d": 16})
        < 0.3
    )


def test_the_setters_notes_can_be_searched():
    hits = search("die cracked collar")
    assert hits and hits[0]["ref"] == "J-1998-014"
    assert search("") == []


def _client(tmp_path, settings_factory):
    s = settings_factory(
        db_path=str(tmp_path / "dash.db"), blacklist_check_enabled=False
    )
    return main.create_dashboard_app(s).test_client(), Database(s.db_path)


def _post_lot(client, **over):
    body = {k: v for k, v in LOT.items() if k != "created_at"}
    return client.post("/api/carbon/lots", json={**body, **over})


def test_a_lot_is_recorded_as_a_draft_and_its_passport_held_until_approved(
    tmp_path, settings_factory
):
    client, db = _client(tmp_path, settings_factory)
    r = _post_lot(client)
    assert r.status_code == 201, r.get_json()
    lot = r.get_json()
    assert lot["status"] == "draft" and lot["footprint"]["times_lower"] > 5

    assert client.get(f"/api/carbon/lots/{lot['id']}/passport.pdf").status_code == 409
    assert client.get(f"/api/carbon/lots/{lot['id']}/passport.json").status_code == 409

    ok = client.post(f"/api/carbon/lots/{lot['id']}/approve", json={})
    assert ok.status_code == 200 and ok.get_json()["reviewer"] == "admin"
    assert (
        client.post(f"/api/carbon/lots/{lot['id']}/approve", json={}).status_code == 409
    )

    pdf = client.get(f"/api/carbon/lots/{lot['id']}/passport.pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")
    pcf = json.loads(client.get(f"/api/carbon/lots/{lot['id']}/passport.json").data)
    assert pcf["productId"] == "L-26-0417" and pcf["declaredUnit"] == "piece"

    logged = db.conn.execute(
        "SELECT COUNT(*) FROM activity_log WHERE action IN ('PASSPORT_DRAFTED', 'PASSPORT_APPROVED')"
    ).fetchone()[0]
    assert logged == 2


def test_a_lot_with_missing_or_bad_figures_is_refused(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    assert _post_lot(client, wire_kg="lots").status_code == 400
    assert _post_lot(client, part_name="").status_code == 400
    assert _post_lot(client).status_code == 201
    assert _post_lot(client).status_code == 409  # same lot code twice


def test_the_summary_counts_lots_and_the_co2_avoided(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    _post_lot(client)
    s = client.get("/api/carbon/summary").get_json()
    assert (s["lots"], s["drafts"], s["approved"]) == (1, 1, 0)
    assert s["avoided_t_co2"] > 0


def test_the_cbam_route_checks_its_inputs(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    assert client.get("/api/carbon/cbam?mass_g=11.9&competitor=60").status_code == 400
    assert (
        client.get("/api/carbon/cbam?mass_g=-1&competitor=60&omega=87").status_code
        == 400
    )
    r = client.get(
        "/api/carbon/cbam?mass_g=11.9&competitor=60&omega=87&price=0"
    ).get_json()
    assert r["certificate_eur_per_t"] == 0 and len(r["rows"]) == 9


def test_made_before_through_the_dashboard(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    part = {
        "length": 40,
        "shank_d": 6,
        "head_d": 12,
        "head_h": 4,
        "collar_d": 8,
        "thread": "M6x1",
    }
    r = client.post(
        "/api/memory/similar", json={"part": part, "description": "stepped pin"}
    )
    assert r.status_code == 200 and r.get_json()[0]["ref"] == "J-1998-014"
    hits = client.get("/api/memory/search?q=galled").get_json()
    assert hits and all("closeness" in h for h in hits)


def test_the_carbon_paragraph_needs_an_approved_passport(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    assert client.get("/api/carbon/paragraph").get_json()["paragraph"] is None
    lot = _post_lot(client).get_json()
    assert client.get("/api/carbon/paragraph").get_json()["paragraph"] is None
    client.post(f"/api/carbon/lots/{lot['id']}/approve", json={})
    text = client.get("/api/carbon/paragraph").get_json()["paragraph"]
    assert str(lot["footprint"]["per_part_g_co2"]) in text and "Scope 3" in text
    assert "Cina" in client.get("/api/carbon/paragraph?lang=it").get_json()["paragraph"]


def test_carbon_pressure_is_shown_not_scored():
    from modules.fit_score import assess

    tier1 = {
        "organization": "Tier-1 automotive supplier of door systems, net zero by 2039",
        "company_country": "DE",
    }
    a = assess(tier1)
    assert a["carbon_pressure"]["level"] == "high"
    assert (
        a["total"]
        == assess(
            {**tier1, "organization": "Tier-1 automotive supplier of door systems"}
        )["total"]
    )
    assert (
        assess(
            {"organization": "maker of carbon steel bolts", "company_country": "US"}
        )["carbon_pressure"]["level"]
        == "none"
    )


def test_the_app_shell_is_the_front_page_and_runs_under_the_csp(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    page = client.get("/")
    html = page.get_data(as_text=True)
    assert 'id="screen-carbon"' in html and 'href="/classic"' in html
    assert "<script>" not in html and 'src="/static/app/app.js"' in html
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert client.get("/static/app/app.js").status_code == 200
    assert 'id="tab-review"' in client.get("/classic").get_data(as_text=True)
