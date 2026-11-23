"""Every contact has a retention period, and the first email comes within it.

Retention kept anyone who replied, any client and any lead forever, and gave
a contact never emailed 90 days - although the first email carries the
notice Art. 14(3)(a) requires within a month of collection. A decline was
filed as an ordinary reply, and kept forever too.

Now (L1.8, L1.16 and L5.1 - the developer's proposals, pending counsel):
- a contact not emailed within FIRST_CONTACT_MAX_AGE_DAYS (30, at most a
  month) of collection is refused at the send check and erased at the next
  run;
- a contact who replied, or became a lead, is erased RETENTION_DAYS_ENGAGED
  (730) after the last exchange: the last email, reply or lead alert;
- a clean decline from someone we emailed is an objection: the address goes
  on the do-not-contact list and the rest is erased at the next run. A reply
  that also shows interest is left for a person to read;
- a contact at a client company is erased like any other.
"""

import pytest
from pydantic import ValidationError

from agent import OutreachAgent
from modules import reply_intent
from modules.database import Database, erasure_token
from modules.imap_monitor import ImapMonitor
from modules.retention import apply_retention


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "r.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


def _ago(db, table, column, row_id, days):
    db.conn.execute(
        f"UPDATE {table} SET {column} = datetime('now','localtime',?) WHERE id = ?",
        (f"-{days} days", row_id),
    )
    db.conn.commit()


def _contact(db, email, *, age_days, status="pending", emailed_days_ago=None):
    cid = db.add_contact(db.log_search("q"), email, name="Anna", organization="Brand")
    db.update_contact_status(cid, status)
    _ago(db, "contacts", "timestamp", cid, age_days)
    if emailed_days_ago is not None:
        log_id = db.log_email(cid, email, "Hello", delivery_status="sent")
        _ago(db, "email_log", "timestamp", log_id, emailed_days_ago)
    return cid


def _erased(db, cid):
    return db.get_contact_by_id(cid)["status"] == "erased"


def _monitor(db):
    monitor = ImapMonitor.__new__(ImapMonitor)
    monitor.db = db
    return monitor


def _reply(email, body):
    return {"email": email, "subject": "Re: Contract manufacturing", "body": body}


# ── The first email comes within a month of collection ──


def test_a_contact_not_emailed_within_the_limit_is_erased(db, s):
    late = _contact(db, "late@brand.de", age_days=31)
    fresh = _contact(db, "fresh@brand.de", age_days=20)
    apply_retention(db, s)
    assert _erased(db, late)
    assert not _erased(db, fresh)


@pytest.mark.parametrize("days", [0, 31, 90])
def test_the_limit_is_at_most_a_month(settings_factory, days):
    assert (
        settings_factory(FIRST_CONTACT_MAX_AGE_DAYS=14).first_contact_max_age_days == 14
    )
    with pytest.raises(ValidationError):
        settings_factory(FIRST_CONTACT_MAX_AGE_DAYS=days)


def _agent(settings_factory, tmp_path, monkeypatch):
    agent = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            ai_humanize_emails=False,
        )
    )
    agent.dispatched = []
    monkeypatch.setattr(
        agent.email_sender,
        "dispatch_to_contact",
        lambda **k: agent.dispatched.append(k["contact"]["email"]) or 1,
    )
    return agent


