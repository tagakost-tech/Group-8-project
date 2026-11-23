"""Two guards that could be deleted with the suite still green.

Both were found by mutation, not by review, and both are the same shape: the
tests that appear to cover them never actually reach them.

_process_contact is the single point every send passes through - fresh
discovery, the backlog, follow-ups, retries and Direct Send all funnel here -
and its suppression check is the last thing standing between an opt-out and
another email. Three test files exercise those paths, and all three replace
_process_contact with a stub, so the check inside it was never executed once.

_clamp_limit bounds a caller-supplied row limit. `?limit=-1` reaches SQLite as
`LIMIT -1`, which means UNLIMITED - the entire prospect database, names, emails,
organisations and scores, in one response.
"""

import pytest

import main
from modules.clients import ClientRegister
from modules.database import Database


class _RecordingSender:
    """A sender that records instead of sending, so the guard above it is the
    only thing that can prevent a dispatch."""

    sender_name = "Marco"
    smtp_username = "agent@suppleco.test"

    def __init__(self):
        self.dispatched = []

    def dispatch_to_contact(self, contact, *args, **kw):
        self.dispatched.append(contact.get("email"))
        return 1


def _agent(tmp_path, settings_factory, **kw):
    from agent import OutreachAgent

    a = OutreachAgent(settings_factory(db_path=str(tmp_path / "c.db"), **kw))
    a._running = True
    a.relevance_filter = None
    a.lead_scorer = None
    a.email_verifier = None
    # A loaded list naming one unrelated company: it screens no contact
    # here. An empty or missing list stops live sends
    # (test_an_empty_client_list_refuses_live_sends.py).
    a.client_register = ClientRegister(["Zeta Unrelated Holding S.p.A."])
    return a


# ── The chokepoint itself ──────────────────────────────────────────────────


def test_the_send_chokepoint_refuses_a_suppressed_address(tmp_path, settings_factory):
    """_process_contact is driven for REAL here - only the SMTP sender is
    stubbed. Every other test replaces _process_contact wholesale, which is why
    deleting this check changed nothing anywhere in the suite."""
    a = _agent(tmp_path, settings_factory, min_lead_score_to_email=0)
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "gone@brand.de", organization="Brand")
    a.db.add_to_suppression("gone@brand.de", "unsubscribe")

    sender = _RecordingSender()
    ok = a._process_contact(
        {"id": cid, "email": "gone@brand.de", "organization": "Brand"},
        sender=sender,
        account_key="",
    )

    assert ok is False
    assert sender.dispatched == [], (
        "an unsubscribed address was dispatched - this is the guard every send "
        "path relies on, and nothing else stands behind it"
    )


def test_the_chokepoint_still_sends_to_everyone_else(tmp_path, settings_factory):
    """Control. Without it the test above passes on a chokepoint that refuses
    everything, which would be a worse bug and an invisible one."""
    a = _agent(tmp_path, settings_factory, min_lead_score_to_email=0)
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "fine@brand.de", organization="Brand")

    sender = _RecordingSender()
    a._process_contact(
        {"id": cid, "email": "fine@brand.de", "organization": "Brand"},
        sender=sender,
        account_key="",
    )

    assert sender.dispatched == ["fine@brand.de"]


def test_suppression_is_checked_case_insensitively_at_the_chokepoint(
    tmp_path, settings_factory
):
    """Addresses are scraped with the source page's casing; the suppression
    list stores lowercase. If the chokepoint compared raw strings, an opt-out
    would sit in the table matching nothing."""
    a = _agent(tmp_path, settings_factory, min_lead_score_to_email=0)
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "Info@Brand.de", organization="Brand")
    a.db.add_to_suppression("info@brand.de", "unsubscribe")

    sender = _RecordingSender()
    a._process_contact(
        {"id": cid, "email": "Info@Brand.de", "organization": "Brand"},
        sender=sender,
        account_key="",
    )

    assert sender.dispatched == []


# ── The row limit ──────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "w.db"))
    db = Database(str(tmp_path / "w.db"))
    sid = db.log_search("q", source="test")
    for i in range(30):
        db.add_contact(sid, f"p{i}@brand.de", organization=f"Co {i}")
    return main.create_dashboard_app(s).test_client()


@pytest.mark.parametrize("bad", [-1, 0, -999])
def test_a_non_positive_limit_does_not_dump_the_database(client, bad):
    """SQLite reads LIMIT -1 as unlimited. The whole prospect list - names,
    addresses, organisations, scores - in a single response."""
    resp = client.get(f"/api/contacts?limit={bad}")
    assert resp.status_code == 200
    rows = resp.get_json()
    assert 0 < len(rows) <= 30, f"limit={bad} returned {len(rows)} rows"


def test_an_absurd_limit_is_bounded(client):
    """Large positive values are a memory and CPU spike rather than a leak,
    but they are just as easy to send."""
    resp = client.get("/api/contacts?limit=100000")
    assert resp.status_code == 200
    assert len(resp.get_json()) <= 1000


def test_a_sensible_limit_is_honoured(client):
    """Control: clamping must not flatten every request to one value."""
    assert len(client.get("/api/contacts?limit=5").get_json()) == 5
