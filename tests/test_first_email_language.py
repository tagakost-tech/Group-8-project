"""SUPPLECO's first email, in Italian or in English.

On 10 November 2026 SUPPLECO sent its own first email, in both languages, and
asked for the Italian text to go to Italian customers and the English one to
everyone else. Until then the first email was written per contact, by an AI
or from a default paragraph, in English for everyone, under the subject
"Partnership Opportunity - <company>".

One function now decides the language of every email to a contact, from the
country its address shows: Italian for the countries in ITALIAN_COUNTRIES
(Italy by default), English otherwise, a .com or .eu address included. The two
first-email templates hold SUPPLECO's text as they wrote it, so nothing is
generated for them. Their placeholders are filled from settings: what the
sender is responsible for (SENDER_TITLE) and the signature (SENDER_SIGNATURE),
for each sending mailbox. The subject follows the language as well, and so
does Direct Send.
"""

import html
import itertools
import json
import re
import smtplib
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent import OutreachAgent
from config import Settings
from modules import compliance
from modules.email_sender import EmailSender

ROOT = Path(__file__).resolve().parent.parent

# SUPPLECO's text of 10 November 2026, with their placeholders, as they wrote it:
# "Dears,", the two spaces after "responsible for" and the rest are theirs.
ENGLISH = """\
Dears,
My name is {{Sender Name}} and I am responsible for  {{Sender’s Job title}} at Suppleco.
I am writing to introduce Suppleco as a CDMO and explore a potential partnership with {{organization}}.
We specialize in the development, manufacturing and packaging of Food Supplements, Medical Devices and Foods for Special Medical Purposes, in solid, liquid and semi-solid dosage forms.
Please find attached our Company Profile, which provides a comprehensive overview of our manufacturing capabilities, technologies and certifications.
Without any obligation on your side, I would welcome the opportunity to schedule a brief introductory call to present our company and share some relevant case studies of private label projects developed with other partners in the industry.
I look forward to your response.
Best regards,
{{Sender’s Signature}}
"""
ITALIAN = """\
Buongiorno,
Mi chiamo {{Sender Name}} e mi occupo di {{Sender’s Job title}} in Suppleco.
Le scrivo per presentarle Suppleco come CDMO e per esplorare una possibile collaborazione con {{organization}}.
Siamo specializzati nello sviluppo, produzione e confezionamento di Integratori Alimentari, Dispositivi Medici e Alimenti a Fini Medici Speciali, in forme farmaceutiche solide, liquide e semisolide.
In allegato troverà il nostro Company Profile, che offre una panoramica completa delle nostre capacità produttive, tecnologie e certificazioni.
Senza alcun impegno da parte sua, sarei lieto di organizzare una breve call introduttiva per presentarle la nostra azienda e condividere alcuni case study di progetti di private label sviluppati con altri partner del settore.
Resto in attesa di un suo riscontro.
Cordiali saluti,
{{Sender’s Signature}}
"""
TEXTS = {"it": ITALIAN, "en": ENGLISH}
SUBJECT = "Suppleco CDMO | Company Profile e Capabilities"
SENDER = "Giulia Bianchi"
COMPANY = "Suppleco"
# One recipient per language: the .de one stands for every foreign customer.
TO = {"it": "acquisti@rossi-integratori.it", "en": "einkauf@brand.de"}

