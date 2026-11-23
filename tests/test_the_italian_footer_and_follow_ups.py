"""An Italian recipient reads Italian to the end of every email.

SUPPLECO asked for its Italian text to go to Italian customers. The first email
is only the start of what a contact gets: under it sits the legal footer, the
first layer of the privacy notice, and after it come two follow-ups. Left as
they were, an Italian customer would have read SUPPLECO's Italian, then a
notice in English, then two English follow-ups.

So the footer is written in the recipient's language, sentence for sentence,
and the notice version recorded with each send says which one went out: the
Italian versions end in "-it". The follow-ups have Italian templates and
subjects, with the same placeholders as the English ones, and when an Italian
file is missing the English email goes out whole, said once in the log.

The inbox reader knows our own email when it comes back quoted under a reply
by its opt-out line. It now knows the Italian line too, so the Italian footer,
which names the word to reply with, is never read as the recipient's words.
"""

import logging
import os
import re
import shutil
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from agent import OutreachAgent
from modules import compliance, reply_intent
from modules.contact_explainer import explain_contact
from modules.export import records_held_about, write_access_answer
from modules.file_handler import FileHandler

ROOT = Path(__file__).resolve().parent.parent
# The fixed client's email texts (conftest.py), not the live client's.
TEMPLATES = Path(os.environ["CLIENT_TEMPLATES_DIR"])
UNSUB = "https://outreach.example.test/unsubscribe/t"
CONTACT = {"email": "anna@marca.it", "source_url": "https://www.marca.it/contatti"}
BASES = ["consent", "existing_customer", "legitimate_interest"]
TO = {"it": "acquisti@rossi-integratori.it", "en": "einkauf@brand.de"}
FIRST_SUBJECT = "Suppleco CDMO | Company Profile e Capabilities"
FOLLOW_UPS = {
    "it": [
        (
            "Faccio seguito alla mia email precedente — {org}",
            "Mi permetto di dare seguito",
        ),
        ("Un ultimo messaggio per {org}", "questo sarà il mio ultimo messaggio"),
    ],
    "en": [
        ("Following up — {org}", "gently follow up"),
        ("One last note, {org}", "my last note"),
    ],
}


def _footer(settings_factory, basis="legitimate_interest", contact=CONTACT, **kw):
    return compliance.render_legal_footer(
        settings_factory(**kw), contact, UNSUB, basis=basis, language="it"
    )


def _words(html_text):
    """What the recipient reads: the text, without tags or link addresses."""
    return " ".join(BeautifulSoup(html_text, "html.parser").get_text(" ").split())


class _Wire:
    """An SMTP session that keeps each message it is handed, so no test opens
    a real one."""

    def __init__(self):
        self.sent = []

    def noop(self):
        return (250, b"ok")

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


def _agent(settings_factory, tmp_path, wire=False, **kw):
    """The real agent and send path. The hand-off to SMTP is replaced by a
    recorder, or (wire=True) only the SMTP session is, so the email log is
    written and the message is built."""
    settings = {
        "db_path": str(tmp_path / "a.db"),
        "verify_emails": False,
        "send_window_enabled": False,
        "blacklist_check_enabled": False,
        "bounce_guard_enabled": False,
        "imap_enabled": False,
        # A new mailbox's warm-up allows five sends a day; some tests send
        # three emails to each of several contacts.
        "warmup_enabled": False,
        "sequences_enabled": True,
        "smtp_username": "outreach@suppleco.example",
        "smtp_password": "pw",
        "sender_name": "Giulia Bianchi",
        "company_name": "Suppleco",
    }
    a = OutreachAgent(settings_factory(**{**settings, **kw}))
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    if wire:
        sent = _Wire()
        a.email_sender._new_connection = lambda: sent
        return a, sent.sent
    sent = []
    a.email_sender.dispatch_to_contact = lambda **k: sent.append(k) or 1
    return a, sent


def _stored(a, email, name="Maria Rossi", organization="Rossi Integratori"):
    cid = a.db.add_contact(
        a.db.log_search("q"), email=email, name=name, organization=organization
    )
    return a.db.get_contact_by_id(cid)


