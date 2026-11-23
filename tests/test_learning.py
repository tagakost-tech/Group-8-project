"""Tests for the performance learning loop and backlog signals."""
from modules.learning import PerformanceLearner


def test_count_pending_contacts(db):
    sid = db.log_search("q")
    db.add_contact(sid, "p1@x.com")
    db.add_contact(sid, "p2@x.com")
    assert db.count_pending_contacts() == 2
    db.add_to_suppression("p1@x.com", "test")
    assert db.count_pending_contacts() == 1


def test_search_outcomes_reports_verified_and_replies(db):
    sid = db.log_search("cosmetics manufacturer France")
    db.update_search(sid, results_count=5, status="completed")
    cid = db.add_contact(sid, "a@x.fr")
    db.update_contact_verification(cid, True)
    db.update_contact_status(cid, "responded")
    row = next(o for o in db.get_search_outcomes()
               if o["search_query"] == "cosmetics manufacturer France")
    assert row["results_count"] == 5
    assert row["verified"] == 1
    assert row["responded"] == 1


def _seed(db):
    # Productive: cosmetics / France -> verified + reply
    s1 = db.log_search("cosmetics manufacturer France")
    db.update_search(s1, results_count=5, status="completed")
    c1 = db.add_contact(s1, "a@x.fr")
    db.update_contact_verification(c1, True)
    db.update_contact_status(c1, "responded")
    # Barren: pharma / Germany -> repeated zero results
    for _ in range(3):
        sid = db.log_search("pharma manufacturer Germany")
        db.update_search(sid, results_count=0, status="no_results")


def test_learner_weights_prefer_productive(db):
    _seed(db)
    learner = PerformanceLearner(db, ["cosmetics", "pharma"], ["France", "Germany"])
    seed_w, country_w = learner.weights()
    assert country_w["France"] > country_w["Germany"]
    assert seed_w["cosmetics"] > seed_w["pharma"]


def test_learner_avoids_barren_terms(db):
    _seed(db)
    learner = PerformanceLearner(db, ["cosmetics", "pharma"], ["France", "Germany"])
    avoid = learner.avoid_terms()
    assert "Germany" in avoid and "pharma" in avoid
    assert "France" not in avoid


def test_learner_focus_summary(db):
    _seed(db)
    learner = PerformanceLearner(db, ["cosmetics", "pharma"], ["France", "Germany"])
    summary = learner.focus_summary()
    assert "working well" in summary and "avoid" in summary
