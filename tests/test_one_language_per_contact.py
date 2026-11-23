"""Every email, page and panel gives a contact the same one language.

The first email, the follow-ups, a retry, Direct Send, the unsubscribe page,
the contacts list and the why panel each asked the address for a language.
With the company's country now counting (an Italian company on a .com
address gets Italian), each of them must ask the stored contact instead, or
a contact would get the Italian first email, English follow-ups and the
English unsubscribe page. And once a contact was sent an email, that email's
language, recorded with it as the notice version, holds for the rest of the
thread, whatever its evidence or the operator's choice says later.

The harness's recorder sends nothing and writes no email_log row, so a test
about the first email's language writes that row itself.
"""

import pytest
from test_first_email_language import TO, _agent, _due, _lines, _Wire

import main
from modules.database import Database
from modules.unsubscribe import UnsubscribeHandler

COM = "acquisti@rossi-integratori.com"


def _italian_company(a, email=COM, **kw):
    """A contact whose company website is Italian, on an address that is not."""
    cid = a.db.add_contact(
        a.db.log_search("q"),
        email=email,
        name="Maria Rossi",
        organization="Rossi Integratori",
        source_url="https://www.rossi-integratori.it/contatti",
        company_country="IT",
        company_country_source="website",
        **kw,
    )
    return a.db.get_contact_by_id(cid)


def _plain(a, email, **kw):
    cid = a.db.add_contact(
        a.db.log_search("q"),
        email=email,
        name="Maria Rossi",
        organization="Rossi Integratori",
        **kw,
    )
    return a.db.get_contact_by_id(cid)


def _logged_first(a, contact, version):
    """The first email as the send records it, in the language its notice
    version names."""
    a.db.log_email(
        contact["id"],
        contact["email"],
        "s",
        delivery_status="sent",
        notice_version=version,
    )
    a.db.update_contact_status(contact["id"], "emailed")


def _follow_up(a, sent):
    before = len(sent)
    _due(a)
    a._process_sequences(dry_run=False)
    assert len(sent) == before + 1, "the follow-up did not go"
    return sent[-1]


# ── The first email ────────────────────────────────────────────────────────


def test_an_italian_company_on_a_com_address_gets_the_italian_first_email(
    settings_factory, tmp_path
):
    a, sent = _agent(settings_factory, tmp_path)
    assert a._process_contact(_italian_company(a), dry_run=False)
    email = sent[-1]
    assert _lines(email)[0] == "Buongiorno,"
    assert email["notice_version"].endswith("-it")
    assert "comunicazione commerciale" in email["plain_body"]


def test_the_title_check_reads_the_contacts_language(settings_factory, tmp_path):
    """The Italian first email decides whether the sender's title is needed,
    for an Italian company on a .com address as for a .it one."""
    a, _sent = _agent(settings_factory, tmp_path)
    a._email_template = "<html>{{legal_footer}}{{unsubscribe_url}}</html>"
    a._email_template_it = (
        "<html>{{sender_title}}{{legal_footer}}{{unsubscribe_url}}</html>"
    )
    pipeline = a.dispatch_pipeline
    assert pipeline.first_email_needs_title(_italian_company(a)) is True
    assert pipeline.first_email_needs_title(_plain(a, "buyer@brand.com")) is False


# ── The thread keeps its first email's language ────────────────────────────


def test_a_follow_up_keeps_the_first_emails_english(settings_factory, tmp_path):
    a, sent = _agent(settings_factory, tmp_path, sequences_enabled=True)
    contact = _plain(a, TO["it"])
    assert a._process_contact(contact, dry_run=False)
    _logged_first(a, contact, "notice-li-2")
    email = _follow_up(a, sent)
    assert email["notice_version"] == "notice-li-2"
    assert _lines(email)[0] == "Hi Maria,"


def test_a_follow_up_keeps_italian_when_the_evidence_and_choice_change(
    settings_factory, tmp_path
):
    a, sent = _agent(settings_factory, tmp_path, sequences_enabled=True)
    contact = _italian_company(a)
    assert a._process_contact(contact, dry_run=False)
    assert sent[-1]["notice_version"].endswith("-it")
    _logged_first(a, contact, sent[-1]["notice_version"])
    a.db.conn.execute(
        "UPDATE contacts SET company_country = NULL, company_country_source = NULL,"
        " language_choice = 'en' WHERE id = ?",
        (contact["id"],),
    )
    a.db.conn.commit()
    email = _follow_up(a, sent)
    assert email["notice_version"].endswith("-it")
    assert email["subject"].startswith("Faccio seguito")


def test_a_dry_run_does_not_fix_the_language(settings_factory, tmp_path):
    a, sent = _agent(settings_factory, tmp_path)
    contact = _italian_company(a)
    a.db.log_email(
        contact["id"],
        COM,
        "s",
        delivery_status="dry_run",
        notice_version="notice-li-1-it",
    )
    assert a.db.set_language_choice(contact["id"], "en")
    assert a._process_contact(a.db.get_contact_by_id(contact["id"]), dry_run=False)
    assert sent[-1]["notice_version"] == "notice-li-2"
    assert _lines(sent[-1])[0] == "Dears,"


