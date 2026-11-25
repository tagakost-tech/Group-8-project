"""Where each conversation ended up, and the funnel it adds up to.

A reply is not the goal; a meeting and an order are. The agent cannot know
either, so a person records them: one stage per company, changed or cleared
at any time, with who set it and when. The funnel counts companies, never
messages, so no stage can be wider than the one before it."""

from __future__ import annotations

from modules import clock
from modules.database import SENT_STATUSES_SQL

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outcomes (
    contact_id INTEGER PRIMARY KEY REFERENCES contacts(id),
    stage TEXT NOT NULL CHECK (stage IN ('interested', 'meeting', 'won', 'lost')),
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""

STAGES = ("interested", "meeting", "won", "lost")
# Each stage implies the ones before it: an order means there was a meeting.
_AT_LEAST = {
    "interested": ("interested", "meeting", "won"),
    "meeting": ("meeting", "won"),
    "won": ("won",),
}


def set_stage(db, contact_id: int, stage: str | None, who: str) -> str | None:
    """Record a company's stage; None clears it. ValueError for an unknown
    stage, LookupError for an unknown contact."""
    if stage is not None and stage not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    if db.get_contact_by_id(contact_id) is None:
        raise LookupError("No such contact.")
    with db._transaction() as conn:
        if stage is None:
            conn.execute("DELETE FROM outcomes WHERE contact_id = ?", (contact_id,))
        else:
            conn.execute(
                """INSERT INTO outcomes (contact_id, stage, updated_at, updated_by)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(contact_id) DO UPDATE SET
                     stage = excluded.stage, updated_at = excluded.updated_at,
                     updated_by = excluded.updated_by""",
                (contact_id, stage, clock.db_now(), who or None),
            )
    db.log_activity(
        "OUTCOME_SET",
        f"contact {contact_id} marked {stage or 'no outcome'} by {who or 'unknown'}",
        status="success",
    )
    return stage


def stages_by_contact(db) -> dict[int, str]:
    return {
        r[0]: r[1]
        for r in db.conn.execute("SELECT contact_id, stage FROM outcomes").fetchall()
    }


def stage_of(db, contact_id: int) -> str | None:
    row = db.conn.execute(
        "SELECT stage FROM outcomes WHERE contact_id = ?", (contact_id,)
    ).fetchone()
    return row[0] if row else None


def funnel(db) -> dict:
    """Found, emailed, replied, interested, meeting, won: companies at each."""
    q = db.conn.execute

    def at_least(stage: str) -> int:
        marks = ",".join("?" for _ in _AT_LEAST[stage])
        return q(
            f"SELECT COUNT(*) FROM outcomes WHERE stage IN ({marks})", _AT_LEAST[stage]
        ).fetchone()[0]

    found = q("SELECT COUNT(*) FROM contacts WHERE status != 'erased'").fetchone()[0]
    emailed = q(
        f"""SELECT COUNT(DISTINCT contact_id) FROM email_log
            WHERE contact_id IS NOT NULL AND delivery_status IN {SENT_STATUSES_SQL}"""
    ).fetchone()[0]
    # A reply sets 'responded', but a later status (client, opted out) can
    # replace it; an outcome a person recorded also means they answered.
    replied = q(
        """SELECT COUNT(*) FROM contacts c
           WHERE c.status = 'responded'
              OR EXISTS (SELECT 1 FROM outcomes o WHERE o.contact_id = c.id)
              OR EXISTS (SELECT 1 FROM review_queue r
                         WHERE r.contact_id = c.id AND r.stage = 'reply')"""
    ).fetchone()[0]
    stages = [
        ("found", "Found", found),
        ("emailed", "Emailed", emailed),
        ("replied", "Replied", replied),
        ("interested", "Interested", at_least("interested")),
        ("meeting", "Meeting booked", at_least("meeting")),
        ("won", "Order won", at_least("won")),
    ]

    def rate(a: int, b: int) -> float | None:
        return round(100 * a / b, 1) if b else None

    return {
        "stages": [{"key": k, "label": label, "count": n} for k, label, n in stages],
        "rates": {
            "reply": rate(replied, emailed),
            "meeting": rate(at_least("meeting"), emailed),
            "won": rate(at_least("won"), at_least("meeting")),
        },
        "lost": q("SELECT COUNT(*) FROM outcomes WHERE stage = 'lost'").fetchone()[0],
    }
