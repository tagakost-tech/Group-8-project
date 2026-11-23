"""Tests for the SQLite Database layer (dedup, suppression, backlog, stats)."""


def test_add_contact_returns_id(db):
    sid = db.log_search("q")
    cid = db.add_contact(sid, "a@example.com", organization="Org")
    assert isinstance(cid, int) and cid > 0


def test_add_contact_dedups_by_email(db):
    sid = db.log_search("q")
    first = db.add_contact(sid, "dup@example.com")
    second = db.add_contact(sid, "dup@example.com")
    assert first is not None
    assert second is None


def test_add_contact_skips_suppressed(db):
    sid = db.log_search("q")
    db.add_to_suppression("blocked@example.com", "test")
    assert db.add_contact(sid, "blocked@example.com") is None


def test_is_suppressed(db):
    assert db.is_suppressed("x@example.com") is False
    db.add_to_suppression("x@example.com", "test")
    assert db.is_suppressed("x@example.com") is True


def test_get_pending_contacts_lifecycle(db):
    sid = db.log_search("q")
    cid = db.add_contact(sid, "p@example.com")
    assert any(c["id"] == cid for c in db.get_pending_contacts())
    db.update_contact_status(cid, "emailed")
    assert all(c["id"] != cid for c in db.get_pending_contacts())


def test_get_pending_contacts_excludes_suppressed(db):
    sid = db.log_search("q")
    db.add_contact(sid, "sup@example.com")
    db.add_to_suppression("sup@example.com", "bounce")
    assert all(c["email"] != "sup@example.com" for c in db.get_pending_contacts())


def test_email_log_and_stats(db):
    sid = db.log_search("q")
    cid = db.add_contact(sid, "e@example.com")
    eid = db.log_email(cid, "e@example.com", "Subj")
    db.update_email_status(eid, "sent")
    stats = db.get_stats()
    assert stats["total_contacts"] >= 1
    assert stats["emails_sent"] >= 1
    assert 0 <= stats["success_rate"] <= 100


def test_pending_searches(db):
    db.log_search("q1", status="pending")
    assert any(p["search_query"] == "q1" for p in db.get_pending_searches())
