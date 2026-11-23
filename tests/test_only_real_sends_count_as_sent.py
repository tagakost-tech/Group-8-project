"""A dry run or a failed send is not an email sent.

The Emails Sent card counted mail that went out (SENT_STATUSES). The charts
beside it counted rows: Emails Sent per Day took every row of email_log, the
funnel's Emailed took every contact with a row, and the Success Rate took every
row as an attempt. A dry run of a stored contact writes a row, so a rehearsal
drew bars and "Emailed" contacts next to a card reading 0.

Nothing tested a row in any state but 'sent', which is how a chart and a card
on one page could disagree. One rule now: SENT_STATUSES went out, and a dry run
is not an attempt at all.
"""

import pytest

import main
from modules.analytics import Analytics
from modules.contact_explainer import explain_contact
from modules.database import Database
from modules.email_sender import EmailSender

WENT_OUT = ("sent", "sent_unconfirmed")
DID_NOT_GO_OUT = ("dry_run", "failed", "rejected", "preparing")


@pytest.fixture
def analytics(db):
    return Analytics(db)


def _logged(db, status, email="buyer@brand.de"):
    """A stored contact with one email in this state."""
    cid = db.add_contact(db.log_search("q", source="test"), email)
    return cid, db.log_email(cid, email, "s", delivery_status=status)


def _funnel(analytics):
    data = analytics.get_chart_data()["lead_funnel"]
    return dict(zip(data["labels"], data["data"]))


def _per_day(analytics):
    return sum(analytics.get_chart_data()["emails_per_day"]["data"])


def _rate(analytics):
    """The rate of the one day these tests log on. Every other day reads 0."""
    return max(analytics.get_chart_data()["success_rate_trend"]["data"])


def test_a_dry_run_of_a_stored_contact_is_not_an_email(analytics, db):
    """The reported bug, on the row the product really writes rather than a
    status typed into a test."""
    email = "prospect@brand.de"
    cid = db.add_contact(db.log_search("q", source="test"), email)
    sender = EmailSender(
        db=db,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="s@example.com",
        smtp_password="pw",
        sender_name="S",
    )
    sender.dispatch_to_contact(
        contact={"id": cid, "email": email},
        subject="s",
        html_body="<p>x</p>",
        plain_body="x",
        pdf_path=None,
        dry_run=True,
    )
    assert [e["delivery_status"] for e in db.get_all_emails()] == ["dry_run"]

    assert _per_day(analytics) == 0, "a rehearsal drew a bar of emails sent"
    assert _funnel(analytics) == {"Found": 1, "Emailed": 0, "Opened": 0, "Replied": 0}


def test_a_bounce_or_an_opt_out_does_not_unsend_the_mail(analytics, db):
    """Four contacts, of whom two were really written to. What happens after a
    send changes the contact, not the send."""
    _logged(db, "dry_run", "rehearsed@brand.de")
    _logged(db, "failed", "refused@brand.de")
    bounced, _ = _logged(db, "sent", "bounced@brand.de")
    db.add_to_suppression("bounced@brand.de", "bounce")
    db.update_contact_status(bounced, "bounced")
    unsubscribed, _ = _logged(db, "sent", "gone@brand.de")
    db.add_to_suppression("gone@brand.de", "unsubscribe")
    db.update_contact_status(unsubscribed, "unsubscribed")

    assert _funnel(analytics) == {"Found": 4, "Emailed": 2, "Opened": 0, "Replied": 0}
    assert _per_day(analytics) == 2


@pytest.mark.parametrize("status", DID_NOT_GO_OUT)
def test_mail_that_did_not_go_out_is_not_counted_as_sent(analytics, db, status):
    _logged(db, status)

    assert _funnel(analytics)["Emailed"] == 0
    assert _per_day(analytics) == 0


@pytest.mark.parametrize("status", WENT_OUT)
def test_mail_that_went_out_is_counted_as_sent(analytics, db, status):
    """Control: the fix must not work by counting nothing, and an unconfirmed
    send is deliberately never resent, so it has to count as a send."""
    _logged(db, status)

    assert _funnel(analytics)["Emailed"] == 1
    assert _per_day(analytics) == 1


def test_a_contact_reached_after_a_failure_counts_once(analytics, db):
    """The chart counts emails and the funnel counts people: one failure and two
    sends to one contact are two emails and one person."""
    cid, _ = _logged(db, "failed")
    db.log_email(cid, "buyer@brand.de", "s", delivery_status="sent")
    db.log_email(cid, "buyer@brand.de", "s2", delivery_status="sent")

    assert _funnel(analytics)["Emailed"] == 1
    assert _per_day(analytics) == 2


@pytest.mark.parametrize("status", ["dry_run", "failed"])
def test_an_open_on_mail_that_did_not_go_out_is_not_an_open(analytics, db, status):
    """Opened is a subset of Emailed. With a different rule on each stage the
    funnel widens, from Emailed 0 to Opened 1, and the Opens Tracked card reads
    this same figure."""
    _, log_id = _logged(db, status)
    analytics.record_open(analytics.generate_tracking_id(log_id))

    f = _funnel(analytics)

    assert f["Opened"] == 0
    assert f["Emailed"] >= f["Opened"], f


