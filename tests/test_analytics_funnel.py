"""The lead funnel must narrow.

Found, Emailed, Opened, Replied are stages of one journey, so each can only be
a subset of the one before it. Three of them counted CONTACTS and the fourth
counted MESSAGES: a prospect on a follow-up sequence has several email_log
rows, so opening both their first mail and its follow-up scored 2 against an
"Emailed" of 1 and drew Opened taller than Emailed.

Nothing tested lead_funnel at all, which is why a chart that visibly widens
could ship.
"""

import pytest


@pytest.fixture
def analytics(db):
    from modules.analytics import Analytics

    return Analytics(db)


def _open(analytics, db, log_id):
    """Record a real open, the way the tracking pixel does."""
    analytics.record_open(analytics.generate_tracking_id(log_id))


def _funnel(analytics):
    data = analytics.get_chart_data()["lead_funnel"]
    return dict(zip(data["labels"], data["data"]))


def test_a_follow_up_open_does_not_inflate_the_funnel(analytics, db):
    """The reported bug: one contact, two messages, both opened."""
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "p@brand.de")
    first = db.log_email(cid, "p@brand.de", "intro", delivery_status="sent")
    second = db.log_email(cid, "p@brand.de", "follow-up", delivery_status="sent")
    _open(analytics, db, first)
    _open(analytics, db, second)

    f = _funnel(analytics)

    assert f["Emailed"] == 1
    assert f["Opened"] == 1, (
        f"counted {f['Opened']} opens for one contact - the funnel draws "
        f"Opened wider than Emailed, which is visibly nonsense to an operator"
    )


def test_opening_the_same_email_twice_counts_once(analytics, db):
    """record_open inserts a SECOND open_tracking row for a repeat open, so
    even a single-message contact could exceed 1 under a per-row count."""
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "p@brand.de")
    log_id = db.log_email(cid, "p@brand.de", "intro", delivery_status="sent")
    tid = analytics.generate_tracking_id(log_id)
    analytics.record_open(tid)
    analytics.record_open(tid)

    assert _funnel(analytics)["Opened"] == 1


def test_the_funnel_never_widens(analytics, db):
    """The general property, on a mixed population."""
    sid = db.log_search("q", source="test")
    for i in range(6):
        cid = db.add_contact(sid, f"opened{i}@brand.de")
        _open(
            analytics,
            db,
            db.log_email(cid, f"opened{i}@brand.de", "s", delivery_status="sent"),
        )
    for i in range(4):
        cid = db.add_contact(sid, f"quiet{i}@brand.de")
        db.log_email(cid, f"quiet{i}@brand.de", "s", delivery_status="sent")
    db.add_contact(sid, "never-emailed@brand.de")

    f = _funnel(analytics)

    assert f["Found"] >= f["Emailed"] >= f["Opened"] >= f["Replied"], f


def test_opens_are_still_counted(analytics, db):
    """Control: the fix must not work by counting nothing."""
    sid = db.log_search("q", source="test")
    for i in range(3):
        cid = db.add_contact(sid, f"o{i}@brand.de")
        _open(
            analytics,
            db,
            db.log_email(cid, f"o{i}@brand.de", "s", delivery_status="sent"),
        )
    for i in range(2):
        cid = db.add_contact(sid, f"q{i}@brand.de")
        db.log_email(cid, f"q{i}@brand.de", "s", delivery_status="sent")

    f = _funnel(analytics)

    assert f["Emailed"] == 5
    assert f["Opened"] == 3
