"""Regression tests for the Batch 3 concurrency defects.

All three could deliver a cold email twice, or abort a batch mid-run.
"""

import smtplib
import sqlite3
import threading
from email.message import EmailMessage

import main
from modules.email_sender import EmailSender

# ── 1. _deliver must not re-send a message the server may have accepted ──────
# smtplib.getreply() converts ANY socket error during the post-DATA wait -
# including the 30s timeout - into SMTPServerDisconnected. The old code caught
# that and blind-retried after a reconnect, so a slow acknowledgement (AV scan,
# throttling, greylisting) delivered the message twice.


class _FakeSMTP:
    """Minimal SMTP double that records how many times a message was sent."""

    def __init__(self, fail_send_with=None, noop_code=250):
        self.sends = 0
        self.fail_send_with = fail_send_with
        self.noop_code = noop_code
        self.closed = False

    def noop(self):
        if isinstance(self.noop_code, Exception):
            raise self.noop_code
        return (self.noop_code, b"OK")

    def send_message(self, msg):
        self.sends += 1
        if self.fail_send_with:
            raise self.fail_send_with

    def close(self):
        self.closed = True

    def quit(self):
        self.closed = True


def _sender(db, fake):
    s = EmailSender(
        db, "smtp.test", 587, "me@test.de", "pw", sender_name="Me", company_name="Co"
    )
    s._smtp = fake
    s._new_connection = lambda: fake
    return s


def _deliver(sender, msg=None):
    """Call _deliver and normalise the result to 4 fields.

    _deliver returned a 3-tuple before `indeterminate` was added. Unpacking a
    fixed arity here would make these tests fail on the signature change rather
    than on behaviour, which would make them useless as evidence that the
    duplicate-send bug is fixed. Tolerating both shapes means the only way they
    can fail is if the send COUNT or the verdict is wrong.
    """
    result = sender._deliver(msg if msg is not None else EmailMessage())
    success, message, permanent = result[0], result[1], result[2]
    indeterminate = result[3] if len(result) > 3 else False
    return success, message, permanent, indeterminate


def test_disconnect_after_send_is_not_retried(db):
    """The message must be transmitted exactly once."""
    fake = _FakeSMTP(fail_send_with=smtplib.SMTPServerDisconnected("conn closed"))
    sender = _sender(db, fake)

    success, _msg, permanent, indeterminate = _deliver(sender)

    assert fake.sends == 1, f"message must be sent once, was sent {fake.sends}x"
    assert success is False
    assert indeterminate is True, "outcome is unknown, not a plain failure"
    assert permanent is False, "an unknown outcome must never suppress the address"


def test_stale_connection_is_replaced_before_anything_is_sent(db):
    """A dead pooled connection must be swapped out at the pre-flight probe,
    where reconnecting is provably safe because nothing was transmitted."""
    dead = _FakeSMTP(noop_code=smtplib.SMTPServerDisconnected("stale"))
    sender = _sender(db, dead)
    fresh = _FakeSMTP()
    sender._new_connection = lambda: fresh

    success, _msg, _perm, indeterminate = _deliver(sender)

    assert success is True
    assert indeterminate is False
    assert dead.sends == 0, "nothing may be sent on the dead connection"
    assert fresh.sends == 1, "exactly one send on the replacement"


def test_healthy_send_is_sent_once(db):
    fake = _FakeSMTP()
    sender = _sender(db, fake)
    success, _m, _p, indeterminate = _deliver(sender)
    assert (success, indeterminate, fake.sends) == (True, False, 1)


def test_recipient_refusal_is_still_permanent(db):
    """The over-correction guard: a real 5xx verdict must still suppress."""
    refused = smtplib.SMTPRecipientsRefused({"x@y.de": (550, b"user unknown")})
    fake = _FakeSMTP(fail_send_with=refused)
    sender = _sender(db, fake)

    success, _m, permanent, indeterminate = _deliver(sender)

    assert success is False
    assert permanent is True, "a 5xx recipient verdict is still permanent"
    assert indeterminate is False, "a definite verdict is not indeterminate"


