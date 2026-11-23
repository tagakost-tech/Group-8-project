"""The agent proposes, a person decides.

Every email the agent would send on its own - a first email, a follow-up, an
answer to a reply - is first written as a proposal here, with the evidence it
rests on. Nothing in a stage set to ``approve`` leaves the building until a
named person has opened the proposal and approved it, and what goes out is
exactly the text they approved: the approval stores a hash of the subject and
body, and the send refuses any text that does not match it.

Three levels per stage (``AUTONOMY_<STAGE>`` settings):

  manual   the agent does nothing at this stage; a person writes and sends
  approve  the agent drafts, a person approves, edits or rejects (default)
  auto     the agent acts alone, as the template it came from did

Opt-outs, bounces and erasures are deliberately NOT stages: protecting a
person never waits in a queue.

The mechanical parts of an email - the unsubscribe link, the legal footer and
the tracking pixel - stay as placeholders in the proposal and are filled at
send time, so a reviewer can neither remove them nor approve a stale link.
"""

import hashlib
import json
import logging
import re
from datetime import datetime

from modules.clock import db_now, get_timezone, today_str

logger = logging.getLogger(__name__)

STAGES = ("first_email", "followup", "reply")
LEVELS = ("manual", "approve", "auto")
DEFAULT_LEVEL = "approve"

# Placeholders filled at send time. A proposal that had one must still have it
# when approved: the footer and the opt-out link are the law, not the copy.
MECHANICAL_PLACEHOLDERS = (
    "{{legal_footer}}",
    "{{unsubscribe_url}}",
    "{{tracking_pixel}}",
)
REQUIRED_ON_APPROVAL = ("{{legal_footer}}",)

# A note left for the reviewer in a suggested text, such as "[Answer their
# question here.]". Sent as written, it tells the prospect nobody read it.
UNFILLED_NOTE = re.compile(r"\[[A-Z][^\]\n]{3,120}\]")

