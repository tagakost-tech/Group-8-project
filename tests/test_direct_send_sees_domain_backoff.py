"""Direct Send honours the per-domain bounce backoff it claims to check.

send_single_email refused a domain that _domain_backed_off() reported as
bouncing - but that reads a cache filled by _refresh_domain_backoff(), which
only the batch loops call. The dashboard's dedicated send agent never runs a
batch, so its cache was always empty and the check could never fire: an
operator could keep hand-sending into a domain that was rejecting us.
"""

from agent import OutreachAgent


def _seed(db, domain, sent, bounced):
    sid = db.log_search("q", source="test")
    for i in range(sent):
        cid = db.add_contact(sid, f"user{i}@{domain}")
        db.log_email(cid, f"user{i}@{domain}", "s", delivery_status="sent")
    for i in range(bounced):
        db.add_to_suppression(f"user{i}@{domain}", "bounce")


def _agent(settings_factory, tmp_path):
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            domain_bounce_backoff_enabled=True,
            max_domain_bounce_rate=0.30,
            domain_bounce_min_sends=5,
        )
    )


def test_direct_send_refuses_a_bouncing_domain(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path)
    _seed(a.db, "dead.example", sent=10, bounced=6)
    sent = []
    monkeypatch.setattr(
        a, "_process_contact", lambda c, *x, **k: sent.append(c) or True
    )

    ok, message = a.send_single_email("new@dead.example", dry_run=False)

    assert ok is False
    assert "bouncing" in message
    assert sent == []


def test_a_healthy_domain_still_sends(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path)
    _seed(a.db, "fine.example", sent=10, bounced=0)
    monkeypatch.setattr(a, "_process_contact", lambda c, *x, **k: True)
    ok, _ = a.send_single_email("new@fine.example", dry_run=False)
    assert ok is True