# An operator's own first email, which still asks for the parts the product
# writes per contact.
OWN_TEMPLATE = (
    "<html><body><p>{{greeting}}</p><p>{{opening_line}}</p>{{body}}"
    "<p>{{sender_name}}</p>"
    '<p><a href="{{unsubscribe_url}}">unsubscribe here</a></p>'
    "{{legal_footer}}{{tracking_pixel}}</body></html>"
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
            raise smtplib.SMTPException("451 4.7.1 try again later")
        self.sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


def _agent(settings_factory, tmp_path, **kw):
    """The real agent and send path. Each mailbox's hand-off to SMTP is
    replaced by a recorder, which keeps what would have been sent."""
    settings = {
        "db_path": str(tmp_path / "a.db"),
        "verify_emails": False,
        "send_window_enabled": False,
        "blacklist_check_enabled": False,
        "bounce_guard_enabled": False,
        "imap_enabled": False,
        "smtp_username": "outreach@suppleco.example",
        "smtp_password": "pw",
        "sender_name": SENDER,
        "company_name": COMPANY,
    }
    a = OutreachAgent(settings_factory(**{**settings, **kw}))
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    sent = []
    for _key, _name, mailbox in a.senders:
        mailbox.dispatch_to_contact = lambda _from=mailbox.smtp_username, **k: (
            sent.append({**k, "from": _from}) or 1
        )
    return a, sent


def _stored(a, email, name="Maria Rossi", organization="Rossi Integratori"):
    cid = a.db.add_contact(
        a.db.log_search("q"), email=email, name=name, organization=organization
    )
    return a.db.get_contact_by_id(cid)


def _first_email(a, sent, email, dry_run=False, **fields):
    """The first email to a contact stored under this address, composed by the
    campaign's own send."""
    assert a._process_contact(_stored(a, email, **fields), dry_run=dry_run)
    return sent[-1]


def _title(a):
    return getattr(a.settings, "sender_title", "")


def _as_sent(text, title, organization="Rossi Integratori", signature=None):
    """SUPPLECO's text with their placeholders filled, line by line."""
    if signature is None:
        signature = [SENDER, title, COMPANY]
    return (
        text.replace("{{Sender Name}}", SENDER)
        .replace("{{Sender’s Job title}}", title)
        .replace("{{organization}}", organization)
        .replace("{{Sender’s Signature}}", "\n".join(signature))
        .splitlines()
    )


def _lines(email):
    return email["plain_body"].splitlines()


# ── One rule decides the language ──────────────────────────────────────────


@pytest.mark.parametrize(
    "address,language",
    [
        ("acquisti@rossi-integratori.it", "it"),
        ("ACQUISTI@ROSSI-INTEGRATORI.IT", "it"),
        ("einkauf@brand.de", "en"),
        ("achats@marque.fr", "en"),
        ("buyer@brand.co.uk", "en"),
        ("buyer@brand.com", "en"),
        ("buyer@brand.eu", "en"),
        ("buyer@brand.swiss", "en"),
        ("not an address", "en"),
    ],
)
def test_the_language_follows_the_country_the_address_shows(
    settings_factory, address, language
):
    """Italy gets Italian. A foreign country, and a domain that shows no
    country at all, get English."""
    assert compliance.email_language(settings_factory(), address) == language


def test_italian_countries_lists_who_is_written_to_in_italian(settings_factory):
    wider = settings_factory(ITALIAN_COUNTRIES="it, SM ,ch")
    assert compliance.italian_countries(wider) == {"IT", "SM", "CH"}
    assert compliance.email_language(wider, "info@farmacia.sm") == "it"
    assert compliance.email_language(wider, "info@marke.ch") == "it"
    assert compliance.email_language(wider, "info@marke.at") == "en"
    nobody = settings_factory(ITALIAN_COUNTRIES="")
    assert compliance.email_language(nobody, "info@rossi.it") == "en"


def test_italy_is_the_default():
    assert Settings(_env_file=None).italian_countries == "IT"


@pytest.mark.parametrize("value", ["Italy", "IT,ITA", "IT;SM", "I1"])
def test_italian_countries_takes_country_codes_only(settings_factory, value):
    """Validated as ENABLED_COUNTRIES is: a name or a typing slip would read
    as no country, and every Italian customer would get English."""
    with pytest.raises(ValidationError) as refused:
        settings_factory(ITALIAN_COUNTRIES=value)
    assert "ITALIAN_COUNTRIES takes two-letter country codes" in str(refused.value)


def test_the_country_lock_still_names_its_own_setting(settings_factory):
    with pytest.raises(ValidationError) as refused:
        settings_factory(ENABLED_COUNTRIES="Italy")
    assert "ENABLED_COUNTRIES takes two-letter country codes" in str(refused.value)


# ── The text is SUPPLECO's, in both languages ────────────────────────────────


@pytest.mark.parametrize("language", ["it", "en"])
def test_the_first_email_is_supplecos_text_word_for_word(
    settings_factory, tmp_path, language
):
    """The plain part opens with their lines and nothing else: no greeting by
    name, no generated paragraph, and their placeholders filled."""
    a, sent = _agent(settings_factory, tmp_path)
    email = _first_email(a, sent, TO[language])
    expected = _as_sent(TEXTS[language], _title(a))
    assert _lines(email)[: len(expected)] == expected
    for line in expected:
        assert html.escape(line) in email["html_body"]


@pytest.mark.parametrize("language", ["it", "en"])
def test_no_placeholder_is_left_for_the_recipient_to_read(
    settings_factory, tmp_path, language
):
    a, sent = _agent(settings_factory, tmp_path)
    email = _first_email(a, sent, TO[language])
    assert "{{" not in email["html_body"]
    assert "{{" not in email["plain_body"]
    assert "[set " not in email["plain_body"]


@pytest.mark.parametrize(
    "name,lang",
    [("email_template.html", "en"), ("email_template_it.html", "it")],
)
def test_each_template_keeps_the_layout_and_what_every_email_must_carry(name, lang):
    template = (ROOT / "templates" / name).read_text(encoding="utf-8")
    assert compliance.missing_placeholders(template) == []
    for kept in (
        "{{tracking_pixel}}",
        "{{sender_name}}",
        "{{sender_title}}",
        "{{sender_signature}}",
        "{{organization}}",
        f'<html lang="{lang}">',
        'width="560"',
    ):
        assert kept in template, f"{name} has lost {kept}"
    # Their placeholders became the product's, and nothing is left to generate.
    for gone in ("{{Sender", "{{greeting}}", "{{opening_line}}", "{{body}}"):
        assert gone not in template, f"{name} still holds {gone}"


def test_the_two_templates_differ_only_in_their_words():
    """One layout: the same tags, in the same order, with the same styles."""

    def markup(name):
        text = (ROOT / "templates" / name).read_text(encoding="utf-8")
        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        return [t for t in re.findall(r"<[^>]+>", text) if not t.startswith("<html")]

    assert markup("email_template_it.html") == markup("email_template.html")


@pytest.mark.parametrize(
    "language,wording",
    [("it", "può cancellarsi qui"), ("en", "you can unsubscribe here")],
)
def test_the_opt_out_line_is_in_the_emails_language(
    settings_factory, tmp_path, language, wording
):
    a, sent = _agent(settings_factory, tmp_path)
    email = _first_email(a, sent, TO[language])
    plain = " ".join(email["plain_body"].split())
    assert f"{wording} ({email['unsubscribe_url']})" in plain
    assert f'href="{html.escape(email["unsubscribe_url"])}"' in email["html_body"]
    if language == "it":
        assert "unsubscribe here" not in plain


class _CountingAI:
    """An AI provider that is configured, and counts what it is asked for."""

    is_configured = True

    def __init__(self):
        self.asked = []

    def generate_email_body(self, *_a, **_k):
        self.asked.append("body")
        return "A paragraph the AI wrote."

    def generate_opening_line(self, *_a, **_k):
        self.asked.append("opening line")
        return "A line the AI wrote."


@pytest.mark.parametrize("language", ["it", "en"])
def test_nothing_is_generated_for_a_text_that_uses_none_of_it(
    settings_factory, tmp_path, language
):
    """The AI switches are on and a provider is configured, and it is still
    not asked: a call for text that will not be used costs money and sends
    the contact's details to the provider for nothing."""
    a, sent = _agent(
        settings_factory,
        tmp_path,
        ai_humanize_emails=True,
        ai_personalization_enabled=True,
    )
    a.ai_gen = _CountingAI()
    email = _first_email(a, sent, TO[language])
    assert a.ai_gen.asked == []
    assert "the AI wrote" not in email["html_body"]
    assert not a._stats.get("ai_body") and not a._stats.get("ai_body_fallback")


def test_an_operators_own_template_keeps_the_parts_written_per_contact(
    settings_factory, tmp_path
):
    """A template that still asks for a greeting, an opening line and a body
    gets them as before, and its greeting follows the language."""
    a, sent = _agent(
        settings_factory,
        tmp_path,
        ai_humanize_emails=True,
        ai_personalization_enabled=True,
    )
    a.ai_gen = _CountingAI()
    a._email_template = OWN_TEMPLATE
    a._email_template_it = OWN_TEMPLATE
    english = _first_email(a, sent, TO["en"])
    assert _lines(english)[:3] == [
        "Hi Maria,",
        "A line the AI wrote.",
        "A paragraph the AI wrote.",
    ]
    italian = _first_email(a, sent, TO["it"])
    assert _lines(italian)[0] == "Buongiorno,"
    assert sorted(a.ai_gen.asked) == ["body", "body", "opening line", "opening line"]


# ── The placeholders ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "language,sentence",
    [
        ("it", "Mi chiamo Giulia Bianchi e mi occupo di {title} in Suppleco."),
        (
            "en",
            "My name is Giulia Bianchi and I am responsible for  {title} at Suppleco.",
        ),
    ],
)
def test_the_title_is_what_the_sender_is_responsible_for(
    settings_factory, tmp_path, language, sentence
):
    """SENDER_TITLE is text an operator typed: it is escaped once in the HTML
    and reads as typed in the plain part."""
    title = 'Quality & "Regulatory" <Affairs>'
    a, sent = _agent(settings_factory, tmp_path, sender_title=title)
    email = _first_email(a, sent, TO[language])
    assert sentence.format(title=title) in _lines(email)
    assert html.escape(sentence.format(title=title)) in email["html_body"]
    assert "<Affairs>" not in email["html_body"]
    assert "&amp;amp;" not in email["html_body"]


