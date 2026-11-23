"""Regressions for the whole-surface feature review findings.

Three defects, all in code that predated the review rounds: GDPR erasure
crashing on any contact we actually emailed, opens never reaching email_log,
and every discovery query leaving a permanently-hung duplicate search row.
"""

from modules.analytics import Analytics
from modules.export import DataExporter
from modules.search import SearchModule

# ── GDPR Art. 17 erasure ───────────────────────────────────────────────────


def test_erase_works_for_a_contact_we_actually_emailed(db):
    """The only people who file erasure requests are people we contacted -
    and every contacted contact has email_log rows whose FOREIGN KEY made the
    DELETE throw. Worse, the suppression insert came after the crash point,
    so the person stayed contactable while the operator believed the request
    was honoured."""
    sid = db.log_search("q")
    cid = db.add_contact(
        sid, email="prospect@brand.de", name="Anna Rossi", organization="Brand"
    )
    db.log_email(cid, "prospect@brand.de", "subj", "preview", delivery_status="sent")

    summary = DataExporter(db).gdpr_erase("Prospect@Brand.DE")  # request casing varies

    assert db.is_suppressed("prospect@brand.de") is True
    row = db.get_contact_by_id(cid)
    assert "prospect@brand.de" not in (row["email"] or "")
    assert row["name"] is None  # personal data gone
    assert row["status"] == "erased"
    for e in db.get_all_emails():
        assert "prospect@brand.de" not in (e["recipient_email"] or "")
    assert summary["actions"]


def test_erase_survives_a_retry_queue_reference(db):
    from modules.retry_queue import RetryQueue

    sid = db.log_search("q")
    cid = db.add_contact(sid, email="prospect@brand.de")
    log_id = db.log_email(cid, "prospect@brand.de", "s", "p", delivery_status="failed")
    RetryQueue(db).add_to_retry(log_id, cid, "send failed")

    DataExporter(db).gdpr_erase("prospect@brand.de")
    assert db.is_suppressed("prospect@brand.de") is True


def test_erase_of_an_unknown_address_still_suppresses(db):
    """Even with no rows anywhere, the request means "never contact me" -
    suppression must happen so a future scrape cannot re-discover them."""
    DataExporter(db).gdpr_erase("stranger@nowhere.de")
    assert db.is_suppressed("stranger@nowhere.de") is True


# ── Opens must reach email_log, not only open_tracking ─────────────────────


def test_recorded_open_reaches_email_log_and_ab_stats(db):
    """record_open wrote only open_tracking, but the Emails table's Opens
    column and the A/B panel both read email_log.opened_at - so the A/B table
    reported a confident 0.0% open rate for every variant forever, instead of
    "no data". The operator would conclude neither subject line works."""
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de")
    log_id = db.log_email(
        cid,
        "buyer@brand.de",
        "s",
        "p",
        delivery_status="sent",
        tracking_id="TRK-1",
        ab_variant="subject|s0",
    )

    a = Analytics(db)
    a.generate_tracking_id(log_id, tracking_id="TRK-1")
    a.record_open("TRK-1", ip_address="1.2.3.4", user_agent="test")

    assert db.get_all_emails(limit=1)[0]["opened_at"] is not None
    ab = {r["variant"]: r for r in db.get_ab_stats()}
    assert ab["subject|s0"]["opened"] == 1


def test_second_open_does_not_reset_the_first_timestamp(db):
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de")
    log_id = db.log_email(
        cid, "buyer@brand.de", "s", "p", delivery_status="sent", tracking_id="TRK-2"
    )
    a = Analytics(db)
    a.generate_tracking_id(log_id, tracking_id="TRK-2")
    a.record_open("TRK-2")
    first = db.get_all_emails(limit=1)[0]["opened_at"]
    a.record_open("TRK-2")
    assert db.get_all_emails(limit=1)[0]["opened_at"] == first


# ── Discovery queue rows must not be duplicated and hung ───────────────────


def _searcher(db, monkeypatch):
    s = SearchModule(db, rate_limit=0.0)
    monkeypatch.setattr(s, "_ddg_search", lambda q: [])
    return s


def test_queued_discovery_search_reuses_its_row(db, monkeypatch):
    """The agent marked the queued row in_progress, then search_and_extract
    minted a SECOND row and completed only that one - so the Search Logs tab
    filled up with permanently-hung amber duplicates and the learning loop's
    per-query denominator was doubled."""
    sid = db.log_search("vitamin brand Germany", source="discovery")
    db.update_search(sid, status="in_progress")

    _searcher(db, monkeypatch).search_and_extract(
        "vitamin brand Germany", search_id=sid
    )

    rows = [
        r for r in db.get_all_searches() if r["search_query"] == "vitamin brand Germany"
    ]
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    assert rows[0]["status"] != "in_progress"  # completed or no_results, not hung


def test_adhoc_search_still_logs_its_own_row(db, monkeypatch):
    """The CLI path passes no id and must keep working exactly as before."""
    _searcher(db, monkeypatch).search_and_extract("probiotic distributor Spain")
    rows = [
        r
        for r in db.get_all_searches()
        if r["search_query"] == "probiotic distributor Spain"
    ]
    assert len(rows) == 1
    assert rows[0]["status"] != "in_progress"