def _due(a, contact_id):
    a.db.conn.execute(
        "UPDATE sequence_enrollments SET enrolled_at = '2000-01-01 00:00:00',"
        " last_sent_at = '2000-01-01 00:00:00' WHERE contact_id = ?",
        (contact_id,),
    )
    a.db.conn.commit()


def _follow_ups(a, sent, email, **fields):
    """Both follow-ups to a contact stored under this address: the campaign's
    first email, then the sequence engine's two steps as each falls due."""
    contact = _stored(a, email, **fields)
    assert a._process_contact(contact, dry_run=False), "the first email did not go"
    a.db.update_contact_status(contact["id"], "emailed")
    before = len(sent)
    for _step in range(2):
        _due(a, contact["id"])
        a._process_sequences(dry_run=False)
    assert len(sent) == before + 2, "a follow-up did not go"
    return sent[-2:]


def _template(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


# ── The footer, in Italian ─────────────────────────────────────────────────


@pytest.mark.parametrize("basis", BASES)
def test_the_italian_footer_carries_every_first_layer_item(settings_factory, basis):
    footer = _footer(settings_factory, basis)
    for must in (
        "comunicazione commerciale",
        "Test Manufacturing S.r.l.",  # the controller
        "privacy@example.test",  # the privacy contact
        "il Suo nome, il Suo ruolo e il Suo indirizzo email aziendale",  # the data
        "marca.it",  # where it was found
        "produzione conto terzi di integratori alimentari",  # the purpose
        "fornitore di posta elettronica",  # the recipients
        "365 giorni",  # the retention
        "l'accesso",
        "la rettifica",
        "la cancellazione",
        "reclamo",
        "il Garante",  # the rights, with a complaint
        "https://example.test/privacy",  # the full notice
    ):
        assert must in footer, f"{basis}: the Italian footer is missing {must!r}"


def test_each_basis_states_its_own_basis_in_italian(settings_factory):
    li = _footer(settings_factory, "legitimate_interest")
    consent = _footer(settings_factory, "consent")
    customer = _footer(settings_factory, "existing_customer")
    assert "legittimo interesse" in li and "art. 6, par. 1, lett. f)" in li
    assert "consenso" in consent and "revocare" in consent
    assert "art. 6, par. 1, lett. a)" in consent
    assert "legittimo interesse" not in consent, "a consenting contact is misinformed"
    assert "nostra cliente" in customer and "art. 130, comma 4" in customer


@pytest.mark.parametrize("basis", BASES)
@pytest.mark.parametrize(
    "channel", [None, "inbound_request", "trade_fair", "existing_customer", "referral"]
)
def test_no_english_sentence_is_left_in_the_italian_footer(
    settings_factory, basis, channel
):
    """Every sentence the footer can write, switched on: the AI line, the
    retention line, and each way an operator's contact reached SUPPLECO."""
    contact = {"email": "anna@marca.it", "source_channel": channel}
    read = _words(
        _footer(settings_factory, basis, contact, ai_humanize_emails=True)
    ).lower()
    for english in (
        "this is",
        "we hold",
        "we rely",
        "we write",
        "obtained from",
        "your company",
        "a trade fair",
        "a referral",
        "our dealings",
        "publicly available",
        "email provider",
        "an ai service",
        "we erase",
        "days after",
        "you may",
        "privacy contact",
        "full notice",
        "unsubscribe",
        " reply ",
    ):
        assert english not in read, f"{basis}/{channel}: {english!r} is English"


def test_the_ai_line_and_the_retention_line_are_italian(settings_factory):
    plain = _words(_footer(settings_factory))
    assert "intelligenza artificiale" not in plain
    with_ai = _words(_footer(settings_factory, ai_reply_classification_enabled=True))
    assert (
        "per recapitare le nostre email, e un modello di intelligenza artificiale "
        "in esecuzione sul nostro server ci aiuta a scrivere le nostre email e a "
        "leggere le risposte, senza che alcuna società esterna di intelligenza "
        "artificiale riceva tali dati; li cancelliamo 365 giorni dopo la nostra "
        "ultima email, se non risponde."
    ) in with_ai
    kept = _words(_footer(settings_factory, retention_enabled=False))
    assert "giorni" not in kept, "a period nobody enforces is a false statement"


@pytest.mark.parametrize(
    "channel,source",
    [
        ("inbound_request", "una richiesta della Sua azienda"),
        ("trade_fair", "una fiera"),
        ("existing_customer", "i nostri rapporti con la Sua azienda"),
        ("referral", "una segnalazione"),
        (None, "fonti pubblicamente accessibili"),
    ],
)
def test_where_the_address_came_from_is_said_in_italian(
    settings_factory, channel, source
):
    contact = {"email": "anna@marca.it", "send_channel": channel}
    assert f"ottenuti tramite {source}," in _words(
        _footer(settings_factory, contact=contact)
    )


def test_the_objection_keeps_a_paragraph_of_its_own(settings_factory):
    footer = _footer(settings_factory)
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", footer, flags=re.DOTALL)
    assert len(paragraphs) == 3
    [objection] = [p for p in paragraphs if UNSUB in p]
    assert "Può opporsi a queste email in qualsiasi momento" in objection
    assert "privacy" not in objection.lower(), "the objection shares its paragraph"


@pytest.mark.parametrize("language,word", [("it", "cancellami"), ("en", "unsubscribe")])
def test_the_word_the_footer_says_to_reply_with_is_one_the_inbox_reader_acts_on(
    settings_factory, language, word
):
    """The footer promises that a reply holding one word stops the emails.
    The promise holds only while the inbox reader takes that word for an
    opt-out, so the word is read from the footer and put to the reader."""
    footer = compliance.render_legal_footer(
        settings_factory(), CONTACT, UNSUB, language=language
    )
    quoted = re.findall(r'"(\w+)"', _words(footer))
    assert quoted == [word]
    assert reply_intent.is_optout("", quoted[0])
    assert reply_intent.dsr_kind("", quoted[0]) is None


def test_the_identity_is_the_same_legal_identity(settings_factory):
    """The identity line is the company's own details and the two mentions
    Cod. Civ. 2250 asks for, which are Italian in the English footer too."""
    english = compliance.render_legal_footer(
        settings_factory(legal_single_member="yes"), CONTACT, UNSUB
    )
    italian = _footer(settings_factory, legal_single_member="yes")
    first = r"<p[^>]*>(.*?)</p>"
    assert re.search(first, italian)[1] == re.search(first, english)[1]
    for must in (
        "Test Manufacturing S.r.l.",
        "Via Roma 1, 00100 Roma RM, Italy",
        "P.IVA IT00000000000",
        "Cap. soc. EUR 10.000,00 i.v.",
        "Socio unico",
    ):
        assert must in italian
    assert "Socio unico" not in _footer(settings_factory, legal_single_member="no")


def test_a_blank_field_is_marked_and_values_are_escaped(settings_factory):
    blank = _footer(settings_factory, legal_entity_name="", privacy_notice_url="")
    assert "[set LEGAL_ENTITY_NAME]" in blank
    assert "[set PRIVACY_NOTICE_URL]" in blank
    typed = _footer(settings_factory, legal_entity_name="A & B <Srl>")
    assert "A &amp; B &lt;Srl&gt;" in typed
    assert "<Srl>" not in typed
    scraped = _footer(settings_factory, contact={"source_url": 'https://x.it"><b>/c'})
    assert "<b>" not in scraped


def test_the_footer_is_english_unless_italian_is_asked_for(settings_factory):
    s = settings_factory()
    default = compliance.render_legal_footer(s, CONTACT, UNSUB)
    assert "This is a commercial communication" in default
    for language in ("en", "fr", ""):
        assert compliance.render_legal_footer(s, CONTACT, UNSUB, language=language) == (
            default
        )


# ── The notice version says which footer went out ──────────────────────────


@pytest.mark.parametrize("basis", BASES)
def test_the_italian_notice_has_a_version_of_its_own(basis):
    italian = compliance.notice_version(basis, "it")
    assert italian.endswith("-it")
    assert italian != compliance.notice_version(basis)
    assert compliance.notice_version(basis, "en") == compliance.notice_version(basis)


def test_an_unknown_basis_gets_the_italian_legitimate_interest_notice():
    assert compliance.notice_version(None, "it") == compliance.notice_version(
        "legitimate_interest", "it"
    )
    assert len({compliance.notice_version(b, "it") for b in BASES}) == 3


# The words of each Italian version, fingerprinted, as the English ones are in
# tests/test_the_footer_is_the_first_layer_notice.py. Changing the words
# without a new version fails here: give the text a new version in
# NOTICE_VERSIONS_IT, then record its fingerprint below. (The native-speaker
# review changed the words of all three while keeping their ids: no email had
# gone out with the Italian notice yet.)
# Version 1 (7261b2a68c8fa21d, 344761061f2b703f, ac0a2cf36f0be5f9) said "un
# servizio di intelligenza artificiale"; version 2 says the model runs on
# SUPPLECO's own server and no outside AI company receives the data.
RECORDED_IT = {
    "notice-li-2-it": "5d62d91e1c8d26c7",
    "notice-consent-2-it": "f56a3afef57522e6",
    "notice-customer-2-it": "ad3502f5c7b5c168",
}


def test_changed_italian_words_need_a_new_version():
    assert set(compliance.NOTICE_VERSIONS_IT.values()) == set(RECORDED_IT)
    for basis, version in compliance.NOTICE_VERSIONS_IT.items():
        assert compliance.notice_fingerprint(basis, "it") == RECORDED_IT[version], basis
        assert compliance.notice_fingerprint(basis, "it") != (
            compliance.notice_fingerprint(basis)
        )


def test_the_send_log_records_which_notice_went_out(settings_factory, tmp_path):
    a, wire = _agent(settings_factory, tmp_path, wire=True)
    for address in TO.values():
        assert a._process_contact(_stored(a, address), dry_run=False)
    assert len(wire) == 2
    logged = {r["recipient_email"]: r["notice_version"] for r in a.db.get_all_emails()}
    assert logged == {
        TO["it"]: compliance.notice_version("legitimate_interest", "it"),
        TO["en"]: compliance.notice_version("legitimate_interest"),
    }


def test_a_consenting_italian_contact_gets_the_italian_consent_notice(
    settings_factory, tmp_path
):
    a, sent = _agent(settings_factory, tmp_path)
    ok, message = a.send_single_email(TO["it"], channel="inbound_request")
    assert ok, message
    assert sent[0]["notice_version"] == compliance.notice_version("consent", "it")
    assert "revocare" in sent[0]["html_body"]
    assert "legittimo interesse" not in sent[0]["html_body"]
    # Not "fonti pubblicamente accessibili": the operator said how.
    assert "una richiesta della Sua azienda" in sent[0]["html_body"]


def test_the_access_answer_and_the_why_panel_tell_the_two_notices_apart(
    settings_factory, tmp_path
):
    """The version is read where it is shown: the Art. 15 answer lists every
    email with the notice it carried, and so does the contact's history."""
    a, _wire = _agent(settings_factory, tmp_path, wire=True)
    versions = {
        "it": compliance.notice_version("legitimate_interest", "it"),
        "en": compliance.notice_version("legitimate_interest"),
    }
    for language, address in TO.items():
        contact = _stored(a, address)
        assert a._process_contact(contact, dry_run=False)
        held = records_held_about(a.db, address)
        assert [e["notice_version"] for e in held["email_log"]] == [versions[language]]
        folder = write_access_answer(
            a.db, address, controller="Suppleco", notice_url="https://example.test/p"
        )
        answer = (folder / "records.json").read_text(encoding="utf-8")
        assert f'"notice_version": "{versions[language]}"' in answer
        history = explain_contact(a.db, contact["id"])["timeline"]
        emails = [e["what"] for e in history if e["what"].startswith("Email sent")]
        said = {"it": "in Italian", "en": "in English"}[language]
        assert emails == [
            f'Email sent: "{FIRST_SUBJECT}" ({versions[language]}) ({said})'
        ]


def test_the_history_of_an_email_without_a_version_is_unchanged(
    settings_factory, tmp_path
):
    """Emails sent before versions were recorded have none to show."""
    a, _sent = _agent(settings_factory, tmp_path)
    contact = _stored(a, TO["en"])
    a.db.log_email(contact["id"], TO["en"], "Hello", delivery_status="sent")
    history = explain_contact(a.db, contact["id"])["timeline"]
    assert 'Email sent: "Hello"' in [e["what"] for e in history]


# ── The follow-ups, in Italian ─────────────────────────────────────────────


@pytest.mark.parametrize("step", ["followup_1", "followup_2"])
def test_each_italian_follow_up_has_the_placeholders_of_the_english_one(step):
    english = _template(f"{step}.html")
    italian = _template(f"{step}_it.html")
    placeholders = r"\{\{\w+\}\}"
    assert sorted(re.findall(placeholders, italian)) == sorted(
        re.findall(placeholders, english)
    )
    assert compliance.missing_placeholders(italian) == []
    assert '<html lang="it">' in italian
    # One layout: the same tags in the same order, with the same styles.
    tags = r"<(?!html|title)[^>]+>"
    assert re.findall(tags, italian) == re.findall(tags, english)


@pytest.mark.parametrize("language", ["it", "en"])
def test_a_contact_gets_both_follow_ups_in_their_language(
    settings_factory, tmp_path, language
):
    a, sent = _agent(settings_factory, tmp_path)
    emails = _follow_ups(a, sent, TO[language])
    greeting = {"it": "Buongiorno,", "en": "Hi Maria,"}[language]
    footer = {"it": "comunicazione commerciale", "en": "commercial communication"}
    for email, (subject, wording) in zip(emails, FOLLOW_UPS[language]):
        assert email["subject"] == subject.format(org="Rossi Integratori")
        assert email["plain_body"].splitlines()[0] == greeting
        assert wording in email["plain_body"]
        assert footer[language] in email["plain_body"]
        assert email["notice_version"] == compliance.notice_version(
            "legitimate_interest", language
        )
        assert "{{" not in email["html_body"] and "{{" not in email["plain_body"]


@pytest.mark.parametrize(
    "name,english",
    [
        ("Maria Rossi", "Hi Maria,"),
        ("Dott.ssa Anna Bianchi", "Hi Anna,"),
        ("gian luca bianchi", "Hi Gian,"),
        (None, "Hi there,"),
        ("Sir/Madam", "Hi there,"),
        ("Chi Siamo", "Hi there,"),
        ("Servizio Clienti Rossi Integratori", "Hi there,"),
        ("Dott. Rossi", "Hi there,"),
    ],
)
def test_an_italian_greeting_opens_as_the_clients_first_email_does(name, english):
    """SUPPLECO's own first email opens "Buongiorno," with no name, and a first
    name before formal Lei text mixes two registers: the Italian follow-ups
    open the same way whatever the name. The English greeting is unchanged."""
    assert OutreachAgent._build_greeting({"name": name}, "it") == "Buongiorno,"
    assert OutreachAgent._build_greeting({"name": name}) == english
    assert OutreachAgent._build_greeting({"name": name}, "en") == english


def test_an_italian_follow_up_reads_well_without_an_organisation(
    settings_factory, tmp_path
):
    """ "la Sua azienda" stands in, after a preposition that takes it."""
    a, sent = _agent(settings_factory, tmp_path)
    first, last = _follow_ups(a, sent, TO["it"], organization=None)
    assert first["subject"] == (
        "Faccio seguito alla mia email precedente — la Sua azienda"
    )
    assert last["subject"] == "Un ultimo messaggio per la Sua azienda"
    assert "So che presso la Sua azienda le giornate sono piene" in _words(
        first["html_body"]
    )
    assert "non è al momento una priorità per la Sua azienda" in _words(
        last["html_body"]
    )
    for email in (first, last):
        assert "your" not in email["plain_body"].split("Test Manufacturing")[0]


def _without(tmp_path, *names):
    """A product folder whose templates/ lacks these files, as it would be
    after someone deleted them."""
    root = tmp_path / "product"
    shutil.copytree(
        TEMPLATES, root / "templates", ignore=shutil.ignore_patterns(*names)
    )
    return FileHandler(str(root))


def test_without_the_italian_file_the_english_follow_up_goes_whole_said_once(
    settings_factory, tmp_path, caplog
):
    """Not an English text under an Italian subject and over an Italian
    footer: the email is English throughout, and its notice version says so.
    The next step, whose Italian file is there, is Italian again."""
    a, sent = _agent(settings_factory, tmp_path)
    a.file_handler = _without(tmp_path, "followup_1_it.html")
    with caplog.at_level(logging.WARNING):
        for i in range(3):
            first, last = _follow_ups(a, sent, f"acquisti@rossi{i}.it")
            assert first["subject"] == "Following up — Rossi Integratori"
            assert first["plain_body"].splitlines()[0] == "Hi Maria,"
            assert "gently follow up" in first["plain_body"]
            assert "commercial communication" in first["plain_body"]
            assert "comunicazione commerciale" not in first["plain_body"]
            assert first["notice_version"] == compliance.notice_version(
                "legitimate_interest"
            )
            assert last["subject"] == "Un ultimo messaggio per Rossi Integratori"
            assert "questo sarà il mio ultimo messaggio" in last["plain_body"]
    said = [r for r in caplog.records if "followup_1_it.html" in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in said]
    assert said[0].levelno == logging.WARNING


def _blanked(tmp_path, monkeypatch, *names):
    """The product folder with these templates holding only white space, as
    an editor can leave them, read by an agent started after the edit."""
    handler = _without(tmp_path)
    for name in names:
        (handler.root / "templates" / name).write_text(" \n\t\n", encoding="utf-8")
    monkeypatch.setattr("agent.FileHandler", lambda: handler)


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "live"])
def test_a_blank_italian_first_email_counts_as_missing(
    settings_factory, tmp_path, monkeypatch, dry_run
):
    """A blank Italian text was taken as present: a dry run showed an empty
    email under the Italian notice, and the live send was refused by the
    footer guard for every Italian contact. Blank is missing: English goes."""
    _blanked(tmp_path, monkeypatch, "email_template_it.html")
    a, sent = _agent(settings_factory, tmp_path)
    assert a._process_contact(_stored(a, TO["it"]), dry_run=dry_run)
    assert sent[0]["notice_version"] == compliance.notice_version(
        "legitimate_interest"
    )
    assert sent[0]["plain_body"].splitlines()[0] == "Dears,"
    assert "commercial communication" in sent[0]["plain_body"]
    assert "comunicazione commerciale" not in sent[0]["plain_body"]


