"""Reaching the daily warm-up cap is written to the activity log once.

Every other guard writes its activity row once per episode and clears the
latch when the condition lifts. The warm-up cap wrote a WARMUP_LIMIT row on
every loop for the rest of the day - about 108 rows a day at the default
loop delay - burying the rows an operator actually needs to see. It now logs
once when the cap is reached, and again only after capacity has returned and
been used up a second time.
"""

import pytest

from agent import OutreachAgent


def _agent(settings_factory, tmp_path):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    a._interruptible_sleep = lambda *_a, **_k: None
    a._running = True
    return a


def _warmup_rows(a):
    return [
        r for r in a.db.get_recent_activity(limit=500) if r["action"] == "WARMUP_LIMIT"
    ]


@pytest.fixture
def capped(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path)
    assert a.warmup is not None
    a.capacity = False
    monkeypatch.setattr(a.warmup, "can_send", lambda account=None: a.capacity)
    a.sent = []
    monkeypatch.setattr(
        a, "_process_contact", lambda c, *x, **k: a.sent.append(c) or True
    )
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="buyer@brand.de", organization="Brand")
    a.batch = [a.db.get_contact_by_id(cid)]
    return a


def test_three_capped_loops_write_one_row(capped):
    a = capped
    for _ in range(3):
        a._email_contacts(a.batch)
    assert len(_warmup_rows(a)) == 1


def test_a_new_cap_after_capacity_returned_is_logged_again(capped):
    a = capped
    a._email_contacts(a.batch)
    a.capacity = True
    a._email_contacts(a.batch)
    assert a.sent, "the control: with capacity the contact is sent"
    a.capacity = False
    a._email_contacts(a.batch)
    assert len(_warmup_rows(a)) == 2
