"""doctor watches the machinery the GDPR steps added, not just the settings.

Retention, backups, log masking, held opt-outs, the database's log
truncation, request deadlines, the AI agreement and the erasure key could
each fail quietly: the job never scheduled, a log line written in clear, a
request past its month. doctor now reports each one (step G17):
- retention or a backup not run in the last 2 days;
- a log file holding an address in clear;
- an opt-out held because the database was locked;
- a truncation of the database's log left unfinished;
- a data-subject request past its deadline;
- an AI seeing recipients' data with no processing agreement recorded
  (AI_DPA_REFERENCE);
- an ERASURE_KEY other than the one the database was erased with - blocking,
  as the agent refuses to start.
"""

from datetime import timedelta

import pytest

from main import run_doctor_checks
from modules import clock, optout_spool
from modules.database import Database, set_erasure_key

EMAIL = "anna.rossi@brandco.de"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "d.db"))


@pytest.fixture
def db(s):
    return Database(s.get_abs_db_path())


def _check(s, label, log_dir=None):
    checks = run_doctor_checks(s, network=False, log_dir=log_dir)
    return next(c for c in checks if c.label == label)


def test_retention_must_have_run_in_the_last_two_days(s, db):
    assert _check(s, "Retention run").ok is False  # never
    db.set_state(
        "retention:last_run_date",
        (clock.now() - timedelta(days=3)).strftime("%Y-%m-%d"),
    )
    assert _check(s, "Retention run").ok is False
    db.set_state("retention:last_run_date", clock.today_str())
    assert _check(s, "Retention run").ok is True


def test_a_backup_must_have_run_in_the_last_two_days(s, db):
    assert _check(s, "Backup").ok is False
    db.set_state("backup:last_run", (clock.now() - timedelta(days=3)).isoformat())
    assert _check(s, "Backup").ok is False
    db.set_state("backup:last_run", clock.now_iso())
    assert _check(s, "Backup").ok is True


def test_a_log_file_with_an_address_in_clear_is_reported(s, db, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.log").write_text(
        "Sent to a***@brandco.de; erased_1a2b3c4d5e6f7a8b@redacted.invalid\n",
        encoding="utf-8",
    )
    assert _check(s, "Log files masked", logs).ok is True
    (logs / "run.log.2026-10-01").write_text(f"Sent to {EMAIL}\n", encoding="utf-8")
    check = _check(s, "Log files masked", logs)
    assert check.ok is False and EMAIL not in check.detail


def test_a_held_opt_out_is_reported(s, db):
    assert _check(s, "Held opt-outs").ok is True
    optout_spool.spool_optout(db.db_path, EMAIL, "link")
    check = _check(s, "Held opt-outs")
    assert check.ok is False and EMAIL not in check.detail


def test_a_held_click_no_link_matches_is_kept_quietly(s, db):
    """Kept up to a year in case its recipient comes up again (a restore can
    lose the record of the link); it is not a failure to fix."""
    optout_spool.spool_optout(db.db_path, None, "link", token="u" + "1" * 32)
    optout_spool.replay_optout_spool(db, resolve=lambda token: None)
    check = _check(s, "Held opt-outs")
    assert check.ok is True and "match no link" in check.detail


def test_an_unfinished_log_truncation_is_reported(s, db):
    assert _check(s, "Database log truncated").ok is True
    db.set_state("wal_truncate_pending", "1")
    assert _check(s, "Database log truncated").ok is False


def test_an_overdue_request_is_reported(s, db):
    assert _check(s, "Data-subject requests").ok is True
    rid = db.add_dsr(EMAIL, "access", "reply")
    db.conn.execute(
        "UPDATE dsr_requests SET due_at = ? WHERE id = ?",
        ((clock.now() - timedelta(days=1)).isoformat(), rid),
    )
    db.conn.commit()
    check = _check(s, "Data-subject requests")
    assert check.ok is False and f"#{rid}" in check.detail and EMAIL not in check.detail


def test_an_ai_without_a_processing_agreement_is_reported(settings_factory, tmp_path):
    def ai(**kw):
        s = settings_factory(db_path=str(tmp_path / "ai.db"), **kw)
        return _check(s, "AI processing agreement").ok

    assert ai() is None  # no AI sees anyone's data: nothing to report
    assert ai(ai_humanize_emails=True) is False
    assert ai(ai_reply_classification_enabled=True) is False
    assert (
        ai(
            ai_reply_classification_enabled=True,
            AI_DPA_REFERENCE="Own server, no outside processor, 2026-11-16",
        )
        is True
    )


def test_the_company_country_ai_counts_as_an_ai_seeing_data(settings_factory, tmp_path):
    """COMPANY_COUNTRY_AI is on by default and sends a company's name,
    domain and page description, yet doctor said no AI saw anyone's data."""

    def ai(**kw):
        s = settings_factory(
            db_path=str(tmp_path / "cc.db"),
            llm_provider="openai_compatible", LLM_BASE_URL="http://127.0.0.1:11434/v1", LLM_MODEL="test-model",
            **kw,
        )
        return _check(s, "AI processing agreement")

    assert ai().ok is False
    assert "no AI sees" not in ai().detail
    assert ai(AI_DPA_REFERENCE="Own server, no outside processor, 2026-11-16").ok is True
    assert ai(company_country_ai=False).ok is None
    assert ai(ITALIAN_COUNTRIES="").ok is None


def test_another_erasure_key_is_blocking(settings_factory, tmp_path):
    path = str(tmp_path / "k.db")
    Database(path)  # stamped with the test key's fingerprint
    s = settings_factory(db_path=path, ERASURE_KEY="a-different-key-" + "1" * 32)
    check = _check(s, "Erasure key matches the database")
    assert check.ok is False and check.blocking
    set_erasure_key(s.erasure_key)  # restored by the autouse fixture


def test_another_erasure_key_is_caught_as_main_runs_doctor(settings_factory, tmp_path):
    """main() gives doctor the configured key too, so the database itself
    refuses to open. The check passed then, saying no key was recorded -
    found by the review of the counsel sheet. It reads the fingerprint
    without opening the database through the key check."""
    path = str(tmp_path / "k2.db")
    Database(path)  # stamped with the test key's fingerprint
    s = settings_factory(db_path=path, ERASURE_KEY="a-different-key-" + "1" * 32)
    set_erasure_key(s.erasure_key)  # as main() does for doctor
    check = _check(s, "Erasure key matches the database")
    assert check.ok is False and check.blocking


def test_the_same_erasure_key_passes(s, db):
    assert _check(s, "Erasure key matches the database").ok is True
