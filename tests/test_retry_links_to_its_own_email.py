"""A queued retry points at the email that actually failed.

The retry's email_log_id came from get_all_emails(limit=1): the newest row in
the WHOLE table, not this contact's. With more than one account sending, or
anything logged in between, the Retries panel showed a retry for one
prospect against another prospect's email. The comment above it said "for
this contact".
"""

from agent import OutreachAgent


def test_the_retry_links_to_this_contacts_failed_email(
    settings_factory, tmp_path, monkeypatch
):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    sid = a.db.log_search("q")
    anna = a.db.add_contact(sid, email="anna@brand.de", organization="Brand")
    marco = a.db.add_contact(sid, email="marco@other.de", organization="Other")
    rows = {}

    def dispatch(contact, **_k):
        rows["anna"] = a.db.log_email(
            anna, "anna@brand.de", "s", delivery_status="failed"
        )
        # Something else is logged before the retry is queued - another
        # account's send, a manual send, anything.
        rows["marco"] = a.db.log_email(
            marco, "marco@other.de", "s", delivery_status="sent"
        )
        # In a test both land in the same second; in production the other
        # send is later, which is what makes it "the newest row".
        a.db.conn.execute(
            "UPDATE email_log SET timestamp = datetime(timestamp, '+1 minute') "
            "WHERE id = ?",
            (rows["marco"],),
        )
        a.db.conn.commit()
        return False

    monkeypatch.setattr(a.email_sender, "dispatch_to_contact", dispatch)
    a._process_contact({"id": anna, "email": "anna@brand.de"}, dry_run=False)

    linked = a.db.conn.execute("SELECT email_log_id FROM retry_queue").fetchone()[0]
    assert linked == rows["anna"], "the retry was linked to another prospect's email"
