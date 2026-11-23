"""Every email greets a person by first name, or greets no one by name.

The follow-ups said "Hi {{name}}," with the stored name as it was, while the
first email took its first word: Maria Rossi got "Hi Maria," and then
"Hi Maria Rossi,", and a contact without a name "Hi there," and then "Hello,".
A scraped name is the first two to four capitalised words beside the address,
title included, so page text is stored as a name and a title as a first name:
"Customer Reviews" got "Hi Customer," and then "Hi Customer Reviews,", and
Dr. Anna Rossi got "Hi Dr.,". Now every email greets through one helper. It
skips a title, and takes a surname alone, four words or more, or a word that
names a department or a page rather than a person, for no name.
"""

import pytest
from bs4 import BeautifulSoup

from agent import OutreachAgent
from modules.search import SearchModule

# An operator's own first email, which still asks for the parts written per
# contact. SUPPLECO's (templates/email_template.html since 10 Nov 2026) is fixed
# text: no greeting, no body.
OWN_FIRST_EMAIL = (
    '<html><body><p style="margin: 0 0 16px 0;">{{greeting}}</p>{{body}}'
    "<p>Best,</p><p>{{sender_name}}<br>{{company_name}}</p>"
    '<p><a href="{{unsubscribe_url}}">unsubscribe here</a></p>'
    "{{legal_footer}}{{tracking_pixel}}</body></html>"
)


def _agent(settings_factory, tmp_path, monkeypatch):
    """The real agent, with only the hand-off to SMTP replaced by a recorder."""
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            sequences_enabled=True,
            smtp_username="outreach@suppleco.example",
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    a._email_template = OWN_FIRST_EMAIL
    sent = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: sent.append(k) or 1
    )
    return a, sent


def _stored(a, name):
    cid = a.db.add_contact(
        a.db.log_search("q"),
        email="buyer@brand.de",
        name=name,
        organization="Brand GmbH",
    )
    return a.db.get_contact_by_id(cid)


def _emails(a, sent, name):
    """The first email and both follow-ups to a contact stored with this name:
    the campaign's send, then the sequence engine's two steps as they fall due.
    A follow-up file that went missing would quietly become the first email,
    so each step is checked by its own wording."""
    contact = _stored(a, name)
    assert a._process_contact(contact, dry_run=False)
    a.db.update_contact_status(contact["id"], "emailed")
    for wording in ("gently follow up", "my last note"):
        a.db.conn.execute(
            "UPDATE sequence_enrollments SET enrolled_at = datetime('now','-10 days'),"
            " last_sent_at = datetime('now','-10 days') WHERE contact_id = ?",
            (contact["id"],),
        )
        a.db.conn.commit()
        a._process_sequences(dry_run=False)
        assert wording in sent[-1]["plain_body"]
    assert len(sent) == 3
    return sent


def _greetings(emails):
    """The line each email opens with, as the recipient reads it."""
    return [email["plain_body"].splitlines()[0] for email in emails]


def _greeting(name):
    return OutreachAgent._build_greeting({"name": name})


def _name_scraped_beside(text):
    """What the scraper stores as the name of an address printed below this."""
    page = BeautifulSoup(f"<div><p>{text}</p><p>a@brand.de</p></div>", "html.parser")
    found = SearchModule.__new__(SearchModule)._find_name_near_email(page, "a@brand.de")
    return found["name"]


def test_a_follow_up_greets_by_first_name_like_the_first_email(
    settings_factory, tmp_path, monkeypatch
):
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    emails = _emails(a, sent, "Maria Rossi")
    assert _greetings(emails) == ["Hi Maria,"] * 3
    for email in emails:
        assert "Hi Maria,</p>" in email["html_body"]


def test_no_placeholder_is_left_for_the_recipient_to_read(
    settings_factory, tmp_path, monkeypatch
):
    """A placeholder nothing fills stays in the email as it is, so a template
    switched to {{greeting}} that the send did not fill would open with it."""
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    for email in _emails(a, sent, "Maria Rossi"):
        assert "{{" not in email["html_body"]
        assert "{{" not in email["plain_body"]


@pytest.mark.parametrize("name", [None, "Sir/Madam"])
def test_a_contact_without_a_name_gets_the_same_greeting_in_every_email(
    settings_factory, tmp_path, monkeypatch, name
):
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    assert _greetings(_emails(a, sent, name)) == ["Hi there,"] * 3


@pytest.mark.parametrize(
    "name",
    ["Customer Reviews", "Natural Health Products Company"],
    ids=["the review's case", "four words"],
)
def test_page_text_stored_as_a_name_is_greeted_as_no_one(
    settings_factory, tmp_path, monkeypatch, name
):
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    assert _greetings(_emails(a, sent, name)) == ["Hi there,"] * 3


def test_a_title_stored_with_the_name_is_not_greeted(
    settings_factory, tmp_path, monkeypatch
):
    """The follow-ups printed the whole name, so taking the first word alone
    would have spread the first email's "Hi Dr.," to all three."""
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    assert _greetings(_emails(a, sent, "Dr. Anna Rossi")) == ["Hi Anna,"] * 3