def test_a_choice_made_after_the_batch_read_the_contact_counts(
    settings_factory, tmp_path
):
    """The batch's copy of the contact is hours old; the operator chose since."""
    a, sent = _agent(settings_factory, tmp_path)
    contact = _italian_company(a)
    a.db.set_language_choice(contact["id"], "en")
    assert a._process_contact(contact, dry_run=False)
    assert sent[-1]["notice_version"] == "notice-li-2"


def test_a_retry_of_an_italian_companys_first_email_is_italian(
    settings_factory, tmp_path
):
    a, _sent = _agent(settings_factory, tmp_path)
    for _key, _name, mailbox in a.senders:
        del mailbox.dispatch_to_contact
    wire = _Wire(failures=1)
    a.email_sender._new_connection = lambda: wire
    contact = _italian_company(a)
    assert a._process_contact(contact, dry_run=False) is False
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    a.db.conn.commit()
    a._process_retries()
    assert len(wire.sent) == 1, "the retry did not go"
    text = wire.sent[0].get_body(("plain",)).get_content()
    assert text.splitlines()[0] == "Buongiorno,"
    assert a.db.first_sent_language(contact["id"]) == "it"


# ── Direct Send ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("preview", [True, False], ids=["preview", "send"])
def test_direct_send_says_and_sends_the_contacts_language(
    settings_factory, tmp_path, preview
):
    a, sent = _agent(settings_factory, tmp_path)
    _italian_company(a)
    ok, message = a.send_single_email(COM, channel="referral", dry_run=preview)
    assert ok, message
    assert " Written in Italian." in message
    assert sent[-1]["notice_version"].endswith("-it")


def test_direct_send_after_an_english_first_email_stays_english(
    settings_factory, tmp_path
):
    a, sent = _agent(settings_factory, tmp_path)
    contact = _italian_company(a)
    a.db.log_email(
        contact["id"],
        COM,
        "s",
        delivery_status="sent",
        notice_version="notice-li-1",
    )
    ok, message = a.send_single_email(COM, channel="referral", dry_run=True)
    assert ok, message
    assert " Written in English." in message
    assert sent[-1]["notice_version"] == "notice-li-2"


def test_a_preview_of_an_unknown_com_address_is_english(settings_factory, tmp_path):
    a, _sent = _agent(settings_factory, tmp_path)
    ok, message = a.send_single_email("new@brand.com", dry_run=True)
    assert ok, message
    assert " Written in English." in message


class _CountryAI:
    """The AI client the Direct Send agent holds, answering Italy."""

    is_configured = True

    def __init__(self, answer="IT"):
        self.answer = answer
        self.asked = []

    def classify_company_country(self, contact):
        self.asked.append(contact["email"])
        return self.answer


@pytest.mark.parametrize("preview", [True, False], ids=["preview", "send"])
def test_direct_send_to_a_new_italian_company_reads_its_name(
    settings_factory, tmp_path, preview
):
    """A new address typed into Direct Send was never qualified, so an
    S.p.A. on a .com address got the English email, and kept English for
    life."""
    a, sent = _agent(settings_factory, tmp_path)
    a.ai_gen = _CountryAI("DE")
    new = "acquisti@rossiintegratori.com"
    ok, message = a.send_single_email(
        new,
        organization="Rossi Integratori S.p.A.",
        channel="referral",
        dry_run=preview,
    )
    assert ok, message
    assert " Written in Italian." in message
    assert sent[-1]["notice_version"].endswith("-it")
    assert a.ai_gen.asked == []
    row = a.db.get_contact_by_email(new)
    if preview:
        assert row is None
    else:
        assert (row["company_country"], row["company_country_source"]) == (
            "IT",
            "page",
        )


def test_direct_send_to_a_new_company_asks_the_ai_once(settings_factory, tmp_path):
    a, sent = _agent(settings_factory, tmp_path)
    a.ai_gen = _CountryAI("IT")
    new = "acquisti@verdinutrizione.com"
    ok, message = a.send_single_email(
        new, organization="Verdi Nutrizione", channel="referral"
    )
    assert ok, message
    assert " Written in Italian." in message
    assert sent[-1]["notice_version"].endswith("-it")
    assert a.ai_gen.asked == [new]
    ok, _message = a.send_single_email(new, channel="referral")
    assert a.ai_gen.asked == [new]


def test_a_sent_direct_send_is_reported_sent_whatever_the_database_does_after(
    settings_factory, tmp_path
):
    """The reply read the contact's language after the send. A locked
    database at that moment answered "Send failed" for a delivered email,
    and the operator's second click sent it again."""
    import sqlite3

    a, sent = _agent(settings_factory, tmp_path)
    _italian_company(a)
    real = a.db.first_sent_languages

    def locked_once_sent(ids):
        if sent:
            raise sqlite3.OperationalError("database is locked")
        return real(ids)

    a.db.first_sent_languages = locked_once_sent
    ok, message = a.send_single_email(COM, channel="referral")
    assert len(sent) == 1
    assert ok, message
    assert message.startswith(f"Email sent to {COM}. Written in Italian.")


