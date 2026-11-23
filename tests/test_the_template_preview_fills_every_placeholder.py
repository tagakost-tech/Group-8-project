"""The Template Editor's preview shows the email as it will read, not raw
placeholders and invented names.

At a walk-through the English first email previewed as "My name is Jane
Smith and I am responsible for {{sender_title}} at Suppleco ... Acme Corp", and
Italian follow-up 1 opened on "{{greeting}}": the page filled a handful of
placeholders with hard-coded samples and left the rest. GET /api/template now
gives, beside the text, what the send path would put in each placeholder:
the installation's own sender name, title, company and signature, a sample
recipient, and short sample blocks for the legal footer and the body. One
list: the preview fills every placeholder the send path fills.
"""

import re
import shutil
from pathlib import Path

import pytest
from test_first_email_language import TO, _agent, _stored

import main
from modules.dispatch_pipeline import preview_placeholders

ROOT = Path(main.__file__).resolve().parent

# Every {{word}} written anywhere in the product's code and texts: whatever
# the send path fills is among them.
CANDIDATES = sorted(
    {
        key
        for path in [
            *ROOT.glob("*.py"),
            *(ROOT / "modules").glob("*.py"),
            *(ROOT / "templates").glob("*.html"),
        ]
        for key in re.findall(r"\{\{(\w+)\}\}", path.read_text(encoding="utf-8"))
    }
)


def _filled_by_the_send_path(settings_factory, tmp_path, language):
    """The placeholders a dry-run send replaces, given a text that holds
    every candidate between markers."""
    a, sent = _agent(settings_factory, tmp_path, sender_title="Sales Manager")
    template = "".join(f"<p>[{k}]{{{{{k}}}}}[/{k}]</p>" for k in CANDIDATES)
    contact = _stored(a, TO[language])
    assert a._process_contact(
        contact, dry_run=True, template_override=template, language=language
    )
    body = sent[-1]["html_body"]
    return {k for k in CANDIDATES if "{{" + k + "}}" not in body}


@pytest.mark.parametrize("language", ["en", "it"])
def test_the_preview_fills_every_placeholder_the_send_path_fills(
    settings_factory, tmp_path, language
):
    filled = _filled_by_the_send_path(settings_factory, tmp_path, language)
    assert {"sender_title", "greeting", "legal_footer"} <= filled
    s = settings_factory(db_path=str(tmp_path / "p.db"))
    missing = filled - set(preview_placeholders(s, language))
    assert not missing, f"the preview leaves these raw: {sorted(missing)}"


@pytest.fixture
def client(tmp_path, settings_factory, monkeypatch):
    shutil.copytree(ROOT / "templates", tmp_path / "templates")
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)

    def make(**kw):
        s = settings_factory(db_path=str(tmp_path / "t.db"), **kw)
        return main.create_dashboard_app(s).test_client()

    return make


def test_the_preview_uses_the_installation_s_own_sender(client):
    c = client(
        sender_name="Giulia <Bianchi> & Co",
        sender_title="Business Development",
        company_name="SUPPLECO",
        sender_signature="Giulia Bianchi\\nSUPPLECO S.r.l.",
    )
    preview = c.get("/api/template?name=email_template").get_json()["preview"]
    assert preview["sender_name"] == "Giulia &lt;Bianchi&gt; &amp; Co"
    assert preview["sender_title"] == "Business Development"
    assert preview["company_name"] == "SUPPLECO"
    assert preview["sender_signature"] == "Giulia Bianchi<br>SUPPLECO S.r.l."
    for value in preview.values():
        assert "{{" not in value
        assert "Jane Smith" not in value and "Acme" not in value


def test_a_blank_title_shows_a_marker_not_a_placeholder(client):
    c = client(sender_name="Giulia Bianchi", sender_title="", company_name="SUPPLECO")
    preview = c.get("/api/template?name=email_template").get_json()["preview"]
    assert preview["sender_title"] == "[sender title not set]"
    assert preview["sender_signature"] == "Giulia Bianchi<br>SUPPLECO"


def test_an_italian_text_greets_as_the_italian_send_does(client):
    c = client()
    italian = c.get("/api/template?name=followup_1_it").get_json()["preview"]
    english = c.get("/api/template?name=email_template").get_json()["preview"]
    assert italian["greeting"] == "Buongiorno,"
    assert english["greeting"].startswith("Hi ")
    assert "sample" in english["organization"].lower()