# How many proposals one reviewer may approve in a day. A cap, not a target:
# past it, approvals stop being reviews.
DEFAULT_DAILY_APPROVAL_CAP = 60

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS review_queue (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    stage             TEXT NOT NULL,
    contact_id        INTEGER NOT NULL,
    sequence_step_id  INTEGER,
    status            TEXT NOT NULL DEFAULT 'pending',
    language          TEXT,
    proposed_subject  TEXT NOT NULL,
    proposed_body     TEXT NOT NULL,
    evidence          TEXT,
    ai_written        INTEGER DEFAULT 0,
    final_subject     TEXT,
    final_body        TEXT,
    approved_hash     TEXT,
    edited            INTEGER DEFAULT 0,
    opened_at         TEXT,
    opened_by         TEXT,
    decided_at        TEXT,
    reviewer          TEXT,
    reason            TEXT,
    sent_at           TEXT,
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_queue_item
    ON review_queue(contact_id, stage, IFNULL(sequence_step_id, 0));
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_queue(status);
"""


class ReviewError(ValueError):
    """A review action that is refused, with the reason in words."""


def text_hash(subject: str, body: str) -> str:
    return hashlib.sha256(f"{subject}\x00{body}".encode()).hexdigest()


def level_for(settings, stage: str) -> str:
    """The autonomy level of one stage; an unknown value reads as ``approve``,
    the safe side, never as ``auto``."""
    value = (
        (getattr(settings, f"autonomy_{stage}", DEFAULT_LEVEL) or "").strip().lower()
    )
    return value if value in LEVELS else DEFAULT_LEVEL


def stage_for(purpose: str) -> str | None:
    """The review stage of a send purpose. A manual send (Direct Send) is a
    person's own act and needs no second approval."""
    return {"first": "first_email", "retry": "first_email", "followup": "followup"}.get(
        purpose
    )


class ReviewQueue:
    """Proposals and decisions, stored next to the contacts they are about."""

    def __init__(self, db, daily_cap: int = DEFAULT_DAILY_APPROVAL_CAP):
        self.db = db
        self.daily_cap = daily_cap

    # -- reading -----------------------------------------------------------

    def item_for(self, contact_id, stage: str, step_id=None) -> dict | None:
        row = self.db.conn.execute(
            """SELECT * FROM review_queue
               WHERE contact_id = ? AND stage = ? AND IFNULL(sequence_step_id, 0) = ?""",
            (contact_id, stage, step_id or 0),
        ).fetchone()
        return self._row(row)

    def get(self, item_id: int) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM review_queue WHERE id = ?", (item_id,)
        ).fetchone()
        return self._row(row)

    def pending(self, limit: int = 100) -> list[dict]:
        rows = self.db.conn.execute(
            """SELECT r.*, c.organization, c.email, c.lead_score, c.lead_tier,
                      c.source_url, c.company_country
               FROM review_queue r JOIN contacts c ON c.id = r.contact_id
               WHERE r.status = 'pending'
               ORDER BY c.lead_score DESC, r.id
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [item for r in rows if (item := self._row(r)) is not None]

    def listing(self, statuses: tuple, limit: int = 100) -> list[dict]:
        """Items in the given states, the latest decision first: what the
        Review tab shows under Approved and Decided."""
        marks = ",".join("?" for _ in statuses)
        rows = self.db.conn.execute(
            f"""SELECT r.*, c.organization, c.email, c.lead_score, c.lead_tier,
                       c.source_url, c.company_country
                FROM review_queue r JOIN contacts c ON c.id = r.contact_id
                WHERE r.status IN ({marks})
                ORDER BY COALESCE(r.sent_at, r.decided_at, r.created_at) DESC, r.id DESC
                LIMIT ?""",
            (*statuses, limit),
        ).fetchall()
        return [item for r in rows if (item := self._row(r)) is not None]

    def counts(self) -> dict:
        """What waits for people, for the badge and the overview."""
        rows = self.db.conn.execute(
            "SELECT stage, status, COUNT(*) FROM review_queue GROUP BY stage, status"
        ).fetchall()
        total = {
            "waiting": 0,
            "approved": 0,
            "replies_waiting": 0,
            "sent": 0,
            "rejected": 0,
        }
        for stage, status, n in rows:
            if status == "pending":
                total["waiting"] += n
                if stage == "reply":
                    total["replies_waiting"] += n
            elif status in ("approved", "sent", "rejected"):
                total[status] += n
        return total

    def _row(self, row) -> dict | None:
        if row is None:
            return None
        item = dict(row) if not isinstance(row, dict) else row
        if isinstance(item.get("evidence"), str):
            try:
                item["evidence"] = json.loads(item["evidence"])
            except json.JSONDecodeError:
                item["evidence"] = {"raw": item["evidence"]}
        return item

    # -- the agent's side --------------------------------------------------

    def propose(
        self,
        contact_id: int,
        stage: str,
        subject: str,
        body: str,
        *,
        step_id=None,
        language: str | None = None,
        evidence: dict | None = None,
        ai_written: bool = False,
    ) -> int | None:
        """Queue a proposal. One per contact, stage and step: proposing again
        while one exists changes nothing, so a pass that runs twice never
        doubles the reviewer's work."""
        if stage not in STAGES:
            raise ValueError(f"unknown review stage {stage!r}")
        with self.db._transaction() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO review_queue
                   (created_at, stage, contact_id, sequence_step_id, language,
                    proposed_subject, proposed_body, evidence, ai_written)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now(),
                    stage,
                    contact_id,
                    step_id,
                    language,
                    subject,
                    body,
                    json.dumps(evidence or {}, ensure_ascii=False),
                    int(bool(ai_written)),
                ),
            )
            new_id = cur.lastrowid if cur.rowcount else None
        # Logged after the transaction: the log is another connection, and
        # writing it while this one holds the lock waits for itself.
        if new_id is not None:
            self.db.log_activity(
                "REVIEW_PROPOSED",
                f"{stage} for contact {contact_id} waits for a person",
                status="info",
            )
        return new_id

    def approved_text(self, item: dict) -> tuple[str, str] | None:
        """The approved subject and body, or None if the stored text no longer
        matches the hash taken when it was approved."""
        if item.get("status") != "approved":
            return None
        subject, body = item.get("final_subject") or "", item.get("final_body") or ""
        if text_hash(subject, body) != item.get("approved_hash"):
            logger.error(
                "Review item %s: approved text changed after approval", item["id"]
            )
            self.db.log_activity(
                "REVIEW_TAMPER",
                f"Review item {item['id']}: the stored text does not match what "
                "was approved; it is not sent",
                status="error",
            )
            return None
        return subject, body

    def mark_sent(self, item_id: int) -> None:
        with self.db._transaction() as conn:
            conn.execute(
                "UPDATE review_queue SET status = 'sent', sent_at = ? WHERE id = ?",
                (_now(), item_id),
            )

    # -- the person's side -------------------------------------------------

    def open(self, item_id: int, reviewer: str) -> dict:
        """Record that a person has opened the proposal. Approval needs it: an
        item nobody looked at cannot be approved, in a batch or otherwise."""
        item = self._require(item_id)
        if not item.get("opened_at"):
            with self.db._transaction() as conn:
                conn.execute(
                    "UPDATE review_queue SET opened_at = ?, opened_by = ? WHERE id = ?",
                    (_now(), reviewer, item_id),
                )
        return self._require(item_id)

    def approve(
        self,
        item_id: int,
        reviewer: str,
        subject: str | None = None,
        body: str | None = None,
        note: str = "",
    ) -> dict:
        item = self._require(item_id)
        reviewer = (reviewer or "").strip()
        if not reviewer:
            raise ReviewError("an approval needs a named reviewer")
        if item["status"] != "pending":
            raise ReviewError(
                f"this proposal is {item['status']}, not waiting for a decision"
            )
        if not item.get("opened_at"):
            raise ReviewError("open the proposal before approving it")
        subject = item["proposed_subject"] if subject is None else subject.strip()
        body = item["proposed_body"] if body is None else body
        if not subject:
            raise ReviewError("the subject is empty")
        for placeholder in MECHANICAL_PLACEHOLDERS:
            if placeholder in item["proposed_body"] and placeholder not in body:
                raise ReviewError(
                    f"{placeholder} was removed; the legal footer and the opt-out "
                    "link are filled at send time and must stay"
                )
        note_left = UNFILLED_NOTE.search(f"{subject}\n{body}")
        if note_left:
            raise ReviewError(
                f"fill in or delete {note_left.group(0)} first: it would be sent as written"
            )
        for placeholder in REQUIRED_ON_APPROVAL:
            if placeholder not in body:
                raise ReviewError(f"{placeholder} is missing")
        if self.approvals_today(reviewer) >= self.daily_cap:
            raise ReviewError(
                f"{reviewer} has approved {self.daily_cap} proposals today, the daily "
                "cap; more would stop being reviews"
            )
        edited = subject != item["proposed_subject"] or body != item["proposed_body"]
        with self.db._transaction() as conn:
            cur = conn.execute(
                """UPDATE review_queue
                   SET status = 'approved', final_subject = ?, final_body = ?,
                       approved_hash = ?, edited = ?, decided_at = ?, reviewer = ?,
                       reason = ?
                   WHERE id = ? AND status = 'pending'""",
                (
                    subject,
                    body,
                    text_hash(subject, body),
                    int(edited),
                    _now(),
                    reviewer,
                    note.strip() or None,
                    item_id,
                ),
            )
            if not cur.rowcount:
                raise ReviewError("someone else decided this proposal first")
        self.db.log_activity(
            "REVIEW_APPROVED",
            f"{item['stage']} for contact {item['contact_id']} approved by {reviewer}"
            + (" with edits" if edited else ""),
            status="success",
        )
        return self._require(item_id)

    def reject(self, item_id: int, reviewer: str, reason: str) -> dict:
        """Reject with a reason. The reason is required: it is what the agent
        learns from, and an override nobody can explain is not oversight."""
        item = self._require(item_id)
        reviewer, reason = (reviewer or "").strip(), (reason or "").strip()
        if not reviewer:
            raise ReviewError("a rejection needs a named reviewer")
        if not reason:
            raise ReviewError("say why: a rejection needs a reason")
        if item["status"] not in ("pending", "approved"):
            raise ReviewError(f"this proposal is {item['status']}")
        with self.db._transaction() as conn:
            conn.execute(
                """UPDATE review_queue
                   SET status = 'rejected', decided_at = ?, reviewer = ?, reason = ?,
                       approved_hash = NULL
                   WHERE id = ?""",
                (_now(), reviewer, reason, item_id),
            )
            if item["stage"] == "first_email":
                conn.execute(
                    """UPDATE contacts SET status = 'rejected_in_review', discard_reason = ?
                       WHERE id = ? AND status = 'pending'""",
                    (f"rejected in review: {reason}"[:300], item["contact_id"]),
                )
        self.db.log_activity(
            "REVIEW_REJECTED",
            f"{item['stage']} for contact {item['contact_id']} rejected by {reviewer}: {reason}",
            status="info",
        )
        return self._require(item_id)

    def reopen(self, item_id: int, reviewer: str) -> dict:
        """Take an approval back before it is sent, to edit it again."""
        item = self._require(item_id)
        if item["status"] != "approved":
            raise ReviewError(
                f"only an approved, unsent proposal can be reopened ({item['status']})"
            )
        with self.db._transaction() as conn:
            conn.execute(
                """UPDATE review_queue
                   SET status = 'pending', approved_hash = NULL, decided_at = NULL,
                       reviewer = NULL
                   WHERE id = ? AND status = 'approved'""",
                (item_id,),
            )
        self.db.log_activity(
            "REVIEW_REOPENED",
            f"review item {item_id} reopened by {reviewer}",
            status="info",
        )
        return self._require(item_id)

    # -- measuring the augmentation ---------------------------------------

    def approvals_today(self, reviewer: str) -> int:
        today = today_str()
        row = self.db.conn.execute(
            """SELECT COUNT(*) FROM review_queue
               WHERE reviewer = ? AND status IN ('approved', 'sent')
                 AND substr(decided_at, 1, 10) = ?""",
            (reviewer, today),
        ).fetchone()
        return row[0]

    def metrics(self) -> dict:
        """Paired measures, so neither can be gamed alone: approvals with the
        share that needed edits, speed with the rejections that came with it."""
        rows = self.db.conn.execute(
            """SELECT status, edited, opened_at, decided_at, created_at
               FROM review_queue"""
        ).fetchall()
        decided = [r for r in rows if r[0] in ("approved", "sent", "rejected")]
        approved = [r for r in decided if r[0] in ("approved", "sent")]
        rejected = [r for r in decided if r[0] == "rejected"]
        seconds = []
        for r in decided:
            opened, done = _parse(r[2]), _parse(r[3])
            if opened and done:
                seconds.append((done - opened).total_seconds())
        seconds.sort()
        return {
            "pending": sum(1 for r in rows if r[0] == "pending"),
            "decided": len(decided),
            "approved": len(approved),
            "rejected": len(rejected),
            "approval_rate": round(len(approved) / len(decided), 3)
            if decided
            else None,
            "approved_without_edits": (
                round(sum(1 for r in approved if not r[1]) / len(approved), 3)
                if approved
                else None
            ),
            "median_seconds_to_decide": seconds[len(seconds) // 2] if seconds else None,
        }

    def _require(self, item_id: int) -> dict:
        item = self.get(item_id)
        if item is None:
            raise ReviewError(f"no review item {item_id}")
        return item


def _now() -> str:
    return db_now()


def _parse(value: str):
    try:
        return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=get_timezone()
        )
    except (TypeError, ValueError):
        return None


__all__ = [
    "LEVELS",
    "STAGES",
    "ReviewError",
    "ReviewQueue",
    "level_for",
    "stage_for",
    "text_hash",
]
