"""The search learns from the people who review its finds.

A reviewer's approval says a search found the right kind of company; a
rejection says it did not. Both now steer which searches the agent runs next
(modules/learning.py), alongside the verified finds and replies it counted
before - so a sector whose companies reviewers keep rejecting is searched
less, and one whose companies they approve is searched more.
"""

from modules.learning import PerformanceLearner
from modules.review_queue import ReviewQueue

SEEDS = ["heat pump", "e-bike"]
COUNTRIES = ["Germany"]


def _found(db, query, n, decide, run=0):
    """n companies from one search, each proposed and decided by a person."""
    sid = db.log_search(query)
    q = ReviewQueue(db, daily_cap=1000)
    for i in range(n):
        slug = query.replace(" ", "-")
        cid = db.add_contact(
            sid, f"buyer{i}@{slug}-{run}-{i}.example", organization=f"{query} {run} {i}"
        )
        item = q.propose(cid, "first_email", "s", "<p>b</p>{{legal_footer}}")
        q.open(item, "maria")
        if decide == "approve":
            q.approve(item, "maria")
        elif decide == "reject":
            q.reject(item, "maria", "not a buyer of metal parts")


def test_rejected_finds_lower_a_search_and_approved_ones_raise_it(db):
    # The rejected sector is searched first in each round, so recency alone
    # would favour it: only the reviewers' decisions can put it below.
    for run in range(4):
        _found(db, "e-bike manufacturer Germany", 3, "reject", run)
        _found(db, "heat pump manufacturer Germany", 3, "approve", run)

    seeds = PerformanceLearner(db, SEEDS, COUNTRIES).weights()[0]

    assert seeds["heat pump"] > seeds["e-bike"], seeds


def test_the_outcomes_count_what_reviewers_decided(db):
    _found(db, "heat pump manufacturer Germany", 2, "approve")
    _found(db, "e-bike manufacturer Germany", 3, "reject")

    by_query = {o["search_query"]: o for o in db.get_search_outcomes(10)}

    assert by_query["heat pump manufacturer Germany"]["approved"] == 2
    assert by_query["heat pump manufacturer Germany"]["rejected"] == 0
    assert by_query["e-bike manufacturer Germany"]["rejected"] == 3


def test_without_reviews_the_weights_are_what_they_were(db):
    """No decision recorded: the weight is the old formula's, unchanged."""
    db.log_search("heat pump manufacturer Germany")
    learner = PerformanceLearner(db, SEEDS, COUNTRIES)
    d = learner._aggregate()["seeds"]["heat pump"]
    assert d["approved"] == 0 and d["rejected"] == 0
    old = {k: v for k, v in d.items() if k not in ("approved", "rejected")}
    assert learner.weights()[0]["heat pump"] == PerformanceLearner._weight(old)


def test_rejections_alone_lower_a_search(db):
    """Nothing approved anywhere: one sector's finds are rejected, the other's
    wait undecided. Searched first in each round, the rejected sector would
    win on recency alone; the rejections must still put it below."""
    for run in range(4):
        _found(db, "e-bike manufacturer Germany", 3, "reject", run)
        _found(db, "heat pump manufacturer Germany", 3, "wait", run)

    seeds = PerformanceLearner(db, SEEDS, COUNTRIES).weights()[0]

    assert seeds["heat pump"] > seeds["e-bike"], seeds
