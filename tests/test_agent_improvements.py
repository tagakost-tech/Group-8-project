"""Regression tests for the core-agent improvements.

Three changes, all about spending a scarce resource well: the daily send
allowance, sender reputation, and wall-clock time spent doing nothing.
"""

import sqlite3
import time

import pytest

from modules.database import Database

# ── 1. The daily cap must be spent on the best leads ────────────────────────
# _email_contacts stops the moment _pick_sender() runs out of warm-up capacity,
# so the ORDER of the list decides who gets today's sends. get_pending_contacts
# sorted the backlog by score, but _qualify_contacts returned fresh contacts in
# SCRAPE order and _run_pending concatenated backlog + newly-qualified without
# re-sorting across the join. A 90-scoring lead found on page forty lost its
# send to a 16-scoring lead found on page one.


def _agent(tmp_path, settings_factory, **overrides):
    from agent import OutreachAgent

    s = settings_factory(db_path=str(tmp_path / "imp.db"), **overrides)
    agent = OutreachAgent(s)
    agent._running = True
    return agent


def test_the_send_order_is_by_lead_score(tmp_path, settings_factory):
    agent = _agent(tmp_path, settings_factory)
    scrape_order = [
        {"id": 1, "email": "a@x.de", "lead_score": 16},
        {"id": 2, "email": "b@x.de", "lead_score": 91},
        {"id": 3, "email": "c@x.de", "lead_score": 44},
        {"id": 4, "email": "d@x.de", "lead_score": 88},
    ]
    ordered = agent._by_lead_score(scrape_order)
    assert [c["lead_score"] for c in ordered] == [91, 88, 44, 16]


def test_ties_keep_discovery_order(tmp_path, settings_factory):
    """Stable, so the sort never reshuffles a batch it cannot rank."""
    agent = _agent(tmp_path, settings_factory)
    same = [{"id": i, "email": f"{i}@x.de", "lead_score": 50} for i in range(6)]
    assert [c["id"] for c in agent._by_lead_score(same)] == [0, 1, 2, 3, 4, 5]


def test_a_missing_score_sorts_last_and_does_not_crash(tmp_path, settings_factory):
    """Contacts added by Direct Send and legacy rows may have no score at all;
    None must not blow up the comparison."""
    agent = _agent(tmp_path, settings_factory)
    mixed = [
        {"id": 1, "email": "none@x.de", "lead_score": None},
        {"id": 2, "email": "good@x.de", "lead_score": 70},
        {"id": 3, "email": "absent@x.de"},
    ]
    ordered = agent._by_lead_score(mixed)
    assert ordered[0]["id"] == 2


def test_a_capped_batch_sends_to_the_highest_scorers(tmp_path, settings_factory):
    """The behaviour that matters: with capacity for 3 of 8, the 3 that go must
    be the top 3 by score, not the first 3 discovered."""
    agent = _agent(tmp_path, settings_factory)

    # Scores ascending in scrape order, so "first three" and "best three" are
    # disjoint - without the fix this test cannot pass by accident. Stored
    # rows, as production batches are: the send check re-reads each one.
    sid = agent.db.log_search("q", source="test")
    contacts = [
        agent.db.get_contact_by_id(
            agent.db.add_contact(sid, f"c{i}@x.de", lead_score=i * 10)
        )
        for i in range(1, 9)
    ]

    sent = []
    budget = {"left": 3}

    def fake_pick_sender():
        if budget["left"] <= 0:
            return None  # every account at its daily cap
        budget["left"] -= 1
        return ("acct", "Name", object())

    def fake_process(contact, *a, **kw):
        sent.append(contact["lead_score"])
        return True

    agent._pick_sender = fake_pick_sender
    agent._process_contact = fake_process
    agent.domain_throttle = None
    agent.warmup = None
    agent._within_send_window = lambda: True
    agent._bounce_guard_tripped = lambda: False
    agent._blacklist_guard_tripped = lambda: False

    agent._email_contacts(contacts)

    assert sorted(sent, reverse=True) == [80, 70, 60], (
        f"the cap was spent on {sorted(sent, reverse=True)} instead of the "
        f"three highest-scoring leads"
    )


# ── 2. One bad recipient domain must not take the whole campaign down ───────
# The global guard blends every domain into one rate. A provider rejecting us
# at 40% while everything else sits at 1% stays under the threshold, so we keep
# hammering it - until it finally drags the global rate over the line and
# sending stops to EVERY domain, healthy ones included.