# ── 2. An unconfirmed delivery must not be re-queued ─────────────────────────


def test_indeterminate_delivery_marks_the_contact_emailed(db):
    """If it might have gone out, the contact must not be picked up again."""
    fake = _FakeSMTP(fail_send_with=smtplib.SMTPServerDisconnected("lost"))
    sender = _sender(db, fake)
    sid = db.log_search("q", source="t")
    cid = db.add_contact(sid, "unconfirmed@brand.de")

    result = sender.dispatch_to_contact(
        contact={"id": cid, "email": "unconfirmed@brand.de", "name": "N"},
        subject="s",
        html_body="<p>hi</p>",
        plain_body="hi",
    )

    assert result, "must return the log id, not False, so no retry is queued"
    raw = sqlite3.connect(db.db_path)
    status = raw.execute("SELECT status FROM contacts WHERE id = ?", (cid,)).fetchone()[
        0
    ]
    log_status = raw.execute(
        "SELECT delivery_status FROM email_log WHERE contact_id = ?", (cid,)
    ).fetchone()[0]
    raw.close()

    assert status == "emailed", "an unconfirmed send must not stay pending"
    assert log_status == "sent_unconfirmed"
    assert db.get_pending_contacts() == [], "must not be re-picked by the send loop"


def test_unconfirmed_sends_still_count_as_sent(db):
    """A new delivery_status must not silently vanish from the metrics."""
    fake = _FakeSMTP(fail_send_with=smtplib.SMTPServerDisconnected("lost"))
    sender = _sender(db, fake)
    sid = db.log_search("q", source="t")
    cid = db.add_contact(sid, "counted@brand.de")
    sender.dispatch_to_contact(
        contact={"id": cid, "email": "counted@brand.de", "name": "N"},
        subject="s",
        html_body="<p>hi</p>",
        plain_body="hi",
    )
    assert db.get_stats()["emails_sent"] == 1


def test_a_failed_post_send_write_does_not_re_queue_the_contact(db):
    """Bookkeeping runs after the message is on the wire. Its failure used to
    unwind through _email_contacts, killing the batch and leaving the contact
    'pending' - so the next loop emailed the same person again."""
    fake = _FakeSMTP()
    sender = _sender(db, fake)
    sid = db.log_search("q", source="t")
    cid = db.add_contact(sid, "writefail@brand.de")

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    db.update_contact_status = _boom

    # Must not raise: a delivered message may not abort the batch.
    sender.dispatch_to_contact(
        contact={"id": cid, "email": "writefail@brand.de", "name": "N"},
        subject="s",
        html_body="<p>hi</p>",
        plain_body="hi",
    )
    assert fake.sends == 1


# ── 3. Flask double-start race ───────────────────────────────────────────────
# The guard and the thread start were not atomic and Flask runs threaded, so two
# near-simultaneous POSTs both passed and two campaign loops ran against the same
# pending set. run_agent also read `agent` late, so both drove the same instance.


def test_concurrent_start_requests_launch_one_agent(
    tmp_path, settings_factory, monkeypatch
):
    s = settings_factory(db_path=str(tmp_path / "race.db"))
    built, started = [], []

    release = threading.Event()

    class _FakeAgent:
        def __init__(self, *a, **kw):
            built.append(self)

        def run_continuous(self, *a, **kw):
            started.append(self)
            release.wait(timeout=5)  # hold the thread so is_alive() is true

        def run_single(self, *a, **kw):
            started.append(self)
            release.wait(timeout=5)

        def stop(self):
            release.set()

    monkeypatch.setattr(main, "OutreachAgent", _FakeAgent)
    client = main.create_dashboard_app(s).test_client()

    codes, barrier = [], threading.Barrier(2)

    def fire():
        barrier.wait()
        codes.append(
            client.post("/api/agent/start", json={"mode": "continuous"}).status_code
        )

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    release.set()
    for t in threads:
        t.join(timeout=5)

    assert sorted(codes) == [200, 409], f"exactly one start may win, got {codes}"
    assert len(started) == 1, f"only one loop may run, {len(started)} did"
