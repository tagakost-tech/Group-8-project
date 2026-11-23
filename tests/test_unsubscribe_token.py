"""Tests for the signed one-click unsubscribe token.

The token is the only thing standing between the public unsubscribe URL and
anyone who can guess an address: without a valid signature check, a stranger
could suppress a company's whole prospect list by iterating base64(email).
Replacing the hmac comparison with `True` previously went undetected.
"""

import base64

from modules.database import Database
from modules.unsubscribe import UnsubscribeHandler


def _handler(tmp_path, secret="test-secret"):
    return UnsubscribeHandler(Database(str(tmp_path / "u.db")), secret)


def _forge(email, signature=b"anything"):
    """A token whose payload is real but whose signature was never signed."""
    return f"{base64.urlsafe_b64encode(email.encode()).decode()}.{base64.urlsafe_b64encode(signature).decode()}"


def _legacy(email, secret="test-secret"):
    """A token in the form used before step G20: base64(address).signature,
    correctly signed. No email carrying one was ever sent."""
    import hashlib
    import hmac

    sig = hmac.new(secret.encode(), email.encode(), hashlib.sha256).digest()
    return (
        f"{base64.urlsafe_b64encode(email.encode()).decode()}."
        f"{base64.urlsafe_b64encode(sig).decode()}"
    )


def test_valid_token_round_trips(tmp_path):
    h = _handler(tmp_path)
    h.db.add_contact(h.db.log_search("q"), "buyer@brand.de")
    assert h.verify_token(h.generate_token("buyer@brand.de")) == "buyer@brand.de"


def test_a_correctly_signed_token_in_the_old_form_is_refused(tmp_path):
    """The old form carried the address itself, so whoever holds
    UNSUBSCRIBE_SECRET could make a working link for ANY address, emailed or
    not. No such link was ever sent, so none is honoured."""
    h = _handler(tmp_path)
    h.db.add_contact(h.db.log_search("q"), "buyer@brand.de")
    h.generate_token("buyer@brand.de")
    assert h.verify_token(_legacy("buyer@brand.de")) is None
    ok, _ = h.process_unsubscribe(_legacy("buyer@brand.de"), one_click=True)
    assert ok is False
    assert h.db.is_suppressed("buyer@brand.de") is False
    assert not h.db.company_blocked("other@brand.de")


def test_forged_signature_is_rejected(tmp_path):
    h = _handler(tmp_path)
    assert h.verify_token(_forge("buyer@brand.de")) is None


def test_forged_token_does_not_suppress(tmp_path):
    """The end that matters: a rejected token must not reach the DB write."""
    h = _handler(tmp_path)
    ok, _ = h.process_unsubscribe(_forge("victim@brand.de"))
    assert ok is False
    assert h.db.is_suppressed("victim@brand.de") is False


def test_payload_cannot_be_swapped_under_a_valid_signature(tmp_path):
    """Take a legitimately signed token and point it at someone else - the
    signature covers the address, so it must stop matching."""
    h = _handler(tmp_path)
    _payload, sig = _legacy("mine@brand.de").split(".")
    swapped = "{}.{}".format(base64.urlsafe_b64encode(b"victim@brand.de").decode(), sig)
    assert h.verify_token(swapped) is None


def test_token_from_another_secret_is_rejected(tmp_path):
    """A token minted by a different deployment must not be honoured by this
    one. (A link this deployment recorded stays valid after its key changes:
    tests/test_a_held_click_is_never_lost.py.)"""
    ours = _handler(tmp_path, secret="test-secret")
    ours.generate_token("buyer@brand.de")
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    attacker = _handler(theirs, secret="not-our-secret")
    assert ours.verify_token(attacker.generate_token("victim@brand.de")) is None


def test_malformed_tokens_are_rejected_without_crashing(tmp_path):
    h = _handler(tmp_path)
    for bad in ("", ".", "no-dot", "a.b.c", "!!!.???", "x." + "A" * 64):
        assert h.verify_token(bad) is None, bad


def test_unsubscribe_cancels_the_sequence_for_a_mixed_case_contact(tmp_path):
    """The bug this guards: the address was scraped as 'Info@Brand.de', the
    suppression list stores it lowercased, but the contact lookup used the raw
    casing and found nothing - so the person was suppressed yet stayed enrolled
    in a live follow-up sequence and kept receiving mail (GDPR Art. 21)."""
    from modules.sequences import SequenceEngine

    h = _handler(tmp_path)
    sid = h.db.log_search("q")
    # Insert with the source page's casing, bypassing add_contact's normaliser
    # exactly as a pre-normalisation row in a live DB would look.
    h.db.conn.execute(
        "INSERT INTO contacts (search_id, email, organization) VALUES (?,?,?)",
        (sid, "Info@Brand.de", "Brand"),
    )
    h.db.conn.commit()
    contact_id = h.db.conn.execute(
        "SELECT id FROM contacts WHERE email = 'Info@Brand.de'"
    ).fetchone()[0]

    seq = SequenceEngine(db=h.db)
    seq_id = seq.create_sequence(
        "followups",
        [{"delay_days": 3, "subject": "Following up", "template_name": None}],
    )
    seq.enroll_contact(contact_id, seq_id)

    # The unsubscribe arrives in a DIFFERENT casing from the stored row - the
    # normal case, since the suppression list and every List-Unsubscribe path
    # normalise to lowercase while the contact keeps the source page's casing.
    #
    # This previously used "Info@Brand.de" on BOTH sides, so the old
    # case-SENSITIVE `WHERE email = ?` matched perfectly and the test passed
    # against the very code it was written to condemn. A mismatch is the whole
    # bug; without one there is nothing to detect.
    ok, _ = h.process_unsubscribe(h.generate_token("info@brand.de"))
    assert ok

    status = h.db.conn.execute(
        "SELECT status FROM sequence_enrollments WHERE contact_id = ?", (contact_id,)
    ).fetchone()[0]
    assert status == "cancelled"
    assert h.db.get_contact_by_id(contact_id)["status"] == "unsubscribed"
