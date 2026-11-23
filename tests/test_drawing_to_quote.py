"""Drawing to quote, carbon per part, and likeness to the best customers.

The estimator is the one who quotes; these check that what the agent puts in
front of them is right, explained, and only leaves once a person approves."""

import json
from pathlib import Path

import pytest

import main
from conftest import sent_to
from modules.database import Database
from modules.lookalike import Lookalike
from modules.quoting import Part, estimate, read_drawing_pdf, read_drawing_text
from modules.review_queue import ReviewQueue

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "assets" / "sample-drawing-stepped-pin.pdf"

PIN = "STEPPED PIN Material: C10C L=40 Ø12 head Ø6 H: 4 M6x1 b: 22 ±0.05 annual volume 500.000 pcs"


def test_the_reader_finds_the_sizes_a_drawing_states():
    p = read_drawing_text(PIN)
    assert (p.length, p.shank_d, p.head_d, p.head_h) == (40, 6, 12, 4)
    assert p.thread == "M6x1" and p.thread_len == 22
    assert p.material == "carbon_steel" and p.tolerance_mm == 0.05
    assert p.annual_volume == 500000 and p.notes == []


def test_what_the_reader_cannot_find_is_named_not_guessed_silently():
    p = read_drawing_text("Ø10 Ø5 brass part")
    assert p.material == "brass"
    assert any("length" in n for n in p.notes)


def test_the_sample_drawing_reads_completely():
    p = read_drawing_pdf(SAMPLE.read_bytes())
    assert (p.collar_d, p.collar_h) == (8, 2)
    assert p.notes == []


def test_a_formable_part_gets_stations_price_and_carbon():
    est = estimate(read_drawing_text(PIN))
    assert est["formability"]["verdict"] == "formable"
    assert est["formability"]["stations"] == 3
    assert "thread rolling" in est["formability"]["secondary"]
    assert est["cost"]["per_1000_eur"] > 0
    k = est["carbon"]
    assert k["material_used_formed_g"] < k["material_used_machined_g"]
    assert 0 < k["saving_pct"] < 100


def test_wire_outside_omegas_range_is_not_formable():
    est = estimate(Part(length=120, shank_d=30, head_d=45, head_h=12))
    assert est["formability"]["verdict"] == "not formable"
    assert est["cost"] is None


def test_a_head_too_big_to_gather_is_not_formable():
    est = estimate(Part(length=20, shank_d=2, head_d=12, head_h=6))
    assert est["formability"]["verdict"] == "not formable"


def test_a_very_tight_tolerance_needs_a_secondary_operation():
    est = estimate(Part(length=30, shank_d=5, head_d=8, head_h=3, tolerance_mm=0.01))
    assert est["formability"]["verdict"] == "formable with secondary operations"


def test_the_best_customers_rank_above_strangers_and_rejects():
    like = Lookalike(
        ["Tier-1 supplier of car door locks and door modules"],
        ["wholesale distributor of screws and bolts"],
    )
    door, bakery, distributor = like.score(
        [
            "Hersteller von Schließsystemen, door locks for cars, Tier-1",
            "artisan bakery and pastry shop",
            "wholesale distributor of industrial screws",
        ]
    )
    assert door["score"] > bakery["score"]
    assert door["score"] > distributor["score"]
    assert distributor["unlike"] is not None


def _client(tmp_path, settings_factory):
    s = settings_factory(
        db_path=str(tmp_path / "dash.db"), blacklist_check_enabled=False
    )
    return main.create_dashboard_app(s).test_client(), Database(s.db_path)


def test_reading_a_drawing_through_the_dashboard(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    with SAMPLE.open("rb") as f:
        r = client.post("/api/quotes/read", data={"drawing": (f, "pin.pdf")})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["estimate"]["formability"]["stations"] == 4


def test_a_file_that_is_not_a_pdf_is_refused(tmp_path, settings_factory):
    import io

    client, _db = _client(tmp_path, settings_factory)
    r = client.post(
        "/api/quotes/read", data={"drawing": (io.BytesIO(b"MZ..."), "x.exe")}
    )
    assert r.status_code == 400


def test_an_approved_quote_is_stored_with_its_reviewer_and_points_at_the_answer(
    tmp_path, settings_factory
):
    client, db = _client(tmp_path, settings_factory)
    sid = db.log_search("q")
    cid = db.add_contact(sid, "m.weber@heat.example", organization="Heat GmbH")
    sent_to(db, "m.weber@heat.example")
    reply = ReviewQueue(db).propose(
        cid, "reply", "Re: parts", "<p>Thanks.</p>{{legal_footer}}"
    )

    part = json.loads(json.dumps(read_drawing_text(PIN).__dict__))
    r = client.post(
        "/api/quotes",
        json={"part": part, "email": "m.weber@heat.example", "language": "en"},
    ).get_json()

    assert "EUR" in r["quote_text"] and r["reply_item_id"] == reply
    row = db.conn.execute("SELECT status, reviewer, contact_id FROM quotes").fetchone()
    assert tuple(row) == ("approved", "admin", cid)


def test_a_part_that_cannot_be_formed_cannot_be_quoted(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    r = client.post(
        "/api/quotes",
        json={"part": {"length": 100, "shank_d": 40, "head_d": 50, "head_h": 10}},
    )
    assert r.status_code == 400


@pytest.mark.parametrize("bad", [{"length": "forty"}, {"shank_d": "6mm"}])
def test_a_size_that_is_not_a_number_is_refused(tmp_path, settings_factory, bad):
    client, _db = _client(tmp_path, settings_factory)
    r = client.post("/api/quotes/estimate", json={"part": bad})
    assert r.status_code == 400
