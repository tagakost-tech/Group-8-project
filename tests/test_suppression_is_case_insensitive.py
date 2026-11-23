"""Suppression must not depend on how a mail server spelled the address.

add_to_suppression is the single funnel every opt-out and every hard bounce
goes through: modules/unsubscribe.py, modules/imap_monitor.py (both the bounce
and the unsubscribe branch), modules/email_sender.py on an SMTP rejection, and
modules/export.py on a GDPR erasure. None of them lowercase first - they hand
over whatever casing arrived. A bounce message echoes the address back in the
casing the remote MTA chose; a scraped address keeps the casing of the page it
came from.

So if the INSERT stores the raw string, "Info@Brand.de" lands in the table
while every reader looks for "info@brand.de". The row exists, the dashboard
counts it, the address looks suppressed - and the person is emailed again on
the next cycle. A silent GDPR Art. 21 breach that reads as a working opt-out,
which is exactly the failure mode this codebase keeps rediscovering.

The mirror-image risk is tested too: normalising must not become "match
anything". Suppression has no undo, and wrongly suppressing a live prospect
loses them permanently.
"""


def test_an_optout_arriving_in_mixed_case_still_suppresses_the_person(db):
    """A bounce or unsubscribe echoed back as 'Info@Brand.de' must suppress
    that human being, not a string that nothing will ever look up again."""
    db.add_to_suppression("Info@Brand.de", "bounce")

    assert db.is_suppressed("Info@Brand.de") is True, (
        "the address was not suppressed in the casing it arrived in"
    )
    assert db.is_suppressed("info@brand.de") is True, (
        "stored with its original casing, so the lowercase lookup every caller "
        "actually makes misses it - the prospect gets emailed again"
    )
    assert db.is_suppressed("INFO@BRAND.DE") is True


def test_a_suppressed_address_cannot_be_rediscovered_and_re_added(db):
    """Discovery scrapes the same site next month and finds the address again.
    add_contact must refuse it, or the opt-out is undone by the crawler."""
    db.add_to_suppression("Sales@Nutrabrand.it", "unsubscribe")

    sid = db.log_search("integratori alimentari", source="test")
    assert db.add_contact(sid, "sales@nutrabrand.it") is None
    assert db.add_contact(sid, "Sales@Nutrabrand.it") is None


def test_a_suppressed_contact_drops_out_of_the_send_backlog(db):
    """The consequence that actually sends an email. get_pending_contacts is
    what the agent iterates, and it excludes on LOWER(c.email) NOT IN
    (SELECT email FROM suppression_list) - a mixed-case row in that table
    matches nothing, so the person stays in the queue and is mailed again."""
    sid = db.log_search("food supplement brand", source="test")
    cid = db.add_contact(sid, "info@brand.de")
    assert any(c["id"] == cid for c in db.get_pending_contacts())

    db.add_to_suppression("Info@Brand.de", "unsubscribe")

    remaining = [c["email"] for c in db.get_pending_contacts()]
    assert "info@brand.de" not in remaining, (
        f"suppressed address is still in the send backlog: {remaining}"
    )


def test_normalising_does_not_suppress_anyone_else(db):
    """The control. Suppression has no undo, so a filter that folds too
    aggressively is worse than one that folds too little - it destroys live
    prospects. Only the address that opted out may be affected."""
    db.add_to_suppression("Info@Brand.de", "unsubscribe")

    assert db.is_suppressed("info@other-brand.de") is False
    assert db.is_suppressed("sales@brand.de") is False
    assert db.is_suppressed("info@brand.it") is False

    sid = db.log_search("q", source="test")
    still_reachable = db.add_contact(sid, "sales@brand.de")
    assert still_reachable is not None
    assert any(c["id"] == still_reachable for c in db.get_pending_contacts())