def test_a_blank_italian_follow_up_counts_as_missing(
    settings_factory, tmp_path, monkeypatch
):
    _blanked(tmp_path, monkeypatch, "followup_1_it.html")
    a, sent = _agent(settings_factory, tmp_path)
    first, last = _follow_ups(a, sent, TO["it"])
    assert first["subject"] == "Following up — Rossi Integratori"
    assert "gently follow up" in first["plain_body"]
    assert first["notice_version"] == compliance.notice_version("legitimate_interest")
    assert "questo sarà il mio ultimo messaggio" in last["plain_body"]


def test_follow_ups_carry_no_attachment_in_either_language(settings_factory, tmp_path):
    pdf = tmp_path / "Suppleco Company Profile.pdf"
    pdf.write_bytes(b"%PDF-1.4 the profile")
    a, wire = _agent(settings_factory, tmp_path, wire=True, pdf_path=str(pdf))
    for address in TO.values():
        _follow_ups(a, wire, address)
    attached = [[p.get_filename() for p in msg.iter_attachments()] for msg in wire]
    assert attached == [[pdf.name], [], []] * 2
    assert "Mi permetto di dare seguito" in wire[1].get_body(("plain",)).get_content()


@pytest.mark.parametrize("step", ["followup_1_it.html", "followup_2_it.html"])
def test_the_italian_follow_ups_use_lei_and_do_not_gender_the_sender(step):
    """SUPPLECO's own text says "Le scrivo". A sending mailbox may be anyone's,
    so the sender is never "lieto" or "lieta"."""
    read = _words(_template(step))
    informal = re.findall(
        r"\b(?:tu|te|ti|tuo|tua|tuoi|tue|voi|vostr[oaie])\b", read, re.IGNORECASE
    )
    assert informal == [], f"{step} addresses the reader informally"
    assert re.search(r"\b(?:Sua|Suo|Suoi|Sue|Le|Lei)\b", read), f"{step} has no Lei"
    gendered = re.findall(
        r"\b(?:liet|grat|content|sicur|convint|onorat|dispost|pront|felicissim)[oa]\b",
        read,
        re.IGNORECASE,
    )
    assert gendered == [], f"{step} genders its sender"


