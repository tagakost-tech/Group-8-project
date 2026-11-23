"""Tests for multi-account sending: per-account warmup and round-robin rotation
that adds each mailbox's daily cap together."""

import json

# ── config: account list ────────────────────────────────────────────────────


def test_single_account_by_default(settings_factory):
    s = settings_factory(smtp_username="a@x.com", smtp_password="p", sender_name="A")
    accts = s.get_sender_accounts()
    assert len(accts) == 1
    assert accts[0]["username"] == "a@x.com"


def test_extra_accounts_parsed_and_inherit(settings_factory):
    s = settings_factory(
        smtp_username="a@x.com",
        smtp_password="p",
        sender_name="A",
        smtp_host="smtp.office365.com",
        smtp_port=587,
        sender_accounts=json.dumps(
            [{"username": "b@x.com", "password": "p2", "sender_name": "B"}]
        ),
    )
    accts = s.get_sender_accounts()
    assert [a["username"] for a in accts] == ["a@x.com", "b@x.com"]
    assert accts[1]["smtp_host"] == "smtp.office365.com"  # inherited host
    assert accts[1]["smtp_port"] == 587
    assert accts[1]["imap_username"] == "b@x.com"  # defaults to own


def test_malformed_accounts_falls_back_to_primary(settings_factory):
    s = settings_factory(smtp_username="a@x.com", sender_accounts="not json[")
    assert len(s.get_sender_accounts()) == 1


def test_numbered_sender_slots(settings_factory):
    s = settings_factory(
        smtp_username="main@suppleco.com",
        smtp_password="p1",
        sender_name="Main",
        smtp_host="smtp.office365.com",
        smtp_port=587,
        sender_2_email="sales@suppleco.com",
        sender_2_password="p2",
        sender_2_name="Maria",
        sender_4_email="info@suppleco.com",
        sender_4_password="p4",
    )
    accts = s.get_sender_accounts()
    assert [a["username"] for a in accts] == [
        "main@suppleco.com",
        "sales@suppleco.com",
        "info@suppleco.com",
    ]  # blank slot 3 skipped
    assert accts[1]["sender_name"] == "Maria"
    assert accts[2]["sender_name"] == "Main"  # falls back to primary name
    assert accts[1]["smtp_host"] == "smtp.office365.com"  # inherits server
    assert accts[1]["imap_username"] == "sales@suppleco.com"


def test_numbered_and_json_accounts_combine(settings_factory):
    s = settings_factory(
        smtp_username="a@x.com",
        smtp_password="p",
        sender_2_email="b@x.com",
        sender_2_password="p2",
        sender_accounts=json.dumps([{"username": "c@x.com", "password": "p3"}]),
    )
    assert [a["username"] for a in s.get_sender_accounts()] == [
        "a@x.com",
        "b@x.com",
        "c@x.com",
    ]


# ── warmup is per-account ────────────────────────────────────────────────────


def test_warmup_counts_each_account_separately(db):
    from modules.warmup import WarmupScheduler

    w = WarmupScheduler(
        db,
        warmup_enabled=True,
        warmup_start_daily=2,
        warmup_increment=0,
        warmup_max_daily=2,
    )
    w.record_send(account="")
    w.record_send(account="")
    assert w.can_send(account="") is False  # primary capped at 2
    assert w.can_send(account="b@x.com") is True  # second mailbox untouched
    w.record_send(account="b@x.com")
    w.record_send(account="b@x.com")
    assert w.can_send(account="b@x.com") is False


def test_warmup_combined_status_sums(db):
    from modules.warmup import WarmupScheduler

    w = WarmupScheduler(
        db,
        warmup_enabled=True,
        warmup_start_daily=3,
        warmup_increment=0,
        warmup_max_daily=3,
    )
    w.record_send(account="")
    w.record_send(account="b@x.com")
    st = w.get_combined_status(["", "b@x.com"])
    assert st["daily_allowance"] == 6  # 3 + 3
    assert st["sent_today"] == 2
    assert st["remaining"] == 4
    assert st["accounts"] == 2


# ── agent rotation ───────────────────────────────────────────────────────────


def _agent(settings_factory, tmp_path, **overrides):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        delay_between_emails=0.0,
        ai_humanize_emails=False,
        send_window_enabled=False,
        bounce_guard_enabled=False,
        relevance_filter_enabled=False,
        min_lead_score_to_email=0,
        **overrides,
    )
    return OutreachAgent(s)


