"""The log files hold no addresses, tokens or IP addresses, and age out.

Around a hundred log lines interpolate an address ("Skipping suppressed
email: anna.rossi@..."), werkzeug's request log writes each unsubscribe
link with its token (which decodes to the address) and the client's IP,
and the file rotated by size only, so nothing was ever deleted by age.
DEPLOY.md said nothing sensitive is written to the logs.

Every handler now formats through PrivacyFormatter: an address keeps its
domain, which delivery problems are diagnosed by, and loses its local part,
which is the person. Unsubscribe tokens and IPv4 addresses are masked;
erasure tokens stay readable, since an erasure is logged by its token. The
file rotates daily, one file per command (two processes rotating one file
fail on Windows), and keeps LOG_RETENTION_DAYS of history.
"""

import logging
import logging.handlers

import pytest

import main
from modules.compliance import mask_personal
from modules.database import erasure_token


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "Skipping suppressed email: anna.rossi@brand.de",
            "Skipping suppressed email: a***@brand.de",
        ),
        (
            "GET /unsubscribe/YW5uYUBicmFuZC5kZQ==.xpR9XJ HTTP/1.1",
            "GET /unsubscribe/[token] HTTP/1.1",
        ),
        ("open from 192.168.1.20 at 10:00", "open from [ip] at 10:00"),
        ("550 5.1.1 <info@brand.de> rejected", "550 5.1.1 <i***@brand.de> rejected"),
    ],
)
def test_what_identifies_a_person_is_masked(line, expected):
    assert mask_personal(line) == expected


def test_an_erasure_token_stays_readable():
    token = erasure_token("anna@brand.de")
    assert mask_personal(f"Erased {token}") == f"Erased {token}"


@pytest.fixture
def configured(tmp_path):
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    main.setup_logging(False, name="run", retention_days=7, log_dir=tmp_path)
    yield tmp_path
    for handler in root.handlers:
        handler.close()
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    logging.getLogger("werkzeug").setLevel(logging.NOTSET)


def test_the_log_file_masks_messages_and_tracebacks(configured):
    log = logging.getLogger("modules.anything")
    log.info("Sent to anna.rossi@brand.de from 10.1.2.3")
    try:
        raise ValueError("bad recipient anna.rossi@brand.de")
    except ValueError:
        log.exception("send failed")
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = (configured / "run.log").read_text(encoding="utf-8")
    assert "anna.rossi@brand.de" not in text
    assert "10.1.2.3" not in text
    # In the message, and in the traceback's source line and exception text.
    assert text.count("a***@brand.de") >= 2


def test_the_file_rotates_daily_and_keeps_only_the_retention_period(configured):
    files = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(files) == 1
    assert files[0].when == "MIDNIGHT" and files[0].backupCount == 7
    assert files[0].baseFilename.endswith("run.log")


def test_every_handler_masks(configured):
    # pytest adds its own capture handler to the root logger; ours are the
    # standard-library ones.
    ours = [
        h for h in logging.getLogger().handlers if type(h).__module__.startswith("logging")
    ]
    assert len(ours) == 2
    for handler in ours:
        assert isinstance(handler.formatter, main.PrivacyFormatter)


def test_the_request_log_is_quiet(configured):
    assert logging.getLogger("werkzeug").level == logging.WARNING


def test_each_command_logs_to_its_own_file_for_the_set_period(
    settings_factory, tmp_path, monkeypatch
):
    seen = {}
    s = settings_factory(db_path=str(tmp_path / "s.db"), LOG_RETENTION_DAYS=14)
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: seen.update(k))
    monkeypatch.setattr("sys.argv", ["main.py", "status"])

    main.main()

    assert seen["name"] == "status" and seen["retention_days"] == 14