@pytest.mark.parametrize("language", ["it", "en"])
def test_a_blank_signature_is_the_name_the_title_and_the_company(
    settings_factory, tmp_path, language
):
    a, sent = _agent(
        settings_factory, tmp_path, sender_title="Sales", sender_signature=""
    )
    email = _first_email(a, sent, TO[language])
    closing = {"it": "Cordiali saluti,", "en": "Best regards,"}[language]
    at = _lines(email).index(closing)
    assert _lines(email)[at + 1 : at + 4] == [SENDER, "Sales", COMPANY]
    assert f"{SENDER}<br>Sales<br>{COMPANY}</p>" in email["html_body"]


@pytest.mark.parametrize(
    "typed",
    [
        r'Giulia "G" Bianchi\nR&D <Lab> Manager\n\nSuppleco S.r.l. | +39 02 0000 0000',
        'Giulia "G" Bianchi\nR&D <Lab> Manager\r\nSuppleco S.r.l. | +39 02 0000 0000 ',
    ],
    ids=["the two characters backslash n, as .env holds them", "real line breaks"],
)
def test_a_signature_is_its_lines_each_escaped_once(settings_factory, tmp_path, typed):
    """SENDER_SIGNATURE replaces the default signature whole, one line per
    "\\n". A quoted value in .env arrives with real line breaks instead."""
    lines = [
        'Giulia "G" Bianchi',
        "R&D <Lab> Manager",
        "Suppleco S.r.l. | +39 02 0000 0000",
    ]
    a, sent = _agent(settings_factory, tmp_path, sender_signature=typed)
    email = _first_email(a, sent, TO["en"])
    at = _lines(email).index("Best regards,")
    assert _lines(email)[at + 1 : at + 4] == lines
    assert (
        "<br>".join(html.escape(line) for line in lines) + "</p>" in email["html_body"]
    )
    assert "<Lab>" not in email["html_body"]
    assert "\\n" not in email["html_body"] and "\\n" not in email["plain_body"]
    assert "&amp;amp;" not in email["html_body"]


