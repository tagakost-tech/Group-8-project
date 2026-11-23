"""The activity_log entries that explain WHY a contact stopped moving.

Every one of these writes sits inside a try/except that swallows failures, so
a wrong kwarg or a renamed signature would never raise, never fail a test, and
never appear in the log - the row would simply not exist. The two suppression
entries are the dated record proving when and why an address stopped being
contacted (GDPR Art. 21 accountability), so their absence is not cosmetic.
"""

import smtplib

from conftest import sent_to
from modules.database import Database
from modules.email_sender import EmailSender
from modules.imap_monitor import ImapMonitor
from modules.retry_queue import RetryQueue


def _actions(db, action, limit=50):
    return [r for r in db.get_recent_activity(limit) if r["action"] == action]


def _monitor(tmp_path):
    return ImapMonitor(
        Database(str(tmp_path / "i.db")), "imap.test", 993, "me@corp.com", "pw"
    )


def test_hard_bounce_suppression_is_recorded(tmp_path):
    m = _monitor(tmp_path)
    sent_to(m.db, "dead@brand.de")
    m.process_bounces([{"email": "dead@brand.de", "subject": "Undeliverable"}])
    rows = _actions(m.db, "SUPPRESSED_BOUNCE")
    assert len(rows) == 1
    assert "dead@brand.de" in rows[0]["details"]


def test_optout_suppression_is_recorded(tmp_path):
    m = _monitor(tmp_path)
    m.process_unsubscribes([{"email": "buyer@brand.de"}])
    rows = _actions(m.db, "SUPPRESSED_OPTOUT")
    assert len(rows) == 1
    assert "buyer@brand.de" in rows[0]["details"]


def test_an_already_suppressed_address_is_not_recorded_twice(tmp_path):
    """The log is an audit trail, not a heartbeat - re-seeing the same bounce
    on the next scan must not add a second row."""
    m = _monitor(tmp_path)
    sent_to(m.db, "dead@brand.de")
    entry = [{"email": "dead@brand.de", "subject": "Undeliverable"}]
    m.process_bounces(entry)
    m.process_bounces(entry)
    assert len(_actions(m.db, "SUPPRESSED_BOUNCE")) == 1


def test_soft_bounce_is_recorded_but_not_suppressed(tmp_path):
    """A domain that soft-bounces repeatedly is a deliverability signal an
    operator should see, and it explains why a contact is stalled."""
    import tests.test_imap_inbox_scan as scan

    m = _monitor(tmp_path)
    # failed + 4.x.x = the MTA gave up on a TRANSIENT condition (mailbox full).
    # 'delayed' + 4.x.x is different - still queued - and is dsn_info, not this.
    fake = scan.FakeIMAP([scan._dsn_raw("full@brand.de", "failed", "4.2.2")])
    m._connect = lambda: fake
    m.run_check()

    assert len(_actions(m.db, "SOFT_BOUNCE")) == 1
    assert m.db.is_suppressed("full@brand.de") is False


def test_permanent_smtp_rejection_is_recorded(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    s = EmailSender(db, "smtp.test", 587, "u@corp.com", "pw")

    class Refusing:
        def noop(self):
            return (250, b"ok")

        def send_message(self, msg):
            raise smtplib.SMTPRecipientsRefused(
                {"dead@brand.de": (550, b"5.1.1 User unknown")}
            )

        def close(self):
            pass

    s._new_connection = lambda: Refusing()
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="dead@brand.de", organization="Brand")
    s.dispatch_to_contact(
        {"id": cid, "email": "dead@brand.de", "organization": "Brand"},
        "Hi",
        "<p>h</p>",
        "h",
    )

    rows = _actions(db, "EMAIL_REJECTED")
    assert len(rows) == 1 and "dead@brand.de" in rows[0]["details"]
    # And the contact must be visibly bounced, not just "failed".
    assert db.get_contact_by_id(cid)["status"] == "bounced"


def test_retry_exhaustion_is_recorded(tmp_path):
    """Giving up on a contact was previously visible only in the log file, so
    from the dashboard the contact simply stopped moving with no explanation."""
    db = Database(str(tmp_path / "r.db"))
    q = RetryQueue(db, max_retries=2, base_delay_minutes=0)
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="x@brand.de")
    log_id = db.log_email(cid, "x@brand.de", "s", "p", delivery_status="failed")

    rid = q.add_to_retry(log_id, cid, "first failure")
    q.mark_retry_failed(rid, "attempt 1 failed")
    q.mark_retry_failed(rid, "attempt 2 failed")

    rows = _actions(db, "RETRY_EXHAUSTED")
    assert len(rows) == 1
    assert "attempt 2 failed" in rows[0]["details"]


def test_a_scheduled_retry_is_not_reported_as_exhausted(tmp_path):
    db = Database(str(tmp_path / "r.db"))
    q = RetryQueue(db, max_retries=3, base_delay_minutes=0)
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="x@brand.de")
    log_id = db.log_email(cid, "x@brand.de", "s", "p", delivery_status="failed")

    rid = q.add_to_retry(log_id, cid, "first failure")
    q.mark_retry_failed(rid, "attempt 1 failed")
    assert _actions(db, "RETRY_EXHAUSTED") == []


def test_web_unsubscribe_is_recorded(tmp_path):
    """The one-click link is the primary opt-out route when BASE_URL is public,
    and it was the only suppression path leaving nothing on the dashboard."""
    from modules.unsubscribe import UnsubscribeHandler

    db = Database(str(tmp_path / "u.db"))
    db.add_contact(db.log_search("q"), "buyer@brand.de")
    h = UnsubscribeHandler(db, "secret")
    ok, _ = h.process_unsubscribe(h.generate_token("buyer@brand.de"))
    assert ok

    rows = _actions(db, "SUPPRESSED_OPTOUT")
    assert len(rows) == 1 and "buyer@brand.de" in rows[0]["details"]


def test_a_failed_send_is_transmitted_only_once(tmp_path):
    """smtplib.SMTPException subclasses OSError, so the 'reconnect once'
    handler used to catch every failure and re-send it. A refused recipient
    got asked twice, doubling the unknown-user rate; a drop after the server's
    250 delivered the mail twice."""
    db = Database(str(tmp_path / "s.db"))
    s = EmailSender(db, "smtp.test", 587, "u@corp.com", "pw")
    attempts = []

    class Refusing:
        def noop(self):
            return (250, b"ok")

        def send_message(self, msg):
            attempts.append(1)
            raise smtplib.SMTPRecipientsRefused(
                {"dead@brand.de": (550, b"5.1.1 User unknown")}
            )

        def close(self):
            pass

    s._new_connection = lambda: Refusing()
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="dead@brand.de")
    s.dispatch_to_contact({"id": cid, "email": "dead@brand.de"}, "Hi", "<p>h</p>", "h")
    assert len(attempts) == 1
