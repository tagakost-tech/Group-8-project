"""The tests never read SUPPLECO's real client list.

The client screen is on by default, and every live send is refused while its
list is not loaded. The default CLIENT_LIST_PATH names SUPPLECO's real client
register in the project folder, a file git does not hold. settings_factory
left that default in place, so a fresh clone failed 94 tests on the refusal,
and wherever the file was present every live-path test read SUPPLECO's
customers. The factory now gives each test an invented list in the test's own
folder; tests about the list still pass their own. So does the dashboard the
browser tests serve, whose "Mark as client" writes to its list.
"""

from pathlib import Path

from config import PROJECT_ROOT, Settings

REAL_LIST = (PROJECT_ROOT / Settings.model_fields["client_list_path"].default).resolve()


def _list_of(settings):
    # Resolved the way modules/exclusion_manager.py resolves it.
    return (PROJECT_ROOT / settings.client_list_path).resolve()


def test_the_factory_list_is_not_the_project_root_workbook(settings_factory, tmp_path):
    path = _list_of(settings_factory())
    assert path != REAL_LIST, "the tests screen against SUPPLECO's real client list"
    assert path.is_relative_to(tmp_path.resolve()), f"{path} is outside the test"


def test_an_agent_from_the_factory_reads_its_list_from_the_test_folder(
    settings_factory, tmp_path, monkeypatch
):
    from agent import OutreachAgent
    from modules import clients

    real = clients.load_from_xlsx
    folder = tmp_path.resolve()
    read = []

    def spy(path, **kw):
        path = Path(path).resolve()
        read.append(path)
        if not path.is_relative_to(folder):
            # Refused, not read: before the fix this was SUPPLECO's own file.
            raise OSError(f"a test tried to read {path}")
        return real(path, **kw)

    monkeypatch.setattr(clients, "load_from_xlsx", spy)
    agent = OutreachAgent(settings_factory(verify_emails=False, imap_enabled=False))
    assert read, "the agent loaded no client list, so every live send is refused"
    assert all(p.is_relative_to(folder) for p in read), read
    assert agent.client_register is not None


def test_the_browser_tests_dashboard_marks_clients_in_an_invented_list(tmp_path):
    """tests/ui builds its dashboard outside the factory, and it was given
    SUPPLECO's real client register. No browser test marks a client yet, so
    nothing was written, but the first one to do so would have added a
    company to SUPPLECO's own file."""
    import main
    from conftest import _browser_dashboard_settings
    from modules.clients import load_from_xlsx

    settings = _browser_dashboard_settings(tmp_path)
    path = _list_of(settings)
    # Checked before anything is marked, so a failure here writes nothing.
    assert path != REAL_LIST, "the browser tests mark clients in SUPPLECO's real list"
    assert path.is_relative_to(tmp_path.resolve()), f"{path} is outside the test"

    db = main.Database(settings.get_abs_db_path())
    cid = db.add_contact(db.log_search("q"), "anna@brand.de", organization="Brand GmbH")
    client = main.create_dashboard_app(settings).test_client()
    resp = client.post(f"/api/contacts/{cid}/action", json={"action": "mark_client"})
    assert resp.status_code == 200, resp.get_json()
    assert "Brand GmbH" in load_from_xlsx(path, key="")
