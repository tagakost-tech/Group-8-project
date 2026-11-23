"""The agent and the dashboard refuse to start on an erasure key doctor fails.

doctor's "Erasure key" and "Erasure key matches the database" are blocking
checks, and doctor says live sending is refused while one fails. But `run` and
`dashboard` never ran them. A database that already kept a key shorter than 32
characters started the agent and sent, though anyone holding a copy of the
database could guess that key and test addresses against every erasure. A
database erased under another key stopped on a Python traceback.

Now both commands run those two checks before anything opens the database,
and stop with the check's own words. The other blocking security rules already
refuse at every start, for every command: UNSUBSCRIBE_SECRET, the AI address
and Microsoft sign-in when the settings load, a blank ERASURE_KEY when it is
set, the dashboard password in cmd_dashboard.
"""

from types import SimpleNamespace

import pytest
import waitress

import main
from modules import database
from modules.database import Database, erasure_key_fingerprint, set_erasure_key

SHORT = "k" * 31
OTHER = "another-long-erasure-key-" + "y" * 32


@pytest.fixture
def started(monkeypatch):
    """What got past the start: the agent built, the dashboard's app built."""
    built = []

    class Agent:
        def __init__(self, settings):
            built.append("agent")

        def run_single(self, **kwargs):
            built.append("ran")

    def app(settings):
        built.append("dashboard")
        return object()

    monkeypatch.setattr(main, "OutreachAgent", Agent)
    monkeypatch.setattr(main, "create_dashboard_app", app)
    monkeypatch.setattr(waitress, "serve", lambda app, **k: built.append("served"))
    return built


RUN = SimpleNamespace(dry_run=False, query=None, continuous=False, loop_delay=None)


def _start(command, settings):
    if command == "run":
        main.cmd_run(RUN, settings)
    else:
        main.cmd_dashboard(SimpleNamespace(), settings)


def _database_keeping(path, key):
    """A database made before the 32-character minimum, keeping a short key."""
    set_erasure_key("a-long-key-to-create-it-" + "x" * 32)
    Database(str(path)).set_state(
        database.ERASURE_KEY_FINGERPRINT, erasure_key_fingerprint(key)
    )


@pytest.mark.parametrize("command", ["run", "dashboard"])
def test_a_short_key_the_database_keeps_stops_the_start(
    command, settings_factory, tmp_path, capsys, started
):
    path = tmp_path / "old.db"
    _database_keeping(path, SHORT)
    set_erasure_key(SHORT)
    s = settings_factory(db_path=str(path), ERASURE_KEY=SHORT)

    with pytest.raises(SystemExit) as stopped:
        _start(command, s)

    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "Refusing to start - Erasure key:" in out
    assert "new database" in out
    assert SHORT not in out
    assert started == []


@pytest.mark.parametrize("command", ["run", "dashboard"])
def test_a_key_the_database_was_not_erased_with_stops_the_start_in_words(
    command, settings_factory, tmp_path, capsys, started
):
    path = tmp_path / "agent.db"
    Database(str(path))  # erased under the test key
    set_erasure_key(OTHER)
    s = settings_factory(db_path=str(path), ERASURE_KEY=OTHER)

    with pytest.raises(SystemExit) as stopped:
        _start(command, s)

    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "Refusing to start - Erasure key matches the database:" in out
    assert "restore the original key" in out
    assert OTHER not in out
    assert started == []


@pytest.mark.parametrize(
    "command, gets_to",
    [("run", ["agent", "ran"]), ("dashboard", ["dashboard", "served"])],
)
def test_a_sound_key_starts(command, gets_to, settings_factory, tmp_path, started):
    s = settings_factory(db_path=str(tmp_path / "agent.db"))
    Database(s.get_abs_db_path())

    _start(command, s)

    assert started == gets_to


def test_the_dashboard_password_is_still_checked_first(
    settings_factory, tmp_path, capsys, started
):
    s = settings_factory(db_path=str(tmp_path / "agent.db"), DASHBOARD_PASSWORD="")
    with pytest.raises(SystemExit):
        _start("dashboard", s)
    assert "DASHBOARD_PASSWORD is blank" in capsys.readouterr().out
    assert started == []