def test_a_first_email_past_the_limit_is_refused(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    late = _contact(agent.db, "late@brand.de", age_days=31)
    fresh = _contact(agent.db, "fresh@brand.de", age_days=29)

    assert agent._process_contact(agent.db.get_contact_by_id(late)) is False
    assert agent._process_contact(agent.db.get_contact_by_id(fresh)) is True
    assert agent.dispatched == ["fresh@brand.de"]


def test_a_follow_up_is_not_a_first_email(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    cid = _contact(
        agent.db, "seq@brand.de", age_days=40, status="emailed", emailed_days_ago=5
    )
    contact = agent.db.get_contact_by_id(cid)
    assert agent._process_contact(contact, is_followup=True) is True


# ── Engaged contacts: 730 days from the last exchange ──


def test_a_reply_is_kept_730_days_from_the_last_exchange(db, s):
    old = _contact(
        db, "old@brand.de", age_days=900, status="responded", emailed_days_ago=800
    )
    _ago(db, "contacts", "last_reply_at", old, 790)
    recent = _contact(
        db, "recent@brand.de", age_days=900, status="responded", emailed_days_ago=800
    )
    _ago(db, "contacts", "last_reply_at", recent, 10)
    within = _contact(
        db, "within@brand.de", age_days=500, status="responded", emailed_days_ago=400
    )

    apply_retention(db, s)

    assert _erased(db, old)
    assert not _erased(db, recent), "the reply 10 days ago is the last exchange"
    assert not _erased(db, within)


def test_a_lead_is_kept_730_days_from_the_alert(db, s):
    old = _contact(
        db, "old@brand.de", age_days=900, status="emailed", emailed_days_ago=800
    )
    db.record_lead_alert("old@brand.de", notified_to="ops@co.it", reply_excerpt="Yes")
    db.conn.execute(
        "UPDATE lead_alerts SET timestamp = datetime('now','localtime','-790 days')"
    )
    db.conn.commit()
    recent = _contact(
        db, "recent@brand.de", age_days=900, status="emailed", emailed_days_ago=800
    )
    db.record_lead_alert(
        "recent@brand.de", notified_to="ops@co.it", reply_excerpt="Yes"
    )

    apply_retention(db, s)

    assert _erased(db, old)
    assert not _erased(db, recent)


def test_a_client_company_contact_is_erased_like_any_other(db, s):
    cid = _contact(db, "buyer@client.de", age_days=40, status="active_client")
    apply_retention(db, s)
    assert _erased(db, cid)


def test_a_reply_records_when_it_came(db):
    cid = _contact(
        db, "anna@brand.de", age_days=5, status="emailed", emailed_days_ago=3
    )
    _monitor(db).process_replies([_reply("Anna@Brand.de", "Thanks, tell me more")])
    assert db.get_contact_by_id(cid)["last_reply_at"]


# ── A decline is an objection ──


@pytest.mark.parametrize(
    "body,decline",
    [
        ("Not interested, thanks.", True),
        ("No grazie, non siamo interessati.", True),
        ("We already work with a manufacturer, but would like a quote.", False),
        ("Not at this time - could you send samples next spring?", False),
        ("Sounds interesting, tell me more.", False),
        ("> not interested\n\nActually, please send the price list.", False),
    ],
)
def test_a_decline_is_a_reply_with_no_sign_of_interest(body, decline):
    assert reply_intent.is_decline("Re: Offer", body) is decline


def test_a_clean_decline_is_an_objection_and_is_erased(db, s):
    cid = _contact(db, "no@brand.de", age_days=5, status="emailed", emailed_days_ago=3)
    _monitor(db).process_replies([_reply("no@brand.de", "Not interested, thanks.")])

    assert db.is_suppressed("no@brand.de")
    assert db.get_contact_by_id(cid)["status"] == "unsubscribed"

    apply_retention(db, s)
    assert db.get_contact_by_id(cid)["email"] == erasure_token("no@brand.de")
    assert db.is_suppressed("no@brand.de"), "the objection outlives the contact"


def test_a_reply_that_also_shows_interest_is_not_an_objection(db):
    cid = _contact(
        db, "maybe@brand.de", age_days=5, status="emailed", emailed_days_ago=3
    )
    _monitor(db).process_replies(
        [_reply("maybe@brand.de", "We already work with a supplier, but send a quote.")]
    )
    assert not db.is_suppressed("maybe@brand.de")
    assert db.get_contact_by_id(cid)["status"] == "responded"


def test_a_decline_from_someone_never_emailed_records_nothing(db):
    _contact(db, "stranger@brand.de", age_days=5)
    _monitor(db).process_replies([_reply("stranger@brand.de", "Not interested.")])
    assert not db.is_suppressed("stranger@brand.de")


def test_an_opt_out_is_erased_at_the_next_run(db, s):
    cid = _contact(db, "out@brand.de", age_days=5, status="emailed", emailed_days_ago=3)
    db.record_opt_out("out@brand.de", "Opted out by email")
    apply_retention(db, s)
    assert _erased(db, cid)
    assert db.is_suppressed("out@brand.de")