def _seed(db, domain, sent, bounced):
    sid = db.log_search("q", source="test")
    for i in range(sent):
        cid = db.add_contact(sid, f"user{i}@{domain}")
        db.log_email(cid, f"user{i}@{domain}", "s", delivery_status="sent")
    for i in range(bounced):
        db.add_to_suppression(f"user{i}@{domain}", "bounce")


def test_a_hard_bouncing_domain_is_identified(tmp_path):
    db = Database(str(tmp_path / "d.db"))
    _seed(db, "bad.de", sent=10, bounced=6)
    _seed(db, "good.de", sent=10, bounced=0)

    stats = db.get_domain_bounce_stats(min_sends=5)

    assert stats["bad.de"]["bounce_rate"] == 0.6
    assert stats["bad.de"]["enough_data"] is True
    assert stats["good.de"]["bounce_rate"] == 0.0


def test_a_domain_with_too_little_data_is_not_judged(tmp_path):
    """One bounce on a single send reads as 100% and would blacklist a
    perfectly good domain forever on one dead address."""
    db = Database(str(tmp_path / "d.db"))
    _seed(db, "tiny.de", sent=1, bounced=1)

    stats = db.get_domain_bounce_stats(min_sends=5)

    assert stats["tiny.de"]["bounce_rate"] == 1.0
    assert stats["tiny.de"]["enough_data"] is False


def test_the_backoff_skips_only_the_bad_domain(tmp_path, settings_factory):
    agent = _agent(
        tmp_path,
        settings_factory,
        domain_bounce_backoff_enabled=True,
        max_domain_bounce_rate=0.30,
        domain_bounce_min_sends=5,
    )
    _seed(agent.db, "bad.de", sent=10, bounced=6)
    _seed(agent.db, "good.de", sent=10, bounced=0)

    agent._refresh_domain_backoff()

    assert agent._domain_backed_off("someone@bad.de") is not None
    assert agent._domain_backed_off("someone@good.de") is None
    # Case and whitespace must not be a way around the check.
    assert agent._domain_backed_off("  Someone@BAD.de ") is not None


def test_a_backed_off_domain_is_not_emailed(tmp_path, settings_factory):
    agent = _agent(
        tmp_path,
        settings_factory,
        domain_bounce_backoff_enabled=True,
        max_domain_bounce_rate=0.30,
        domain_bounce_min_sends=5,
    )
    _seed(agent.db, "bad.de", sent=10, bounced=6)
    _seed(agent.db, "good.de", sent=10, bounced=0)

    sent = []
    agent._pick_sender = lambda: ("acct", "Name", object())
    agent._process_contact = lambda c, *a, **kw: sent.append(c["email"]) or True
    agent.domain_throttle = None
    agent.warmup = None
    agent._within_send_window = lambda: True
    agent._bounce_guard_tripped = lambda: False
    agent._blacklist_guard_tripped = lambda: False

    sid = agent.db.log_search("q", source="test")
    agent._email_contacts(
        [
            agent.db.get_contact_by_id(
                agent.db.add_contact(sid, "new@bad.de", lead_score=90)
            ),
            agent.db.get_contact_by_id(
                agent.db.add_contact(sid, "new@good.de", lead_score=10)
            ),
        ]
    )

    assert "new@good.de" in sent, "the healthy domain must keep receiving mail"
    assert "new@bad.de" not in sent, (
        "kept emailing a domain bouncing at 60% - this is what burns the "
        "sending reputation before the global guard ever trips"
    )


def test_the_backoff_can_be_switched_off(tmp_path, settings_factory):
    """Guard: the feature flag must actually disable it."""
    agent = _agent(tmp_path, settings_factory, domain_bounce_backoff_enabled=False)
    _seed(agent.db, "bad.de", sent=10, bounced=9)

    agent._refresh_domain_backoff()

    assert agent._domain_backed_off("x@bad.de") is None


