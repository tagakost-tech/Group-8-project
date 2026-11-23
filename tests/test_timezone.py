"""Tests for the app clock / Italy timezone switch."""

from datetime import datetime, timezone

from modules import clock


def test_default_timezone_setting_is_italy(settings_factory):
    assert settings_factory().app_timezone == "Europe/Rome"


def test_now_is_timezone_aware_italy_offset():
    clock.set_timezone("Europe/Rome")
    n = clock.now()
    assert n.tzinfo is not None
    # Rome is UTC+1 (CET) or UTC+2 (CEST, summer).
    assert n.utcoffset().total_seconds() / 3600 in (1, 2)


def test_db_now_is_sqlite_shaped():
    clock.set_timezone("Europe/Rome")
    # No %z on purpose: db_now() producing an OFFSET-FREE string is the
    # property being asserted (an offset breaks SQLite's datetime()).
    datetime.strptime(clock.db_now(), "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007


def test_today_str_matches_local_date():
    clock.set_timezone("Europe/Rome")
    assert clock.today_str() == clock.now().strftime("%Y-%m-%d")


def test_unknown_timezone_falls_back_to_utc():
    try:
        assert clock.set_timezone("Not/AZone") == timezone.utc
    finally:
        clock.set_timezone("Europe/Rome")  # restore for other tests


def test_warmup_uses_app_clock(settings_factory, tmp_path):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"), verify_emails=False, app_timezone="Europe/Rome"
    )
    a = OutreachAgent(s)
    assert a.warmup._today_str() == clock.today_str()
