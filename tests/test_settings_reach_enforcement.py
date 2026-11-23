"""A setting the operator can change must reach the code that enforces it.

test_runtime_config.py already covers what CANNOT be edited. This file covers
the other half, which is where the real defects were: a value that the operator
edits, the API accepts, and the dashboard reports as applied - while the code
actually doing the enforcing keeps using the old number, or never reads the
setting at all.

Both failures below are silent by construction. Nothing errors, nothing logs a
warning, and the dashboard shows the new value. The only symptom is that the
agent goes on behaving the way it did before, which is exactly the situation an
operator reaches for these dials to escape.
"""

import pytest

from config import RUNTIME_EDITABLE


def _agent(tmp_path, settings_factory, **kw):
    from agent import OutreachAgent

    return OutreachAgent(settings_factory(db_path=str(tmp_path / "s.db"), **kw))


# ── The setting must reach the object that copied it ───────────────────────


def test_lowering_the_domain_cap_reaches_the_throttle(tmp_path, settings_factory):
    """DomainThrottle takes max_per_domain_per_day as a plain int in __init__.
    Replacing self.settings does not move that copy, so the override applied to
    the Settings object and nothing else.

    The scenario: a domain starts bouncing mid-run, the operator drops the cap
    to 1 from the dashboard to contain it, and the agent keeps sending 3/day.
    """
    a = _agent(tmp_path, settings_factory, max_per_domain_per_day=3)
    assert a.domain_throttle.max_per_domain_per_day == 3

    a.db.set_runtime_override("max_per_domain_per_day", 1)
    a._apply_runtime_overrides()

    assert a.settings.max_per_domain_per_day == 1
    assert a.domain_throttle.max_per_domain_per_day == 1, (
        "the override reached Settings but not the object that enforces it - "
        "the dashboard reports a cap the sender is not applying"
    )


def test_the_lowered_domain_cap_actually_blocks(tmp_path, settings_factory):
    """Reading the attribute back proves the assignment; this proves the
    decision changes. Without it the test above passes on a throttle that reads
    its limit from somewhere else entirely."""
    a = _agent(tmp_path, settings_factory, max_per_domain_per_day=3)
    a.domain_throttle.record_domain_send("acme.com")
    assert a.domain_throttle.can_send_to_domain("acme.com") is True

    a.db.set_runtime_override("max_per_domain_per_day", 1)
    a._apply_runtime_overrides()

    assert a.domain_throttle.can_send_to_domain("acme.com") is False, (
        "one send already went to acme.com today and the cap is now 1"
    )


def test_raising_the_domain_cap_also_reaches_the_throttle(tmp_path, settings_factory):
    """Control, in the other direction: a resync that only ever tightened would
    pass the two tests above while still being wrong."""
    a = _agent(tmp_path, settings_factory, max_per_domain_per_day=1)
    a.domain_throttle.record_domain_send("acme.com")
    assert a.domain_throttle.can_send_to_domain("acme.com") is False

    a.db.set_runtime_override("max_per_domain_per_day", 5)
    a._apply_runtime_overrides()

    assert a.domain_throttle.can_send_to_domain("acme.com") is True


# ── A setting nothing reads is a lie on the dashboard ──────────────────────


def test_emails_per_hour_paces_sending(tmp_path, settings_factory):
    """emails_per_hour was validated, stored, offered on the dashboard and read
    by nothing. The sender used delay_between_emails alone, so the two defaults
    disagreed by a factor of 24 and no one noticed, because the disagreement
    could not surface.
    """
    a = _agent(
        tmp_path,
        settings_factory,
        emails_per_hour=30,
        delay_between_emails=5.0,
        send_jitter_seconds=0.0,
    )
    assert a._send_delay() == pytest.approx(120.0), (
        "30 emails an hour is one every 120s; a 5s delay sends 720/hour"
    )


def test_a_longer_explicit_delay_still_wins(tmp_path, settings_factory):
    """emails_per_hour is a floor, not an override. An operator who set a
    deliberately slow delay must not have it sped up by the hourly rate."""
    a = _agent(
        tmp_path,
        settings_factory,
        emails_per_hour=60,  # implies a 60s floor
        delay_between_emails=300.0,
        send_jitter_seconds=0.0,
    )
    assert a._send_delay() == pytest.approx(300.0)


def test_lowering_the_hourly_rate_slows_sending(tmp_path, settings_factory):
    """The operator-facing behaviour: turn the dial down, sends get further
    apart. This is the whole point of the control."""
    a = _agent(tmp_path, settings_factory, emails_per_hour=60, send_jitter_seconds=0.0)
    before = a._send_delay()

    a.db.set_runtime_override("emails_per_hour", 10)
    a._apply_runtime_overrides()

    assert a._send_delay() > before
    assert a._send_delay() == pytest.approx(360.0)


def test_jitter_is_added_on_top_of_the_floor(tmp_path, settings_factory):
    """Jitter exists so sends do not land on a metronome. Applying the floor
    after jitter would quantise every send back onto one, undoing it."""
    a = _agent(
        tmp_path,
        settings_factory,
        emails_per_hour=60,
        delay_between_emails=1.0,
        send_jitter_seconds=30.0,
    )
    seen = {a._send_delay() for _ in range(50)}
    assert all(d >= 60.0 for d in seen), "the floor must hold"
    assert len(seen) > 1, "jitter was flattened away by the floor"
    assert max(seen) <= 90.0


# ── The general rule ───────────────────────────────────────────────────────


def test_every_runtime_editable_setting_is_read_somewhere():
    """A field on the allow-list that no code reads is a control that does
    nothing - worse than absent, because the operator believes they have acted.
    emails_per_hour sat here unread; this catches the next one.

    Scoped to the settings being READ rather than obeyed - proving a value is
    obeyed is what the specific tests above are for.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    sources = [root / "agent.py", root / "main.py"] + list(
        (root / "modules").glob("*.py")
    )
    text = "\n".join(p.read_text(encoding="utf-8") for p in sources)

    unread = []
    for field in sorted(RUNTIME_EDITABLE):
        # A real attribute access, not the bare name: main.py's config panel
        # lists every editable field by name, so matching the name alone would
        # call all of them "read".
        #
        # get_<field>() counts too. send_window_days holds a raw string like
        # "0,1,2,3,4" and is always consumed through Settings.get_send_window_days(),
        # which parses it - so the attribute itself appears only in config.py.
        if f".{field}" not in text and f"get_{field}()" not in text:
            unread.append(field)

    assert not unread, (
        f"{unread} can be edited from the dashboard but no code reads the "
        f"setting - the operator would be changing nothing"
    )
