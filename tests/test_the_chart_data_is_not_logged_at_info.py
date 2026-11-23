"""Building the chart data writes nothing to the log at INFO.

The dashboard asks for the charts on its poll, and each answer wrote
"Generated chart data for 30 days" at INFO: thousands of lines a day that say
nothing, around the ones that do. It is a DEBUG line now. A failure to build
them is still an ERROR.
"""

import logging
import sqlite3

from modules.analytics import Analytics
from modules.database import Database


def test_the_charts_log_nothing_at_info(tmp_path, caplog):
    analytics = Analytics(Database(str(tmp_path / "a.db")))
    caplog.set_level(logging.INFO, logger="modules.analytics")

    analytics.get_chart_data()

    assert [r.getMessage() for r in caplog.records] == []


def test_the_line_is_still_there_at_debug(tmp_path, caplog):
    analytics = Analytics(Database(str(tmp_path / "a.db")))
    caplog.set_level(logging.DEBUG, logger="modules.analytics")

    analytics.get_chart_data(days=7)

    assert "Generated chart data for 7 days" in caplog.text


def test_a_failure_to_build_them_is_still_an_error(tmp_path, caplog, monkeypatch):
    db = Database(str(tmp_path / "a.db"))
    analytics = Analytics(db)

    def broken():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "_transaction", broken)
    caplog.set_level(logging.INFO, logger="modules.analytics")

    analytics.get_chart_data()

    assert [r.levelname for r in caplog.records] == ["ERROR"]
