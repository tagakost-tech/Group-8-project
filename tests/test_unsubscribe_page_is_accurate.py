"""The unsubscribe confirmation says what actually happened.

It said "Your email address has been removed from our mailing list". The
address is in fact KEPT - on the suppression list, which is what guarantees
it is never emailed again (GDPR Art. 17(3), 21(3)). Telling someone their
data was removed when it was retained is the kind of inaccuracy Art. 12
transparency forbids, and the kind a recipient checks with a subject-access
request.
"""

import main
from modules.unsubscribe import UnsubscribeHandler


def test_the_page_does_not_claim_removal():
    page = UnsubscribeHandler.CONFIRMATION_HTML
    assert "removed from our mailing list" not in page
    assert "do-not-contact list" in page


def test_the_real_route_serves_it(settings_factory, tmp_path):
    s = settings_factory(
        db_path=str(tmp_path / "u.db"), unsubscribe_secret="a-test-secret-value"
    )
    client = main.create_dashboard_app(s).test_client()
    db = main.Database(s.get_abs_db_path())
    token = UnsubscribeHandler(db, s.unsubscribe_secret).generate_token("x@brand.de")
    body = client.get(f"/unsubscribe/{token}").get_data(as_text=True)
    assert "do-not-contact list" in body


def test_a_link_we_cannot_match_promises_nothing(settings_factory, tmp_path):
    """A link that matches nobody - for an address retention has since removed,
    or not one of ours - records nothing. The page still said "You have been
    unsubscribed. We will not email you again." (found while finishing step
    G20)."""
    from modules.export import anonymise_contact_rows

    s = settings_factory(
        db_path=str(tmp_path / "n.db"), unsubscribe_secret="a-test-secret-value"
    )
    client = main.create_dashboard_app(s).test_client()
    db = main.Database(s.get_abs_db_path())
    db.add_contact(db.log_search("q"), "x@brand.de")
    removed = UnsubscribeHandler(db, s.unsubscribe_secret).generate_token("x@brand.de")
    anonymise_contact_rows(db, "x@brand.de", discard_reason="retention: no reply")
    db.conn.commit()
    for token in (removed, "u" + "0" * 32):
        body = client.get(f"/unsubscribe/{token}").get_data(as_text=True)
        assert "will not email you again" not in body
        assert "unsubscribe link in it" in body
    assert not db.is_suppressed("x@brand.de")
