"""Smoke tests for the Flask dashboard app (via the create_dashboard_app factory)."""

import pytest

import main


@pytest.fixture(autouse=True)
def _no_dns_records(monkeypatch):
    """The deliverability panel looks up the sending domain's SPF and DMARC
    records; here every lookup finds none instead of asking a real resolver."""
    import dns.resolver

    class _NoRecords:
        def resolve(self, *_a, **_k):
            raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "Resolver", _NoRecords)


def _client(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "dash.db"), blacklist_check_enabled=False)
    return main.create_dashboard_app(s).test_client()


def test_json_endpoints_smoke(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    endpoints = [
        "/api/stats",
        "/api/charts",
        "/api/warmup",
        "/api/retries",
        "/api/sequences",
        "/api/abtest",
        "/api/domains",
        "/api/errors",
        "/api/learning",
        "/api/config",
        "/api/contacts",
        "/api/emails",
        "/api/searches",
        "/api/activity",
        "/api/agent/status",
        "/api/deliverability",
        "/api/leads",
        "/api/blacklist",
    ]
    for ep in endpoints:
        resp = client.get(ep)
        assert resp.status_code == 200, f"{ep} -> {resp.status_code}"
        assert resp.is_json, f"{ep} did not return JSON"


def test_deliverability_guard_matches_the_agent(tmp_path, settings_factory):
    """The dashboard's breaker verdict must be computed the same way the agent
    computes it: on the RECENT window, not the lifetime total.

    The distinguishing case is a long-running campaign - thousands of healthy
    sends historically, only a trickle recently. Gating on the lifetime count
    clears the minimum-sends bar on history alone and reports "tripped" off a
    tiny recent sample; the agent, judging the window, does not. The dashboard
    then contradicts the agent it is supposed to be reporting on.
    """
    s = settings_factory(
        db_path=str(tmp_path / "d.db"),
        bounce_guard_enabled=True,
        bounce_guard_min_sends=5,
        max_bounce_rate=0.05,
    )
    app = main.create_dashboard_app(s)
    from modules.database import Database

    db = Database(str(tmp_path / "d.db"))
    sid = db.log_search("q")

    # 18 healthy sends from months ago: lifetime total is high...
    for i in range(18):
        cid = db.add_contact(sid, email=f"old{i}@b.de")
        db.log_email(cid, f"old{i}@b.de", "s", "p", delivery_status="sent")
    db.conn.execute(
        "UPDATE email_log SET timestamp = datetime('now','localtime','-60 days')"
    )
    db.conn.commit()

    # ...but only 2 sends inside the window, so there is not enough recent
    # signal to judge, however bad those two look.
    for i in range(2):
        cid = db.add_contact(sid, email=f"new{i}@b.de")
        db.log_email(cid, f"new{i}@b.de", "s", "p", delivery_status="sent")
        db.add_to_suppression(f"new{i}@b.de", reason="bounce")

    data = app.test_client().get("/api/deliverability").get_json()
    m = data["metrics"]
    assert m["sent"] == 20 and m["recent_sent"] == 2  # lifetime vs window
    assert m["bounce_rate"] == 1.0  # 2/2 in the window
    # Below bounce_guard_min_sends on the RECENT count -> not enough signal.
    assert data["guard"]["tripped"] is False


def test_index_page_renders(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    resp = client.get("/classic")
    assert resp.status_code == 200
    assert b"Outreach" in resp.data


def test_stats_shape(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    data = client.get("/api/stats").get_json()
    for key in ("total_contacts", "emails_sent", "success_rate", "pending_contacts"):
        assert key in data


def test_auth_enforced_when_password_set(tmp_path, settings_factory):
    import base64

    from conftest import anonymous

    s = settings_factory(
        db_path=str(tmp_path / "auth.db"),
        DASHBOARD_USERNAME="admin",
        DASHBOARD_PASSWORD="secret-passphrase",
    )
    client = anonymous(main.create_dashboard_app(s).test_client())
    # No credentials -> 401
    assert client.get("/api/stats").status_code == 401
    # Correct credentials -> 200
    good = base64.b64encode(b"admin:secret-passphrase").decode()
    assert (
        client.get("/api/stats", headers={"Authorization": f"Basic {good}"}).status_code
        == 200
    )
    # Recipient-facing endpoints stay public (not gated)
    assert client.get("/track/anything").status_code != 401


def test_no_dashboard_when_password_blank(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "noauth.db"), DASHBOARD_PASSWORD="")
    with pytest.raises(ValueError, match="DASHBOARD_PASSWORD is blank"):
        main.create_dashboard_app(s)


def test_config_endpoint_shape(tmp_path, settings_factory):
    client = _client(tmp_path, settings_factory)
    data = client.get("/api/config").get_json()
    assert "features" in data and "settings" in data
    assert "sequences" in data["features"]
    # No secrets leaked in the config payload.
    blob = str(data).lower()
    assert "smtp_password" not in blob and "llm_api_key" not in blob


def test_stats_count_rejected_sends_as_failures(tmp_path, settings_factory):
    """The headline "Emails Failed" read 0 while every send was being
    permanently rejected - the deliverability panel was fixed but the primary
    stats panel kept the broken-looks-quiet shape."""
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    app = main.create_dashboard_app(s)
    from modules.database import Database

    db = Database(str(tmp_path / "d.db"))
    sid = db.log_search("q")
    for i, status in enumerate(["sent", "failed", "rejected", "rejected"]):
        cid = db.add_contact(sid, email=f"x{i}@b.de")
        db.log_email(cid, f"x{i}@b.de", "s", "p", delivery_status=status)

    data = app.test_client().get("/api/stats").get_json()
    assert data["emails_sent"] == 1
    assert data["emails_failed"] == 3  # 'failed' + both 'rejected'


def test_emails_api_reports_the_sending_account(tmp_path, settings_factory):
    """Sender rotation means "which mailbox did this go out from?" must be
    answerable from the Emails table, not only from the log file."""
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    app = main.create_dashboard_app(s)
    from modules.database import Database

    db = Database(str(tmp_path / "d.db"))
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="x@b.de")
    db.log_email(
        cid,
        "x@b.de",
        "s",
        "p",
        delivery_status="sent",
        sender_account="export@suppleco.it",
    )

    rows = app.test_client().get("/api/emails").get_json()
    assert rows[0]["sender_account"] == "export@suppleco.it"


# ── Unblock must not reverse suppressions that are not ours to reverse ──────
# Adding an "unblock" action makes a dashboard block undoable, which is the
# point. But the suppression list also holds unsubscribes, GDPR erasures,
# bounces and SMTP rejections. Un-suppressing any of those puts mail back on
# the wire that must not go out - a right-to-object violation in the first two
# cases, a guaranteed hard bounce in the other two. So the SUPPRESSION REASON
# is checked, not the contact's status column: a contact can read "blocked"
# while its suppression row says "unsubscribe".

import pytest

from modules.database import Database


def _contact(tmp_path, settings_factory, reason, status="blocked"):
    s = settings_factory(db_path=str(tmp_path / "unblock.db"))
    app = main.create_dashboard_app(s)
    db = Database(str(tmp_path / "unblock.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "person@brand.de")
    db.add_to_suppression("person@brand.de", reason)
    db.update_contact_status(cid, status)
    return app.test_client(), db, cid


@pytest.mark.parametrize(
    "reason",
    ["unsubscribe", "gdpr_erasure", "bounce", "smtp_rejected", "manual"],
)
def test_unblock_refuses_to_reverse_a_protected_suppression(
    tmp_path, settings_factory, reason
):
    client, db, cid = _contact(tmp_path, settings_factory, reason)

    resp = client.post(f"/api/contacts/{cid}/action", json={"action": "unblock"})

    assert resp.status_code == 409, f"{reason} must be refused, not reversed"
    assert db.is_suppressed("person@brand.de"), (
        f"{reason} suppression was lifted - this address can now be emailed again"
    )
    assert db.get_suppression_reason("person@brand.de") == reason
    contact = db.get_contact_by_id(cid)
    assert contact["status"] == "blocked", "the contact must not re-enter the queue"


def test_unblock_reverses_a_dashboard_block(tmp_path, settings_factory):
    """Control: without this the guard above would pass by never working."""
    client, db, cid = _contact(tmp_path, settings_factory, "blocked_via_dashboard")

    resp = client.post(f"/api/contacts/{cid}/action", json={"action": "unblock"})

    assert resp.status_code == 200
    assert not db.is_suppressed("person@brand.de")
    assert db.get_contact_by_id(cid)["status"] == "pending"


def test_block_advertises_its_own_undo(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "b.db"))
    app = main.create_dashboard_app(s)
    db = Database(str(tmp_path / "b.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "x@brand.de")

    resp = app.test_client().post(
        f"/api/contacts/{cid}/action", json={"action": "block"}
    )
    assert resp.status_code == 200
    assert resp.get_json().get("undo") == "unblock"
    assert db.get_suppression_reason("x@brand.de") == "blocked_via_dashboard"


def test_remove_from_suppression_will_not_take_an_empty_reason_list(tmp_path):
    """There is deliberately no "remove regardless" path; an empty tuple must
    raise rather than quietly widen to "delete anything"."""
    db = Database(str(tmp_path / "r.db"))
    db.add_to_suppression("a@b.de", "unsubscribe")
    with pytest.raises(ValueError):
        db.remove_from_suppression("a@b.de", only_reasons=())
    assert db.is_suppressed("a@b.de")


def test_each_contact_says_the_language_of_its_emails(tmp_path, settings_factory):
    """Every email to a contact is Italian or English by email_language();
    the list says which. An erased contact's address is a token, which would
    read as English, so it says nothing."""
    s = settings_factory(db_path=str(tmp_path / "lang.db"), ITALIAN_COUNTRIES="IT")
    client = main.create_dashboard_app(s).test_client()
    db = Database(str(tmp_path / "lang.db"))
    sid = db.log_search("q")
    for email in ("acquisti@rossi.it", "sales@brand.com", "einkauf@brand.de"):
        db.add_contact(sid, email)
    erased = db.add_contact(sid, "gone@bianchi.it")
    db.update_contact_status(erased, "erased")

    rows = {c["email"]: c for c in client.get("/api/contacts").get_json()}
    assert rows["acquisti@rossi.it"]["language"] == "it"
    assert rows["sales@brand.com"]["language"] == "en"
    assert rows["einkauf@brand.de"]["language"] == "en"
    assert rows["gone@bianchi.it"]["language"] is None


def test_emails_sent_counts_the_unconfirmed_and_not_the_unsettled(
    tmp_path, settings_factory
):
    """A guard: the plain-words badges change what the page says, not what
    it counts. A send cut off after acceptance may have reached the person;
    one still being prepared has not."""
    s = settings_factory(db_path=str(tmp_path / "count.db"))
    client = main.create_dashboard_app(s).test_client()
    db = Database(str(tmp_path / "count.db"))
    sid = db.log_search("q")
    for i, status in enumerate(
        ["sent", "sent_unconfirmed", "preparing", "rejected", "dry_run"]
    ):
        cid = db.add_contact(sid, f"p{i}@brand{i}.de")
        db.log_email(cid, f"p{i}@brand{i}.de", "s", delivery_status=status)
    assert client.get("/api/stats").get_json()["emails_sent"] == 2
