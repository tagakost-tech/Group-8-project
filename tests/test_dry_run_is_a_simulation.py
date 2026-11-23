"""A dry run must change nothing you would not want changed.

dispatch_to_contact set contacts.status = 'dry_run' when simulating a send.
Nothing in the codebase reads that value - it is a dead end - but
get_pending_contacts selects only status='pending', and nothing anywhere moves
a contact back. So one `python main.py run --dry-run` against a live database
silently made every contact it touched permanently unsendable, with no error
and no visible symptom.

One real contact was already stuck this way in the production database.
"""

import sqlite3

from modules.email_sender import EmailSender


def _sender(db):
    return EmailSender(
        db=db,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="s@example.com",
        smtp_password="pw",
        sender_name="S",
    )


def _contact(db, email="prospect@brand.de"):
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, email)
    return cid


def _status(db, cid):
    raw = sqlite3.connect(db.db_path)
    row = raw.execute("SELECT status FROM contacts WHERE id = ?", (cid,)).fetchone()
    raw.close()
    return row[0]


def test_a_dry_run_leaves_the_contact_pending(db):
    """The behaviour that matters: the contact must still be selectable for a
    real send afterwards."""
    cid = _contact(db)
    assert _status(db, cid) == "pending"

    _sender(db).dispatch_to_contact(
        contact={"id": cid, "email": "prospect@brand.de"},
        subject="s",
        html_body="<p>x</p>",
        plain_body="x",
        pdf_path=None,
        dry_run=True,
    )

    assert _status(db, cid) == "pending", (
        "a dry run changed the contact's status - get_pending_contacts selects "
        "only 'pending', so this contact can never be emailed again"
    )


def test_a_dry_run_contact_is_still_in_the_pending_queue(db):
    """Asserted through the query the agent actually uses, not just the column."""
    cid = _contact(db, "queued@brand.de")

    _sender(db).dispatch_to_contact(
        contact={"id": cid, "email": "queued@brand.de"},
        subject="s",
        html_body="<p>x</p>",
        plain_body="x",
        pdf_path=None,
        dry_run=True,
    )

    pending = [c["id"] for c in db.get_pending_contacts()]
    assert cid in pending, "the contact dropped out of the send queue"


def test_a_dry_run_is_still_recorded_in_the_email_log(db):
    """Guard: the simulation must remain visible. The email_log row is the
    right place for it - that is what the Emails table lists."""
    cid = _contact(db, "logged@brand.de")

    _sender(db).dispatch_to_contact(
        contact={"id": cid, "email": "logged@brand.de"},
        subject="s",
        html_body="<p>x</p>",
        plain_body="x",
        pdf_path=None,
        dry_run=True,
    )

    raw = sqlite3.connect(db.db_path)
    statuses = [
        r[0]
        for r in raw.execute(
            "SELECT delivery_status FROM email_log WHERE contact_id = ?", (cid,)
        )
    ]
    raw.close()
    assert "dry_run" in statuses, "the simulated send was not logged at all"


def test_nothing_reads_a_dry_run_contact_status():
    """The status was a dead end: written in one place, read nowhere. If a
    future change starts depending on it, this test says so before the
    dead-end behaviour is reintroduced."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for path in list(root.glob("*.py")) + list((root / "modules").glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "update_contact_status" in line and "dry_run" in line:
                hits.append(f"{path.name}:{i}")
    assert not hits, f"a dry run is writing contact status again: {hits}"
