"""A refresh of the per-domain bounce backoff never shows an empty map.

Direct Send runs on the dashboard's request thread against the running
campaign agent, and reads the same backoff map. The refresh used to empty the
map first and then refill it after a GROUP BY over two tables, so a Direct
Send that landed during that query saw "no domain is backed off" and could
email a domain the agent had stopped emailing for bouncing. The map is now
built aside and swapped in whole.
"""

from agent import OutreachAgent

_BOUNCING = {
    "bouncy.example": {
        "enough_data": True,
        "bounce_rate": 0.5,
        "bounced": 5,
        "sent": 10,
    }
}


def _agent(settings_factory, tmp_path):
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            domain_bounce_backoff_enabled=True,
            max_domain_bounce_rate=0.1,
        )
    )


def test_a_reader_during_the_refresh_still_sees_the_backoff(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    a.db.get_domain_bounce_stats = lambda min_sends: dict(_BOUNCING)
    a._refresh_domain_backoff()
    assert a._domain_backed_off("x@bouncy.example")

    seen_mid_refresh = []

    def _slow_stats(min_sends):
        seen_mid_refresh.append(a._domain_backed_off("x@bouncy.example"))
        return dict(_BOUNCING)

    a.db.get_domain_bounce_stats = _slow_stats
    a._refresh_domain_backoff()

    assert seen_mid_refresh and seen_mid_refresh[0], (
        "the backoff map was empty while the refresh was running"
    )
    assert a._domain_backed_off("x@bouncy.example")


def test_a_recovered_domain_is_dropped_by_the_refresh(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    a.db.get_domain_bounce_stats = lambda min_sends: dict(_BOUNCING)
    a._refresh_domain_backoff()
    a.db.get_domain_bounce_stats = lambda min_sends: {}
    a._refresh_domain_backoff()
    assert a._domain_backed_off("x@bouncy.example") is None
