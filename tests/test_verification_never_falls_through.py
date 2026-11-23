"""No verdict is not a pass.

Verification is computed inside a try and acted on outside it, so that a locked
database cannot stop a "no MX record" verdict from being applied. That left
verify_result as None when verify_email() itself raised - and all three guards
below it are written `if verify_result is not None and ...`, so every one of
them was skipped and the contact fell through to qualified.append().

The result: verification crashed, and the contact was emailed THIS pass,
unverified, rather than being left pending for a later one. The comment in the
except block said the opposite ("the lead is kept for a later pass"), which is
how it survived review - the stated intent and the control flow disagreed.
"""

import pytest


class _Boom:
    """A verifier that fails the way the real one can: a socket error out of
    smtplib (an OSError), or an address the parser rejects (a ValueError)."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def verify_email(self, email, skip_smtp=True):
        self.calls += 1
        raise self.exc


def _agent(tmp_path, settings_factory):
    from agent import OutreachAgent

    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "v.db"),
            verify_mx=True,
            min_lead_score_to_email=0,
        )
    )
    a._running = True
    a.relevance_filter = None
    a.lead_scorer = None
    a.client_register = None
    return a


@pytest.mark.parametrize(
    "exc",
    [
        OSError("connection reset by peer"),
        ValueError("not a valid address"),
        UnicodeEncodeError("ascii", "b\xfcro@x.de", 1, 2, "ordinal not in range"),
    ],
)
def test_a_crashing_verifier_does_not_qualify_the_contact(
    tmp_path, settings_factory, exc
):
    a = _agent(tmp_path, settings_factory)
    a.email_verifier = _Boom(exc)
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "someone@brand.de", organization="Brand")

    qualified = a._qualify_contacts(
        [{"id": cid, "email": "someone@brand.de", "organization": "Brand"}]
    )

    assert a.email_verifier.calls == 1, "the verifier was never consulted"
    assert qualified == [], (
        "verification crashed and the contact was queued for sending anyway - "
        "an unverified address emailed on the strength of a failed check"
    )


def test_the_contact_stays_pending_for_another_attempt(tmp_path, settings_factory):
    """It must not be retired either. 'unverified' is terminal - nothing moves
    a contact back out of it - and a transient socket error is not evidence
    that an address is bad. 235 good contacts were retired that way once."""
    a = _agent(tmp_path, settings_factory)
    a.email_verifier = _Boom(OSError("temporary failure in name resolution"))
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "someone@brand.de")

    a._qualify_contacts([{"id": cid, "email": "someone@brand.de"}])

    status = a.db.get_contact_by_id(cid)["status"]
    assert status != "unverified", (
        "a transient verification failure retired the contact permanently"
    )


def test_a_working_verifier_still_qualifies(tmp_path, settings_factory):
    """Control: without this, refusing everything would pass the tests above."""

    class _Ok:
        def verify_email(self, email, skip_smtp=True):
            return {"valid": True, "reason": "mx ok", "indeterminate": False}

    a = _agent(tmp_path, settings_factory)
    a.email_verifier = _Ok()
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "good@brand.de")

    qualified = a._qualify_contacts([{"id": cid, "email": "good@brand.de"}])

    assert len(qualified) == 1


def test_an_invalid_verdict_is_still_a_discard(tmp_path, settings_factory):
    """The other control: a real 'this address does not exist' must still be
    acted on, and recorded, rather than being swept into 'try again later'."""

    class _Invalid:
        def verify_email(self, email, skip_smtp=True):
            return {"valid": False, "reason": "no MX record", "indeterminate": False}

    a = _agent(tmp_path, settings_factory)
    a.email_verifier = _Invalid()
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "nobody@nowhere.invalid")

    qualified = a._qualify_contacts([{"id": cid, "email": "nobody@nowhere.invalid"}])

    assert qualified == []
    assert a.db.get_contact_by_id(cid)["status"] == "unverified"
