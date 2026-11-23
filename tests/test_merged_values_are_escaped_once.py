"""Every value merged into an email's HTML is escaped once, and only once.

The follow-ups filled {{name}} and {{organization}} with plain str.replace,
and the first email's greeting and the opening line went in the same way. An
organisation is whatever a scraped page calls itself (its og:site_name or its
<title>), and a name is whatever an operator typed into Direct Send, so a page
could put a live link into an email SUPPLECO signs and sends from its own
mailbox. Only the first email's body was escaped, and the placeholders were
filled one after another over the whole email, so an organisation reading
"{{organization}}" pulled a raw copy of itself back into that escaped body.

The plain-text part is made from the HTML. A value escaped once reads there
exactly as it was typed; escaped twice, it reads "R&amp;D". Subjects are mail
headers, not HTML, and are never escaped.
"""

import html

import pytest

from agent import OutreachAgent
from modules.email_sender import EmailSender

SCRIPT = "<script>alert(1)</script>"
# What an og:site_name holding an entity-encoded link comes back as (F-33).
LINK = '<a href="https://attacker.example/login">Verify your account</a>'
QUOTES = "D'Arcy \"Dee\" O'Neil"
ACCENTED = "Zoë Müller-Lüdenscheidt"


# An operator's own first email, which still asks for the parts written per
# contact. SUPPLECO's (templates/email_template.html since 10 Nov 2026) is fixed
# text: no greeting, no body.
OWN_FIRST_EMAIL = (
    '<html><body><p style="margin: 0 0 16px 0;">{{greeting}}</p>{{body}}'
    "<p>Best,</p><p>{{sender_name}}<br>{{company_name}}</p>"
    '<p><a href="{{unsubscribe_url}}">unsubscribe here</a></p>'
    "{{legal_footer}}{{tracking_pixel}}</body></html>"
)


def _agent(settings_factory, tmp_path, monkeypatch, **kw):
    """The real agent, with only the hand-off to SMTP replaced by a recorder."""
    settings = {
        "db_path": str(tmp_path / "a.db"),
        "verify_emails": False,
        "imap_enabled": False,
        "ai_humanize_emails": False,
        "smtp_username": "outreach@suppleco.example",
    }
    a = OutreachAgent(settings_factory(**{**settings, **kw}))
    a._email_template = OWN_FIRST_EMAIL
    sent = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: sent.append(k) or 1
    )
    return a, sent


def _stored(a, **fields):
    """A contact as the database holds it, which is what every send path reads."""
    cid = a.db.add_contact(a.db.log_search("q"), email="buyer@brand.de", **fields)
    return a.db.get_contact_by_id(cid)


def _first_email(a, sent, contact):
    assert a._process_contact(contact, dry_run=True)
    return sent[-1]


def _follow_up(a, sent, contact, step="followup_1"):
    """Composed the way process_sequences composes a due step."""
    assert a._process_contact(
        contact,
        dry_run=True,
        template_override=a._load_followup_template(step),
        subject_override="Following up — {org}",
        is_followup=True,
        attach_pdf=False,
    )
    return sent[-1]


def test_a_name_typed_into_direct_send_is_text_in_the_greeting(
    settings_factory, tmp_path, monkeypatch
):
    """A scraped name is capitalised words only; a typed one can be anything."""
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    ok, msg = a.send_single_email("buyer@brand.de", name=SCRIPT, dry_run=True)
    assert ok, msg
    email = sent[-1]
    assert "<script>" not in email["html_body"]
    assert f"Hi {html.escape(SCRIPT)}," in email["html_body"]
    assert f"Hi {SCRIPT}," in email["plain_body"]


def test_a_stored_name_is_text_in_a_follow_up(settings_factory, tmp_path, monkeypatch):
    """Direct Send stores the typed name with a new contact, and the
    follow-ups greet with its first word, which here is all of it."""
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    email = _follow_up(a, sent, _stored(a, name=SCRIPT, organization="Brand GmbH"))
    assert "<script>" not in email["html_body"]
    assert f"Hi {html.escape(SCRIPT)}," in email["html_body"]
    assert f"Hi {SCRIPT}," in email["plain_body"]


@pytest.mark.parametrize(
    "tag",
    [
        f'<meta property="og:site_name" content="{html.escape(LINK)}">',
        f"<title>{html.escape(LINK)}</title>",
    ],
    ids=["og:site_name", "title"],
)
def test_a_link_in_a_scraped_site_name_is_text_in_every_email(
    settings_factory, tmp_path, monkeypatch, tag
):
    """F-33's route from the page to the inbox. The page parser decodes the
    entities, so the organisation stored is a live link until the email
    escapes it."""
    from bs4 import BeautifulSoup

    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    page = BeautifulSoup(f"<html><head>{tag}</head></html>", "html.parser")
    org = a.search._extract_org_name(page, "https://www.brand.de/kontakt")
    assert org == LINK
    contact = _stored(a, name="Maria Rossi", organization=org)
    for email in (
        _first_email(a, sent, contact),
        _follow_up(a, sent, contact, "followup_1"),
        _follow_up(a, sent, contact, "followup_2"),
    ):
        assert '<a href="https://attacker.example' not in email["html_body"]
        assert html.escape(LINK) in email["html_body"]
        assert LINK in email["plain_body"]


