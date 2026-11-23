"""The common Italian ways of saying "stop emailing me" are opt-outs.

The inbox reader knew "cancellami", "cancellatemi" and "non contattarmi", and
little else. "Disiscrivimi", "rimuovetemi", "non scrivetemi più", "non voglio
più ricevere email" and a reply that is only "stop" came back as ordinary
replies: the contact was marked 'responded', not suppressed, and an objection
under GDPR Art. 21 went unrecorded (native-speaker review, MUST 2).

The everyday verbs these phrases are made of must still read as they did:
"rimuovere il collega dalla copia", "cancellare l'appuntamento", "lo stop della
produzione". A data request keeps winning over an opt-out.
"""

import pytest

from modules import reply_intent
from modules.database import Database
from modules.imap_monitor import ImapMonitor


class FakeIMAP:
    """Enough of imaplib.IMAP4_SSL for a whole inbox pass."""

    def __init__(self, messages):
        self.messages = messages

    def list(self, *args):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def select(self, folder, readonly=False):
        return "OK", [b"1"]

    def search(self, charset, criteria):
        ids = b" ".join(str(i + 1).encode() for i in range(len(self.messages)))
        return "OK", [ids]

    def fetch(self, msg_id, parts):
        return "OK", [(b"1 (RFC822 {})", self.messages[int(msg_id) - 1])]

    def logout(self):
        return "BYE", [b""]


OPT_OUTS = [
    "disiscrivimi",
    "disiscrivetemi",
    "disiscrizione",
    "rimuovimi",
    "rimuovetemi",
    "toglietemi dalla lista",
    "non scrivetemi più",
    "non contattatemi più",
    "non mi scriva più",
    "non voglio più ricevere email",
    "non desidero ricevere altre email",
    "vi prego di cancellarmi",
    "cancellatemi dalla lista",
    "eliminatemi",
]


def _forms(phrase):
    """The phrase alone, in a sentence, shouted, and typed without accents."""
    sentence = f"Buongiorno, {phrase}, grazie. Cordiali saluti, Anna"
    return [
        phrase,
        sentence,
        phrase.upper(),
        sentence.replace("ù", "u").replace("ì", "i"),
        phrase.capitalize() + ".",
    ]


@pytest.mark.parametrize("phrase", OPT_OUTS)
def test_each_common_italian_opt_out_is_one(phrase):
    for text in _forms(phrase):
        fresh = reply_intent.strip_quoted(text)
        assert reply_intent.is_optout("Re: Suppleco CDMO", fresh), text
        assert reply_intent.dsr_kind("Re: Suppleco CDMO", fresh) is None, text


@pytest.mark.parametrize(
    "body", ["stop", "STOP", "Stop.", "  stop!\n", "Stop\n--\nAnna"]
)
def test_a_reply_that_is_only_stop_is_an_opt_out(body):
    fresh = reply_intent.strip_quoted(body)
    assert reply_intent.is_optout("Re: Suppleco CDMO", fresh), body


# Each keeps the verdict it had before the Italian opt-outs were added.
GUARDS = [
    ("non riesco a scrivere prima di lunedì", reply_intent.UNCLEAR),
    ("può rimuovere il collega dalla copia?", reply_intent.UNCLEAR),
    ("ci fermiamo allo stop della produzione ad agosto", reply_intent.UNCLEAR),
    ("può cancellare l'appuntamento di martedì?", reply_intent.UNCLEAR),
    ("non voglio più aspettare: sentiamoci", reply_intent.UNCLEAR),
    ("Stop, prima mi mandi il listino.", reply_intent.INTERESTED),
    ("Siamo interessati, ci mandi un preventivo.", reply_intent.INTERESTED),
    ("Non scriverò più di due righe: mandateci i campioni.", reply_intent.INTERESTED),
]


@pytest.mark.parametrize("body,verdict", GUARDS)
def test_an_ordinary_italian_reply_is_not_an_opt_out(body, verdict):
    fresh = reply_intent.strip_quoted(body)
    assert not reply_intent.is_optout("Re: Suppleco CDMO", fresh)
    assert reply_intent.dsr_kind("Re: Suppleco CDMO", fresh) is None
    assert reply_intent.classify("Re: Suppleco CDMO", body) == verdict


@pytest.mark.parametrize(
    "body",
    [
        "cancellate i miei dati",
        "Eliminatemi dal vostro database, per favore.",
        "Rimuovetemi dai vostri archivi.",
    ],
)
def test_a_data_request_is_still_a_data_request(body):
    assert reply_intent.dsr_kind("", reply_intent.strip_quoted(body)) == "erasure"


# ── Through the inbox reader ───────────────────────────────────────────────


def _raw(frm, body):
    return (
        f"From: {frm}\r\nTo: me@corp.com\r\nSubject: Re: Suppleco CDMO\r\n"
        "Date: Mon, 1 Jun 2026 10:00:00 +0200\r\nMIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: 8bit\r\n\r\n{body}"
    ).encode()


def _monitor(tmp_path, messages):
    db = Database(str(tmp_path / "i.db"))
    sid = db.log_search("q", source="test")
    for i, _ in enumerate(messages):
        email = f"anna{i}@rossi-integratori.it"
        cid = db.add_contact(sid, email, name="Anna Rossi", organization="Rossi")
        db.log_email(cid, email, "Suppleco CDMO", delivery_status="sent")
    m = ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw")
    fake = FakeIMAP(
        [_raw(f"anna{i}@rossi-integratori.it", b) for i, b in enumerate(messages)]
    )
    m._connect = lambda: fake
    return m


@pytest.mark.parametrize(
    "body",
    [
        "Buongiorno,\r\nnon scrivetemi più, grazie.\r\n\r\nCordiali saluti,\r\nAnna",
        "Disiscrivetemi",
        "STOP",
    ],
)
def test_an_italian_opt_out_reply_suppresses_the_sender(tmp_path, body):
    m = _monitor(tmp_path, [body])
    m.run_check()
    assert m.db.is_suppressed("anna0@rossi-integratori.it") is True


def test_an_ordinary_italian_reply_leaves_the_sender_contactable(tmp_path):
    m = _monitor(tmp_path, [body for body, _ in GUARDS[:5]])
    res = m.check_inbox()
    assert res["unsubscribes"] == []
    assert len(res["replies"]) == 5
    m.run_check()
    for i in range(5):
        assert m.db.is_suppressed(f"anna{i}@rossi-integratori.it") is False
