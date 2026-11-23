"""A first email is not sent with a hole in its text.

SUPPLECO's first email says two things the product has to make true. "I am
responsible for <title> at Suppleco": with SENDER_TITLE blank the sentence would
go out as "responsible for  at Suppleco". And "Please find attached our Company
Profile": with the PDF missing the email went out anyway, with a line in the
log saying "PDF not found" and nothing attached.

Both now refuse the live first email, in the way a blank legal identity does:
nothing is sent, the reason is recorded once, the contact stays pending and a
queued retry keeps its attempts, so fixing the setting loses nothing. A dry
run still composes the email, with "[set SENDER_TITLE]" where the title would
be, and says that the PDF is missing. Follow-ups carry neither, and go.
doctor reports both, and that both first-email templates are in place.
"""

import logging
import shutil
from pathlib import Path

import pytest

import main
from agent import OutreachAgent
from modules.file_handler import FileHandler

ROOT = Path(__file__).resolve().parent.parent
TO = {"it": "acquisti@rossi-integratori.it", "en": "einkauf@brand.de"}
SENTENCE = {
    "it": "Mi chiamo Giulia Bianchi e mi occupo di {title} in Suppleco.",
    "en": "My name is Giulia Bianchi and I am responsible for  {title} at Suppleco.",
}
# An operator's own first email, which does not say what its sender does.
NO_TITLE = (
    "<html><body><p>Hello from {{sender_name}}.</p>"
    '<p><a href="{{unsubscribe_url}}">unsubscribe here</a></p>'
    "{{legal_footer}}</body></html>"
)


class _Wire:
    """An SMTP session that keeps each message it is handed, so no test opens
    a real one."""

    def __init__(self, failures=0):
        self.sent = []
        self.failures = failures

    def noop(self):
        return (250, b"ok")

    def send_message(self, msg):
        if self.failures:
            self.failures -= 1
            raise OSError("the connection was reset")
        self.sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


def _agent(settings_factory, tmp_path, failures=0, **kw):
    """The real agent and send path, down to the SMTP session, which keeps
    the messages it is handed."""
    settings = {
        "db_path": str(tmp_path / "a.db"),
        "verify_emails": False,
        "send_window_enabled": False,
        "blacklist_check_enabled": False,
        "bounce_guard_enabled": False,
        "imap_enabled": False,
        "smtp_username": "outreach@suppleco.example",
        "smtp_password": "pw",
        "sender_name": "Giulia Bianchi",
        "company_name": "Suppleco",
    }
    a = OutreachAgent(settings_factory(**{**settings, **kw}))
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    wire = _Wire(failures)
    for _key, _name, mailbox in a.senders:
        mailbox._new_connection = lambda: wire
    return a, wire.sent


def _stored(a, email, organization="Rossi Integratori"):
    cid = a.db.add_contact(
        a.db.log_search("q"), email=email, name="Maria Rossi", organization=organization
    )
    return a.db.get_contact_by_id(cid)


def _status(a, contact):
    return a.db.get_contact_by_id(contact["id"])["status"]


def _activity(a, action):
    return [r for r in a.db.get_recent_activity(100) if r["action"] == action]


def _plain(msg):
    return msg.get_body(("plain",)).get_content()


def _attached(msg):
    return [
        (p.get_filename(), p.get_payload(decode=True)) for p in msg.iter_attachments()
    ]


def _a_failed_first_email(a, wire, contact):
    """A first email the server did not take, waiting in the retry queue."""
    assert a._process_contact(contact, dry_run=False) is False
    assert wire == []
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    a.db.conn.commit()
    assert _retries(a) == [("pending", 0)]


def _retries(a):
    return [
        tuple(r) for r in a.db.conn.execute("SELECT status, attempt FROM retry_queue")
    ]


def _due(a):
    a.db.conn.execute(
        "UPDATE sequence_enrollments SET enrolled_at = '2000-01-01 00:00:00',"
        " last_sent_at = '2000-01-01 00:00:00'"
    )
    a.db.conn.commit()


# ── A blank title is never sent ────────────────────────────────────────────


