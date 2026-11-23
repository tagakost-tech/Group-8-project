"""A Stop request leaves a row in the activity log, and a busy database
cannot swallow the Stop itself.

The stage extraction dropped the `AGENT_STOP_REQUESTED` write from `stop()`,
so a dashboard Stop or a Ctrl+C left no trace: the log showed a campaign that
simply went quiet. The write is back, after the flag is cleared and inside its
own error handling - the SIGINT handler calls `stop()` on the main thread,
which may itself be holding the write lock at that moment.
"""

import sqlite3

from agent import OutreachAgent


def _agent(settings_factory, tmp_path):
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )


def test_a_stop_is_written_to_the_activity_log(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    a._running = True
    a.stop()
    actions = [r["action"] for r in a.db.get_recent_activity(limit=20)]
    assert "AGENT_STOP_REQUESTED" in actions


def test_a_locked_database_does_not_break_the_stop(settings_factory, tmp_path):
    a = _agent(settings_factory, tmp_path)
    a._running = True

    def _locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    a.db.log_activity = _locked
    a.stop()
    assert a._running is False
