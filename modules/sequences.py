"""
Multi-touch follow-up email sequence engine.
Manages automated email sequences with enrollment, scheduling, and tracking.
"""

import logging
from datetime import datetime, timedelta, timezone

from modules import clock
from modules.database import Database, undoes_a_failed_write

logger = logging.getLogger(__name__)


class SequenceEngine:
    """Engine for managing multi-step email follow-up sequences."""

    def __init__(self, db: Database):
        self.db = db

    @undoes_a_failed_write
    def create_sequence(self, name: str, steps: list) -> int:
        """
        Create a sequence definition with ordered steps.

        Args:
            name: Name of the sequence.
            steps: List of dicts with keys: delay_days, subject, template_name.

        Returns:
            The ID of the newly created sequence.
        """
        now = clock.now_iso()
        cursor = self.db.conn.execute(
            "INSERT INTO sequences (name, created_at) VALUES (?, ?)", (name, now)
        )
        sequence_id = cursor.lastrowid

        for i, step in enumerate(steps):
            self.db.conn.execute(
                """INSERT INTO sequence_steps
                   (sequence_id, step_number, delay_days, subject, template_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    sequence_id,
                    i,
                    step["delay_days"],
                    step["subject"],
                    step["template_name"],
                ),
            )

        self.db.conn.commit()
        logger.info(
            "Created sequence '%s' with %d steps (ID: %s)",
            name,
            len(steps),
            sequence_id,
        )
        return sequence_id

    def get_or_create_default(self, name: str, steps: list) -> int:
        """Return the id of the sequence named ``name``, creating it if absent.

        Idempotent: prevents duplicate sequences being inserted on repeated
        agent startups."""
        row = self.db.conn.execute(
            "SELECT id FROM sequences WHERE name = ? ORDER BY id ASC LIMIT 1", (name,)
        ).fetchone()
        if row:
            return row[0]
        return self.create_sequence(name, steps)

    @undoes_a_failed_write
    def enroll_contact(self, contact_id: int, sequence_id: int) -> int:
        """
        Enroll a contact in a sequence.

        Args:
            contact_id: ID of the contact to enroll.
            sequence_id: ID of the sequence.

        Returns:
            The enrollment ID.
        """
        # INSERT OR IGNORE against the partial unique index, NOT a SELECT
        # followed by an INSERT. sqlite3 opens its write transaction at the
        # DML, so the old check-then-act ran unserialised: the agent thread
        # (enrolling after a first send) and a Flask Direct Send with
        # "enroll in follow-ups" could both see "not enrolled" and both
        # insert. Two active enrollments make get_due_followups return the
        # contact twice at every step, so they receive the entire sequence
        # twice - permanently, because nothing ever merges the rows back.
        now = clock.now_iso()
        cursor = self.db.conn.execute(
            """INSERT OR IGNORE INTO sequence_enrollments
               (contact_id, sequence_id, current_step, status, enrolled_at)
               VALUES (?, ?, 0, 'active', ?)""",
            (contact_id, sequence_id, now),
        )
        self.db.conn.commit()

        if cursor.rowcount:
            enrollment_id = cursor.lastrowid
            logger.info(
                "Enrolled contact %s in sequence %s (enrollment %s)",
                contact_id,
                sequence_id,
                enrollment_id,
            )
            return enrollment_id

        # Ignored: an active enrollment already existed. Read it back so the
        # caller still gets a usable id.
        existing = self.db.conn.execute(
            """SELECT id FROM sequence_enrollments
               WHERE contact_id = ? AND sequence_id = ? AND status = 'active'""",
            (contact_id, sequence_id),
        ).fetchone()
        if existing:
            logger.info(
                "Contact %s already enrolled in sequence %s (enrollment %s)",
                contact_id,
                sequence_id,
                existing[0],
            )
            return existing[0]

        # No row inserted and none found: the unique index is absent (its
        # migration failed and said so). Fall back to the old behaviour rather
        # than silently skipping the enrollment.
        logger.warning(
            "Enrollment for contact %s was ignored but no active row exists - "
            "the uniqueness index is probably missing",
            contact_id,
        )
        cursor = self.db.conn.execute(
            """INSERT INTO sequence_enrollments
               (contact_id, sequence_id, current_step, status, enrolled_at)
               VALUES (?, ?, 0, 'active', ?)""",
            (contact_id, sequence_id, now),
        )
        self.db.conn.commit()
        return cursor.lastrowid

    def get_due_followups(self) -> list:
        """
        Find all enrollments where the current step's delay has elapsed.

        For step 0, the delay is measured from enrolled_at.
        For subsequent steps, the delay is measured from last_sent_at.

        Returns:
            List of dicts with enrollment, contact, and step details.
        """
        now = clock.now()
        due = []

        def _aware(ts: str) -> datetime:
            # Parse a stored timestamp; older rows may be naive - treat those as
            # UTC so comparisons never mix naive and aware. (Comparison across
            # zones is still correct: both sides are absolute instants.)
            dt = datetime.fromisoformat(ts)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        rows = self.db.conn.execute(
            """SELECT
                   se.id AS enrollment_id,
                   se.contact_id,
                   se.sequence_id,
                   se.current_step,
                   se.enrolled_at,
                   se.last_sent_at,
                   ss.delay_days,
                   ss.subject,
                   ss.template_name,
                   s.name AS sequence_name,
                   ss.id AS step_id
               FROM sequence_enrollments se
               JOIN sequence_steps ss
                   ON ss.sequence_id = se.sequence_id
                   AND ss.step_number = se.current_step
               JOIN sequences s ON s.id = se.sequence_id
               WHERE se.status = 'active'"""
        ).fetchall()

        columns = [
            "enrollment_id",
            "contact_id",
            "sequence_id",
            "current_step",
            "enrolled_at",
            "last_sent_at",
            "delay_days",
            "subject",
            "template_name",
            "sequence_name",
            "step_id",
        ]

        for row in rows:
            record = dict(zip(columns, row))

            # Determine reference time: last_sent_at for step > 0, enrolled_at for step 0
            if record["current_step"] == 0 or record["last_sent_at"] is None:
                reference_time = _aware(record["enrolled_at"])
            else:
                reference_time = _aware(record["last_sent_at"])

            due_at = reference_time + timedelta(days=record["delay_days"])

            if now >= due_at:
                # Fetch contact info
                contact = self.db.conn.execute(
                    "SELECT * FROM contacts WHERE id = ?", (record["contact_id"],)
                ).fetchone()

                if contact:
                    contact_cols = [
                        desc[0]
                        for desc in self.db.conn.execute(
                            "SELECT * FROM contacts LIMIT 0"
                        ).description
                    ]
                    record["contact"] = dict(zip(contact_cols, contact))

                record["due_at"] = due_at.isoformat()
                due.append(record)

        logger.info("Found %d due followups", len(due))
        return due

    def at_step(self, enrollment_id: int, step: int) -> bool:
        """Whether an enrollment is still active and at ``step``.

        A pass reads its due list once and sleeps the send delay after every
        follow-up, so an item can be hours old when the pass reaches it: the
        enrollment may have moved on, or ended, in another process since.
        """
        row = self.db.conn.execute(
            "SELECT status, current_step FROM sequence_enrollments WHERE id = ?",
            (enrollment_id,),
        ).fetchone()
        return row is not None and row[0] == "active" and row[1] == step

    @undoes_a_failed_write
    def advance_enrollment(self, enrollment_id: int, step: int | None = None):
        """
        Advance an enrollment to the next step.
        If no more steps remain, mark the enrollment as completed.

        Compare-and-set on the step it is given, so a process whose send the
        enrollment has already moved past - settled by another process, or
        moved on by a later pass - cannot skip the step after it.

        Args:
            enrollment_id: ID of the enrollment to advance.
            step: The step the caller sent; None takes the one it is at now.
        """
        enrollment = self.db.conn.execute(
            "SELECT * FROM sequence_enrollments WHERE id = ?", (enrollment_id,)
        ).fetchone()

        if not enrollment:
            logger.warning("Enrollment %s not found", enrollment_id)
            return

        cols = [
            desc[0]
            for desc in self.db.conn.execute(
                "SELECT * FROM sequence_enrollments LIMIT 0"
            ).description
        ]
        record = dict(zip(cols, enrollment))

        from_step = record["current_step"] if step is None else step
        next_step = from_step + 1
        now = clock.now_iso()

        # Check if there is a next step
        next_exists = self.db.conn.execute(
            """SELECT id FROM sequence_steps
               WHERE sequence_id = ? AND step_number = ?""",
            (record["sequence_id"], next_step),
        ).fetchone()

        if next_exists:
            moved = self.db.conn.execute(
                """UPDATE sequence_enrollments
                   SET current_step = ?, last_sent_at = ?
                   WHERE id = ? AND current_step = ?""",
                (next_step, now, enrollment_id, from_step),
            ).rowcount
            self.db.conn.commit()
            if moved:
                logger.info(
                    "Advanced enrollment %s to step %s", enrollment_id, next_step
                )
        else:
            moved = self.db.conn.execute(
                """UPDATE sequence_enrollments
                   SET status = 'completed', completed_at = ?, last_sent_at = ?
                   WHERE id = ? AND current_step = ?""",
                (now, now, enrollment_id, from_step),
            ).rowcount
            self.db.conn.commit()
            if moved:
                logger.info("Enrollment %s completed (no more steps)", enrollment_id)
        if not moved:
            logger.info(
                "Enrollment %s has already moved past step %s", enrollment_id, from_step
            )

    @undoes_a_failed_write
    def cancel_enrollment(self, enrollment_id: int, reason: str):
        """
        Cancel an active enrollment.

        Args:
            enrollment_id: ID of the enrollment to cancel.
            reason: Reason for cancellation (e.g., 'replied', 'unsubscribed').
        """
        now = clock.now_iso()
        self.db.conn.execute(
            """UPDATE sequence_enrollments
               SET status = 'cancelled', completed_at = ?, cancel_reason = ?
               WHERE id = ? AND status = 'active'""",
            (now, reason, enrollment_id),
        )
        self.db.conn.commit()
        logger.info("Cancelled enrollment %s (reason: %s)", enrollment_id, reason)

    def get_sequence_stats(self) -> dict:
        """
        Get aggregate statistics for all sequence enrollments.

        Returns:
            Dict with active_enrollments, completed, cancelled, total counts.
        """
        row = self.db.conn.execute(
            """SELECT
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active_enrollments,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
               FROM sequence_enrollments"""
        ).fetchone()

        stats = {
            "total": row[0] or 0,
            "active_enrollments": row[1] or 0,
            "completed": row[2] or 0,
            "cancelled": row[3] or 0,
        }
        logger.info(
            "Stats: %s active, %s completed, %s cancelled, %s total",
            stats["active_enrollments"],
            stats["completed"],
            stats["cancelled"],
            stats["total"],
        )
        return stats
