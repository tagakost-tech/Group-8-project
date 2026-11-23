"""A bounce read from the inbox marks the contact as bounced.

A hard bounce arrives by one of two routes. Rejected during the SMTP
conversation, the sender suppresses the address AND sets the contact to
'bounced'. Returned later as a delivery-status notification in the inbox,
the IMAP monitor only suppressed it - the contact kept saying 'emailed', so
the dashboard showed a dead address as a live prospect.
"""

from conftest import sent_to
from modules.database import Database
from modules.imap_monitor import ImapMonitor


def _monitor(tmp_path):
    return ImapMonitor(
        Database(str(tmp_path / "i.db")), "imap.test", 993, "me@corp.com", "pw"
    )


def _contact(db, email, status):
    sid = db.log_search("q")
    cid = db.add_contact(sid, email=email, organization="Brand")
    db.update_contact_status(cid, status)
    return cid


def test_a_dsn_bounce_marks_the_contact_bounced(tmp_path):
    m = _monitor(tmp_path)
    cid = _contact(m.db, "dead@brand.de", "emailed")
    sent_to(m.db, "dead@brand.de")
    m.process_bounces([{"email": "dead@brand.de", "subject": "Undeliverable"}])
    assert m.db.get_contact_by_id(cid)["status"] == "bounced"


def test_a_contact_who_replied_keeps_that_status(tmp_path):
    """Replied is the more important fact about a contact. A late DSN for an
    alias must not overwrite it."""
    m = _monitor(tmp_path)
    cid = _contact(m.db, "anna@brand.de", "responded")
    sent_to(m.db, "anna@brand.de")
    m.process_bounces([{"email": "anna@brand.de", "subject": "Undeliverable"}])
    assert m.db.get_contact_by_id(cid)["status"] == "responded"


def test_a_bounce_for_an_address_the_database_no_longer_holds_is_suppressed(
    tmp_path,
):
    """A send a restore kept only by the address's token (prior_sends)."""
    m = _monitor(tmp_path)
    sent_to(m.db, "ghost@brand.de")
    assert m.db.get_contact_by_email("ghost@brand.de") is None
    m.process_bounces([{"email": "ghost@brand.de", "subject": "Undeliverable"}])
    assert m.db.is_suppressed("ghost@brand.de")
