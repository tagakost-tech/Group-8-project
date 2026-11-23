"""The legal footer names the company with one full stop, not two.

The notice sentence ends after the company's name: "... from {company}."
Most Italian legal names end in a full stop of their own ("Suppleco S.r.l.",
"Rossi S.p.A."), so recipients read "from Suppleco S.r.l.. We hold ..." and
"di Suppleco S.r.l.. Trattiamo ...". The notice words are versioned and
fingerprinted, so they stay as they are; the name's own stop takes the place
of the sentence's when the footer is rendered, as the template's does for
"{{organization}}.".
"""

import pytest

from agent import OutreachAgent
from modules import compliance

TO = {"it": "acquisti@rossi-integratori.it", "en": "einkauf@brand.de"}
SENTENCE = {
    "en": ("This is a commercial communication from {name}", " We hold your name"),
    "it": ("Questa è una comunicazione commerciale di {name}", " Trattiamo il Suo"),
}


def _sent_email(settings_factory, tmp_path, legal_name, language):
    """The first email to one contact, through the real agent and render path;
    only the hand-off to SMTP is replaced by a recorder."""
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            warmup_enabled=False,
            smtp_username="outreach@suppleco.example",
            smtp_password="pw",
            sender_name="Giulia Bianchi",
            company_name="Suppleco",
            legal_entity_name=legal_name,
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    sent = []
    a.email_sender.dispatch_to_contact = lambda **k: sent.append(k) or 1
    cid = a.db.add_contact(
        a.db.log_search("q"),
        email=TO[language],
        name="Maria Rossi",
        organization="Rossi Integratori",
    )
    assert a._process_contact(a.db.get_contact_by_id(cid), dry_run=False)
    assert len(sent) == 1
    assert sent[0]["notice_version"] == compliance.notice_version(
        "legitimate_interest", language
    )
    return sent[0]


@pytest.mark.parametrize("language", ["en", "it"])
@pytest.mark.parametrize(
    "legal_name, shown",
    [
        ("Suppleco S.r.l.", "Suppleco S.r.l."),
        ("Rossi Integratori S.p.A.", "Rossi Integratori S.p.A."),
        ("Suppleco Srl", "Suppleco Srl."),
    ],
)
def test_the_notice_sentence_ends_with_one_full_stop(
    settings_factory, tmp_path, legal_name, shown, language
):
    email = _sent_email(settings_factory, tmp_path, legal_name, language)
    opening, after = SENTENCE[language]
    expected = opening.format(name=shown) + after
    for part in ("html_body", "plain_body"):
        text = " ".join(email[part].split())
        assert expected in text, f"{part}: {text[:400]}"
        assert ".. " not in text, f"{part} has a doubled full stop"


def test_the_identity_line_keeps_the_name_as_typed(settings_factory, tmp_path):
    email = _sent_email(settings_factory, tmp_path, "Suppleco S.r.l.", "en")
    assert "Suppleco S.r.l. &middot;" in email["html_body"]
