"""Likeness to Omega's best customers, shown beside each proposed email."""

from modules.lookalike import Lookalike


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
