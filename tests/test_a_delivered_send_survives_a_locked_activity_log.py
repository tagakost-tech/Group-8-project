"""Once an email is out, a failed activity-log write cannot unsend it.

dispatch_to_contact contains the status writes it makes after delivery - a
delivered message must never look unsent because a row could not be written -
but the EMAIL_SENT / EMAIL_UNCONFIRMED activity rows right after them sat
outside that handler. With the database locked, the status write was
contained and the log write then raised anyway: the exception reached the
campaign loop and ended the batch, a retry stayed claimed and was later sent
again, and Direct Send told the operator "Send failed" for an email already in
the prospect's inbox, inviting a second one.
"""

import sqlite3

import pytest

from modules.database import Database
from modules.email_sender import EmailSender


@pytest.fixture
def sender(tmp_path, monkeypatch):
    s = EmailSender(
        Database(str(tmp_path / "s.db")), "smtp.test", 465, "u@test.com", "pw"
    )
    real = s.db.log_activity

    def locked_after_send(action, *a, **k):
        if action in ("EMAIL_SENT", "EMAIL_UNCONFIRMED"):
            raise sqlite3.OperationalError("database is locked")
        return real(action, *a, **k)

    monkeypatch.setattr(s.db, "log_activity", locked_after_send)
    return s


@pytest.mark.parametrize(
    "outcome",
    [(True, "250 OK", False, False), (False, "dropped mid-send", False, True)],
    ids=["sent", "unconfirmed"],
)
def test_the_send_is_still_reported_as_out(sender, monkeypatch, outcome):
    db = sender.db
    cid = db.add_contact(db.log_search("q"), "buyer@brand.de")
    monkeypatch.setattr(sender, "_deliver", lambda msg, blocked=None: outcome)
    result = sender.dispatch_to_contact(
        contact=db.get_contact_by_id(cid),
        subject="s",
        html_body="<p>h</p>",
        plain_body="h",
    )
    assert result, "a delivered email was reported as not sent"