def test_a_failed_backoff_query_does_not_stop_sending(tmp_path, settings_factory):
    """Fails OPEN, unlike the global guard. This is a refinement on top of a
    protection that is still in place; failing closed here would let one bad
    query halt the entire campaign."""
    agent = _agent(tmp_path, settings_factory, domain_bounce_backoff_enabled=True)

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    agent.db.get_domain_bounce_stats = _boom
    agent._refresh_domain_backoff()

    assert agent._domain_backoff == {}
    assert agent._domain_backed_off("anyone@anywhere.de") is None


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"max_domain_bounce_rate": 0.0}, "fraction in"),
        ({"max_domain_bounce_rate": 1.5}, "fraction in"),
        # At 1 a domain is backed off only on more bounces than sends.
        ({"max_domain_bounce_rate": 1.0}, "fraction in"),
        ({"domain_bounce_min_sends": 0}, "at least 1"),
        ({"domain_bounce_min_sends": -1}, "at least 1"),
    ],
)
def test_dangerous_backoff_settings_are_rejected(settings_factory, overrides, expected):
    """Matches the validator's own message on purpose. `pytest.raises(Exception)`
    alone passed against the pre-change config for entirely the wrong reason:
    pydantic forbids unknown fields, so "this setting does not exist yet" and
    "this value is dangerous" were indistinguishable."""
    with pytest.raises(Exception, match=expected):
        settings_factory(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"max_domain_bounce_rate": 0.30},
        {"domain_bounce_min_sends": 1},
        {"domain_bounce_min_sends": 50},
        {"domain_bounce_backoff_enabled": False},
    ],
)
def test_valid_backoff_settings_are_accepted(settings_factory, overrides):
    """The discriminating half of the pair above, and an over-correction guard:
    the new validators must not reject legitimate configuration."""
    assert settings_factory(**overrides) is not None


# ── 3. Do not poll a closed send window all night ──────────────────────────
# The loop woke every loop_delay seconds to discover it still could not send -
# roughly 156 no-op passes over a 13-hour night.


def test_the_loop_sleeps_until_the_window_opens(tmp_path, settings_factory):
    agent = _agent(
        tmp_path,
        settings_factory,
        send_window_enabled=True,
        send_window_start_hour=8,
        send_window_end_hour=19,
    )
    agent.imap_monitors = []  # no inbox to starve
    agent._within_send_window = lambda: False
    agent._seconds_until_send_window = lambda: 9 * 3600

    assert agent._next_loop_delay(300.0) == 9 * 3600


def test_an_open_window_keeps_the_normal_delay(tmp_path, settings_factory):
    """Guard: this must not slow the agent down while it can actually work."""
    agent = _agent(tmp_path, settings_factory, send_window_enabled=True)
    agent._within_send_window = lambda: True

    assert agent._next_loop_delay(300.0) == 300.0


def test_the_long_sleep_never_starves_the_inbox_check(tmp_path, settings_factory):
    """_maybe_check_imap is deliberately NOT gated on the send window: reading
    the inbox is how bounces get suppressed and opt-outs honoured. Sleeping
    past it would switch off opt-out processing until morning - a legal
    problem, not a latency one."""
    agent = _agent(
        tmp_path,
        settings_factory,
        send_window_enabled=True,
        imap_check_interval_minutes=15,
    )
    agent.imap_monitors = [object()]  # an inbox that must keep being read
    agent._within_send_window = lambda: False
    agent._seconds_until_send_window = lambda: 9 * 3600
    agent._last_imap_check = time.time()  # just checked; next due in 15 min

    delay = agent._next_loop_delay(300.0)

    assert delay <= 15 * 60 + 1, (
        f"slept {delay}s, past the next inbox check - bounces and opt-outs "
        f"would go unprocessed until the window reopened"
    )


def test_the_window_open_time_is_computed_from_the_configured_days(
    tmp_path, settings_factory
):
    """Walks forward day by day: the day set can be any subset, and stepping
    through midnight in a DST-aware zone is not a fixed number of hours."""
    agent = _agent(
        tmp_path,
        settings_factory,
        send_window_enabled=True,
        send_window_days="0,1,2,3,4",
        send_window_start_hour=8,
        send_window_end_hour=19,
    )
    agent._within_send_window = lambda: False

    wait = agent._seconds_until_send_window()

    assert wait > 0, "outside the window there must be a positive wait"
    assert wait <= 8 * 24 * 3600, "must resolve within a week, not run away"


def test_an_open_window_needs_no_wait(tmp_path, settings_factory):
    agent = _agent(tmp_path, settings_factory, send_window_enabled=True)
    agent._within_send_window = lambda: True
    assert agent._seconds_until_send_window() == 0.0


def test_a_disabled_window_needs_no_wait(tmp_path, settings_factory):
    agent = _agent(tmp_path, settings_factory, send_window_enabled=False)
    assert agent._seconds_until_send_window() == 0.0
    assert agent._next_loop_delay(300.0) == 300.0