def test_a_signature_cannot_bring_markup_or_a_placeholder_with_it(
    settings_factory, tmp_path
):
    """The lines are escaped where the signature is built, and it goes in with
    everything else in one pass, so nothing in it is filled again."""
    a, sent = _agent(
        settings_factory,
        tmp_path,
        sender_signature=r'<a href="https://x.example">Giulia</a>\n{{organization}}',
    )
    email = _first_email(a, sent, TO["en"])
    assert '<a href="https://x.example"' not in email["html_body"]
    assert "{{organization}}" in _lines(email)


def _mailboxes(a, sent, language):
    """The first email from each sending mailbox in turn, keyed by mailbox."""
    domain = TO[language].rsplit(".", 1)[1]
    for i, (key, _name, mailbox) in enumerate(a.senders):
        contact = _stored(a, f"buyer{i}@brand{i}.{domain}")
        assert a._process_contact(contact, sender=mailbox, account_key=key)
    return {email["from"]: email for email in sent}


def test_each_numbered_mailbox_signs_with_its_own_title_and_signature(
    settings_factory, tmp_path
):
    a, sent = _agent(
        settings_factory,
        tmp_path,
        sender_title="Business Development",
        sender_signature=r"Giulia Bianchi\nHead of Business Development\nSuppleco",
        sender_2_email="marco@suppleco.example",
        sender_2_password="pw2",
        sender_2_name="Marco Ferri",
        sender_2_title="Export Sales",
        sender_2_signature=r"Marco Ferri\nExport Sales Manager\nSuppleco",
        sender_3_email="anna@suppleco.example",
        sender_3_password="pw3",
        sender_3_name="Anna Conti",
        sender_4_email="giulia.b@suppleco.example",
        sender_4_password="pw4",
    )
    by_mailbox = _mailboxes(a, sent, "en")
    own = _lines(by_mailbox["marco@suppleco.example"])
    assert (
        "My name is Marco Ferri and I am responsible for  Export Sales at Suppleco."
        in own
    )
    assert own[own.index("Best regards,") + 1 :][:3] == [
        "Marco Ferri",
        "Export Sales Manager",
        "Suppleco",
    ]
    # A mailbox with a name of its own and nothing else takes the shared
    # title, and is never signed with the first mailbox's signature: that
    # signature names someone else.
    named = _lines(by_mailbox["anna@suppleco.example"])
    assert (
        "My name is Anna Conti and I am responsible for  Business Development at Suppleco."
        in named
    )
    assert named[named.index("Best regards,") + 1 :][:3] == [
        "Anna Conti",
        "Business Development",
        "Suppleco",
    ]
    assert "Giulia Bianchi" not in by_mailbox["anna@suppleco.example"]["plain_body"]
    # A second mailbox of the same sender, with no name of its own, signs as
    # the first does.
    same = _lines(by_mailbox["giulia.b@suppleco.example"])
    assert same[same.index("Best regards,") + 1 :][:3] == [
        "Giulia Bianchi",
        "Head of Business Development",
        "Suppleco",
    ]


