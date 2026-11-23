"""A connection the server closes before the message is sent is a failure.

_deliver treated every SMTPServerDisconnected as "lost mid-send, delivery
unconfirmed", because the handler assumed it could only come from
send_message. smtplib raises the same exception when the server closes the
connection while it is being opened - greeting, EHLO, STARTTLS, LOGIN - and
on the pre-flight reconnect, when nothing has been transmitted. An
unconfirmed send is recorded as 'sent_unconfirmed' and the contact as
'emailed': warm-up and domain counts went up, the follow-up sequence was
enrolled, and the next follow-up was threaded under a message that never
left. Only a drop during send_message is unconfirmed now.
"""

import smtplib
from email.message import EmailMessage

import pytest

from modules.database import Database
from modules.email_sender import EmailSender


def _closed(*_a, **_k):
    raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


class _Conn:
    def __init__(self, noop_ok=True, send_drops=False):
        self.noop_ok, self.send_drops = noop_ok, send_drops

    def noop(self):
        if not self.noop_ok:
            _closed()
        return (250, b"OK")

    def send_message(self, msg):
        if self.send_drops:
            _closed()

    def quit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def sender(tmp_path):
    return EmailSender(
        Database(str(tmp_path / "s.db")), "smtp.test", 465, "u@test.com", "pw"
    )


def test_a_connection_closed_while_opening_is_a_failure(sender, monkeypatch):
    monkeypatch.setattr(sender, "_ensure_connection", _closed)
    ok, _msg, _permanent, indeterminate = sender._deliver(EmailMessage())
    assert ok is False
    assert indeterminate is False, "nothing was sent, yet it counts as maybe sent"


def test_a_reconnect_the_server_closes_is_a_failure(sender, monkeypatch):
    calls = []

    def ensure():
        calls.append(1)
        if len(calls) == 1:
            return _Conn(noop_ok=False)  # stale pooled connection
        _closed()  # and the fresh one is refused

    monkeypatch.setattr(sender, "_ensure_connection", ensure)
    _ok, _msg, _permanent, indeterminate = sender._deliver(EmailMessage())
    assert indeterminate is False


def test_the_contact_is_not_recorded_as_emailed(sender, monkeypatch):
    db = sender.db
    cid = db.add_contact(db.log_search("q"), "buyer@brand.de")
    monkeypatch.setattr(sender, "_ensure_connection", _closed)
    result = sender.dispatch_to_contact(
        contact=db.get_contact_by_id(cid),
        subject="s",
        html_body="<p>h</p>",
        plain_body="h",
    )
    assert not result
    assert db.get_contact_by_id(cid)["status"] != "emailed"
    status = db.conn.execute("SELECT delivery_status FROM email_log").fetchone()[0]
    assert status != "sent_unconfirmed"


def test_a_drop_during_the_send_is_still_unconfirmed(sender, monkeypatch):
    """The control: the message may be queued, so it must not be resent."""
    monkeypatch.setattr(sender, "_ensure_connection", lambda: _Conn(send_drops=True))
    _ok, _msg, _permanent, indeterminate = sender._deliver(EmailMessage())
    assert indeterminate is True
