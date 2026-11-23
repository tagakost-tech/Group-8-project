"""The plain-text part of every email keeps its link targets.

The text alternative was built with BeautifulSoup's get_text(), which keeps a
link's words and drops its href. A text-only client - or a reader who prefers
plain text - saw "unsubscribe here" with nothing to follow. The opt-out the
law requires in the email existed only in the HTML part.
"""

from modules.email_sender import EmailSender


def _plain(html):
    return EmailSender.render_template(html, {"name": "Anna"}, "Co", "Sender")[1]


def test_the_unsubscribe_url_survives_into_plain_text():
    plain = _plain(
        '<p>You can <a href="https://x.test/unsubscribe/abc">unsubscribe here</a>.</p>'
    )
    assert "https://x.test/unsubscribe/abc" in plain


def test_a_mailto_optout_survives_too():
    """The default route while BASE_URL is local."""
    plain = _plain('<a href="mailto:out@co.it?subject=Unsubscribe">unsubscribe</a>')
    assert "mailto:out@co.it?subject=Unsubscribe" in plain


def test_a_link_whose_text_is_its_url_is_not_doubled():
    plain = _plain('<a href="https://x.test/p">https://x.test/p</a>')
    assert plain.count("https://x.test/p") == 1


def test_an_anchor_link_is_left_alone():
    assert "#" not in _plain('<a href="#">top</a>')


def test_the_real_email_carries_it(settings_factory, tmp_path, monkeypatch):
    """Through the real compose path, with the real template."""
    from agent import OutreachAgent

    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            smtp_username="outreach@co.it",
            # A local BASE_URL is what makes the opt-out a mailto link.
            base_url="http://127.0.0.1:5000",
        )
    )
    captured = []
    monkeypatch.setattr(
        a.email_sender, "dispatch_to_contact", lambda **k: captured.append(k) or 1
    )
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="info@brand.de", organization="Brand")
    a._process_contact({"id": cid, "email": "info@brand.de"}, dry_run=True)
    assert "mailto:outreach@co.it?subject=Unsubscribe" in captured[0]["plain_body"]