# ── Our own email, quoted under a reply ────────────────────────────────────


def _our_emails(a, sent, language):
    """The plain text of the first email and both follow-ups, by name."""
    first_at = len(sent)
    _follow_ups(a, sent, TO[language])
    names = ["first email", "follow-up 1", "follow-up 2"]
    emails = dict(zip(names, sent[first_at:]))
    opening = {"it": "Buongiorno,", "en": "Dears,"}[language]
    assert emails["first email"]["plain_body"].splitlines()[0] == opening
    assert FOLLOW_UPS[language][0][1] in emails["follow-up 1"]["plain_body"]
    return emails


REPLIES = {
    "it": "Buongiorno, grazie del messaggio: ne parlo con la direzione e Le faccio sapere.",
    "en": "Thank you for your message. I will raise it with our management.",
}


@pytest.mark.parametrize("which", ["first email", "follow-up 1", "follow-up 2"])
@pytest.mark.parametrize("language", ["it", "en"])
def test_a_reply_above_our_quoted_email_is_judged_on_its_own_words(
    settings_factory, tmp_path, language, which
):
    """Some mail apps quote the original with no mark at all. Our footer
    names the word to reply with and the right to erasure, so read as the
    recipient's it would opt them out, or open an erasure request. The inbox
    reader cuts our email at its opt-out line, and what is left above it
    holds none of the words it acts on."""
    a, sent = _agent(settings_factory, tmp_path)
    ours = _our_emails(a, sent, language)[which]
    reply = f"{REPLIES[language]}\n\n{ours['plain_body']}"
    subject = f"Re: {ours['subject']}"
    fresh = reply_intent.strip_quoted(reply)
    assert fresh.startswith(REPLIES[language])
    for footer_word in ("Garante", "cancellami", "unsubscribe", "privacy@example.test"):
        assert footer_word not in fresh, f"our footer was read as theirs: {footer_word}"
    assert not reply_intent.is_optout(subject, fresh)
    assert reply_intent.dsr_kind(subject, fresh) is None
    assert not reply_intent.is_decline(subject, reply)
    assert reply_intent.classify(subject, reply) == reply_intent.UNCLEAR