def test_agent_builds_pool_and_round_robins(settings_factory, tmp_path):
    a = _agent(
        settings_factory,
        tmp_path,
        smtp_username="a@x.com",
        smtp_password="p",
        sender_accounts=json.dumps([{"username": "b@x.com", "password": "p2"}]),
    )
    assert [k for (k, _, _) in a.senders] == ["", "b@x.com"]
    picks = [a._pick_sender()[0] for _ in range(4)]
    assert picks == ["", "b@x.com", "", "b@x.com"]


def test_two_accounts_double_the_daily_cap(settings_factory, tmp_path, monkeypatch):
    a = _agent(
        settings_factory,
        tmp_path,
        dry_run=False,
        warmup_enabled=True,
        warmup_start_daily=2,
        warmup_increment=0,
        warmup_max_daily=2,
        smtp_username="a@x.com",
        smtp_password="p",
        sender_accounts=json.dumps([{"username": "b@x.com", "password": "p2"}]),
        # The blocklist check looks the sending domain up in public DNS.
        blacklist_check_enabled=False,
    )
    sent = []
    for key, _name, es in a.senders:
        monkeypatch.setattr(
            es,
            "dispatch_to_contact",
            lambda _k=key, **kw: sent.append(_k) or True,
        )
    a._running = True
    sid = a.db.log_search("q")
    for i in range(6):
        a.db.add_contact(sid, f"c{i}@brand{i}.de", organization=f"Brand{i}")
    a._process_pending_contacts(dry_run=False)
    assert len(sent) == 4  # 2 accounts x cap 2
    assert sent.count("") == 2 and sent.count("b@x.com") == 2


# ── The dashboard must show WHICH mailbox sent each email ────────────────────
# With sender rotation live, an email_log row that doesn't say which account
# sent it makes per-mailbox problems (one account landing in spam, one
# throttled) undiagnosable from the dashboard.


class _OkSMTP:
    def noop(self):
        return (250, b"ok")

    def send_message(self, msg):
        pass

    def close(self):
        pass


def test_dispatch_records_the_sending_account(tmp_path):
    from modules.database import Database
    from modules.email_sender import EmailSender

    db = Database(str(tmp_path / "s.db"))
    s = EmailSender(db, "smtp.test", 587, "vendite@suppleco.it", "pw")
    s._new_connection = lambda: _OkSMTP()
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de")
    s.dispatch_to_contact({"id": cid, "email": "buyer@brand.de"}, "Hi", "<p>h</p>", "h")

    row = db.get_all_emails(limit=1)[0]
    assert row["sender_account"] == "vendite@suppleco.it"


def test_each_rotated_account_records_its_own_address(tmp_path):
    from modules.database import Database
    from modules.email_sender import EmailSender

    db = Database(str(tmp_path / "s.db"))
    sid = db.log_search("q")
    for account, rcpt in [
        ("vendite@suppleco.it", "a@brand.de"),
        ("export@suppleco.it", "b@brand.de"),
    ]:
        s = EmailSender(db, "smtp.test", 587, account, "pw")
        s._new_connection = lambda: _OkSMTP()
        cid = db.add_contact(sid, email=rcpt)
        s.dispatch_to_contact({"id": cid, "email": rcpt}, "Hi", "<p>h</p>", "h")

    by_rcpt = {r["recipient_email"]: r["sender_account"] for r in db.get_all_emails()}
    assert by_rcpt == {
        "a@brand.de": "vendite@suppleco.it",
        "b@brand.de": "export@suppleco.it",
    }


def test_dry_run_also_records_the_account(tmp_path):
    """Dry runs are how the customer previews behaviour - they must show the
    same attribution the live run will have."""
    from modules.database import Database
    from modules.email_sender import EmailSender

    db = Database(str(tmp_path / "s.db"))
    s = EmailSender(db, "smtp.test", 587, "vendite@suppleco.it", "pw")
    sid = db.log_search("q")
    cid = db.add_contact(sid, email="buyer@brand.de")
    s.dispatch_to_contact(
        {"id": cid, "email": "buyer@brand.de"}, "Hi", "<p>h</p>", "h", dry_run=True
    )
    assert db.get_all_emails(limit=1)[0]["sender_account"] == "vendite@suppleco.it"
