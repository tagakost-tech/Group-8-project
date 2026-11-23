"""Why was (or wasn't) this contact emailed? - in plain language.

Feeds the dashboard's contact detail panel. One headline per status, the facts
behind it, and the contact's history newest first. Read-only.

Three rules keep it honest:

- A suppression row outranks the status column. An opt-out, a bounce or a
  block is the answer to "why not", whatever status the row last recorded.
- A stored reason is shown only when it belongs to the current status. A
  contact's discard_reason can outlive the verdict that wrote it (older rows
  read 'responded' while still saying "Score 12 below threshold"), and a
  stale reason presented as the answer is worse than none.
- Every status the code can write has a headline. tests/test_contact_
  explainer.py scans the source and fails if one is missing.
"""

from __future__ import annotations

from datetime import datetime

from modules import clock
from modules.database import (
    CLOSED_CONTACT_STATUSES,
    REQUEUED_FROM_DASHBOARD,
    SENT_STATUSES,
    UNSETTLED_SEND,
    Database,
)
from modules.export import records_held_about

TIMELINE_LIMIT = 200

HEADLINES = {
    "pending": "Waiting to be emailed: queued in the backlog.",
    "emailed": "Emailed. Follow-ups, if any, continue from the sequence.",
    "failed": "The last send failed; see the reason below.",
    "bounced": "Not emailed again: the address bounced.",
    "responded": "They replied, so no more automated email goes to them.",
    "active_client": "Not emailed: an active client is never sent the cold pitch.",
    "unsubscribed": "Not emailed: they opted out.",
    "blocked": "Not emailed: blocked from the dashboard.",
    "erased": "Erased: the personal data about them has been removed.",
    "irrelevant": "Not emailed: filtered out as not a fit; see the reason below.",
    "ai_irrelevant": "Not emailed: the AI classifier judged them not a target.",
    "low_score": "Not emailed: the lead score was below the threshold.",
    "rejected_in_review": (
        "Not emailed: a person rejected the proposed email in review; "
        "their reason is below."
    ),
    "unverified": "Not emailed: the address could not be verified.",
    # Written by versions before 6479219, still present in older databases.
    "dry_run": (
        "Left in the old 'dry_run' state by an earlier version; "
        "use Send to queue it again."
    ),
}

SUPPRESSION_HEADLINES = {
    "unsubscribe": "Not emailed: they opted out.",
    "bounce": "Not emailed: the address bounced.",
    "smtp_rejected": "Not emailed: the recipient's server refused the address.",
    "blocked_via_dashboard": "Not emailed: blocked from the dashboard.",
    "bounce_blocked": "Not emailed: blocked from the dashboard, and the address bounced.",
    "gdpr_erasure": "Not emailed: their data was erased at their request.",
}

# Whatever the status column last said: nothing more goes until it settles.
UNSETTLED_HEADLINE = (
    f"Waiting: an email to them is {UNSETTLED_SEND}. Nothing more goes until it "
    "settles."
)

# What each send state means for the person, in the timeline. A state not
# listed reads "Email <state>".
EMAIL_EVENTS = {
    "sent": "Email sent",
    "sent_unconfirmed": (
        "Email may have been sent - the connection was lost and it was not repeated"
    ),
    "preparing": (
        "Email being sent, or cut off - nothing more goes until it is settled"
    ),
    "rejected": "Email refused by the server",
    "dry_run": "Email previewed, not sent",
}

# Which stored reasons belong to which status. Statuses that are themselves a
# filter verdict carry their own reason, whatever its wording.
_VERDICTS = ("irrelevant", "ai_irrelevant", "low_score", "unverified")
_REASON_PREFIXES = {
    "pending": (REQUEUED_FROM_DASHBOARD, "Unblocked from the dashboard"),
    "failed": ("Send failed", "Failed to create email"),
    "bounced": ("Refused by the recipient's server", "hard bounce"),
    "responded": ("Replied by email",),
    "active_client": ("Active client",),
    "unsubscribed": ("Opted out",),
    "blocked": ("Blocked from the dashboard",),
    "erased": ("retention:", "gdpr_erasure"),
}


def _current_reason(status: str, reason: str | None) -> str | None:
    if not reason:
        return None
    if status in _VERDICTS:
        return reason
    if reason.startswith(_REASON_PREFIXES.get(status, ())):
        return reason
    return None


def _sort_key(stamp: str) -> str:
    """A stored timestamp as local "YYYY-MM-DD HH:MM:SS".

    Most tables store local time; the retry queue stores UTC with a "Z" and
    enrolments ISO with an offset. A zone-qualified stamp is converted to
    the app's timezone first - cut to 19 characters as it was, a UTC retry
    showed (and sorted) hours before the failure that queued it.
    """
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp.replace("T", " ")[:19]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(clock.get_timezone())
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _event(when, what: str) -> dict | None:
    return {"when": _sort_key(str(when)), "what": what} if when else None