def test_a_mailbox_listed_in_sender_accounts_has_its_own_too(
    settings_factory, tmp_path
):
    a, sent = _agent(
        settings_factory,
        tmp_path,
        sender_title="Business Development",
        sender_accounts=json.dumps(
            [
                {
                    "username": "marco@suppleco.example",
                    "password": "pw2",
                    "sender_name": "Marco Ferri",
                    "sender_title": "Vendite Italia",
                    "sender_signature": "Marco Ferri\\nVendite Italia\\nSuppleco",
                }
            ]
        ),
    )
    italian = _lines(_mailboxes(a, sent, "it")["marco@suppleco.example"])
    assert "Mi chiamo Marco Ferri e mi occupo di Vendite Italia in Suppleco." in italian
    assert italian[italian.index("Cordiali saluti,") + 1 :][:3] == [
        "Marco Ferri",
        "Vendite Italia",
        "Suppleco",
    ]


@pytest.mark.parametrize(
    "language,sentence",
    [
        ("it", "per esplorare una possibile collaborazione con la Sua azienda."),
        ("en", "and explore a potential partnership with your company."),
    ],
)
@pytest.mark.parametrize("organization", [None, ""])
def test_a_contact_without_an_organisation_reads_in_the_emails_language(
    settings_factory, tmp_path, language, sentence, organization
):
    a, sent = _agent(settings_factory, tmp_path)
    email = _first_email(a, sent, TO[language], organization=organization)
    assert sentence in email["plain_body"]
    assert "your organization" not in email["plain_body"]