@pytest.mark.parametrize("compose", ["first email", "followup_1", "followup_2"])
def test_an_ampersand_is_escaped_once_in_every_email(
    settings_factory, tmp_path, monkeypatch, compose
):
    """The first email's body was already escaped: a fix that escaped the
    organisation again on its way in would send "R&amp;amp;D"."""
    a, sent = _agent(
        settings_factory, tmp_path, monkeypatch, email_subject="Supplements for {org}"
    )
    contact = _stored(a, name="Maria Rossi", organization="R&D GmbH")
    if compose == "first email":
        email = _first_email(a, sent, contact)
    else:
        email = _follow_up(a, sent, contact, compose)
    assert "R&amp;D GmbH" in email["html_body"]
    assert "R&D GmbH" not in email["html_body"]
    assert "&amp;amp;" not in email["html_body"]
    assert "R&D GmbH" in email["plain_body"]
    assert "&amp;" not in email["plain_body"]
    assert "R&D GmbH" in email["subject"]


def test_quotes_are_escaped_once(settings_factory, tmp_path, monkeypatch):
    """Harmless between two tags, but a template edited in the dashboard can
    put a value inside an attribute, where a quote ends it."""
    org = 'Laboratorio "Bio" d\'Italia'
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    contact = _stored(a, name=QUOTES, organization=org)
    first = _first_email(a, sent, contact)
    follow = _follow_up(a, sent, contact)
    assert f"Hi {html.escape(QUOTES.split()[0])}," in first["html_body"]
    assert f"Hi {html.escape(QUOTES.split()[0])}," in follow["html_body"]
    for email in (first, follow):
        assert html.escape(org) in email["html_body"]
        assert "&amp;#x27;" not in email["html_body"]
        assert "&amp;quot;" not in email["html_body"]
        assert org in email["plain_body"]
    assert f"Hi {QUOTES.split()[0]}," in first["plain_body"]
    assert f"Hi {QUOTES.split()[0]}," in follow["plain_body"]


def test_an_accented_name_stays_readable(settings_factory, tmp_path, monkeypatch):
    """Escaping touches only &, <, > and quotes: Zoë must not become Zo&euml;,
    in either part of the message as it is sent."""
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    contact = _stored(a, name=ACCENTED, organization="Lüdenscheider Naturkost GmbH")
    first = _first_email(a, sent, contact)
    follow = _follow_up(a, sent, contact)
    for email in (first, follow):
        msg = a.email_sender.create_email(
            to_email="buyer@brand.de",
            subject=email["subject"],
            html_body=email["html_body"],
            plain_body=email["plain_body"],
        )
        assert "Hi Zoë," in msg.get_body(("html",)).get_content()
        assert "Hi Zoë," in msg.get_body(("plain",)).get_content()
        assert "Lüdenscheider Naturkost GmbH" in msg.get_body(("plain",)).get_content()


def test_an_organisation_cannot_pull_a_raw_copy_of_itself_into_the_body(
    settings_factory, tmp_path, monkeypatch
):
    """The body was escaped, then {{organization}} was filled over the whole
    email, inside that body too: a site name ending in "{{organization}}" put
    its link back in raw."""
    org = LINK + " {{organization}}"
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    email = _first_email(a, sent, _stored(a, name="Maria Rossi", organization=org))
    assert '<a href="https://attacker.example' not in email["html_body"]
    assert email["html_body"].count(html.escape(LINK)) == 1
    assert f"I came across {org} and wanted" in email["plain_body"]


def test_the_opening_line_is_escaped_once(settings_factory, tmp_path, monkeypatch):
    """No shipped template uses {{opening_line}}. The built-in fallback, used
    when templates/email_template.html is missing, does, and so can a
    template edited in the dashboard. The line names the organisation, or is
    the AI's own sentence."""
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    a._email_template = a._default_template()  # what start-up does without the file
    email = _first_email(a, sent, _stored(a, name="Maria Rossi", organization=SCRIPT))
    line = f"I've been following the work at {SCRIPT} with interest."
    assert "<script>" not in email["html_body"]
    assert html.escape(line) in email["html_body"]
    assert line in email["plain_body"]