def test_the_dashboard_says_a_typed_name_made_it_italian():
    from modules.compliance import language_reason_words

    typed = {"source_channel": "referral"}
    assert language_reason_words("it", "page", COM, typed) == (
        "Italian (its company's name shows an Italian company)"
    )
    collected = {"source_channel": "own_website"}
    assert language_reason_words("it", "page", COM, collected) == (
        "Italian (its company's page shows an Italian company)"
    )


def test_a_preview_of_a_new_address_asks_no_ai(settings_factory, tmp_path):
    a, _sent = _agent(settings_factory, tmp_path)
    a.ai_gen = _CountryAI("IT")
    ok, message = a.send_single_email(
        "new@brand.com", organization="Brand", channel="referral", dry_run=True
    )
    assert ok, message
    assert " Written in English." in message
    assert a.ai_gen.asked == []


# ── The unsubscribe page ───────────────────────────────────────────────────


def _page(settings_factory, tmp_path, address, setup=None):
    s = settings_factory(
        db_path=str(tmp_path / "u.db"), unsubscribe_secret="a-test-secret-value"
    )
    db = Database(s.get_abs_db_path())
    if setup:
        setup(db)
    client = main.create_dashboard_app(s).test_client()
    token = UnsubscribeHandler(db, s.unsubscribe_secret).generate_token(address)
    return client.get(f"/unsubscribe/{token}").get_data(as_text=True)


def _held(db, email, version=None, **kw):
    cid = db.add_contact(db.log_search("q"), email, **kw)
    if version is not None:
        db.log_email(cid, email, "s", delivery_status="sent", notice_version=version)
    return cid


def test_an_italian_company_lands_on_the_italian_page(settings_factory, tmp_path):
    page = _page(
        settings_factory,
        tmp_path,
        COM,
        lambda db: _held(
            db, COM, company_country="IT", company_country_source="website"
        ),
    )
    assert '<html lang="it">' in page


def test_the_page_follows_the_first_email_not_the_address(settings_factory, tmp_path):
    page = _page(
        settings_factory,
        tmp_path,
        TO["it"],
        lambda db: _held(db, TO["it"], "notice-li-1"),
    )
    assert '<html lang="en">' in page
    page = _page(
        settings_factory,
        tmp_path / "b",
        COM,
        lambda db: _held(db, COM, "notice-li-1-it"),
    )
    assert '<html lang="it">' in page


def test_an_address_held_by_no_contact_keeps_the_address_rule(
    settings_factory, tmp_path
):
    assert '<html lang="it">' in _page(settings_factory, tmp_path, "nuovo@rossi.it")
    assert '<html lang="en">' in _page(settings_factory, tmp_path / "b", COM)


# ── The contacts list and the why panel ────────────────────────────────────


def _dashboard(settings_factory, tmp_path):
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    client = main.create_dashboard_app(s).test_client()
    return client, Database(s.get_abs_db_path())


def _rows(client):
    return {c["email"]: c for c in client.get("/api/contacts").get_json()}


def _why(client, cid):
    return dict(client.get(f"/api/contacts/{cid}/why").get_json()["facts"])


def test_the_list_and_the_panel_say_the_same_language_and_why(
    settings_factory, tmp_path
):
    client, db = _dashboard(settings_factory, tmp_path)
    company = _held(
        db,
        COM,
        source_url="https://www.rossi-integratori.it/",
        company_country="IT",
        company_country_source="website",
    )
    address = _held(db, "acquisti@rossi.it")
    plain = _held(db, "einkauf@brand.de")
    rows = _rows(client)
    said = "Italian (its company website ends in .it)"
    assert rows[COM]["language"] == "it"
    assert rows[COM]["language_reason"] == said
    assert rows[COM]["language_locked"] is False
    assert _why(client, company)["Language"] == said
    assert rows["acquisti@rossi.it"]["language_reason"] == (
        "Italian (its address ends in .it)"
    )
    assert _why(client, address)["Language"] == "Italian (its address ends in .it)"
    assert rows["einkauf@brand.de"]["language_reason"] == "English"
    assert _why(client, plain)["Language"] == "English"


def test_the_list_and_the_panel_show_the_first_emails_language(
    settings_factory, tmp_path
):
    client, db = _dashboard(settings_factory, tmp_path)
    cid = _held(
        db,
        COM,
        "notice-li-1",
        company_country="IT",
        company_country_source="website",
    )
    row = _rows(client)[COM]
    said = "English (the language of its first email)"
    assert (row["language"], row["language_reason"]) == ("en", said)
    assert row["language_locked"] is True
    assert _why(client, cid)["Language"] == said


def test_an_erased_contact_says_no_language(settings_factory, tmp_path):
    client, db = _dashboard(settings_factory, tmp_path)
    cid = _held(db, "gone@bianchi.it")
    db.update_contact_status(cid, "erased")
    row = _rows(client)["gone@bianchi.it"]
    assert row["language"] is None and row["language_reason"] is None