def test_the_renderer_fills_the_new_placeholders_as_text_and_markup():
    """A title is a value, escaped by the renderer; a signature is markup,
    escaped where it was built. The organisation falls back by language."""
    template = "<p>{{sender_title}}|{{organization}}</p><p>{{sender_signature}}</p>"
    html_body, plain = EmailSender.render_template(
        template,
        {"email": "a@b.it"},
        "Suppleco",
        "Giulia",
        values={"sender_title": "R&D"},
        markup={"sender_signature": "Giulia<br>Suppleco"},
        language="it",
    )
    assert html_body == "<p>R&amp;D|la Sua azienda</p><p>Giulia<br>Suppleco</p>"
    assert plain == "R&D|la Sua azienda\nGiulia\nSuppleco"


# ── The subject follows the language ───────────────────────────────────────


def test_the_fixed_client_sends_its_subject_and_omegas_are_the_defaults(
    settings_factory, tmp_path, monkeypatch
):
    a, sent = _agent(settings_factory, tmp_path)
    assert _first_email(a, sent, TO["it"])["subject"] == SUBJECT
    assert _first_email(a, sent, TO["en"])["subject"] == SUBJECT

    monkeypatch.delenv("EMAIL_SUBJECT")
    monkeypatch.delenv("EMAIL_SUBJECT_IT")
    defaults = Settings(_env_file=None)
    assert defaults.email_subject == "Special cold-formed parts to your drawing | Omega IFS"
    assert defaults.email_subject_it == (
        "Particolari speciali stampati a freddo su disegno | Omega IFS"
    )


def test_an_italian_recipient_gets_the_italian_subject(settings_factory, tmp_path):
    a, sent = _agent(
        settings_factory,
        tmp_path,
        email_subject="{company_name}: company profile for {org}",
        email_subject_it="{company_name}: profilo aziendale per {org}",
    )
    assert (
        _first_email(a, sent, TO["it"])["subject"]
        == "Suppleco: profilo aziendale per Rossi Integratori"
    )
    assert (
        _first_email(a, sent, TO["en"])["subject"]
        == "Suppleco: company profile for Rossi Integratori"
    )


def test_a_subject_names_an_unknown_organisation_in_its_own_language(
    settings_factory, tmp_path
):
    a, sent = _agent(
        settings_factory,
        tmp_path,
        email_subject="Company profile for {org}",
        email_subject_it="Profilo aziendale per {org}",
    )
    italian = _first_email(a, sent, TO["it"], organization=None)
    assert italian["subject"] == "Profilo aziendale per la Sua azienda"
    english = _first_email(a, sent, TO["en"], organization=None)
    assert english["subject"] == "Company profile for your brand"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_italian_subject_falls_back_to_the_english_one(
    settings_factory, tmp_path, blank
):
    a, sent = _agent(
        settings_factory,
        tmp_path,
        email_subject="Company profile for {org}",
        email_subject_it=blank,
    )
    italian = _first_email(a, sent, TO["it"])
    assert italian["subject"] == "Company profile for Rossi Integratori"
    assert _lines(italian)[0] == "Buongiorno,", "the text stays Italian"


def test_a_blank_italian_subject_names_an_unknown_organisation_in_english(
    settings_factory, tmp_path
):
    """The English subject logic, its stand-in for a missing name included:
    an English subject is not finished with "la Sua azienda"."""
    a, sent = _agent(
        settings_factory,
        tmp_path,
        email_subject="Company profile for {org}",
        email_subject_it="",
    )
    italian = _first_email(a, sent, TO["it"], organization=None)
    assert italian["subject"] == "Company profile for your brand"