@pytest.mark.parametrize(
    "theirs,optout,kind",
    [
        ("Per favore cancellatemi dalla vostra lista.", True, None),
        ("cancellami", True, None),
        ("Vi chiedo di cancellare i miei dati dal vostro database.", False, "erasure"),
    ],
)
def test_what_they_wrote_above_it_still_counts(
    settings_factory, tmp_path, theirs, optout, kind
):
    a, sent = _agent(settings_factory, tmp_path)
    ours = _our_emails(a, sent, "it")["first email"]
    fresh = reply_intent.strip_quoted(f"{theirs}\n\n{ours['plain_body']}")
    assert reply_intent.dsr_kind("", fresh) == kind
    assert reply_intent.is_optout("", fresh) is optout


# ── A native speaker's review of our own Italian ───────────────────────────


def test_the_italian_follow_ups_read_as_a_native_speaker_wrote_them(
    settings_factory, tmp_path
):
    """Read from the emails that go out. "Una collaborazione produttiva" is
    "a productive collaboration", not partnering on manufacturing;
    "varrebbe la pena" is a translated "worth exploring"; "intasare" (to
    clog) is harsh; "risentirci" implies a conversation that never happened."""
    a, sent = _agent(settings_factory, tmp_path)
    first, last = (_words(e["html_body"]) for e in _follow_ups(a, sent, TO["it"]))
    assert "Le andrebbe di fissare una breve call di 10 minuti" in first
    assert "Varrebbe la pena" not in first
    assert "Non vorrei appesantire la Sua casella di posta" in last
    assert "Se una collaborazione per la produzione non è al momento" in last
    assert "sarà un piacere sentirci" in last
    for gone in ("intasare", "produttiva", "risentirci"):
        assert gone not in last