@pytest.mark.parametrize("written", ["opening line", "body"])
def test_what_the_ai_writes_is_text(settings_factory, tmp_path, monkeypatch, written):
    """The real generator and its output checks, with only the provider's
    answer scripted. The checks reject a link that starts with http or www,
    not markup, so escaping is all that stands between the model's answer and
    a live link in the email."""
    from modules.ai_generator import AIGenerator

    line = 'Your range caught my eye. <a href="//attacker.example/login">Verify</a>'
    if written == "opening line":
        a, sent = _agent(
            settings_factory, tmp_path, monkeypatch, ai_personalization_enabled=True
        )
        a._email_template = a._default_template()  # the template that uses it
    else:
        a, sent = _agent(
            settings_factory, tmp_path, monkeypatch, ai_humanize_emails=True
        )
    a.ai_gen = AIGenerator.__new__(AIGenerator)
    a.ai_gen.is_configured = True
    a.ai_gen.personal_data_allowed = True  # an agreement on record
    a.ai_gen._complete_with_retry = lambda prompt, label: line

    email = _first_email(a, sent, _stored(a, name="Maria Rossi", organization="Brand"))
    assert '<a href="//attacker.example' not in email["html_body"]
    assert html.escape(line) in email["html_body"]
    assert "&amp;lt;" not in email["html_body"]
    assert line in email["plain_body"]


def test_a_value_that_already_holds_an_entity_is_escaped_too(
    settings_factory, tmp_path, monkeypatch
):
    """A page can store an organisation reading "&amp;" beside a link. An
    escaper that skipped values that look escaped would let the link through;
    one that unescaped first would turn the stored "&amp;" into "&"."""
    from bs4 import BeautifulSoup

    stored = "&amp; " + LINK
    a, sent = _agent(settings_factory, tmp_path, monkeypatch)
    tag = f'<meta property="og:site_name" content="{html.escape(stored)}">'
    page = BeautifulSoup(f"<html><head>{tag}</head></html>", "html.parser")
    org = a.search._extract_org_name(page, "https://www.brand.de/kontakt")
    assert org == stored
    contact = _stored(a, name="Maria Rossi", organization=org)
    for email in (
        _first_email(a, sent, contact),
        _follow_up(a, sent, contact, "followup_1"),
        _follow_up(a, sent, contact, "followup_2"),
    ):
        assert '<a href="https://attacker.example' not in email["html_body"]
        assert html.escape(stored) in email["html_body"]
        assert stored in email["plain_body"]


def test_the_signature_is_escaped_once(settings_factory, tmp_path, monkeypatch):
    """The sender's own names are settings, not scraped, but they go into the
    same HTML, beside a body that already escaped the company name."""
    company = "Rossi & Figli <S.r.l.>"
    sender = 'Giulia "G" D\'Angelo'
    a, sent = _agent(
        settings_factory,
        tmp_path,
        monkeypatch,
        company_name=company,
        sender_name=sender,
    )
    contact = _stored(a, name="Maria Rossi", organization="Brand GmbH")
    for email in (_first_email(a, sent, contact), _follow_up(a, sent, contact)):
        assert "<S.r.l.>" not in email["html_body"]
        assert html.escape(company) in email["html_body"]
        assert html.escape(sender) in email["html_body"]
        assert "&amp;amp;" not in email["html_body"]
        assert company in email["plain_body"]
        assert sender in email["plain_body"]


def test_the_operators_links_are_escaped_once(settings_factory, tmp_path, monkeypatch):
    """BASE_URL and PROFILE_URL are settings that go inside attributes: the
    unsubscribe link, the open-tracking pixel and the company-profile link.
    A quote in one would end its attribute early."""
    base = 'https://outreach.example.test/o?a=1&b="2'
    profile = 'https://suppleco.example/profile?a=1&b="2'
    a, sent = _agent(
        settings_factory,
        tmp_path,
        monkeypatch,
        base_url=base,
        OPEN_TRACKING_ENABLED=True,
        attach_profile=False,
        profile_url=profile,
    )
    email = _first_email(a, sent, _stored(a, name="Maria Rossi", organization="Brand"))
    body = email["html_body"]
    assert 'b="2' not in body, "a quote in a setting ended an attribute early"
    assert "&amp;amp;" not in body
    # The template's own unsubscribe link, and the legal footer's.
    assert body.count(html.escape(email["unsubscribe_url"])) == 2
    assert html.escape(f"{base}/track/{email['tracking_id']}") in body
    assert body.count(html.escape(profile)) == 2  # the link and its text
    assert email["unsubscribe_url"] in email["plain_body"]
    assert profile in email["plain_body"]


def test_the_renderer_escapes_every_value_it_fills():
    """{{role}} is in no shipped template, but a template edited in the
    dashboard may use any placeholder."""
    template = (
        "<p>{{name}}|{{role}}|{{organization}}|{{company_name}}|{{sender_name}}</p>"
    )
    contact = {"name": SCRIPT, "role": QUOTES, "organization": "R&D GmbH"}
    html_body, plain = EmailSender.render_template(
        template, contact, "A & B", "<b>S</b>"
    )
    for raw in (SCRIPT, QUOTES, "R&D GmbH", "A & B", "<b>S</b>"):
        assert html.escape(raw) in html_body
    assert "&amp;amp;" not in html_body
    assert plain == f"{SCRIPT}|{QUOTES}|R&D GmbH|A & B|<b>S</b>"
