"""Tests for the V3 deliverability features: send window, bounce circuit
breaker, and the attach-vs-link profile toggle."""
from datetime import datetime
from zoneinfo import ZoneInfo

from modules import clock


def _agent(settings_factory, tmp_path, **overrides):
    from agent import OutreachAgent
    s = settings_factory(db_path=str(tmp_path / "a.db"), verify_emails=False,
                         dry_run=True, delay_between_emails=0.0,
                         ai_humanize_emails=False, **overrides)
    return OutreachAgent(s)


def _capture(agent, monkeypatch):
    cap = {}
    monkeypatch.setattr(agent.email_sender, "dispatch_to_contact",
                        lambda **kw: (cap.update(kw) or True))
    return cap


# ── Send window ────────────────────────────────────────────────────────────

def test_send_window_open_and_closed(settings_factory, tmp_path, monkeypatch):
    d = datetime(2026, 7, 29, 10, 0, tzinfo=ZoneInfo("Europe/Rome"))  # 10:00
    wd = str(d.weekday())
    monkeypatch.setattr(clock, "now", lambda: d)

    inside = _agent(settings_factory, tmp_path, send_window_enabled=True,
                    send_window_days=wd, send_window_start_hour=8,
                    send_window_end_hour=19)
    assert inside._within_send_window() is True

    after_hours = _agent(settings_factory, tmp_path, send_window_enabled=True,
                         send_window_days=wd, send_window_start_hour=8,
                         send_window_end_hour=9)   # 10:00 is past 09:00
    assert after_hours._within_send_window() is False


def test_send_window_excluded_day(settings_factory, tmp_path, monkeypatch):
    d = datetime(2026, 7, 29, 10, 0, tzinfo=ZoneInfo("Europe/Rome"))
    monkeypatch.setattr(clock, "now", lambda: d)
    other_day = str((d.weekday() + 1) % 7)
    a = _agent(settings_factory, tmp_path, send_window_enabled=True,
               send_window_days=other_day)
    assert a._within_send_window() is False


def test_send_window_disabled_always_open(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path, send_window_enabled=False)
    assert a._within_send_window() is True


# ── Bounce circuit breaker ──────────────────────────────────────────────────

def test_bounce_guard_trips_above_threshold(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path, bounce_guard_enabled=True,
               max_bounce_rate=0.05, bounce_guard_min_sends=5)
    assert a._bounce_guard_tripped() is False           # no data yet
    cid = a.db.add_contact(a.db.log_search("q"), "seed@brand.de", organization="Seed")
    for i in range(6):
        a.db.conn.execute(
            "INSERT INTO email_log (contact_id, recipient_email, subject, delivery_status)"
            " VALUES (?,?,?,'sent')", (cid, f"a{i}@brand.de", "s"))
    a.db.conn.commit()
    # Must be one of the addresses actually emailed above - a bounce from an
    # address we never sent to is not a bounce, and no longer counts.
    a.db.add_to_suppression("a0@brand.de", reason="bounce")  # 1/6 = 16% > 5%
    assert a._bounce_guard_tripped() is True


def test_bounce_guard_ignores_small_samples(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path, bounce_guard_enabled=True,
               max_bounce_rate=0.05, bounce_guard_min_sends=50)
    cid = a.db.add_contact(a.db.log_search("q"), "seed@brand.de", organization="Seed")
    a.db.conn.execute("INSERT INTO email_log (contact_id, recipient_email, subject,"
                      " delivery_status) VALUES (?,'x@brand.de','s','sent')", (cid,))
    a.db.conn.commit()
    a.db.add_to_suppression("b@brand.de", reason="bounce")
    assert a._bounce_guard_tripped() is False           # below min sample size


def test_bounce_guard_disabled(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path, bounce_guard_enabled=False)
    assert a._bounce_guard_tripped() is False


# ── Profile attach vs link ──────────────────────────────────────────────────
# The link goes into the generated body, so these use a template that has
# one: SUPPLECO's own first email (10 Nov 2026) is fixed text, with no {{body}}.
BODY_TEMPLATE = (
    "<html><body><p>{{greeting}}</p>{{body}}"
    "<a href='{{unsubscribe_url}}'>unsubscribe</a>{{legal_footer}}</body></html>"
)


def test_profile_linked_when_not_attached(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, attach_profile=False,
               profile_url="https://suppleco.example/profile.pdf")
    a._email_template = BODY_TEMPLATE
    cap = _capture(a, monkeypatch)
    a._process_contact({"id": 1, "email": "x@brand.de", "organization": "Brand"},
                       dry_run=True)
    assert cap["pdf_path"] is None
    assert "https://suppleco.example/profile.pdf" in cap["html_body"]


def test_profile_not_linked_when_attaching(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, attach_profile=True,
               profile_url="https://suppleco.example/profile.pdf")
    a._email_template = BODY_TEMPLATE
    cap = _capture(a, monkeypatch)
    a._process_contact({"id": 1, "email": "x@brand.de", "organization": "Brand"},
                       dry_run=True)
    assert "suppleco.example/profile.pdf" not in cap["html_body"]


def test_deliverability_metrics_shape(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    m = a.db.get_deliverability_metrics()
    assert set(m) >= {"sent", "failed", "bounced", "unsubscribed",
                      "suppressed", "bounce_rate"}


# ── retries and follow-ups must honour the same guards (regression) ─────────

def test_retries_respect_send_window(settings_factory, tmp_path, monkeypatch):
    """Retries used to bypass the window and could fire in the middle of the night."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    night = datetime(2026, 7, 29, 3, 0, tzinfo=ZoneInfo("Europe/Rome"))
    monkeypatch.setattr(clock, "now", lambda: night)
    a = _agent(settings_factory, tmp_path, retry_enabled=True,
               send_window_enabled=True, send_window_days=str(night.weekday()),
               send_window_start_hour=8, send_window_end_hour=19)
    sent = []
    monkeypatch.setattr(a, "_process_contact",
                        lambda *ar, **kw: (sent.append(1) or True))
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, "x@brand.de", organization="B")
    eid = a.db.log_email(cid, "x@brand.de", "s", delivery_status="failed")
    a.retry_queue.add_to_retry(eid, cid, "smtp error")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    a.db.conn.commit()
    a._running = True
    a._process_retries()
    assert sent == []          # held until business hours


def test_retries_respect_warmup_cap(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, retry_enabled=True,
               send_window_enabled=False, warmup_enabled=True,
               warmup_start_daily=1, warmup_increment=0, warmup_max_daily=1)
    a.warmup.record_send(account="")          # use up the only slot
    sent = []
    monkeypatch.setattr(a, "_process_contact",
                        lambda *ar, **kw: (sent.append(1) or True))
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, "y@brand.de", organization="B")
    eid = a.db.log_email(cid, "y@brand.de", "s", delivery_status="failed")
    a.retry_queue.add_to_retry(eid, cid, "smtp error")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01T00:00:00Z'")
    a.db.conn.commit()
    a._running = True
    a._process_retries()
    assert sent == []          # daily cap respected
