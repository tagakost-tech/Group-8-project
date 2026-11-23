"""Open tracking is off unless deliberately enabled, and records no one.

An open-tracking pixel reads information from the recipient's device, which
ePrivacy Art. 5(3) (Codice Privacy Art. 122) allows only with prior consent.
The Garante applied that to email pixels in April 2026 (provvedimento 284),
following EDPB Guidelines 2/2023. A cold B2B prospect has consented to
nothing, so the pixel cannot be the default.

It shipped ON, and every open stored the reader's IP address and User-Agent -
device data nothing in the agent ever reads. The feature stays for a future
consent-based use; it just starts off, and when on it keeps only the time.
"""


def test_tracking_is_off_by_default(settings_factory):
    assert settings_factory().open_tracking_enabled is False


def test_an_open_records_no_ip_or_user_agent(settings_factory, tmp_path):
    import main
    from modules.analytics import Analytics

    s = settings_factory(db_path=str(tmp_path / "t.db"), OPEN_TRACKING_ENABLED=True)
    app = main.create_dashboard_app(s)
    db = main.Database(s.get_abs_db_path())
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="info@brand.de")
    log_id = db.log_email(cid, "info@brand.de", "s", delivery_status="sent")
    Analytics(db).generate_tracking_id(log_id, "tid-1")

    resp = app.test_client().get(
        "/track/tid-1",
        headers={"User-Agent": "TestAgent/1.0"},
        environ_base={"REMOTE_ADDR": "203.0.113.5"},
    )
    assert resp.status_code == 200

    row = db.conn.execute(
        "SELECT opened_at, ip_address, user_agent FROM open_tracking "
        "WHERE tracking_id = 'tid-1'"
    ).fetchone()
    assert row[0] is not None, "the open itself must still be recorded"
    assert row[1] is None, "the reader's IP address was stored"
    assert row[2] is None, "the reader's User-Agent was stored"


def test_doctor_warns_when_tracking_is_on(settings_factory, tmp_path):
    from main import run_doctor_checks

    def tracking(**kw):
        s = settings_factory(db_path=str(tmp_path / "d.db"), **kw)
        return next(
            c
            for c in run_doctor_checks(s, network=False)
            if c.label.startswith("Open tracking")
        )

    on = tracking(OPEN_TRACKING_ENABLED=True)
    assert on.ok is False and not on.blocking
    assert tracking().ok is True


def _compose(settings_factory, tmp_path, monkeypatch, *, manual=False, **kw):
    from agent import OutreachAgent

    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "c.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            OPEN_TRACKING_ENABLED=True,
            **kw,
        )
    )
    cap = {}
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: cap.update(k) or 1
    )
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="x@brand.de")
    a._process_contact({"id": cid, "email": "x@brand.de"}, dry_run=True, manual=manual)
    return cap


def test_when_enabled_with_a_public_url_the_pixel_is_embedded(
    settings_factory, tmp_path, monkeypatch
):
    """The positive case, so the gate's other conditions are observable."""
    cap = _compose(
        settings_factory, tmp_path, monkeypatch, base_url="https://out.example.com"
    )
    assert cap["tracking_id"]
    assert f"/track/{cap['tracking_id']}" in cap["html_body"]


def test_a_direct_send_never_carries_a_pixel(settings_factory, tmp_path, monkeypatch):
    cap = _compose(
        settings_factory,
        tmp_path,
        monkeypatch,
        manual=True,
        base_url="https://out.example.com",
    )
    assert cap["tracking_id"] is None
