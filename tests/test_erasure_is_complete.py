"""An erasure that reports success must actually have erased.

gdpr_erase covered `contacts` and the address columns of `email_log`, then
logged "GDPR erasure complete". Four other places kept the subject's personal
data, and nothing said so:

  lead_alerts.reply_excerpt   up to 500 characters of what the PROSPECT wrote
  lead_alerts.email           their address, in cleartext
  consent_log.email           their address, plus the page we found them on
  email_log.body_preview      the rendered mail, which opens "Hi <FirstName>,"

Under Art. 17 the answer "we deleted you" has to be true. A partial erasure
that announces completeness is worse than one that fails loudly, because
nobody goes looking.
"""

import sqlite3

import pytest

from modules.database import Database
from modules.export import DataExporter

EMAIL = "prospect@brand.de"
NAME = "Anna Rossi"
REPLY = "Sure, send me pricing - Anna, CFO"


@pytest.fixture
def erased(tmp_path):
    """A contact with data in every table that touches them, then erased."""
    db = Database(str(tmp_path / "g.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, EMAIL, name=NAME, organization="Brand GmbH")
    # body_preview as the sender actually writes it: EmailSender stores the
    # first 200 characters of the rendered plain text, and every template
    # opens with the greeting, so the name is always in there.
    db.log_email(
        cid,
        EMAIL,
        f"A question for {NAME}",
        body_preview=f"Hi {NAME.split()[0]},\n\nI came across Brand GmbH and...",
        delivery_status="sent",
    )
    db.log_consent(EMAIL, "legitimate_interest", "Publicly listed on: https://brand.de")
    db.record_lead_alert(EMAIL, notified_to="ops@suppleco.it", reply_excerpt=REPLY)

    summary = DataExporter(db).gdpr_erase(EMAIL)
    return db, summary


def _surviving(db, needles):
    """Every (table.column, value) anywhere in the DB containing a needle."""
    raw = sqlite3.connect(db.db_path)
    hits = []
    tables = [
        r[0]
        for r in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    for t in tables:
        cols = [c[1] for c in raw.execute(f"PRAGMA table_info({t})").fetchall()]
        for row in raw.execute(f"SELECT * FROM {t}").fetchall():
            for col, val in zip(cols, row):
                if isinstance(val, str) and any(n in val for n in needles):
                    hits.append(f"{t}.{col}")
    raw.close()
    return hits


def test_nothing_survives_not_even_in_the_suppression_list(erased):
    """The suppression entry used to keep the real address, so the opt-out
    could be honoured. It now keeps the keyed token, which the suppression
    checks match against the real address - so nothing is kept in clear."""
    db, _ = erased
    survivors = _surviving(db, [EMAIL, NAME, "Anna, CFO"])
    assert survivors == [], (
        f"personal data survived the erasure in: {survivors}"
    )


def test_the_prospects_own_words_are_gone(erased):
    """lead_alerts.reply_excerpt held whatever they wrote back. Deleted rather
    than anonymised - unlike the suppression entry there is nothing left for
    it to do."""
    db, _ = erased
    raw = sqlite3.connect(db.db_path)
    rows = raw.execute("SELECT * FROM lead_alerts").fetchall()
    raw.close()
    assert rows == []


def test_the_consent_record_is_gone(erased):
    """It documented a legitimate-interest basis for contacting them. An
    erasure request supersedes that basis, so the record has nothing to
    justify."""
    db, _ = erased
    raw = sqlite3.connect(db.db_path)
    rows = raw.execute(
        "SELECT * FROM consent_log WHERE LOWER(email) = ?", (EMAIL,)
    ).fetchall()
    raw.close()
    assert rows == []


def test_the_rendered_message_is_gone(erased):
    """Every template opens "Hi {FirstName}," so body_preview carries the name
    in its first twenty characters for every named contact."""
    db, _ = erased
    raw = sqlite3.connect(db.db_path)
    rows = raw.execute("SELECT subject, body_preview FROM email_log").fetchall()
    raw.close()
    for subject, preview in rows:
        assert NAME not in (subject or "")
        assert NAME not in (preview or "")


def test_the_erasure_does_not_abort_midway(erased):
    """email_log.subject is NOT NULL. Blanking it raised IntegrityError and
    aborted the whole function - after step 0 had already suppressed the
    address, so the caller saw an exception and a half-erased database."""
    db, summary = erased
    assert summary["actions"], "the erasure reported no actions at all"
    contact = db.conn.execute(
        "SELECT status FROM contacts WHERE LOWER(email) = ?", (EMAIL,)
    ).fetchone()
    assert contact is None, "the contact row still carries the original address"


def test_the_opt_out_still_holds_after_erasure(erased):
    """The point of keeping the suppression entry, now as a token."""
    db, _ = erased
    assert db.is_suppressed(EMAIL) is True


def test_erasing_an_unknown_address_is_harmless(tmp_path):
    """Guard: it must not throw, and must not erase anyone else."""
    db = Database(str(tmp_path / "h.db"))
    sid = db.log_search("q", source="test")
    db.add_contact(sid, "someone.else@brand.de", name="Other Person")

    DataExporter(db).gdpr_erase("nobody@nowhere.de")

    row = db.conn.execute(
        "SELECT name FROM contacts WHERE email = ?", ("someone.else@brand.de",)
    ).fetchone()
    assert row[0] == "Other Person"