def _timeline(held: dict, search: dict | None) -> list[dict]:
    events = []
    for c in held.get("contacts", []):
        found = "Found"
        if search and search.get("search_query"):
            found += f' by the search "{search["search_query"]}"'
        events.append(_event(c.get("timestamp"), found))
    for e in held.get("email_log", []):
        status = e.get("delivery_status")
        said = EMAIL_EVENTS.get(status, f"Email {status}")
        what = f'{said}: "{e.get("subject")}"'
        # The notice the email carried, which also says its language: the
        # Italian versions end in "-it". Older emails recorded none.
        if e.get("notice_version"):
            language = (
                "in Italian" if e["notice_version"].endswith("-it") else "in English"
            )
            what += f" ({e['notice_version']}) ({language})"
        if e.get("delivery_status") in ("failed", "rejected") and e.get(
            "smtp_response"
        ):
            what += f" - {e['smtp_response']}"
        events.append(_event(e.get("timestamp"), what))
    for s in held.get("suppression_list", []):
        events.append(
            _event(
                s.get("timestamp"), f"Added to the do-not-contact list ({s['reason']})"
            )
        )
    for c in held.get("consent_log", []):
        events.append(
            _event(
                c.get("timestamp"),
                f"Legal basis recorded: {c.get('legal_basis')} ({c.get('details')})",
            )
        )
    for alert in held.get("lead_alerts", []):
        events.append(_event(alert.get("timestamp"), "Hot-lead alert sent to the team"))
    for en in held.get("sequence_enrollments", []):
        events.append(_event(en.get("enrolled_at"), "Enrolled in the follow-ups"))
        if en.get("completed_at"):
            ended = f"Follow-ups {en.get('status')}"
            if en.get("cancel_reason"):
                ended += f": {en['cancel_reason']}"
            events.append(_event(en["completed_at"], ended))
    for o in held.get("open_tracking", []):
        events.append(_event(o.get("opened_at"), "Email opened"))
    for r in held.get("retry_queue", []):
        what = f"Retry queued (now {r.get('status')})"
        if r.get("last_error"):
            what += f" - {r['last_error']}"
        events.append(_event(r.get("created_at"), what))
    for a in held.get("activity_log", []):
        events.append(_event(a.get("timestamp"), f"{a['action']}: {a.get('details')}"))
    events = [e for e in events if e]
    # Newest first. Events in the same second keep cause before effect: they
    # were collected in pipeline order (found, sent, suppressed, ..., retried,
    # logged), so the later-collected one is listed first.
    order = {id(e): i for i, e in enumerate(events)}
    events.sort(key=lambda e: (e["when"], order[id(e)]), reverse=True)
    return events[:TIMELINE_LIMIT]


def _unsettled_sends(held: dict) -> list[dict]:
    """The contact's 'preparing' rows: not delivered, and not known not to be."""
    return [
        e for e in held.get("email_log", []) if e.get("delivery_status") == "preparing"
    ]


def _facts(contact: dict, held: dict, reason: str | None, suppressed) -> list:
    facts = [("Status", contact.get("status"))]
    if reason:
        facts.append(("Reason", reason))
    if suppressed:
        facts.append(("Do-not-contact list", suppressed))
    if contact.get("lead_score") is not None:
        facts.append(
            ("Lead score", f"{contact['lead_score']} (tier {contact.get('lead_tier')})")
        )
    facts.append(("Qualified", contact.get("qualified_at") or "not yet"))
    if contact.get("is_verified"):
        facts.append(("Address verified", "yes"))
    sent = [
        e
        for e in held.get("email_log", [])
        if e.get("delivery_status") in SENT_STATUSES
    ]
    facts.append(("Emails delivered", str(len(sent))))
    if sent:
        facts.append(("Last delivered", max(e["timestamp"] for e in sent)))
    unsettled = _unsettled_sends(held)
    if unsettled:
        started = max(e["timestamp"] for e in unsettled)
        facts.append(("Unsettled send", f"{UNSETTLED_SEND} (since {started})"))
    for en in held.get("sequence_enrollments", []):
        state = f"{en.get('status')} at step {en.get('current_step')}"
        if en.get("cancel_reason"):
            state += f" ({en['cancel_reason']})"
        facts.append(("Follow-ups", state))
    for r in held.get("retry_queue", []):
        if r.get("status") in ("pending", "in_flight"):
            due = r.get("next_retry_at")
            facts.append(("Retry", f"due {_sort_key(due) if due else 'now'}"))
    return facts


def explain_contact(db: Database, contact_id: int) -> dict | None:
    """The explanation for one contact, or None if there is no such contact."""
    contact = db.get_contact_by_id(contact_id)
    if not contact:
        return None
    status = contact.get("status") or "pending"
    held = records_held_about(db, contact["email"])
    suppression = (held.get("suppression_list") or [{}])[0].get("reason")

    if status == "erased":
        headline = HEADLINES["erased"]
    elif suppression:
        headline = SUPPRESSION_HEADLINES.get(
            suppression, "Not emailed: the address is on the do-not-contact list."
        )
    elif _unsettled_sends(held) and status not in CLOSED_CONTACT_STATUSES:
        headline = UNSETTLED_HEADLINE
    else:
        headline = HEADLINES.get(
            status, f"In the state '{status}', which this view does not describe."
        )

    search = None
    if contact.get("search_id"):
        row = db.conn.execute(
            "SELECT search_query FROM search_log WHERE id = ?", (contact["search_id"],)
        ).fetchone()
        search = dict(row) if row else None

    reason = _current_reason(status, contact.get("discard_reason"))
    return {
        "id": contact["id"],
        "email": contact["email"],
        "status": status,
        "headline": headline,
        "facts": _facts(contact, held, reason, suppression),
        "timeline": _timeline(held, search),
    }
