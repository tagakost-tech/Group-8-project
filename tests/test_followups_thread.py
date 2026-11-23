"""Follow-ups must thread to the original, not arrive as unrelated mail.

Nothing in the codebase set a Message-ID. Microsoft 365 adds one on
submission so mail still delivers, but an id we never chose is an id we
cannot reference later - so every follow-up arrived in the prospect's inbox
as a brand-new, contextless email. Three of those over a week reads as a
machine working through a list, which is precisely the impression this
project is trying to avoid.

Threading is done the honest way, with RFC 5322 headers:

    Message-ID:  set on every outgoing mail and stored against the email_log row
    In-Reply-To: the original's Message-ID, on follow-ups only
    References:  the same, which is what mail clients actually thread on

Deliberately NOT by prefixing "Re:" onto a subject the prospect never
replied to. Clients do thread on subject, so it would work - but it asserts a
conversation that did not happen, and a prospect who notices reads it as a
trick. The headers achieve the same grouping truthfully.
"""

import pytest

from modules.database import Database
from modules.email_sender import EmailSender


@pytest.fixture
def sender(tmp_path):
    return EmailSender(
        Database(str(tmp_path / "s.db")),
        "smtp.test",
        587,
        "vendite@suppleco.it",
        "pw",
        sender_name="Maria",
        company_name="SUPPLECO",
    )


# ── Every outgoing email carries an id we chose ───────────────────────────


def test_every_email_has_a_message_id(sender):
    msg = sender.create_email("buyer@brand.de", "Hi", "<p>h</p>", "h")
    assert msg["Message-ID"], "no Message-ID: nothing can reference this mail later"


def test_the_message_id_is_unique_per_email(sender):
    ids = {
        sender.create_email("buyer@brand.de", "Hi", "<p>h</p>", "h")["Message-ID"]
        for _ in range(5)
    }
    assert len(ids) == 5, f"Message-IDs collided: {ids}"


def test_the_message_id_is_on_the_sending_domain(sender):
    """A Message-ID whose domain does not match the sender is a spam signal
    filters look at. Ours must say suppleco.it."""
    msg = sender.create_email("buyer@brand.de", "Hi", "<p>h</p>", "h")
    assert msg["Message-ID"].rstrip(">").endswith("suppleco.it")


def test_the_message_id_is_well_formed(sender):
    """RFC 5322 requires the angle brackets; some filters check."""
    mid = sender.create_email("buyer@brand.de", "Hi", "<p>h</p>", "h")["Message-ID"]
    assert mid.startswith("<") and mid.endswith(">") and "@" in mid


# ── A first email is not a reply to anything ──────────────────────────────


def test_a_first_email_claims_no_thread(sender):
    msg = sender.create_email("buyer@brand.de", "Hi", "<p>h</p>", "h")
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


# ── A follow-up threads to the original ───────────────────────────────────


def test_a_follow_up_references_the_original(sender):
    original = "<abc123@suppleco.it>"
    msg = sender.create_email(
        "buyer@brand.de", "Hi", "<p>h</p>", "h", in_reply_to=original
    )
    assert msg["In-Reply-To"] == original
    assert original in msg["References"]


def test_a_follow_up_still_gets_its_own_id(sender):
    """Threading must not collapse two mails onto one identity."""
    original = "<abc123@suppleco.it>"
    msg = sender.create_email(
        "buyer@brand.de", "Hi", "<p>h</p>", "h", in_reply_to=original
    )
    assert msg["Message-ID"] and msg["Message-ID"] != original


def test_a_missing_original_does_not_produce_a_broken_header(sender):
    """A contact emailed before this feature existed has no stored id. A
    follow-up must then simply not claim a thread - never send an empty or
    literal 'None' In-Reply-To, which mail clients treat as malformed."""
    for absent in (None, "", "   "):
        msg = sender.create_email(
            "buyer@brand.de", "Hi", "<p>h</p>", "h", in_reply_to=absent
        )
        assert msg["In-Reply-To"] is None
        assert msg["References"] is None


# ── The id is persisted, or nothing can reference it ──────────────────────


class _OkSMTP:
    def noop(self):
        return (250, b"ok")

    def send_message(self, msg):
        pass

    def close(self):
        pass


