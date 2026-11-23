"""A reply that asks for erasure or access is recognised as a request.

"Cancellatemi dal vostro database" matched an opt-out phrase and was filed
as a plain opt-out: suppressed, but with no record that a data-subject
request had arrived, no one-month deadline (Art. 12(3)) and no erasure
(Art. 17). "Where did you get my email?" - a question about the source,
which Art. 15(1)(g) entitles them to have answered - was filed as an
ordinary reply, could raise a hot-lead alert and be sent to the AI. Requests
are now recognised before opt-outs, in the five pilot languages, and the
thin German, French and Spanish opt-out lists are widened.
"""

import pytest

from modules.database import Database
from modules.imap_monitor import ImapMonitor
from modules.reply_intent import dsr_kind, is_optout

ERASURE = [
    "Please delete my personal data.",
    "Remove me from your database, I never agreed to this.",
    "I'm exercising my right to be forgotten under GDPR.",
    "Erase all of our details from your records.",
    "Cancellatemi dal vostro database.",
    "Vi chiedo di cancellare i miei dati.",
    "Esercito il diritto all'oblio.",
    "Bitte löschen Sie meine Daten.",
    "Entfernen Sie meine Adresse aus Ihrer Datenbank.",
    "Merci de supprimer mes données.",
    "Je demande l'effacement de mes coordonnées - droit à l'oubli.",
    "Por favor borren mis datos.",
    "Ejerzo mi derecho de supresión.",
]

ACCESS = [
    "Where did you get my email address?",
    "How did you get my contact details?",
    "What personal data do you hold about me?",
    "This is a subject access request.",
    "Chi vi ha dato il mio contatto?",
    "Dove avete trovato il mio indirizzo?",
    "Woher haben Sie meine E-Mail-Adresse?",
    "Welche Daten haben Sie über mich gespeichert?",
    "Où avez-vous obtenu mon adresse ?",
    "J'exerce mon droit d'accès.",
    "¿De dónde sacaron mi correo?",
    "Ejerzo mi derecho de acceso.",
]

NEITHER = [
    "Please delete the previous email I sent, it had a typo.",
    "Can you send me your product data sheet?",
    "Can you remove the attachment and resend?",
    "Are you GDPR compliant as a manufacturer?",
    "Where did you get this price from?",
    "Dobbiamo cancellare la riunione di domani.",
    "Bitte senden Sie mir Ihre Preisliste.",
    "Pouvez-vous supprimer la pièce jointe ?",
    "Necesitamos borrar la reunión del lunes.",
    "Sure, let's talk next week.",
]


@pytest.mark.parametrize("text", ERASURE)
def test_erasure_requests_are_recognised(text):
    assert dsr_kind("Re: hello", text) == "erasure"


@pytest.mark.parametrize("text", ACCESS)
def test_access_requests_are_recognised(text):
    assert dsr_kind("Re: hello", text) == "access"


@pytest.mark.parametrize("text", NEITHER)
def test_ordinary_replies_are_not_requests(text):
    assert dsr_kind("Re: hello", text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Bitte streichen Sie mich aus Ihrem Verteiler.",
        "Bitte kontaktieren Sie uns nicht mehr.",
        "Merci de ne plus m'envoyer de messages.",
        "Retirez-moi de votre liste.",
        "Por favor no me envíen más correos.",
        "Quítenme de su lista.",
    ],
)
def test_the_wider_opt_out_phrases_are_recognised(text):
    assert is_optout("Re: hello", text)


def _monitor(db):
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    monitor.imap_host, monitor.username = "imap.test", "me@corp.com"
    return monitor


def test_a_request_is_classified_before_an_opt_out(tmp_path):
    monitor = _monitor(Database(str(tmp_path / "c.db")))
    kind = monitor._classify_email(
        "anna@brand.it", "Re: hello", "Cancellatemi dal vostro database."
    )
    assert kind == "request_erasure"


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "r.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "anna@brand.it", name="Anna", organization="Brand")
    db.add_contact(sid, "info@brand.it", organization="Brand")
    db.log_email(cid, "anna@brand.it", "Hello", delivery_status="sent")
    return db


def test_a_request_stops_email_to_the_person_and_their_company(db):
    _monitor(db).process_requests(
        [
            {
                "email": "anna@brand.it",
                "request_kind": "erasure",
                "subject": "Re",
                "body": "",
            }
        ]
    )
    assert db.is_blocked("anna@brand.it")
    assert db.is_blocked("info@brand.it")
    rows = db.conn.execute(
        "SELECT details FROM activity_log WHERE action = 'DSR_RECEIVED'"
    ).fetchall()
    assert len(rows) == 1 and "erasure" in rows[0][0]


def test_a_request_is_never_treated_as_a_reply(db, monkeypatch):
    replies = []
    monitor = _monitor(db)
    monkeypatch.setattr(
        monitor,
        "check_inbox",
        lambda: {
            "bounces": [],
            "unsubscribes": [],
            "requests": [
                {
                    "email": "anna@brand.it",
                    "request_kind": "access",
                    "subject": "Re",
                    "body": "",
                }
            ],
            "replies": [],
            "ok": True,
            "scan_started": None,
        },
    )
    monkeypatch.setattr(monitor, "process_replies", lambda r: replies.extend(r))
    summary = monitor.run_check()
    assert replies == []
    assert summary["requests"] == 1
    assert db.is_blocked("anna@brand.it")


def test_the_inbox_scan_files_a_request_as_a_request(tmp_path):
    """Through the whole scan, with the fake IMAP server of the scan tests."""
    from tests.test_imap_inbox_scan import _monitor as scan_monitor
    from tests.test_imap_inbox_scan import _raw

    monitor, _ = scan_monitor(
        tmp_path,
        [_raw("anna@brand.it", "Re: hello", "Cancellatemi dal vostro database.")],
    )
    results = monitor.check_inbox()
    assert [r["email"] for r in results["requests"]] == ["anna@brand.it"]
    assert results["requests"][0]["request_kind"] == "erasure"
    assert results["unsubscribes"] == [] and results["replies"] == []