@pytest.mark.parametrize(
    "language,phrase",
    [
        ("it", "supporto dalla formulazione alla commercializzazione"),
        ("en", "support from formulation to commercialization"),
    ],
)
def test_the_follow_ups_that_go_out_describe_suppleco_in_its_own_words(
    settings_factory, tmp_path, language, phrase
):
    """The follow-up files are read on every send, so the edited wording is
    what the next follow-up carries."""
    a, sent = _agent(settings_factory, tmp_path)
    for email in _follow_ups(a, sent, TO[language]):
        assert phrase in _words(email["html_body"])
        assert phrase in " ".join(email["plain_body"].split())


@pytest.mark.parametrize(
    "basis,sentence",
    [
        (
            "legitimate_interest",
            (
                "Il trattamento si basa sul legittimo interesse (art. 6, par. 1, "
                "lett. f), del GDPR): offrire"
            ),
        ),
        (
            "consent",
            (
                "Il trattamento si basa sul consenso che ci ha fornito (art. 6, par. "
                "1, lett. a), del GDPR), che può revocare"
            ),
        ),
        ("existing_customer", "(art. 130, comma 4, del D.Lgs. 196/2003)."),
    ],
)
def test_the_italian_notice_uses_the_terms_of_an_italian_notice(
    settings_factory, basis, sentence
):
    read = _words(_footer(settings_factory, basis))
    assert sentence in read
    for term in (
        "Il nostro fornitore di posta elettronica riceve tali dati per recapitare",
        "o la limitazione del trattamento,",
        "in Italia, il Garante per la protezione dei dati personali.",
    ):
        assert term in read, term
    for old in ("Ci basiamo", "Codice privacy", "li riceve", "del loro uso", "f)):"):
        assert old not in read, old


