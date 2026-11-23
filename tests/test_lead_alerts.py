"""Tests for hot-lead alerts: reply-intent classification and notifying a
designated person when a prospect replies with interest."""

from modules import reply_intent


def _agent(settings_factory, tmp_path, **overrides):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        delay_between_emails=0.0,
        imap_enabled=False,
        **overrides,
    )
    return OutreachAgent(s)


# ── intent classification ───────────────────────────────────────────────────


def test_positive_replies_detected():
    for text in [
        "Yes, we are interested - could you send pricing?",
        "Sounds good, let's schedule a call next week.",
        "Please send me more information and a sample.",
        "Can you provide a quote for 10,000 units?",
        "Siamo interessati, potete inviare un preventivo?",  # Italian
        "Wir sind interessiert, bitte ein Angebot.",  # German
    ]:
        assert (
            reply_intent.classify("Re: Quick question", text) == reply_intent.INTERESTED
        ), text


def test_negative_replies_detected():
    for text in [
        "Not interested, thanks.",
        "No thanks, we already work with another manufacturer.",
        "Please remove me from your list.",
        "Non siamo interessati, grazie.",
    ]:
        assert (
            reply_intent.classify("Re: Quick question", text)
            == reply_intent.NOT_INTERESTED
        ), text


def test_neutral_reply_is_unclear():
    assert (
        reply_intent.classify("Re: hello", "Received, thank you.")
        == reply_intent.UNCLEAR
    )


def test_our_own_quoted_email_does_not_trigger_interest():
    # The prospect writes a bare "no thanks" but our original (which contains
    # 'interested'/'call') is quoted underneath - must NOT read as interested.
    body = (
        "No thanks.\n\n"
        "On Mon, 3 Aug 2026, SUPPLECO wrote:\n"
        "> Would you be open to a short call? We'd be interested in discussing "
        "> pricing and samples.\n"
    )
    assert (
        reply_intent.classify("Re: Quick question", body) == reply_intent.NOT_INTERESTED
    )


def test_strip_quoted_keeps_only_new_text():
    body = "Yes please send a quote.\n\n> our original email text here"
    assert "original email" not in reply_intent.strip_quoted(body)


# ── config plug-in point ────────────────────────────────────────────────────


def test_alert_disabled_when_blank(settings_factory):
    assert settings_factory(lead_alert_email="").get_lead_alert_recipients() == []


def test_alert_recipients_parsed(settings_factory):
    s = settings_factory(lead_alert_email="sales@suppleco.com, boss@suppleco.com")
    assert s.get_lead_alert_recipients() == ["sales@suppleco.com", "boss@suppleco.com"]


# ── end-to-end alerting ─────────────────────────────────────────────────────


def _sent_capture(agent, monkeypatch):
    sent = []
    monkeypatch.setattr(
        agent.email_sender, "send_email", lambda msg: sent.append(msg) or (True, "ok")
    )
    return sent


def test_interested_reply_notifies_the_designated_person(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, lead_alert_email="sales@suppleco.com")
    sid = a.db.log_search("q")
    a.db.add_contact(
        sid, "buyer@nordic.se", name="Maria", organization="Nordic Wellness"
    )
    sent = _sent_capture(a, monkeypatch)

    a._handle_interested_replies(
        [
            {
                "email": "buyer@nordic.se",
                "from": "Maria <buyer@nordic.se>",
                "subject": "Re: Quick question about Nordic Wellness",
                "date": "2026-08-03",
                "body": "Yes, we're interested - please send a quote.",
            }
        ]
    )

    assert len(sent) == 1
    msg = sent[0]
    assert msg["To"] == "sales@suppleco.com"
    assert "Nordic Wellness" in msg["Subject"]
    assert a.db.was_lead_alerted("buyer@nordic.se")


def test_no_alert_for_uninterested_reply(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, lead_alert_email="sales@suppleco.com")
    sent = _sent_capture(a, monkeypatch)
    a._handle_interested_replies(
        [
            {
                "email": "x@brand.de",
                "subject": "Re: hi",
                "body": "Not interested, thanks.",
            }
        ]
    )
    assert sent == []
    assert not a.db.was_lead_alerted("x@brand.de")


def test_lead_is_only_alerted_once(settings_factory, tmp_path, monkeypatch):
    a = _agent(settings_factory, tmp_path, lead_alert_email="sales@suppleco.com")
    a.db.add_contact(a.db.log_search("q"), "buyer@nordic.se", organization="Nordic")
    sent = _sent_capture(a, monkeypatch)
    entry = {
        "email": "buyer@nordic.se",
        "subject": "Re: hi",
        "body": "Yes, we are interested in a call.",
    }
    a._handle_interested_replies([entry])
    a._handle_interested_replies([entry])  # same lead again
    assert len(sent) == 1


def test_no_alert_for_someone_we_never_emailed(settings_factory, tmp_path, monkeypatch):
    """Every inbound message that isn't a bounce/unsubscribe/auto-reply reaches
    here - newsletters, suppliers, colleagues. A vendor mailshot saying "price
    list" must not page a human (and be deduped forever)."""
    a = _agent(settings_factory, tmp_path, lead_alert_email="sales@suppleco.com")
    sent = _sent_capture(a, monkeypatch)
    a._handle_interested_replies(
        [
            {
                "email": "newsletter@supplier.com",
                "subject": "Our new price list",
                "body": "Please find our latest price list and quote enclosed.",
            }
        ]
    )
    assert sent == []
    assert not a.db.was_lead_alerted("newsletter@supplier.com")


def test_nothing_sent_when_feature_not_configured(
    settings_factory, tmp_path, monkeypatch
):
    a = _agent(settings_factory, tmp_path, lead_alert_email="")
    sent = _sent_capture(a, monkeypatch)
    a._handle_interested_replies(
        [
            {
                "email": "buyer@nordic.se",
                "subject": "Re: hi",
                "body": "Yes, very interested, send a quote!",
            }
        ]
    )
    assert sent == []
