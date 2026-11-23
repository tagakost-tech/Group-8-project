"""The bounce rate must be a ratio drawn from one population.

The denominator is sends. The numerator counted every bounce-reason row in the
suppression list, whether or not that address had ever been emailed - so
anything that landed on the suppression list inflated the rate.

That is not hypothetical. Before the IMAP classifier was fixed on 31 July it
filed ordinary inbound mail as bounces: a newsletter, Nespresso marketing,
Google notifications, a Mural product email and Italian PEC receipts. Five rows
for addresses the agent had never contacted produced a 5.1% "bounce rate" over
79 sends, which tripped the circuit breaker and paused a campaign that had not
bounced at all.
"""


def _sent(db, email, n=1):
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, email)
    for _ in range(n):
        db.log_email(cid, email, "subject", delivery_status="sent")
    return cid


def test_a_bounce_from_an_address_we_never_emailed_does_not_count(db):
    for i in range(20):
        _sent(db, f"prospect{i}@brand.de")

    # Inbound mail misfiled as a bounce - never a recipient of ours.
    db.add_to_suppression("nespresso@mail-es.nespresso.com", "bounce")
    db.add_to_suppression("gemini-notes@google.com", "bounce")

    m = db.get_deliverability_metrics()

    assert m["recent_sent"] == 20
    assert m["recent_bounced"] == 0, (
        f"counted {m['recent_bounced']} bounce(s) from addresses that were "
        f"never emailed - the breaker would pause on phantom data"
    )
    assert m["bounce_rate"] == 0.0


def test_a_real_bounce_still_counts(db):
    """Control: without this the fix above would pass by never counting."""
    for i in range(20):
        _sent(db, f"prospect{i}@brand.de")
    db.add_to_suppression("prospect3@brand.de", "bounce")

    m = db.get_deliverability_metrics()

    assert m["recent_bounced"] == 1
    assert m["bounce_rate"] == 0.05


def test_an_smtp_rejection_of_a_real_recipient_counts(db):
    for i in range(10):
        _sent(db, f"p{i}@brand.de")
    db.add_to_suppression("p1@brand.de", "smtp_rejected")

    assert db.get_deliverability_metrics()["recent_bounced"] == 1


def test_the_match_is_case_insensitive(db):
    """Scraped addresses keep the source page's casing; the suppression list
    normalises to lower. A case-sensitive join would miss every real bounce."""
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "Info@Brand.de")
    db.log_email(cid, "Info@Brand.de", "s", delivery_status="sent")
    db.add_to_suppression("Info@Brand.de", "bounce")

    assert db.get_deliverability_metrics()["recent_bounced"] == 1


def test_unsubscribes_are_still_not_bounces(db):
    """Guard: only bounce reasons feed the rate."""
    for i in range(10):
        _sent(db, f"p{i}@brand.de")
    db.add_to_suppression("p2@brand.de", "unsubscribe")

    m = db.get_deliverability_metrics()
    assert m["recent_bounced"] == 0
    assert m["unsubscribed"] == 1


# ── The same rule, per domain ──────────────────────────────────────────────
#
# get_domain_bounce_stats was added after the fix above and did not inherit it.
# Per-domain the consequence is worse, not better: the denominator is one
# domain's sends rather than the whole campaign's, so two misfiled rows clear a
# 30% threshold that took five rows to move by 5% globally - and the result is
# that every genuine prospect at that domain silently stops being emailed.


def test_a_domain_is_not_backed_off_by_mail_we_never_sent_to(db):
    for i in range(10):
        _sent(db, f"prospect{i}@acme.com")

    # An autoreply, a DSN forwarded by a human, a postmaster notice - from a
    # real address at acme.com that was never one of our recipients.
    db.add_to_suppression("it-admin@acme.com", "bounce")
    db.add_to_suppression("noreply@acme.com", "bounce")

    s = db.get_domain_bounce_stats()["acme.com"]

    assert s["sent"] == 10
    assert s["bounced"] == 0, (
        f"counted {s['bounced']} bounce(s) from acme.com addresses that were "
        f"never emailed - every real prospect at acme.com stops being contacted"
    )
    assert s["bounce_rate"] == 0.0


def test_a_real_bounce_still_backs_the_domain_off(db):
    """Control: the fix must not work by never counting anything."""
    for i in range(10):
        _sent(db, f"prospect{i}@acme.com")
    for i in range(4):
        db.add_to_suppression(f"prospect{i}@acme.com", "bounce")

    s = db.get_domain_bounce_stats()["acme.com"]

    assert s["bounced"] == 4
    assert s["bounce_rate"] == 0.4
    assert s["enough_data"] is True


def test_one_bad_domain_does_not_implicate_a_healthy_one(db):
    """The whole reason per-domain stats exist."""
    for i in range(10):
        _sent(db, f"p{i}@bad.com")
        _sent(db, f"p{i}@good.com")
    for i in range(5):
        db.add_to_suppression(f"p{i}@bad.com", "bounce")

    stats = db.get_domain_bounce_stats()

    assert stats["bad.com"]["bounce_rate"] == 0.5
    assert stats["good.com"]["bounce_rate"] == 0.0


def test_the_domain_join_is_case_insensitive(db):
    """email_log keeps the casing the page was scraped with; the suppression
    list lower-cases. A case-sensitive join drops every real bounce, which
    fails silently as a domain that never backs off."""
    sid = db.log_search("q", source="test")
    for i in range(5):
        cid = db.add_contact(sid, f"Person{i}@Acme.com")
        db.log_email(cid, f"Person{i}@Acme.com", "s", delivery_status="sent")
    db.add_to_suppression("Person1@Acme.com", "bounce")

    assert db.get_domain_bounce_stats()["acme.com"]["bounced"] == 1


def test_a_domain_that_only_ever_bounced_cannot_trigger_a_backoff(db):
    """A bounce whose send fell outside the window leaves sent=0. That must not
    read as a 100% bounce rate - agent._refresh_domain_backoff skips it only
    because enough_data is False, so the flag has to be right."""
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "someone@stale.com")
    db.log_email(cid, "someone@stale.com", "s", delivery_status="sent")
    db.add_to_suppression("someone@stale.com", "bounce")

    s = db.get_domain_bounce_stats(window_days=7, min_sends=5)["stale.com"]

    assert s["enough_data"] is False, (
        "one send is not evidence; at min_sends=5 this domain must not be "
        "eligible for a backoff"
    )
