"""After downtime the continuous loop reads the inbox before it sends.

Each pass drained the backlog and sent due follow-ups before it read the
inbox, so the first sends after a restart were judged without the opt-outs,
bounces and data requests that arrived while the agent was off. The read
also has to stay ungated by a pause, which is when bounces and opt-outs
most need processing, and has to come before the pause is reconsidered,
so the bounces it finds count.
"""

from agent import OutreachAgent


def _stubbed_agent(settings_factory, tmp_path):
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
    order = []
    a.search.search_and_extract = lambda *_a, **_k: []
    a._process_pending_contacts = lambda *_a, **_k: order.append("backlog_send")
    a._process_sequences = lambda *_a, **_k: order.append("followup_send")
    a._maybe_check_imap = lambda *_a, **_k: order.append("inbox_read")
    a._process_retries = lambda: order.append("retries")
    a._maybe_apply_retention = lambda *_a, **_k: order.append("retention")
    a._interruptible_sleep = lambda *_a, **_k: a.stop()
    return a, order


def test_the_first_pass_reads_the_inbox_before_any_send(settings_factory, tmp_path):
    a, order = _stubbed_agent(settings_factory, tmp_path)

    a.run_continuous(dry_run=False, loop_delay=60)

    assert order[0] == "inbox_read"
    assert order.index("inbox_read") < order.index("backlog_send")
    assert order.index("inbox_read") < order.index("followup_send")


def test_a_paused_agent_still_reads_its_inbox(settings_factory, tmp_path):
    a, order = _stubbed_agent(settings_factory, tmp_path)
    a._sending_paused, a._pause_reason = True, "bounce rate"
    a._safe_to_resume = lambda: False

    a.run_continuous(dry_run=False, loop_delay=60)

    assert "inbox_read" in order
    assert "backlog_send" not in order
    assert "followup_send" not in order


def test_the_inbox_is_read_before_a_pause_is_reconsidered(settings_factory, tmp_path):
    a, order = _stubbed_agent(settings_factory, tmp_path)
    a._sending_paused, a._pause_reason = True, "bounce rate"

    def resume_check():
        order.append("resume_check")
        return False

    a._safe_to_resume = resume_check

    a.run_continuous(dry_run=False, loop_delay=60)

    assert order.index("inbox_read") < order.index("resume_check")