def _in_turn(monkeypatch):
    """random.choice, taking each list's options in turn, so a few sends cover
    them all. The bodies and the subjects are each one list, counted apart."""
    turns = {}
    monkeypatch.setattr(
        "modules.dispatch_pipeline.random.choice",
        lambda options: options[
            next(turns.setdefault(id(options), itertools.count())) % len(options)
        ],
    )


def test_the_subject_variants_are_for_english_recipients_only(
    settings_factory, tmp_path, monkeypatch
):
    _in_turn(monkeypatch)
    a, sent = _agent(
        settings_factory,
        tmp_path,
        ab_testing_enabled=True,
        email_subjects="First variant for {org},Second variant for {org}",
        email_subject_it="Profilo aziendale per {org}",
    )
    italian = [_first_email(a, sent, f"acquisti{i}@rossi{i}.it") for i in range(4)]
    assert {e["subject"] for e in italian} == {
        "Profilo aziendale per Rossi Integratori"
    }
    assert {e["ab_variant"] for e in italian} == {None}
    english = [_first_email(a, sent, f"einkauf{i}@brand{i}.de") for i in range(4)]
    assert {e["subject"] for e in english} == {
        "First variant for Rossi Integratori",
        "Second variant for Rossi Integratori",
    }
    assert {e["ab_variant"] for e in english} == {
        "email_template|s0",
        "email_template|s1",
    }


def test_with_no_italian_subject_an_italian_recipient_joins_the_subject_test(
    settings_factory, tmp_path, monkeypatch
):
    """Blank falls back to the English logic, variants included; the text
    stays the Italian one, and the variant recorded says so."""
    _in_turn(monkeypatch)
    a, sent = _agent(
        settings_factory,
        tmp_path,
        ab_testing_enabled=True,
        email_subjects="First variant for {org},Second variant for {org}",
        email_subject_it="",
    )
    italian = [_first_email(a, sent, f"acquisti{i}@rossi{i}.it") for i in range(4)]
    assert {e["subject"] for e in italian} == {
        "First variant for Rossi Integratori",
        "Second variant for Rossi Integratori",
    }
    assert {e["ab_variant"] for e in italian} == {
        "email_template_it|s0",
        "email_template_it|s1",
    }
    assert {_lines(e)[0] for e in italian} == {"Buongiorno,"}


def test_the_italian_text_is_never_an_arm_of_the_english_test(
    settings_factory, tmp_path, monkeypatch
):
    """The A/B body variants are every templates/email_template*.html, which
    the Italian file's name matches: taken for a variant, it would go to
    every second foreign customer."""
    _in_turn(monkeypatch)
    a, sent = _agent(
        settings_factory,
        tmp_path,
        ab_testing_enabled=True,
        email_subjects="First variant,Second variant",
    )
    assert [name for name, _text in a._template_variants] == ["email_template"]
    english = [_first_email(a, sent, f"einkauf{i}@brand{i}.de") for i in range(6)]
    assert {_lines(e)[0] for e in english} == {"Dears,"}
    italian = [_first_email(a, sent, f"acquisti{i}@rossi{i}.it") for i in range(6)]
    assert {_lines(e)[0] for e in italian} == {"Buongiorno,"}


# ── Every email to a contact is in one language ────────────────────────────


def _due(a):
    a.db.conn.execute(
        "UPDATE sequence_enrollments SET enrolled_at = '2000-01-01 00:00:00',"
        " last_sent_at = '2000-01-01 00:00:00'"
    )
    a.db.conn.commit()


