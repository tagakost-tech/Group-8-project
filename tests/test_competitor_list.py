"""A named list of known competitors, alongside the keyword gate.

COMPETITOR_KEYWORDS guesses from how a company describes itself, and a guess
will always miss cases. It missed "CDMO" - the industry's own term for what
SUPPLECO is - until a live discovery run turned up naturacare.com describing
itself exactly that way. It caught two rivals in the same run only because
they happen to write in Italian.

SUPPLECO knows who its competitors are. A list of names is deterministic where
the keywords are probabilistic, and it catches the rival whose website says
nothing self-incriminating at all.

Deliberately the same mechanism as the active-client list: same file format,
same name-matching (distinctive-set equality, hardened against both over- and
under-matching against the real 182-name client register), same "never raises"
contract, same three rejection points. A second way of doing the same job
would be a second thing to get wrong.
"""

import pytest

from modules.clients import ClientRegister


def _agent(settings_factory, tmp_path, **kw):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        delay_between_emails=0.0,
        ai_humanize_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        **kw,
    )
    return OutreachAgent(s)


@pytest.fixture
def agent_with_competitors(settings_factory, tmp_path):
    """An agent that knows two named competitors."""
    a = _agent(settings_factory, tmp_path)
    a.competitor_register = ClientRegister(
        ["Naturacare", "Fine Foods & Pharmaceuticals S.p.A."]
    )
    return a


# ── The list blocks a rival the keywords would miss ───────────────────────


def test_a_named_competitor_is_not_emailed(agent_with_competitors):
    """The point of the list: naturacare's site said "CDMO" and the keyword
    gate let it through. A name needs no self-description at all."""
    a = agent_with_competitors
    assert a._known_competitor("Naturacare") == "Naturacare"


def test_the_legal_form_does_not_have_to_match(agent_with_competitors):
    """The register holds "Fine Foods & Pharmaceuticals S.p.A."; a website
    calls itself "Fine Foods". Same company."""
    a = agent_with_competitors
    assert a._known_competitor("Fine Foods") is not None


def test_a_genuine_prospect_is_not_blocked(agent_with_competitors):
    """The control that matters most: over-matching here deletes real
    pipeline, and does it invisibly."""
    a = agent_with_competitors
    for org in ["Nordic Wellness AB", "Bio Shop Berlin", "VitalBrand GmbH"]:
        assert a._known_competitor(org) is None, org


def test_the_send_is_actually_refused(agent_with_competitors, monkeypatch):
    """A helper nobody calls blocks nothing. This drives the real send path."""
    a = agent_with_competitors
    sent = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **kw: sent.append(kw) or 1
    )
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="info@naturacare.com", organization="Naturacare")
    a._process_contact(
        {"id": cid, "email": "info@naturacare.com", "organization": "Naturacare"}
    )
    assert sent == [], "an email was composed for a known competitor"


def test_the_refusal_is_recorded_for_the_operator(agent_with_competitors, monkeypatch):
    """Silently dropping a contact looks identical to never finding it."""
    a = agent_with_competitors
    monkeypatch.setattr(a.email_sender, "dispatch_to_contact", lambda **kw: 1)
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="info@naturacare.com", organization="Naturacare")
    a._process_contact(
        {"id": cid, "email": "info@naturacare.com", "organization": "Naturacare"}
    )
    rows = [
        r for r in a.db.get_recent_activity(20) if r["action"] == "KNOWN_COMPETITOR"
    ]
    assert rows, "the skip left no trace on the dashboard"


# ── Absence and failure must be safe ──────────────────────────────────────


def test_no_list_configured_blocks_nobody(settings_factory, tmp_path):
    """The feature is optional. Without a file the agent runs exactly as
    before - it must not reject everything, nor crash."""
    a = _agent(settings_factory, tmp_path)
    assert a.competitor_register is None
    assert a._known_competitor("Anybody At All") is None


def test_a_matching_failure_never_stops_a_run(agent_with_competitors):
    """Same contract as the client register: matching is a screen, not a
    dependency. It fails OPEN (nobody blocked) and says so loudly, because
    failing closed would silently halt the whole campaign."""

    class Exploding:
        def match(self, name):
            raise RuntimeError("register is corrupt")

    a = agent_with_competitors
    a.competitor_register = Exploding()
    assert a._known_competitor("Naturacare") is None


def test_a_missing_organisation_is_not_a_match(agent_with_competitors):
    a = agent_with_competitors
    for org in (None, "", "   "):
        assert a._known_competitor(org) is None
