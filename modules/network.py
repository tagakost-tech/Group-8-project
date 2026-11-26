"""The Omega network: specialist SMEs sharing work by trade and free capacity.

Omega IFS orchestrates. It wins an order, forms what its own presses can and
routes the rest - spare capacity to another cold-forming member, an operation
it does not do (turning, heat treatment, plating) to the member that does.
The customer deals only with Omega: a job names the part and the quantity,
never who ordered it, so no member ever sees Omega's customers.

Like an email, a job goes nowhere until a named person at Omega approves it:
it is written as a draft the recipient cannot see, and approving it sends it.
An order splits into several such jobs at once (Omega forms 200 of 1,000
bolts, a partner forms 800, a turning shop grooves all 1,000).

A job is a request from one member to another. Only the recipient accepts,
declines, starts and finishes it; only the sender cancels it. Accepting books
the quantity against the recipient's weekly capacity, and every way out of
the work (declined, cancelled, done) gives it back, so "free" always reads
what a member can still promise.
"""

from __future__ import annotations

import re
from datetime import date
from typing import NamedTuple

from modules import clock

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS network_members (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    specialisations TEXT NOT NULL DEFAULT '',
    certifications TEXT NOT NULL DEFAULT '',
    capacity_per_week INTEGER NOT NULL DEFAULT 0,
    booked INTEGER NOT NULL DEFAULT 0,
    rating REAL,
    is_self INTEGER NOT NULL DEFAULT 0,
    contact_name TEXT,
    contact_email TEXT,
    login TEXT UNIQUE,
    password_hash TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS network_orders (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    created_by TEXT,
    part TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    self_quantity INTEGER NOT NULL DEFAULT 0,
    due_date TEXT,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS network_jobs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    created_by TEXT,
    order_id INTEGER REFERENCES network_orders(id),
    from_member_id INTEGER NOT NULL REFERENCES network_members(id),
    to_member_id INTEGER NOT NULL REFERENCES network_members(id),
    kind TEXT NOT NULL CHECK (kind IN ('capacity', 'operation')),
    operation TEXT NOT NULL,
    part TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    due_date TEXT,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
        ('draft', 'rejected', 'requested', 'accepted', 'declined', 'in_progress',
         'done', 'cancelled')),
    approved_by TEXT,
    approved_at TEXT,
    decided_by TEXT,
    decided_at TEXT,
    from_unread INTEGER NOT NULL DEFAULT 0,
    to_unread INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_network_jobs_from ON network_jobs(from_member_id);
CREATE INDEX IF NOT EXISTS idx_network_jobs_to ON network_jobs(to_member_id);
CREATE INDEX IF NOT EXISTS idx_network_jobs_order ON network_jobs(order_id);
CREATE TABLE IF NOT EXISTS network_messages (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES network_jobs(id),
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    from_member_id INTEGER NOT NULL REFERENCES network_members(id),
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_network_messages_job ON network_messages(job_id);
"""

SPECIALISATIONS = {
    "cold_forming": "Cold forming",
    "turning": "Turning",
    "heat_treatment": "Heat treatment",
    "plating": "Plating & coating",
    "machining": "Machining",
    "wire": "Wire",
    "logistics": "Logistics",
}
KINDS = ("capacity", "operation")
MAX_QUANTITY = 10_000_000
MAX_PART = 120
MAX_NOTES = 1000
MAX_MESSAGE = 2000
MIN_PASSWORD = 12
_LOGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")

# Which decision each side may take, from which state, and where it leads.
_RECIPIENT = {
    "accept": ("requested", "accepted"),
    "decline": ("requested", "declined"),
    "start": ("accepted", "in_progress"),
    "done": ("in_progress", "done"),
}
_SENDER = {"cancel": (("draft", "requested", "accepted"), "cancelled")}
# Omega's named staff, on a draft: approving sends it, rejecting drops it.
_APPROVER = {"approve": ("draft", "requested"), "reject": ("draft", "rejected")}
DECISIONS = (*_RECIPIENT, *_SENDER, *_APPROVER)
# The states in which the quantity sits in the recipient's booked capacity.
_HOLDS_CAPACITY = ("accepted", "in_progress")
# A job reaches its recipient only once approved; a draft cancelled or
# rejected before that stays unseen for good, so "was it ever sent" is read
# from the approval, never from the current status alone.
_REJECTED = "rejected"

# Omega itself, made on first use so a new installation has an orchestrator.
# Only what Omega publishes about itself; its weekly capacity is Omega's to
# enter (Network, its own card).
_SELF = {
    "name": "Omega IFS",
    "city": "Cesano Maderno",
    "country": "IT",
    "specialisations": ["cold_forming"],
    "certifications": "IATF 16949, ISO 9001, ISO 14001",
    "capacity_per_week": 0,
}


class Invalid(ValueError):
    """What was sent is not a valid answer: wrong type, or out of range."""


class WrongState(Exception):
    """The job is not in a state that allows this decision."""


class Viewer(NamedTuple):
    """Who is asking: the member they act for, whether they are Omega's own
    staff (who see the whole network) and their login name."""

    member_id: int
    role: str  # "orchestrator" or "member"
    name: str

    @property
    def is_staff(self) -> bool:
        return self.role == "orchestrator"


def _specs(text: str) -> list[str]:
    return [s for s in (text or "").split(",") if s]


def member_dict(row) -> dict:
    """A member as the API shows it: never its login or password hash."""
    capacity, booked = int(row["capacity_per_week"]), int(row["booked"])
    return {
        "id": row["id"],
        "name": row["name"],
        "city": row["city"],
        "country": row["country"],
        "specialisations": _specs(row["specialisations"]),
        "certifications": row["certifications"],
        "capacity_per_week": capacity,
        "booked": booked,
        "free": max(capacity - booked, 0),
        "rating": row["rating"],
        "is_self": bool(row["is_self"]),
        "contact_name": row["contact_name"] or "",
        "contact_email": row["contact_email"] or "",
    }


def _insert_member(conn, m: dict, *, is_self=False, login=None, password_hash=None) -> int:
    return conn.execute(
        """INSERT INTO network_members (name, city, country, specialisations,
               certifications, capacity_per_week, booked, rating, is_self,
               contact_name, contact_email, login, password_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            m["name"],
            m.get("city", ""),
            m.get("country", ""),
            ",".join(m["specialisations"]),
            m.get("certifications", ""),
            int(m["capacity_per_week"]),
            int(m.get("booked", 0)),
            m.get("rating"),
            int(is_self),
            m.get("contact_name") or None,
            m.get("contact_email") or None,
            login,
            password_hash,
            clock.db_now(),
        ),
    ).lastrowid


def self_member(db) -> dict:
    """Omega's own member row, made the first time it is asked for."""
    row = db.conn.execute(
        "SELECT * FROM network_members WHERE is_self = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        with db._transaction() as conn:
            _insert_member(conn, _SELF, is_self=True)
        row = db.conn.execute(
            "SELECT * FROM network_members WHERE is_self = 1 ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(row)


def get_member(db, member_id: int) -> dict | None:
    row = db.conn.execute(
        "SELECT * FROM network_members WHERE id = ?", (member_id,)
    ).fetchone()
    return dict(row) if row else None


def member_by_login(db, login: str) -> dict | None:
    """The member who signs in as `login`, with its password hash: for the
    sign-in check only, never for an API answer."""
    if not login:
        return None
    row = db.conn.execute(
        "SELECT * FROM network_members WHERE login = ?", (login.strip().lower(),)
    ).fetchone()
    return dict(row) if row else None


def list_members(db) -> list[dict]:
    self_member(db)
    rows = db.conn.execute(
        "SELECT * FROM network_members ORDER BY is_self DESC, name COLLATE NOCASE"
    ).fetchall()
    return [member_dict(r) for r in rows]


def _whole(value, low: int, high: int, what: str) -> int:
    # bool is an int to Python; true is not a quantity.
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        else:
            raise ValueError(f"{what} must be a whole number")
    if not low <= value <= high:
        raise ValueError(f"{what} must be between {low:,} and {high:,}")
    return value


def _text(data: dict, key: str, high: int, what: str, *, low: int = 0) -> str:
    value = data.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise Invalid(f"{what} must be text")
    value = value.strip()
    if not low <= len(value) <= high:
        raise ValueError(
            f"{what} is needed" if low and not value else f"{what} must be at most {high} characters"
        )
    return value


def _operation(value) -> str:
    if not isinstance(value, str) or value not in SPECIALISATIONS:
        raise ValueError(f"operation must be one of {', '.join(SPECIALISATIONS)}")
    return value


def match(db, viewer: Viewer, operation, quantity) -> list[dict]:
    """The other members that do `operation`: those with room for the whole
    quantity first, then the most free capacity, then the best rated."""
    operation = _operation(operation)
    quantity = _whole(quantity, 1, MAX_QUANTITY, "quantity")
    found = []
    for m in list_members(db):
        if m["id"] == viewer.member_id or operation not in m["specialisations"]:
            continue
        found.append({**m, "fits": m["free"] >= quantity})
    found.sort(key=lambda m: (not m["fits"], -m["free"], -(m["rating"] or 0)))
    return found


def add_member(db, data: dict, password_hash_of, taken_logins=()) -> dict:
    """Admit a member with its own login. `password_hash_of` turns the
    password into the stored hash; `taken_logins` are staff names a member
    may not share, or signing in as one would be ambiguous."""
    name = _text(data, "name", 120, "name", low=1)
    specs = data.get("specialisations")
    if (
        not isinstance(specs, list)
        or not specs
        or any(not isinstance(s, str) or s not in SPECIALISATIONS for s in specs)
    ):
        raise ValueError(
            f"specialisations must list one or more of {', '.join(SPECIALISATIONS)}"
        )
    login = _text(data, "login", 40, "login", low=1).lower()
    if not _LOGIN.match(login):
        raise ValueError(
            "login must be 3 to 40 lower-case letters, digits, dots, dashes or underscores"
        )
    if login in {t.lower() for t in taken_logins} or member_by_login(db, login):
        raise ValueError("that login is already taken")
    password = data.get("password")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    email = _text(data, "contact_email", 254, "contact email")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("contact email is not an address")
    member = {
        "name": name,
        "city": _text(data, "city", 80, "city"),
        "country": _text(data, "country", 2, "country").upper(),
        "specialisations": list(dict.fromkeys(specs)),
        "certifications": _text(data, "certifications", 200, "certifications"),
        "capacity_per_week": _whole(
            data.get("capacity_per_week", 0), 0, MAX_QUANTITY, "capacity per week"
        ),
        "contact_name": _text(data, "contact_name", 120, "contact name"),
        "contact_email": email,
    }
    with db._transaction() as conn:
        new_id = _insert_member(
            conn, member, login=login, password_hash=password_hash_of(password)
        )
    db.log_activity("NETWORK_MEMBER_ADDED", f"member {new_id} added, login {login}")
    return member_dict(get_member(db, new_id))


def set_member_password(db, member_id: int, password_hash: str) -> None:
    """Replace a member's password hash. Its sessions carry a fingerprint of
    the old hash, so every one of them ends."""
    with db._transaction() as conn:
        conn.execute(
            "UPDATE network_members SET password_hash = ? WHERE id = ?",
            (password_hash, member_id),
        )


# What Omega's staff may change on any member, and what a member may change
# on its own card: only its weekly capacity, the number the matching reads.
_STAFF_EDITS = ("city", "country", "certifications", "capacity_per_week",
                "contact_name", "contact_email", "specialisations")
_MEMBER_EDITS = ("capacity_per_week",)


def update_member(db, viewer: Viewer, member_id: int, data: dict) -> dict:
    member = get_member(db, member_id)
    if member is None:
        raise LookupError("No such member.")
    if not viewer.is_staff and member_id != viewer.member_id:
        raise PermissionError("A member can change only its own card.")
    allowed = _STAFF_EDITS if viewer.is_staff else _MEMBER_EDITS
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValueError(f"cannot change: {', '.join(unknown)}")
    if not data:
        raise ValueError("nothing to change")
    changes = {}
    if "capacity_per_week" in data:
        changes["capacity_per_week"] = _whole(
            data["capacity_per_week"], 0, MAX_QUANTITY, "capacity per week"
        )
    for key, high, what in (
        ("city", 80, "city"),
        ("certifications", 200, "certifications"),
        ("contact_name", 120, "contact name"),
    ):
        if key in data:
            changes[key] = _text(data, key, high, what)
    if "country" in data:
        changes["country"] = _text(data, "country", 2, "country").upper()
    if "contact_email" in data:
        email = _text(data, "contact_email", 254, "contact email")
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("contact email is not an address")
        changes["contact_email"] = email or None
    if "specialisations" in data:
        specs = data["specialisations"]
        if (
            not isinstance(specs, list)
            or not specs
            or any(not isinstance(s, str) or s not in SPECIALISATIONS for s in specs)
        ):
            raise ValueError(
                f"specialisations must list one or more of {', '.join(SPECIALISATIONS)}"
            )
        changes["specialisations"] = ",".join(dict.fromkeys(specs))
    with db._transaction() as conn:
        # The column names come from the allow-lists above, never the request.
        conn.execute(
            f"UPDATE network_members SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?",
            (*changes.values(), member_id),
        )
    db.log_activity(
        "NETWORK_MEMBER_CHANGED",
        f"member {member_id}: {', '.join(changes)} changed by {viewer.name or 'unknown'}",
    )
    return member_dict(get_member(db, member_id))


_JOB_SELECT = """
SELECT j.*, f.name AS from_name, t.name AS to_name,
       o.part AS order_part, o.quantity AS order_quantity,
       o.self_quantity AS order_self_quantity
FROM network_jobs j
JOIN network_members f ON f.id = j.from_member_id
JOIN network_members t ON t.id = j.to_member_id
LEFT JOIN network_orders o ON o.id = j.order_id
"""


def _actions(job, viewer: Viewer) -> list[str]:
    status = job["status"]
    actions = []
    if viewer.is_staff and viewer.name:
        actions += [d for d, (was, _) in _APPROVER.items() if was == status]
    if job["to_member_id"] == viewer.member_id:
        actions += [d for d, (was, _) in _RECIPIENT.items() if was == status]
    if job["from_member_id"] == viewer.member_id:
        actions += [d for d, (was, _) in _SENDER.items() if status in was]
    return actions


def job_dict(job, viewer: Viewer) -> dict:
    if job["from_member_id"] == viewer.member_id:
        direction, unread = "out", job["from_unread"]
    elif job["to_member_id"] == viewer.member_id:
        direction, unread = "in", job["to_unread"]
    else:
        direction, unread = "between", 0
    return {
        "id": job["id"],
        "created_at": job["created_at"],
        "from_member": {"id": job["from_member_id"], "name": job["from_name"]},
        "to_member": {"id": job["to_member_id"], "name": job["to_name"]},
        "kind": job["kind"],
        "operation": job["operation"],
        "part": job["part"],
        "quantity": job["quantity"],
        "due_date": job["due_date"],
        "notes": job["notes"],
        "status": job["status"],
        "order": (
            {
                "id": job["order_id"],
                "part": job["order_part"],
                "quantity": job["order_quantity"],
                "self_quantity": job["order_self_quantity"],
            }
            if job["order_id"]
            else None
        ),
        "created_by": job["created_by"],
        "approved_by": job["approved_by"],
        "approved_at": job["approved_at"],
        "decided_by": job["decided_by"],
        "decided_at": job["decided_at"],
        "direction": direction,
        "actions": _actions(job, viewer),
        "unread": int(unread or 0),
    }


def _job_row(db, job_id: int):
    return db.conn.execute(_JOB_SELECT + " WHERE j.id = ?", (job_id,)).fetchone()


def _is_party(job, viewer: Viewer) -> bool:
    """The sender always; the recipient once the job has been sent to it."""
    if job["from_member_id"] == viewer.member_id:
        return True
    return (
        job["to_member_id"] == viewer.member_id
        and job["approved_at"] is not None
        and job["status"] != _REJECTED
    )


def visible_job(db, viewer: Viewer, job_id: int):
    """The job, if this viewer may see it: Omega's staff see every job, a
    member those it sent and those sent to it after approval."""
    job = _job_row(db, job_id)
    if job is None:
        raise LookupError("No such job.")
    if not (viewer.is_staff or _is_party(job, viewer)):
        if job["to_member_id"] == viewer.member_id:
            raise LookupError("No such job.")
        raise PermissionError("Only the two members of a job can see it.")
    return job


def list_jobs(db, viewer: Viewer) -> list[dict]:
    if viewer.is_staff:
        rows = db.conn.execute(_JOB_SELECT + " ORDER BY j.id DESC").fetchall()
    else:
        rows = db.conn.execute(
            _JOB_SELECT
            + """ WHERE j.from_member_id = ?
                   OR (j.to_member_id = ? AND j.approved_at IS NOT NULL
                       AND j.status != ?)
                   ORDER BY j.id DESC""",
            (viewer.member_id, viewer.member_id, _REJECTED),
        ).fetchall()
    return [job_dict(r, viewer) for r in rows]


def _due(value) -> str | None:
    due = value or None
    if due is not None:
        if not isinstance(due, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            raise ValueError("due_date must be a date, YYYY-MM-DD")
        try:
            date.fromisoformat(due)
        except ValueError:
            raise ValueError("due_date is not a real date") from None
    return due


def _insert_job(conn, viewer: Viewer, job: dict, order_id=None) -> int:
    return conn.execute(
        """INSERT INTO network_jobs (created_at, created_by, order_id, from_member_id,
               to_member_id, kind, operation, part, quantity, due_date, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            clock.db_now(),
            viewer.name or None,
            order_id,
            viewer.member_id,
            job["to_member_id"],
            job["kind"],
            job["operation"],
            job["part"],
            job["quantity"],
            job["due_date"],
            job["notes"],
        ),
    ).lastrowid


def create_order(db, viewer: Viewer, data: dict) -> dict:
    """Split one customer order: Omega keeps `self_quantity` of the main
    operation, the rest of it goes to partners as capacity jobs, and any
    operation Omega does not do goes to the members that do. Every job is a
    draft until a named person approves it. The shares of Omega's own
    operation must add up to the order, so no piece is lost or made twice."""
    if not viewer.is_staff:
        raise PermissionError("Only Omega's staff split an order.")
    part = _text(data, "part", MAX_PART, "part", low=1)
    quantity = _whole(data.get("quantity"), 1, MAX_QUANTITY, "quantity")
    self_quantity = _whole(data.get("self_quantity", 0), 0, quantity, "Omega's own share")
    due = _due(data.get("due_date"))
    notes = _text(data, "notes", MAX_NOTES, "notes")
    lines = data.get("jobs")
    if not isinstance(lines, list) or not lines:
        raise ValueError("an order needs at least one job for a partner")
    if len(lines) > 20:
        raise ValueError("an order has at most 20 jobs")
    own = _specs(self_member(db)["specialisations"])
    jobs, shared = [], 0
    for i, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            raise Invalid(f"job {i} must be an object")
        operation = _operation(line.get("operation"))
        kind = "capacity" if operation in own else "operation"
        job = _job_fields(
            db, viewer,
            {**line, "kind": kind, "part": part, "due_date": due,
             "notes": line.get("notes", notes)},
        )
        if kind == "capacity":
            shared += job["quantity"]
        elif job["quantity"] > quantity:
            raise ValueError(f"job {i}: more pieces than the order")
        jobs.append(job)
    if own and self_quantity + shared != quantity:
        raise ValueError(
            f"Omega's {self_quantity:,} and the partners' {shared:,} make "
            f"{self_quantity + shared:,}, not the order's {quantity:,}"
        )
    with db._transaction() as conn:
        order_id = conn.execute(
            """INSERT INTO network_orders (created_at, created_by, part, quantity,
                   self_quantity, due_date, notes) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (clock.db_now(), viewer.name or None, part, quantity, self_quantity, due, notes),
        ).lastrowid
        ids = [_insert_job(conn, viewer, job, order_id) for job in jobs]
    db.log_activity(
        "NETWORK_ORDER_SPLIT",
        f"order {order_id}: {quantity} x {part[:40]}, Omega {self_quantity}, "
        f"draft jobs {ids} by {viewer.name or 'unknown'}",
    )
    return {
        "id": order_id,
        "part": part,
        "quantity": quantity,
        "self_quantity": self_quantity,
        "jobs": [job_dict(_job_row(db, j), viewer) for j in ids],
    }


def create_job(db, viewer: Viewer, data: dict) -> dict:
    """Write a job from the viewer's member as a draft; it is sent once a
    named person at Omega approves it. ValueError says what is wrong."""
    job = _job_fields(db, viewer, data)
    with db._transaction() as conn:
        job_id = _insert_job(conn, viewer, job)
    db.log_activity(
        "NETWORK_JOB_DRAFTED",
        f"job {job_id}: {job['quantity']} x {job['operation']} from member "
        f"{viewer.member_id} to member {job['to_member_id']} by {viewer.name or 'unknown'}",
    )
    return job_dict(_job_row(db, job_id), viewer)


def _job_fields(db, viewer: Viewer, data: dict) -> dict:
    to_id = data.get("to_member_id")
    if isinstance(to_id, bool) or not isinstance(to_id, int):
        raise Invalid("to_member_id must name a member")
    recipient = get_member(db, to_id)
    if recipient is None:
        raise ValueError("No such member.")
    if recipient["id"] == viewer.member_id:
        raise ValueError("A job cannot be sent to your own company.")
    kind = data.get("kind")
    if kind not in KINDS:
        raise ValueError("kind must be capacity or operation")
    operation = _operation(data.get("operation"))
    if operation not in _specs(recipient["specialisations"]):
        raise ValueError(
            f"{recipient['name']} does not do {SPECIALISATIONS[operation].lower()}"
        )
    part = _text(data, "part", MAX_PART, "part", low=1)
    quantity = _whole(data.get("quantity"), 1, MAX_QUANTITY, "quantity")
    notes = _text(data, "notes", MAX_NOTES, "notes")
    return {
        "to_member_id": recipient["id"],
        "kind": kind,
        "operation": operation,
        "part": part,
        "quantity": quantity,
        "due_date": _due(data.get("due_date")),
        "notes": notes,
    }


def decide(db, viewer: Viewer, job_id: int, decision) -> dict:
    """Apply a decision. PermissionError when the viewer is not the side that
    takes it, WrongState when the job has moved past it."""
    job = visible_job(db, viewer, job_id)
    if not isinstance(decision, str):
        raise Invalid(f"decision must be one of {', '.join(DECISIONS)}")
    if decision in _RECIPIENT:
        if job["to_member_id"] != viewer.member_id:
            raise PermissionError("Only the member the job was sent to can do that.")
        before, after = _RECIPIENT[decision]
        allowed = (before,)
    elif decision in _SENDER:
        if job["from_member_id"] != viewer.member_id:
            raise PermissionError("Only the member who sent the job can cancel it.")
        allowed, after = _SENDER[decision]
    elif decision in _APPROVER:
        if not viewer.is_staff:
            raise PermissionError("Only Omega's staff approve a job.")
        if not (viewer.name or "").strip():
            raise PermissionError("An approval needs a named person.")
        before, after = _APPROVER[decision]
        allowed = (before,)
    else:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if job["status"] not in allowed:
        raise WrongState(
            f"The job is {job['status'].replace('_', ' ')}: {decision} is not possible now."
        )
    held = job["status"] in _HOLDS_CAPACITY
    holds = after in _HOLDS_CAPACITY
    with db._transaction() as conn:
        # Guarded on the state read above, so two clicks cannot both book.
        # An approval is recorded apart from the recipient's own decision.
        sets = (
            "approved_by = ?, approved_at = ?"
            if decision in _APPROVER
            else "decided_by = ?, decided_at = ?"
        )
        changed = conn.execute(
            f"UPDATE network_jobs SET status = ?, {sets} WHERE id = ? AND status = ?",
            (after, viewer.name or None, clock.db_now(), job_id, job["status"]),
        ).rowcount
        if not changed:
            raise WrongState("The job changed while you were deciding: reload it.")
        if holds and not held:
            conn.execute(
                "UPDATE network_members SET booked = booked + ? WHERE id = ?",
                (job["quantity"], job["to_member_id"]),
            )
        elif held and not holds:
            conn.execute(
                "UPDATE network_members SET booked = MAX(booked - ?, 0) WHERE id = ?",
                (job["quantity"], job["to_member_id"]),
            )
    db.log_activity(
        "NETWORK_JOB_DECIDED",
        f"job {job_id} {job['status']} -> {after} by {viewer.name or 'unknown'}",
    )
    return job_dict(_job_row(db, job_id), viewer)


def _message_dict(row) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "author": row["author"],
        "from_member": {"id": row["from_member_id"], "name": row["from_name"]},
        "body": row["body"],
    }


def messages(db, viewer: Viewer, job_id: int) -> list[dict]:
    """A job's conversation, oldest first; reading it clears the viewer's
    unread count."""
    job = visible_job(db, viewer, job_id)
    rows = db.conn.execute(
        """SELECT m.*, f.name AS from_name FROM network_messages m
           JOIN network_members f ON f.id = m.from_member_id
           WHERE m.job_id = ? ORDER BY m.id""",
        (job_id,),
    ).fetchall()
    side = (
        "from_unread"
        if job["from_member_id"] == viewer.member_id
        else "to_unread" if job["to_member_id"] == viewer.member_id else None
    )
    if side:
        with db._transaction() as conn:
            conn.execute(f"UPDATE network_jobs SET {side} = 0 WHERE id = ?", (job_id,))
    return [_message_dict(r) for r in rows]


def post_message(db, viewer: Viewer, job_id: int, data: dict) -> dict:
    job = visible_job(db, viewer, job_id)
    body = _text(data, "body", MAX_MESSAGE, "message", low=1)
    # The other side has something new; Omega's staff writing on a job
    # between two partners is news to both of them.
    bump = []
    if job["from_member_id"] != viewer.member_id:
        bump.append("from_unread = from_unread + 1")
    if job["to_member_id"] != viewer.member_id:
        bump.append("to_unread = to_unread + 1")
    with db._transaction() as conn:
        message_id = conn.execute(
            """INSERT INTO network_messages (job_id, created_at, author,
                   from_member_id, body) VALUES (?, ?, ?, ?, ?)""",
            (job_id, clock.db_now(), viewer.name or "unknown", viewer.member_id, body),
        ).lastrowid
        conn.execute(f"UPDATE network_jobs SET {', '.join(bump)} WHERE id = ?", (job_id,))
    row = db.conn.execute(
        """SELECT m.*, f.name AS from_name FROM network_messages m
           JOIN network_members f ON f.id = m.from_member_id WHERE m.id = ?""",
        (message_id,),
    ).fetchone()
    return _message_dict(row)
