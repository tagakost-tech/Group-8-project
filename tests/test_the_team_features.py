"""The features merged in from the team's projects (Hackathon B, Group 8):
an explainable fit score, the results charts, the CRM export and a pasted
reply. Each is checked through what a person sees or receives."""

import csv
import io

import pytest

import main
from conftest import sent_to
from modules import fit_score
from modules.database import Database
from modules.review_queue import ReviewQueue

HEAT_PUMPS = {
    "id": "heat_pumps_hvac",
    "name": "Heat pump makers",
    "type": "blue_ocean",
    "barriers": "Subsidy-driven demand",
    "search_keywords": {
        "en": ["heat pump manufacturer"],
        "de": ["Wärmepumpen Hersteller", "Heizungstechnik Wärmepumpe"],
    },
}
RAIL = {
    "id": "rail",
    "name": "Rail rolling stock makers",
    "type": "blue_ocean",
    "barriers": ["IRIS / ISO 22163 certification"],
    "search_keywords": {"en": ["rolling stock manufacturer"]},
}


@pytest.fixture
def sectors(monkeypatch):
    monkeypatch.setattr(fit_score.PROFILE, "sectors", [HEAT_PUMPS, RAIL])


def test_the_fit_score_explains_each_of_its_four_parts(sectors):
    fit = fit_score.assess(
        {
            "organization": "Klimatechnik Süd GmbH",
            "qualification_rationale": "Hersteller von Wärmepumpen und Heizungstechnik",
            "company_country": "DE",
        }
    )
    assert fit["sector"] == "Heat pump makers"
    assert fit["company_type"] == "OEM"
    assert [p["name"] for p in fit["parts"]] == ["market", "need", "fit", "distance"]
    assert [p["weight"] for p in fit["parts"]] == [30, 30, 25, 15]
    assert all(p["why"] for p in fit["parts"])
    expected = round(0.30 * 80 + 0.30 * 85 + 0.25 * 90 + 0.15 * 85)
    assert fit["total"] == expected


def test_a_missing_certification_is_named_never_a_reason_to_drop(sectors):
    fit = fit_score.assess(
        {
            "qualification_rationale": "rolling stock bogie maker",
            "company_country": "SE",
        }
    )
    assert fit["certifications_needed"] == ["IRIS / ISO 22163"]
    assert 0 < fit["total"] < 70


def test_a_far_country_is_said_and_the_total_stays_in_range(sectors):
    fit = fit_score.assess(
        {"qualification_rationale": "wholesale distributor", "company_country": "US"}
    )
    distance = next(p for p in fit["parts"] if p["name"] == "distance")
    assert "far from Italy" in distance["why"]
    assert fit["company_type"] == "distributor"
    assert 0 <= fit["total"] <= 100
    assert fit_score.band(fit["total"]) in ("good", "mid", "low")


def _client(tmp_path, settings_factory, **overrides):
    s = settings_factory(
        db_path=str(tmp_path / "dash.db"), blacklist_check_enabled=False, **overrides
    )
    return main.create_dashboard_app(s).test_client(), Database(s.db_path)


def _approved_lead(db, email="buyer@heatpumps.example", org="Heat Pumps GmbH"):
    sid = db.log_search("q")
    cid = db.add_contact(sid, email, name="Anna Weber", organization=org)
    q = ReviewQueue(db)
    item = q.propose(cid, "first_email", "s", "<p>b</p>{{legal_footer}}")
    q.open(item, "admin")
    q.approve(item, "admin")
    return cid


def test_the_export_holds_only_approved_leads_in_hubspots_columns(
    tmp_path, settings_factory
):
    client, db = _client(tmp_path, settings_factory)
    _approved_lead(db)
    sid = db.log_search("q")
    db.add_contact(sid, "never@decided.example", organization="Undecided Ltd")

    r = client.get("/api/export/leads.csv")

    assert r.status_code == 200 and r.mimetype == "text/csv"
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("﻿"))))
    assert rows[0][:3] == ["Company name", "Company Domain Name", "Email"]
    assert [row[2] for row in rows[1:]] == ["buyer@heatpumps.example"]
    assert rows[1][3:5] == ["Anna", "Weber"]


def test_a_company_name_cannot_become_a_spreadsheet_formula(tmp_path, settings_factory):
    client, db = _client(tmp_path, settings_factory)
    _approved_lead(db, org='=HYPERLINK("http://evil.example","x")')

    rows = list(
        csv.reader(
            io.StringIO(client.get("/api/export/leads.csv").get_data(as_text=True))
        )
    )

    assert rows[1][0].startswith("'=")


def test_the_results_count_leads_by_status(tmp_path, settings_factory):
    client, db = _client(tmp_path, settings_factory)
    sid = db.log_search("q")
    db.add_contact(sid, "a@one.example", organization="One")
    db.add_contact(sid, "b@two.example", organization="Two")

    data = client.get("/api/results").get_json()

    assert dict(data["per_status"]) == {"Not contacted yet": 2}
    assert sum(n for _s, n in data["per_sector"]) == 2


def test_a_pasted_opt_out_stops_the_company_at_once(tmp_path, settings_factory):
    client, db = _client(tmp_path, settings_factory)
    sid = db.log_search("q")
    db.add_contact(sid, "lena@doorlocks.example", organization="Door Locks GmbH")
    sent_to(db, "lena@doorlocks.example")

    r = client.post(
        "/api/review/paste-reply",
        json={
            "email": "lena@doorlocks.example",
            "subject": "Re: parts",
            "body": "Please remove me from your list.",
        },
    ).get_json()

    assert r["label"] == "opt-out"
    assert db.is_suppressed("lena@doorlocks.example")
    assert db.company_blocked("other.person@doorlocks.example")


def test_a_pasted_interested_reply_waits_as_an_answer(
    tmp_path, settings_factory, monkeypatch
):
    monkeypatch.setenv("AUTONOMY_REPLY", "approve")
    client, db = _client(tmp_path, settings_factory)
    sid = db.log_search("q")
    cid = db.add_contact(sid, "lena@doorlocks.example", organization="Door Locks GmbH")
    sent_to(db, "lena@doorlocks.example")

    r = client.post(
        "/api/review/paste-reply",
        json={
            "email": "lena@doorlocks.example",
            "subject": "Re: parts",
            "body": "Interesting, please send a quote for 200k pins.",
        },
    ).get_json()

    assert r["label"] == "interested"
    assert ReviewQueue(db).item_for(cid, "reply")["status"] == "pending"


def test_a_reply_from_someone_never_emailed_is_refused(tmp_path, settings_factory):
    client, _db = _client(tmp_path, settings_factory)
    r = client.post(
        "/api/review/paste-reply",
        json={"email": "stranger@nowhere.example", "body": "hello"},
    )
    assert r.status_code == 400
