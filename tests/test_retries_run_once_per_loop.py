"""The continuous loop processes the retry queue once per pass.

When a pass ran discovery, it called run_single, whose retry phase ran
whether or not the continuous loop had called it - unlike the backlog,
follow-up and retention phases beside it, which leave that to the loop.
The loop then ran its own retry step, so every such pass claimed the queue
twice and logged RETRY_PROCESSING twice.
"""

from agent import OutreachAgent


def test_a_pass_with_discovery_processes_retries_once(settings_factory, tmp_path):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    a.db.log_search("brand manufacturers", status="pending")
    runs = []
    a.search.search_and_extract = lambda *_a, **_k: []
    a._process_pending_contacts = lambda *_a, **_k: None
    a._process_sequences = lambda *_a, **_k: None
    a._maybe_check_imap = lambda *_a, **_k: None
    a._maybe_apply_retention = lambda *_a, **_k: None
    a._process_retries = lambda: runs.append("retries")
    a._interruptible_sleep = lambda *_a, **_k: a.stop()

    a.run_continuous(dry_run=False, loop_delay=60)

    assert runs == ["retries"]


def test_a_one_off_run_still_processes_retries(settings_factory, tmp_path):
    """The control: `python main.py run` has no loop to do it."""
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
        )
    )
    runs = []
    a.search.search_and_extract = lambda *_a, **_k: []
    a._process_pending_contacts = lambda *_a, **_k: None
    a._process_sequences = lambda *_a, **_k: None
    a._maybe_apply_retention = lambda *_a, **_k: None
    a._process_retries = lambda: runs.append("retries")

    a.run_single(["brand manufacturers"], dry_run=False)

    assert runs == ["retries"]