@pytest.mark.parametrize(
    "name,greeting",
    [
        ("Maria Rossi", "Hi Maria,"),
        ("Customer Reviews", "Hi there,"),
        (None, "Hi there,"),
    ],
)
def test_the_built_in_fallback_template_greets_the_same_way(
    settings_factory, tmp_path, monkeypatch, name, greeting
):
    """Start-up falls back to it when templates/email_template.html is missing.
    It opened "Dear Customer Reviews," and, with no name, "Dear ,"."""
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    a._email_template = a._default_template()
    assert a._process_contact(_stored(a, name), dry_run=True)
    assert _greetings(sent) == [greeting]


@pytest.mark.parametrize(
    "text,stored,greeting",
    [
        ("Dr. Anna Rossi, Procurement Manager", "Dr. Anna Rossi", "Hi Anna,"),
        ("Mr John Smith", "Mr John Smith", "Hi John,"),
        ("Herr Thomas Weber, Einkauf", "Herr Thomas Weber", "Hi Thomas,"),
        ("Herr Weber", "Herr Weber", "Hi there,"),
        ("Customer Reviews", "Customer Reviews", "Hi there,"),
        ("Privacy Policy | Cookie Policy", "Privacy Policy", "Hi there,"),
        ("Impressum Datenschutz AGB", "Impressum Datenschutz", "Hi there,"),
    ],
)
def test_what_the_scraper_stores_from_a_page_is_greeted_as_it_should_be(
    text, stored, greeting
):
    """The route from the page to the greeting, on text the scraper really keeps."""
    assert _name_scraped_beside(text) == stored
    assert _greeting(stored) == greeting


@pytest.mark.parametrize(
    "name",
    [
        "Customer Care",
        "Product Reviews",
        "Our Team",
        "Request Info",
        "Sales Department",
        "Online Shop",
        "Head Office",
        "Technical Support",
        "Service Hotline",
        "Contact Form",
        "Website Admin",
        "Marketing Manager",
        "Kontakt Musterstrasse",
        "Contatti Milano",
        "Contacto Comercial",
        "Impressum Angaben",
        "Imprint Company",
        "Aviso Legal",
        "About Brand",
        "Follow Us",
        "Ueber Uns",
        "Nous Contacter",
        "Over Ons",
        "Chi Siamo",
        "Quienes Somos",
        "Privacy Notice",
        "Return Policy",
        "Cookie Settings",
        "Accept Cookies",
        "Datenschutz Hinweise",
    ],
)
def test_each_generic_word_marks_a_department_or_a_page_not_a_person(name):
    """One heading per word, holding no other: text the scraper finds beside
    an address and stores as a name."""
    assert _greeting(name) == "Hi there,"


@pytest.mark.parametrize("name", ["CUSTOMER SERVICE", "sales team"])
def test_a_generic_word_counts_whatever_its_case(name):
    """Direct Send stores what the operator typed, as they typed it."""
    assert _greeting(name) == "Hi there,"


@pytest.mark.parametrize("name", ["Sales/Marketing", "Customer-Care", "Info:"])
def test_a_generic_word_counts_beside_punctuation(name):
    assert _greeting(name) == "Hi there,"


@pytest.mark.parametrize(
    "name,greeting",
    [
        ("Anna Maria Bianchi", "Hi Anna,"),
        ("Prof. Anna Maria Bianchi", "Hi Anna,"),
        ("Ivan Shopov", "Hi Ivan,"),
        ("Andrea Salesi", "Hi Andrea,"),
        ("Anna M. Rossi", "Hi Anna,"),
    ],
    ids=[
        "three words",
        "three words after a title",
        "shop inside a surname",
        "sales inside a surname",
        "an initial after the first name",
    ],
)
def test_a_persons_name_still_greets_by_first_name(name, greeting):
    """Three words can be a person's name, and a generic word is a whole word:
    a surname that starts with one is still a surname."""
    assert _greeting(name) == greeting


@pytest.mark.parametrize(
    "name,greeting",
    [
        ("Mrs Jane Smith", "Hi Jane,"),
        ("Ms Jane Smith", "Hi Jane,"),
        ("dr anna rossi", "Hi Anna,"),
        ("Prof. Dr. Hans Weber", "Hi Hans,"),
        ("Frau Anna Weber", "Hi Anna,"),
        ("Madame Anne Dupont", "Hi Anne,"),
        ("Monsieur Jean Dupont", "Hi Jean,"),
        ("Mme Anne Dupont", "Hi Anne,"),
        ("Dott. Mario Rossi", "Hi Mario,"),
        ("Dott.ssa Anna Bianchi", "Hi Anna,"),
    ],
)
def test_a_title_is_skipped_to_reach_the_first_name(name, greeting):
    """The scraper keeps a title at the front of the name it stores. Italian
    and other dotted titles reach the helper only when an operator types them."""
    assert _greeting(name) == greeting


@pytest.mark.parametrize(
    "name",
    ["Herr Weber", "Dr Smith", "Dr. Rossi", "J. Smith", "Dr.", "Mr"],
)
def test_a_surname_alone_is_greeted_as_no_one(name):
    """The greeting is a first name's. "Hi Weber," reads as a mail merge."""
    assert _greeting(name) == "Hi there,"


@pytest.mark.parametrize("name", ["mario.rossi@brand.de", "www.brand.de"])
def test_an_address_typed_as_a_name_is_greeted_as_no_one(name):
    assert _greeting(name) == "Hi there,"
