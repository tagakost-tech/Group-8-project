"""explain_contact answers "why was (or wasn't) this contact emailed?".

The dashboard showed a status badge and, at most, a discard_reason - which
could be left over from an earlier state ('responded' still saying "Score 12
below threshold"). The explainer gives a plain-language headline for every
status, lets a suppression outrank the status (an opt-out is the answer,
whatever the status column says), shows only a reason that belongs to the
current status, and lays out the contact's history newest first.
"""

import re
from pathlib import Path

import pytest

from modules.contact_explainer import HEADLINES, explain_contact
from modules.database import Database

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "x.db"))
    db.cid = db.add_contact(db.log_search("brand query"), "buyer@brand.de", "Anna")
    return db


def _set(db, status, reason=None):
    db.update_contact_status(db.cid, status, discard_reason=reason)


def test_an_unknown_contact_has_no_explanation(db):
    assert explain_contact(db, 999) is None


@pytest.mark.parametrize("status", sorted(HEADLINES))
def test_every_status_has_its_own_headline(db, status):
    _set(db, status)
    why = explain_contact(db, db.cid)
    assert why["headline"] == HEADLINES[status]
    assert why["status"] == status


def test_an_opt_out_outranks_the_status(db):
    _set(db, "emailed")
    db.add_to_suppression("buyer@brand.de", "unsubscribe")
    assert "opted out" in explain_contact(db, db.cid)["headline"].lower()


def test_a_reason_from_an_earlier_state_is_not_shown(db):
    _set(db, "responded", "Score 12 below threshold 40")
    facts = dict(explain_contact(db, db.cid)["facts"])
    assert "Reason" not in facts


def test_the_reason_for_the_current_state_is_shown(db):
    _set(db, "low_score", "Score 12 below threshold 40")
    facts = dict(explain_contact(db, db.cid)["facts"])
    assert facts["Reason"] == "Score 12 below threshold 40"


def test_the_timeline_is_newest_first_and_names_what_happened(db):
    db.log_email(db.cid, "buyer@brand.de", "Hello", delivery_status="sent")
    db.add_to_suppression("buyer@brand.de", "unsubscribe")
    db.conn.execute("UPDATE email_log SET timestamp = '2026-01-01 10:00:00';")
    db.conn.execute("UPDATE suppression_list SET timestamp = '2026-01-02 10:00:00';")
    db.conn.commit()
    timeline = explain_contact(db, db.cid)["timeline"]
    stamps = [e["when"] for e in timeline]
    assert stamps == sorted(stamps, reverse=True)
    texts = " | ".join(e["what"] for e in timeline)
    assert "Hello" in texts and "unsubscribe" in texts.lower()


def test_the_timeline_is_capped(db):
    for i in range(250):
        db.log_activity("EMAIL_PREPARING", f"Preparing email to buyer@brand.de #{i}")
    assert len(explain_contact(db, db.cid)["timeline"]) == 200


def test_every_status_the_code_writes_is_explained():
    """A status added somewhere without an explanation fails here, not on the
    operator's screen."""
    written = set()
    for path in [ROOT / "main.py", *(ROOT / "modules").glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        written |= set(re.findall(r'update_contact_status\(\s*[^,]+,\s*"(\w+)"', text))
        written |= set(re.findall(r"contacts\s+SET\s+status\s*=\s*'(\w+)'", text))
    written.add("pending")  # the schema default
    assert written, "the scan found no status writes - the regexes are stale"
    assert written - set(HEADLINES) == set()


# -- Send states in plain words ----------------------------------------------
# The timeline read 'Email sent_unconfirmed: "subject" (notice-li-1-it)': an
# email that may have reached the person read like a code.


def _email_line(db, status, notice=None):
    db.log_email(
        db.cid, "buyer@brand.de", "Proposta", delivery_status=status,
        notice_version=notice,
    )
    return [
        e["what"]
        for e in explain_contact(db, db.cid)["timeline"]
        if e["what"].startswith("Email")
    ]


@pytest.mark.parametrize(
    "status, words",
    [
        ("sent", "Email sent"),
        (
            "sent_unconfirmed",
            (
                "Email may have been sent - the connection was lost and it was "
                "not repeated"
            ),
        ),
        (
            "preparing",
            "Email being sent, or cut off - nothing more goes until it is settled",
        ),
        ("rejected", "Email refused by the server"),
        ("dry_run", "Email previewed, not sent"),
    ],
)
def test_each_send_state_reads_in_plain_words(db, status, words):
    (line,) = _email_line(db, status)
    assert line == f'{words}: "Proposta"'
    assert status == "sent" or status not in line


def test_the_notice_version_says_the_language(db):
    (line,) = _email_line(db, "sent", "notice-li-1-it")
    assert line == 'Email sent: "Proposta" (notice-li-1-it) (in Italian)'
    db.conn.execute("DELETE FROM email_log")
    db.conn.commit()
    (line,) = _email_line(db, "sent_unconfirmed", "notice-li-1")
    assert line.endswith("(notice-li-1) (in English)")
    assert "sent_unconfirmed" not in line


def test_an_unknown_send_state_is_still_shown(db):
    (line,) = _email_line(db, "bounced")
    assert line == 'Email bounced: "Proposta"'
