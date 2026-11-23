"""Tests for EmailSender.render_template personalization and greeting fixes."""
from modules.email_sender import EmailSender

TEMPLATE = ("<p>Hi {{name}},</p>"
            "<p>{{organization}} | {{company_name}} | {{sender_name}} | {{role}}</p>")


def test_placeholders_replaced():
    html, _ = EmailSender.render_template(
        TEMPLATE,
        {"name": "John", "organization": "Acme", "role": "CEO"},
        "SUPPLECO", "Jane",
    )
    for token in ("John", "Acme", "SUPPLECO", "Jane", "CEO"):
        assert token in html
    assert "{{" not in html  # every placeholder resolved


def test_unknown_name_greeting_fixed():
    html, _ = EmailSender.render_template(
        "<p>Hi {{name}},</p>", {"name": None}, "SUPPLECO", "Jane")
    assert "Hello," in html
    assert "Hi ," not in html


def test_placeholder_name_treated_as_unknown():
    html, _ = EmailSender.render_template(
        "<p>Hi {{name}},</p>", {"name": "Sir/Madam"}, "C", "S")
    assert "Hello," in html


def test_plain_text_strips_html():
    _, plain = EmailSender.render_template(
        "<p>Hello <b>world</b></p>", {"name": "X"}, "C", "S")
    assert "Hello" in plain and "world" in plain
    assert "<" not in plain