# ── The unsubscribe page, in the recipient's language ──────────────────────

PAGE_IT = [
    "Cancellazione completata",
    "Non riceverà più nostre email.",
    (
        "Per esserne certi, conserviamo il suo indirizzo in un elenco di "
        "esclusione e non lo usiamo per altro."
    ),
    (
        "Per far cancellare anche gli altri suoi dati, o se si tratta di un "
        "errore, risponda a una qualsiasi delle nostre email."
    ),
]
PAGE_EN = [
    "You have been unsubscribed",
    "We will not email you again.",
    (
        "To make sure of that, we keep your address on a do-not-contact list and "
        "use it for nothing else."
    ),
    (
        "To have your other details erased as well, or if this was a mistake, "
        "reply to any email we sent you."
    ),
]


def _unsubscribe_page(settings_factory, tmp_path, address, token=None):
    import main
    from modules.unsubscribe import UnsubscribeHandler

    s = settings_factory(
        db_path=str(tmp_path / "u.db"), unsubscribe_secret="a-test-secret-value"
    )
    client = main.create_dashboard_app(s).test_client()
    db = main.Database(s.get_abs_db_path())
    if token is None:
        token = UnsubscribeHandler(db, s.unsubscribe_secret).generate_token(address)
    response = client.get(f"/unsubscribe/{token}")
    return response.get_data(as_text=True), db


def test_an_italian_recipient_lands_on_an_italian_page(settings_factory, tmp_path):
    """Every statement the English page makes, and none it does not."""
    page, db = _unsubscribe_page(settings_factory, tmp_path, TO["it"])
    read = _words(page)
    assert '<html lang="it">' in page
    for sentence in PAGE_IT:
        assert sentence in read, sentence
    for sentence in PAGE_EN:
        assert sentence not in read, sentence
    assert len(re.findall(r"<(?:h1|p)\b", page)) == len(PAGE_IT)
    assert db.is_suppressed(TO["it"])


@pytest.mark.parametrize(
    "address,token",
    [
        (TO["en"], None),
        ("anna@marca.com", None),
        (None, "u" + "0" * 32),
    ],
)
def test_anyone_else_lands_on_the_english_page(
    settings_factory, tmp_path, address, token
):
    """An address outside ITALIAN_COUNTRIES, one whose domain shows no
    country, and a link whose address is not known."""
    page, _db = _unsubscribe_page(settings_factory, tmp_path, address, token)
    assert '<html lang="en">' in page
    assert "Cancellazione" not in page
    if address:
        for sentence in PAGE_EN:
            assert sentence in _words(page), sentence