@pytest.mark.parametrize(
    "language,openings",
    [
        ("it", ["Buongiorno,", "Buongiorno,", "Buongiorno,"]),
        ("en", ["Dears,", "Hi Maria,", "Hi Maria,"]),
    ],
)
def test_the_first_email_and_both_follow_ups_share_a_language(
    settings_factory, tmp_path, language, openings
):
    """The campaign's send, then the sequence engine's two steps as they fall
    due: the rule is asked each time, so the answer is the same each time."""
    a, sent = _agent(settings_factory, tmp_path, sequences_enabled=True)
    _first_email(a, sent, TO[language])
    a.db.conn.execute("UPDATE contacts SET status = 'emailed'")
    a.db.conn.commit()
    for _step in range(2):
        _due(a)
        a._process_sequences(dry_run=False)
    assert [_lines(email)[0] for email in sent] == openings
    notices = [email["notice_version"] for email in sent]
    assert len(set(notices)) == 1
    assert notices[0].endswith("-it") == (language == "it")


def test_without_the_italian_first_email_an_italian_contact_gets_the_english_one_whole(
    settings_factory, tmp_path
):
    """templates/email_template_it.html missing: the English email goes, with
    its English footer and notice, not the English text under Italian ones."""
    a, sent = _agent(settings_factory, tmp_path)
    a._email_template_it = None
    email = _first_email(a, sent, TO["it"])
    assert _lines(email)[0] == "Dears,"
    assert email["notice_version"] == "notice-li-2"
    assert "This is a commercial communication from" in email["plain_body"]
    assert "Questa è una comunicazione commerciale" not in email["plain_body"]


def test_without_the_italian_first_email_the_follow_ups_are_english_too(
    settings_factory, tmp_path
):
    """The contact read the English first email, so the sequence goes on in
    English, though Italian follow-ups are on disk."""
    a, sent = _agent(settings_factory, tmp_path, sequences_enabled=True)
    a._email_template_it = None
    _first_email(a, sent, TO["it"])
    a.db.conn.execute("UPDATE contacts SET status = 'emailed'")
    a.db.conn.commit()
    for _step in range(2):
        _due(a)
        a._process_sequences(dry_run=False)
    assert [_lines(email)[0] for email in sent] == ["Dears,", "Hi Maria,", "Hi Maria,"]
    assert {email["notice_version"] for email in sent} == {"notice-li-2"}
    assert not any(s["subject"].startswith("Faccio seguito") for s in sent)


@pytest.mark.parametrize("language,opening", [("it", "Buongiorno,"), ("en", "Dears,")])
def test_a_retry_sends_the_same_language(settings_factory, tmp_path, language, opening):
    """A first email the server deferred is sent again from the retry queue,
    which composes it afresh."""
    a, _sent = _agent(settings_factory, tmp_path)
    for _key, _name, mailbox in a.senders:
        del mailbox.dispatch_to_contact  # the real one, down to the SMTP session
    wire = _Wire(failures=1)
    a.email_sender._new_connection = lambda: wire
    contact = _stored(a, TO[language])
    assert a._process_contact(contact, dry_run=False) is False
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    a.db.conn.commit()
    a._process_retries()
    assert len(wire.sent) == 1, "the retry did not go"
    text = wire.sent[0].get_body(("plain",)).get_content()
    assert text.splitlines()[0] == opening


# ── Direct Send ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("preview", [True, False], ids=["preview", "send"])
@pytest.mark.parametrize("language", ["it", "en"])
def test_direct_send_writes_in_the_recipients_language(
    settings_factory, tmp_path, language, preview
):
    a, sent = _agent(
        settings_factory,
        tmp_path,
        email_subject="Company profile for {org}",
        email_subject_it="Profilo aziendale per {org}",
    )
    ok, message = a.send_single_email(
        TO[language],
        name="Maria Rossi",
        organization="Rossi Integratori",
        channel="referral",
        dry_run=preview,
    )
    assert ok, message
    email = sent[-1]
    expected = _as_sent(TEXTS[language], _title(a))
    assert _lines(email)[: len(expected)] == expected
    assert (
        email["subject"]
        == {
            "it": "Profilo aziendale per Rossi Integratori",
            "en": "Company profile for Rossi Integratori",
        }[language]
    )
    assert email["notice_version"].endswith("-it") == (language == "it")
    footer = {"it": "comunicazione commerciale", "en": "commercial communication"}
    assert footer[language] in email["plain_body"]