@pytest.mark.parametrize("language", ["it", "en"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_a_live_first_email_without_a_title_is_refused(
    settings_factory, tmp_path, language, blank
):
    a, wire = _agent(settings_factory, tmp_path, sender_title=blank)
    contact = _stored(a, TO[language])
    assert a._process_contact(contact, dry_run=False) is False
    assert wire == [], "a first email went out with a hole where the title goes"
    # Refused, not failed: nothing to retry, and the contact still waits.
    assert _status(a, contact) == "pending"
    assert _retries(a) == []
    assert a._stats["emails_failed"] == 0


def test_the_refusal_is_recorded_once_and_names_the_setting(settings_factory, tmp_path):
    """One clear line on the dashboard, not one per prospect in the backlog."""
    a, wire = _agent(settings_factory, tmp_path, sender_title="")
    for i in range(3):
        a._process_contact(_stored(a, f"buyer{i}@brand{i}.de"), dry_run=False)
    rows = _activity(a, "SENDER_TITLE_MISSING")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert "SENDER_TITLE" in rows[0]["details"]
    assert wire == []


@pytest.mark.parametrize("language", ["it", "en"])
def test_a_dry_run_shows_where_the_title_goes(settings_factory, tmp_path, language):
    """A dry run is how the operator reads the email before it is live."""
    a, wire = _agent(settings_factory, tmp_path, sender_title="")
    composed = []
    real = a.email_sender.dispatch_to_contact
    a.email_sender.dispatch_to_contact = lambda **k: composed.append(k) or real(**k)
    assert a._process_contact(_stored(a, TO[language]), dry_run=True)
    marked = SENTENCE[language].format(title="[set SENDER_TITLE]")
    assert marked in composed[0]["plain_body"].splitlines()
    assert marked in composed[0]["html_body"]
    # The signature leaves a missing title out; it does not print the marker.
    assert composed[0]["plain_body"].count("[set SENDER_TITLE]") == 1
    assert "Giulia Bianchi<br>Suppleco</p>" in composed[0]["html_body"]
    assert wire == []
    assert _activity(a, "SENDER_TITLE_MISSING") == []


def test_direct_send_says_which_setting_is_missing(settings_factory, tmp_path):
    a, wire = _agent(settings_factory, tmp_path, sender_title="")
    ok, message = a.send_single_email(TO["en"], channel="referral")
    assert ok is False
    assert "SENDER_TITLE" in message
    assert wire == []
    assert a.db.get_contact_by_email(TO["en"]) is None, (
        "a refused send stored a contact"
    )
    ok, message = a.send_single_email(TO["en"], channel="referral", dry_run=True)
    assert ok, message


def test_the_campaign_stops_instead_of_walking_the_backlog(settings_factory, tmp_path):
    a, wire = _agent(settings_factory, tmp_path, sender_title="")
    contacts = [_stored(a, f"buyer{i}@brand{i}.de") for i in range(3)]
    seen = []
    real = a._process_contact
    a._process_contact = lambda c, *x, **k: seen.append(c) or real(c, *x, **k)
    a._email_contacts(contacts, dry_run=False)
    assert seen == [], "the loop went contact by contact into the same refusal"
    assert wire == []
    assert {_status(a, c) for c in contacts} == {"pending"}


def test_a_retry_keeps_its_attempts_while_the_title_is_missing(
    settings_factory, tmp_path
):
    """The retry queue marks a row in flight and a refused send then counts
    as a failed attempt: three refusals and the lead is gone."""
    a, wire = _agent(settings_factory, tmp_path, failures=1)
    contact = _stored(a, TO["en"])
    _a_failed_first_email(a, wire, contact)
    a.settings.sender_title = ""
    for _key, _name, mailbox in a.senders:
        mailbox.sender_title = ""
    a._process_retries()
    assert wire == []
    assert _retries(a) == [("pending", 0)]


def test_a_template_that_does_not_say_it_needs_no_title(settings_factory, tmp_path):
    """An operator's own first email keeps going out as it did."""
    a, wire = _agent(settings_factory, tmp_path, sender_title="")
    a._email_template = NO_TITLE
    assert a._process_contact(_stored(a, TO["en"]), dry_run=False)
    assert "Hello from Giulia Bianchi." in _plain(wire[0])
    # The Italian text still says it, so it is still refused, on its own.
    assert a._process_contact(_stored(a, TO["it"]), dry_run=False) is False
    assert len(wire) == 1
    assert len(_activity(a, "SENDER_TITLE_MISSING")) == 1


def test_a_mailbox_with_its_own_title_sends_while_the_shared_one_is_blank(
    settings_factory, tmp_path
):
    a, wire = _agent(
        settings_factory,
        tmp_path,
        sender_title="",
        sender_2_email="marco@suppleco.example",
        sender_2_password="pw2",
        sender_2_name="Marco Ferri",
        sender_2_title="Export Sales",
    )
    (_k1, _n1, first), (key, _n2, second) = a.senders
    assert a._process_contact(_stored(a, "a@brand1.de"), sender=first) is False
    assert a._process_contact(_stored(a, "b@brand2.de"), sender=second, account_key=key)
    assert len(wire) == 1
    assert "I am responsible for  Export Sales at Suppleco." in _plain(wire[0])


def _two_mailboxes(settings_factory, tmp_path, failures=0):
    """The shared title blank, mailbox 2 with its own."""
    return _agent(
        settings_factory,
        tmp_path,
        failures=failures,
        sender_title="",
        sender_2_email="marco@suppleco.example",
        sender_2_password="pw2",
        sender_2_name="Marco Ferri",
        sender_2_title="Export Sales",
    )


def test_the_campaign_sends_every_first_email_through_the_titled_mailbox(
    settings_factory, tmp_path
):
    """One mailbox without a title does not stop the batch, and does not take
    its turn in the rotation: nobody waits for a mailbox that cannot say it."""
    a, wire = _two_mailboxes(settings_factory, tmp_path)
    contacts = [_stored(a, f"buyer{i}@brand{i}.de") for i in range(4)]
    a._email_contacts(contacts, dry_run=False)
    assert len(wire) == 4, "a titled mailbox could have sent them all"
    assert all("responsible for  Export Sales at" in _plain(m) for m in wire)
    assert {_status(a, c) for c in contacts} == {"emailed"}


def test_the_campaign_leaves_contacts_waiting_while_only_the_untitled_mailbox_has_allowance(
    settings_factory, tmp_path
):
    a, wire = _two_mailboxes(settings_factory, tmp_path)
    key, _name, _second = a.senders[1]
    a.warmup.can_send = lambda account="": account != key
    contacts = [_stored(a, f"buyer{i}@brand{i}.de") for i in range(3)]
    a._email_contacts(contacts, dry_run=False)
    assert wire == []
    assert {_status(a, c) for c in contacts} == {"pending"}
    assert a._stats["emails_failed"] == 0


def test_a_retry_goes_through_the_titled_mailbox_without_spending_an_attempt(
    settings_factory, tmp_path
):
    """The retry queue counts a refused send as a failed attempt, so a retry
    that lands on the untitled mailbox lost one each time it did."""
    a, wire = _two_mailboxes(settings_factory, tmp_path, failures=1)
    key, _name, second = a.senders[1]
    contact = _stored(a, TO["en"])
    assert (
        a._process_contact(contact, dry_run=False, sender=second, account_key=key)
        is False
    )
    assert wire == []
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    a.db.conn.commit()
    a._process_retries()
    assert len(wire) == 1
    assert "responsible for  Export Sales at" in _plain(wire[0])
    assert _retries(a) == [("succeeded", 1)]


def test_a_retry_waits_while_only_the_untitled_mailbox_has_allowance(
    settings_factory, tmp_path
):
    a, wire = _two_mailboxes(settings_factory, tmp_path, failures=1)
    key, _name, second = a.senders[1]
    contact = _stored(a, TO["en"])
    assert (
        a._process_contact(contact, dry_run=False, sender=second, account_key=key)
        is False
    )
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    a.db.conn.commit()
    a.warmup.can_send = lambda account="": account != key
    for _round in range(3):
        a._process_retries()
    assert wire == []
    assert _retries(a) == [("pending", 0)]


def test_follow_ups_go_without_a_title(settings_factory, tmp_path):
    """They do not say what the sender is responsible for."""
    a, wire = _agent(settings_factory, tmp_path, sequences_enabled=True)
    contact = _stored(a, TO["it"])
    assert a._process_contact(contact, dry_run=False)
    a.settings.sender_title = ""
    for _key, _name, mailbox in a.senders:
        mailbox.sender_title = ""
    _due(a)
    a._process_sequences(dry_run=False)
    assert len(wire) == 2
    assert "Mi permetto di dare seguito" in _plain(wire[1])


def test_with_the_title_set_the_same_contact_goes(settings_factory, tmp_path):
    a, wire = _agent(settings_factory, tmp_path, sender_title="Export Sales")
    assert a._process_contact(_stored(a, TO["en"]), dry_run=False)
    assert SENTENCE["en"].format(title="Export Sales") in _plain(wire[0]).splitlines()


# ── The attachment is part of the text ─────────────────────────────────────


def _profile(tmp_path):
    pdf = tmp_path / "Suppleco Company Profile 2026.pdf"
    pdf.write_bytes(b"%PDF-1.7 the company profile\n%%EOF\n")
    return pdf


def _missing(tmp_path):
    return tmp_path / "not there.pdf"


def _a_folder(tmp_path):
    folder = tmp_path / "a folder.pdf"
    folder.mkdir()
    return folder


def _empty(tmp_path):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    return empty


UNUSABLE = [_missing, _a_folder, _empty]
UNUSABLE_IDS = ["missing", "a folder, which cannot be read", "empty"]


@pytest.mark.parametrize("language", ["it", "en"])
def test_the_first_email_carries_the_company_profile(
    settings_factory, tmp_path, language
):
    pdf = _profile(tmp_path)
    a, wire = _agent(settings_factory, tmp_path, pdf_path=str(pdf))
    assert a._process_contact(_stored(a, TO[language]), dry_run=False)
    assert _attached(wire[0]) == [(pdf.name, pdf.read_bytes())]
    said = {"it": "In allegato troverà", "en": "Please find attached"}[language]
    assert said in _plain(wire[0])
    row = a.db.get_all_emails(limit=1)[0]
    assert (row["has_attachment"], row["attachment_filename"]) == (1, pdf.name)


@pytest.mark.parametrize("unusable", UNUSABLE, ids=UNUSABLE_IDS)
@pytest.mark.parametrize("language", ["it", "en"])
def test_a_live_first_email_is_not_sent_without_the_pdf(
    settings_factory, tmp_path, language, unusable
):
    a, wire = _agent(settings_factory, tmp_path, pdf_path=str(unusable(tmp_path)))
    contact = _stored(a, TO[language])
    assert a._process_contact(contact, dry_run=False) is False
    assert wire == [], 'an email saying "find attached" went out with nothing attached'
    assert _status(a, contact) == "pending"
    assert _retries(a) == []
    assert a._stats["emails_failed"] == 0
    assert a.db.get_all_emails() == []


def test_the_refusal_is_recorded_once_and_names_the_file(settings_factory, tmp_path):
    missing = _missing(tmp_path)
    a, wire = _agent(settings_factory, tmp_path, pdf_path=str(missing))
    for i in range(3):
        a._process_contact(_stored(a, f"buyer{i}@brand{i}.de"), dry_run=False)
    rows = _activity(a, "PROFILE_PDF_MISSING")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert "PDF_PATH" in rows[0]["details"] and missing.name in rows[0]["details"]
    assert wire == []


def test_a_pdf_deleted_while_the_agent_runs_stops_the_next_first_email(
    settings_factory, tmp_path
):
    """It was found at start-up and is looked for again at each send."""
    pdf = _profile(tmp_path)
    a, wire = _agent(settings_factory, tmp_path, pdf_path=str(pdf))
    assert a._process_contact(_stored(a, "a@brand1.de"), dry_run=False)
    pdf.unlink()
    assert a._process_contact(_stored(a, "b@brand2.de"), dry_run=False) is False
    assert len(wire) == 1


def test_a_pdf_put_in_place_is_picked_up_without_a_restart(settings_factory, tmp_path):
    pdf = tmp_path / "Suppleco Company Profile 2026.pdf"
    a, wire = _agent(settings_factory, tmp_path, pdf_path=str(pdf))
    contact = _stored(a, TO["en"])
    assert a._process_contact(contact, dry_run=False) is False
    _profile(tmp_path)
    assert a._process_contact(contact, dry_run=False)
    assert _attached(wire[0]) == [(pdf.name, pdf.read_bytes())]


def test_the_campaign_stops_at_a_missing_pdf(settings_factory, tmp_path):
    a, wire = _agent(settings_factory, tmp_path, pdf_path=str(_missing(tmp_path)))
    contacts = [_stored(a, f"buyer{i}@brand{i}.de") for i in range(3)]
    seen = []
    real = a._process_contact
    a._process_contact = lambda c, *x, **k: seen.append(c) or real(c, *x, **k)
    a._email_contacts(contacts, dry_run=False)
    assert seen == [], "the loop went contact by contact into the same refusal"
    assert {_status(a, c) for c in contacts} == {"pending"}
    assert wire == []


def test_a_retry_keeps_its_attempts_while_the_pdf_is_missing(
    settings_factory, tmp_path
):
    pdf = _profile(tmp_path)
    a, wire = _agent(settings_factory, tmp_path, failures=1, pdf_path=str(pdf))
    contact = _stored(a, TO["en"])
    _a_failed_first_email(a, wire, contact)
    pdf.unlink()
    a._process_retries()
    assert wire == []
    assert _retries(a) == [("pending", 0)]
    _profile(tmp_path)
    a._process_retries()
    assert _attached(wire[0]) == [(pdf.name, pdf.read_bytes())]
    assert _retries(a) == [("succeeded", 1)]


def test_direct_send_says_the_pdf_is_missing_and_stores_nothing(
    settings_factory, tmp_path
):
    missing = _missing(tmp_path)
    a, wire = _agent(settings_factory, tmp_path, pdf_path=str(missing))
    ok, message = a.send_single_email(TO["it"], channel="referral")
    assert ok is False
    assert "Company Profile" in message and missing.name in message
    assert wire == []
    assert a.db.get_contact_by_email(TO["it"]) is None, (
        "a refused send stored a contact"
    )


def test_the_direct_send_preview_says_a_live_send_would_be_refused(
    settings_factory, tmp_path
):
    missing = _missing(tmp_path)
    a, wire = _agent(settings_factory, tmp_path, pdf_path=str(missing))
    ok, message = a.send_single_email(TO["it"], channel="referral", dry_run=True)
    assert ok, message
    assert "would be refused" in message
    assert "Company Profile" in message and missing.name in message
    assert wire == []
    # With the PDF in place the preview says nothing of the kind.
    b, _wire = _agent(
        settings_factory, tmp_path / "b", pdf_path=str(_profile(tmp_path))
    )
    ok, message = b.send_single_email(TO["it"], channel="referral", dry_run=True)
    assert ok and message == (
        f"Draft looks good — would send to {TO['it']}. Written in Italian. "
        "The Company Profile (Suppleco Company Profile 2026.pdf) is attached."
    )


# ── What the Direct Send reply says it would send ─────────────────────────
# The dry run is how the operator, and in a demo the client, reads what will
# go out. It said only "Draft looks good - would send to X."


def _preview(settings_factory, tmp_path, email, **kw):
    a, wire = _agent(settings_factory, tmp_path, ITALIAN_COUNTRIES="IT", **kw)
    ok, message = a.send_single_email(email, channel="referral", dry_run=True)
    assert ok, message
    assert wire == []
    return a, message


def test_the_preview_says_italian_and_names_the_attached_profile(
    settings_factory, tmp_path
):
    pdf = _profile(tmp_path)
    _a, message = _preview(settings_factory, tmp_path, TO["it"], pdf_path=str(pdf))
    assert "Written in Italian." in message
    assert f"The Company Profile ({pdf.name}) is attached." in message


def test_the_preview_says_english_for_a_recipient_outside_italy(
    settings_factory, tmp_path
):
    _a, message = _preview(settings_factory, tmp_path, "buyer@brand.com")
    assert "Written in English." in message
    assert "Written in Italian" not in message


def test_the_preview_says_when_the_italian_text_is_missing(settings_factory, tmp_path):
    a, wire = _agent(settings_factory, tmp_path, ITALIAN_COUNTRIES="IT")
    a._email_template_it = None
    ok, message = a.send_single_email(TO["it"], channel="referral", dry_run=True)
    assert ok, message
    assert wire == []
    assert (
        "Italian recipient, but the Italian first email is missing, so it goes "
        "in English." in message
    )
    assert "Written in Italian" not in message


def test_the_preview_says_the_profile_is_linked_when_it_is_not_attached(
    settings_factory, tmp_path
):
    _a, message = _preview(
        settings_factory,
        tmp_path,
        TO["en"],
        attach_profile=False,
        profile_url="https://suppleco.example/profile",
    )
    assert "The Company Profile is linked, not attached." in message
    assert "is attached." not in message


def test_a_live_direct_send_says_the_language_and_the_attachment(
    settings_factory, tmp_path
):
    pdf = _profile(tmp_path)
    a, wire = _agent(
        settings_factory, tmp_path, ITALIAN_COUNTRIES="IT", pdf_path=str(pdf)
    )
    ok, message = a.send_single_email(TO["it"], channel="referral")
    assert ok, message
    assert len(wire) == 1
    assert message == (
        f"Email sent to {TO['it']}. Written in Italian. "
        f"The Company Profile ({pdf.name}) is attached."
    )


def test_a_dry_run_composes_the_email_and_says_the_pdf_is_missing(
    settings_factory, tmp_path, caplog
):
    missing = _missing(tmp_path)
    a, wire = _agent(settings_factory, tmp_path, pdf_path=str(missing))
    contacts = [_stored(a, f"buyer{i}@brand{i}.de") for i in range(3)]
    with caplog.at_level(logging.WARNING):
        a._email_contacts(contacts, dry_run=True)
    assert wire == []
    rows = a.db.get_all_emails()
    assert [r["delivery_status"] for r in rows] == ["dry_run"] * 3
    assert {r["has_attachment"] for r in rows} == {0}
    # Said once, where a dry run is read: the dashboard's activity and the log.
    said = _activity(a, "PROFILE_PDF_MISSING")
    assert len(said) == 1
    assert said[0]["status"] == "warning"
    assert (
        "would be refused" in said[0]["details"] and missing.name in said[0]["details"]
    )
    logged = [r for r in caplog.records if "would be refused" in r.getMessage()]
    assert len(logged) == 1


def test_start_up_no_longer_promises_to_send_without_it(
    settings_factory, tmp_path, caplog
):
    with caplog.at_level(logging.WARNING):
        _agent(settings_factory, tmp_path, pdf_path=str(_missing(tmp_path)))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "without attachment" not in text
    assert "First emails are refused" in text


def test_with_attach_profile_off_no_pdf_is_needed(settings_factory, tmp_path):
    a, wire = _agent(
        settings_factory,
        tmp_path,
        attach_profile=False,
        pdf_path=str(_missing(tmp_path)),
    )
    assert a._process_contact(_stored(a, TO["en"]), dry_run=False)
    assert _attached(wire[0]) == []
    assert _activity(a, "PROFILE_PDF_MISSING") == []


def test_follow_ups_go_without_the_pdf(settings_factory, tmp_path):
    """They carry no attachment, so a PDF lost after the first email does not
    hold the rest of the sequence."""
    pdf = _profile(tmp_path)
    a, wire = _agent(
        settings_factory, tmp_path, sequences_enabled=True, pdf_path=str(pdf)
    )
    assert a._process_contact(_stored(a, TO["en"]), dry_run=False)
    pdf.unlink()
    _due(a)
    a._process_sequences(dry_run=False)
    assert len(wire) == 2
    assert _attached(wire[1]) == []
    assert "gently follow up" in _plain(wire[1])


# ── doctor ─────────────────────────────────────────────────────────────────


def _check(settings_factory, tmp_path, label, **kw):
    settings = settings_factory(db_path=str(tmp_path / "doctor.db"), **kw)
    checks = main.run_doctor_checks(settings, network=False)
    hits = [c for c in checks if c.label.startswith(label)]
    assert len(hits) == 1, f"{label!r} in {[c.label for c in checks]}"
    return hits[0]


def _product_without(tmp_path, monkeypatch, *names, **rewritten):
    """doctor, pointed at a product folder whose templates/ lacks some files
    and holds others rewritten."""
    root = tmp_path / "product"
    shutil.copytree(
        ROOT / "templates", root / "templates", ignore=shutil.ignore_patterns(*names)
    )
    for name, text in rewritten.items():
        (root / "templates" / name.replace("__", ".")).write_text(
            text, encoding="utf-8"
        )
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(main, "FileHandler", lambda: FileHandler(str(root)))


def test_doctor_finds_both_first_email_templates(settings_factory, tmp_path):
    english = _check(settings_factory, tmp_path, "Email template present")
    italian = _check(settings_factory, tmp_path, "Italian email template present")
    assert (english.ok, italian.ok) == (True, True)
    assert "email_template_it.html" in italian.detail


def test_doctor_says_when_the_italian_template_is_missing(
    settings_factory, tmp_path, monkeypatch
):
    _product_without(tmp_path, monkeypatch, "email_template_it.html")
    italian = _check(settings_factory, tmp_path, "Italian email template present")
    assert italian.ok is False
    assert "English" in italian.detail
    assert _check(settings_factory, tmp_path, "Email template present").ok is True


def test_doctor_fails_an_italian_template_that_lost_what_every_email_must_carry(
    settings_factory, tmp_path, monkeypatch
):
    _product_without(
        tmp_path,
        monkeypatch,
        email_template_it__html="<p>{{sender_title}}</p>{{unsubscribe_url}}",
    )
    footer = _check(settings_factory, tmp_path, "Legal footer in templates")
    assert footer.ok is False and footer.blocking
    assert "email_template_it.html lacks {{legal_footer}}" in footer.detail


def test_doctor_fails_until_the_title_is_set(settings_factory, tmp_path):
    blank = _check(settings_factory, tmp_path, "Sender title", sender_title="")
    assert blank.ok is False and blank.blocking
    assert "SENDER_TITLE" in blank.detail
    named = _check(
        settings_factory, tmp_path, "Sender title", sender_title="Export Sales"
    )
    assert named.ok is True
    assert named.detail == "Export Sales"


def test_doctor_names_the_mailbox_whose_title_is_missing(settings_factory, tmp_path):
    """The shared title covers a mailbox that has none of its own; with the
    shared one blank, the mailbox without its own is the one named."""
    check = _check(
        settings_factory,
        tmp_path,
        "Sender title",
        smtp_username="giulia@suppleco.example",
        sender_title="",
        sender_2_email="marco@suppleco.example",
        sender_2_password="pw2",
        sender_2_title="Export Sales",
    )
    assert check.ok is False and check.blocking
    assert "giulia@suppleco.example" in check.detail
    assert "marco@suppleco.example" not in check.detail


def test_doctor_does_not_ask_for_a_title_no_template_uses(
    settings_factory, tmp_path, monkeypatch
):
    _product_without(
        tmp_path, monkeypatch, "email_template_it.html", email_template__html=NO_TITLE
    )
    check = _check(settings_factory, tmp_path, "Sender title", sender_title="")
    assert check.ok is None, "a FAIL here would say live sending is refused"


@pytest.mark.parametrize("unusable", UNUSABLE, ids=UNUSABLE_IDS)
def test_doctor_fails_while_the_attached_pdf_cannot_be_sent(
    settings_factory, tmp_path, unusable
):
    path = unusable(tmp_path)
    check = _check(
        settings_factory, tmp_path, "PDF attachment present", pdf_path=str(path)
    )
    assert check.ok is False and check.blocking
    assert path.name in check.detail and "refused" in check.detail


def test_doctor_passes_a_pdf_that_is_there(settings_factory, tmp_path):
    pdf = _profile(tmp_path)
    check = _check(
        settings_factory, tmp_path, "PDF attachment present", pdf_path=str(pdf)
    )
    assert check.ok is True
    assert pdf.name in check.detail
    off = _check(
        settings_factory,
        tmp_path,
        "PDF attachment present",
        attach_profile=False,
        pdf_path=str(_missing(tmp_path)),
    )
    assert off.ok is None