def test_an_open_on_an_unconfirmed_send_is_an_open(analytics, db):
    """Control: an unconfirmed send that was opened plainly arrived."""
    _, log_id = _logged(db, "sent_unconfirmed")
    analytics.record_open(analytics.generate_tracking_id(log_id))

    assert _funnel(analytics)["Opened"] == 1


def test_a_dry_run_does_not_lower_the_success_rate(analytics, db):
    """A rehearsal could not have failed. As an attempt, it drew a day of clean
    sends as half failed."""
    _logged(db, "sent", "a@brand.de")
    _logged(db, "dry_run", "b@brand.de")

    assert _rate(analytics) == 100.0


def test_a_failed_send_still_lowers_the_success_rate(analytics, db):
    """Control: the fix must not work by leaving out what went wrong."""
    _logged(db, "sent", "a@brand.de")
    _logged(db, "failed", "b@brand.de")
    _logged(db, "rejected", "c@brand.de")
    _logged(db, "dry_run", "d@brand.de")

    assert _rate(analytics) == 33.3


def test_a_send_that_never_got_an_outcome_still_lowers_the_success_rate(analytics, db):
    """A row left 'preparing' is a real attempt that died part way, and a rate
    that ignored it would read 100% through a crash loop."""
    _logged(db, "sent", "a@brand.de")
    _logged(db, "preparing", "b@brand.de")

    assert _rate(analytics) == 50.0


def test_an_unconfirmed_send_is_a_success(analytics, db):
    _logged(db, "sent_unconfirmed", "a@brand.de")
    _logged(db, "failed", "b@brand.de")

    assert _rate(analytics) == 50.0


def test_the_stats_success_rate_has_the_same_rule_as_the_chart(analytics, db):
    """/api/stats and `main.py status` report this rate. Both sides of it must
    be the ones the Success Rate chart draws."""
    _logged(db, "sent", "a@brand.de")
    _logged(db, "failed", "b@brand.de")
    _logged(db, "dry_run", "c@brand.de")
    _logged(db, "dry_run", "d@brand.de")

    stats = db.get_stats()

    assert stats["emails_sent"] == 1
    assert stats["emails_failed"] == 1
    assert stats["total_emails"] == 2
    assert stats["success_rate"] == 50.0 == _rate(analytics)


def test_every_sent_count_on_the_dashboard_agrees(tmp_path, settings_factory):
    """One email of every state, read back where the operator reads it. The
    deliverability panel is read at its source, since its route looks up DNS."""
    path = str(tmp_path / "dash.db")
    client = main.create_dashboard_app(settings_factory(db_path=path)).test_client()
    db = Database(path)
    contacts = [
        _logged(db, status, f"c{i}@brand.de")[0]
        for i, status in enumerate(WENT_OUT + DID_NOT_GO_OUT)
    ]

    charts = client.get("/api/charts").get_json()
    counts = {
        "Emails Sent card": client.get("/api/stats").get_json()["emails_sent"],
        "the same card, from the agent status": client.get(
            "/api/agent/status"
        ).get_json()["stats"]["emails_sent"],
        "Emails Sent per Day": sum(charts["emails_per_day"]["data"]),
        "funnel, Emailed": charts["lead_funnel"]["data"][1],
        "A/B table": sum(r["sent"] for r in client.get("/api/abtest").get_json()),
        "deliverability panel": db.get_deliverability_metrics()["sent"],
        "contact panels": sum(
            int(dict(explain_contact(db, cid)["facts"])["Emails delivered"])
            for cid in contacts
        ),
    }

    assert counts == dict.fromkeys(counts, len(WENT_OUT))


def test_a_preview_from_the_dashboard_moves_no_sent_count(tmp_path, settings_factory):
    """The demo, end to end: preview a stored contact from Direct Send, then read
    the Overview back. It is listed in the Emails table and counted nowhere."""
    path = str(tmp_path / "dash.db")
    settings = settings_factory(
        db_path=path,
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        ai_humanize_emails=False,
    )
    client = main.create_dashboard_app(settings).test_client()
    db = Database(path)
    db.add_contact(db.log_search("q", source="test"), "prospect@brand.de")

    answer = client.post(
        "/api/send-manual", json={"email": "prospect@brand.de", "dry_run": True}
    )

    assert answer.status_code == 200, answer.get_json()
    emails = client.get("/api/emails").get_json()
    assert [e["delivery_status"] for e in emails] == ["dry_run"]

    charts = client.get("/api/charts").get_json()
    assert client.get("/api/stats").get_json()["emails_sent"] == 0
    assert sum(charts["emails_per_day"]["data"]) == 0
    assert charts["lead_funnel"]["data"] == [1, 0, 0, 0]
    assert client.get("/api/warmup").get_json()["sent_today"] == 0
    assert client.get("/api/domains").get_json() == []
