"""A client-list path that points at nothing usable must not stop the agent.

.env.example ships CLIENT_LIST_PATH= blank. PROJECT_ROOT / "" is the project
folder itself, which exists, so the loader handed a DIRECTORY to openpyxl -
and openpyxl's InvalidFileException subclasses plain Exception, which the
loader's (ImportError, OSError, ValueError) did not catch. A fresh install
copied from the template therefore crashed at startup, dashboard included.

The competitor loader right above it already had both protections - a blank
path means "off", and any failure is contained - because a screen must never
be what stops a run. This is the twin that did not get them.
"""

import logging


def _agent(settings_factory, tmp_path, **kw):
    from agent import OutreachAgent

    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        verify_emails=False,
        imap_enabled=False,
        client_filter_enabled=True,
        **kw,
    )
    return OutreachAgent(s)


def test_a_blank_path_does_not_crash_startup(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path, client_list_path="")
    assert a.client_register is None


def test_a_directory_path_does_not_crash(settings_factory, tmp_path):
    """Same failure by another route: a path that names a folder."""
    folder = tmp_path / "clients"
    folder.mkdir()
    a = _agent(settings_factory, tmp_path, client_list_path=str(folder))
    assert a.client_register is None


def test_a_corrupt_workbook_does_not_crash(settings_factory, tmp_path):
    """A real file openpyxl cannot read - a half-synced OneDrive copy, a CSV
    renamed to .xlsx. It raises exceptions outside OSError/ValueError."""
    bad = tmp_path / "clients.xlsx"
    bad.write_bytes(b"this is not a zip archive")
    a = _agent(settings_factory, tmp_path, client_list_path=str(bad))
    assert a.client_register is None


def test_the_blank_path_is_reported(settings_factory, tmp_path, caplog):
    """The filter is ON and screening nobody. Silence here reads exactly like
    a working client list, and the cost is a cold pitch to a customer."""
    with caplog.at_level(logging.ERROR):
        _agent(settings_factory, tmp_path, client_list_path="")
    assert any("CLIENT_LIST_PATH" in r.getMessage() for r in caplog.records)
