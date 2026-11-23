"""Every call between stages goes through the agent, so a patch on it applies.

The stage extraction's stated protocol is that a stage calls its siblings as
`self.agent._method(...)`, so a test (or a subclass) that replaces a method on
the agent changes what the agent does. Six calls skipped it and called the
stage's own copy: `_list_stamp` and `_load_register` in the exclusion manager,
`_body_to_html` in the dispatch pipeline. Replacing those on the agent changed
nothing - and a test written against one would have passed for the wrong
reason.
"""

from openpyxl import Workbook

from agent import OutreachAgent


def _agent(settings_factory, tmp_path, **kw):
    xlsx = tmp_path / "clients.xlsx"
    wb = Workbook()
    wb.active.append(["Some Company S.r.l."])
    wb.save(xlsx)
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            client_list_path=str(xlsx),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            **kw,
        )
    )


def test_loading_a_list_goes_through_the_agents_loader(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    calls = []
    a._load_register = lambda kind, raw: calls.append(kind) or False
    a.load_client_register()
    assert calls == ["client"]


def test_the_list_refresh_stamps_through_the_agent(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    stamped = []
    a._list_stamp = lambda path: stamped.append(path) or None
    a.refresh_lists_if_changed()
    assert stamped, "the refresh stamped the file without asking the agent"


def test_the_email_body_is_rendered_through_the_agent(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    a._body_to_html = lambda text: "<p>FROM-THE-AGENT</p>"
    body = a._build_email_body({"email": "x@brand.de", "organization": "Brand"})
    assert "FROM-THE-AGENT" in body