def test_the_sent_message_id_is_stored_against_the_email(sender):
    sender._new_connection = lambda: _OkSMTP()
    sid = sender.db.log_search("q")
    cid = sender.db.add_contact(sid, email="buyer@brand.de", organization="Brand")
    sender.dispatch_to_contact(
        {"id": cid, "email": "buyer@brand.de", "organization": "Brand"},
        "Hi",
        "<p>h</p>",
        "h",
    )
    row = sender.db.get_all_emails(limit=1)[0]
    assert row["message_id"], "the id was generated but never stored"
    assert row["message_id"].startswith("<")


def test_the_thread_id_lookup_returns_the_first_email(tmp_path):
    """A follow-up threads to the FIRST mail in the conversation, so the whole
    sequence forms one thread rather than a chain of pairs."""
    db = Database(str(tmp_path / "t.db"))
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de")
    db.log_email(
        cid,
        "buyer@brand.de",
        "first",
        "p",
        delivery_status="sent",
        message_id="<first@suppleco.it>",
    )
    db.log_email(
        cid,
        "buyer@brand.de",
        "second",
        "p",
        delivery_status="sent",
        message_id="<second@suppleco.it>",
    )
    assert db.get_thread_message_id(cid) == "<first@suppleco.it>"


def test_a_contact_with_no_sent_email_has_no_thread(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de")
    assert db.get_thread_message_id(cid) is None


def test_an_unsent_email_does_not_start_a_thread(tmp_path):
    """A failed or dry-run row was never delivered, so referencing it would
    point at a mail the prospect never received."""
    db = Database(str(tmp_path / "t.db"))
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de")
    db.log_email(
        cid,
        "buyer@brand.de",
        "failed one",
        "p",
        delivery_status="failed",
        message_id="<never-arrived@suppleco.it>",
    )
    assert db.get_thread_message_id(cid) is None


# ── The agent must actually pass the thread id on a follow-up ─────────────


def _agent(settings_factory, tmp_path, **kw):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        delay_between_emails=0.0,
        ai_humanize_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        **kw,
    )
    return OutreachAgent(s)


def test_a_followup_is_dispatched_with_the_original_thread_id(
    settings_factory, tmp_path, monkeypatch
):
    """The end-to-end wiring. A helper nobody calls threads nothing."""
    a = _agent(settings_factory, tmp_path)
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="buyer@brand.de", organization="Brand")
    a.db.log_email(
        cid, "buyer@brand.de", "first", "p",
        delivery_status="sent", message_id="<original@suppleco.it>",
    )

    seen = {}
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **kw: seen.update(kw) or 1
    )
    a._process_contact(
        {"id": cid, "email": "buyer@brand.de", "organization": "Brand"},
        is_followup=True,
    )
    assert seen.get("in_reply_to") == "<original@suppleco.it>"


def test_a_first_email_is_dispatched_without_a_thread_id(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path)
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="new@brand.de", organization="Brand")

    seen = {}
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **kw: seen.update(kw) or 1
    )
    a._process_contact({"id": cid, "email": "new@brand.de", "organization": "Brand"})
    assert seen.get("in_reply_to") is None


def test_a_send_whose_acknowledgement_was_lost_still_starts_the_thread(tmp_path):
    """'sent_unconfirmed' is a send whose SMTP acknowledgement went missing
    after DATA. The server most likely accepted it, and the agent deliberately
    does NOT resend - so it counts as delivered everywhere else in this file
    (SENT_STATUSES). This query was written with a hardcoded = 'sent' and
    missed that rule.

    Consequence: the prospect received the first email, but the follow-up
    arrives unthreaded as a contextless new message - precisely the failure
    this feature exists to remove."""
    db = Database(str(tmp_path / "t.db"))
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de")
    db.log_email(
        cid, "buyer@brand.de", "first", "p",
        delivery_status="sent_unconfirmed", message_id="<original@suppleco.it>",
    )
    assert db.get_thread_message_id(cid) == "<original@suppleco.it>"


def test_the_thread_starts_at_the_first_delivered_mail_whatever_its_status(tmp_path):
    """The docstring promises the FIRST mail. Excluding sent_unconfirmed made
    a later, cleanly-sent touch become the thread root instead."""
    db = Database(str(tmp_path / "t.db"))
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de")
    db.log_email(
        cid, "buyer@brand.de", "first", "p",
        delivery_status="sent_unconfirmed", message_id="<first@suppleco.it>",
    )
    db.log_email(
        cid, "buyer@brand.de", "second", "p",
        delivery_status="sent", message_id="<second@suppleco.it>",
    )
    assert db.get_thread_message_id(cid) == "<first@suppleco.it>"
