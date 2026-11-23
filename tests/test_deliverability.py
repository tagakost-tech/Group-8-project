"""Tests for the DeliverabilityLinter advisory checks."""
from modules.deliverability import DeliverabilityLinter


def test_clean_template_has_no_issues():
    html = ("<p>Hello, I'd like to introduce our manufacturing services for "
            "your product line. <a href='#'>unsubscribe</a></p>")
    assert DeliverabilityLinter.lint(html) == []


def test_spam_words_flagged():
    html = "<p>ACT NOW! FREE cash guarantee, click here to buy now! unsubscribe</p>"
    issues = DeliverabilityLinter.lint(html)
    assert any("Spam-trigger" in i for i in issues)


def test_missing_unsubscribe_flagged():
    html = "<p>Hello there, a normal message with enough words to avoid image warnings.</p>"
    issues = DeliverabilityLinter.lint(html)
    assert any("unsubscribe" in i.lower() for i in issues)


def test_too_many_links_flagged():
    links = " ".join(f"<a href='#{i}'>x</a>" for i in range(8))
    html = f"<p>text unsubscribe {links}</p>"
    issues = DeliverabilityLinter.lint(html)
    assert any("links" in i.lower() for i in issues)
