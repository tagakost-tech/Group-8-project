"""A cancelled follow-up sequence records why it was cancelled.

Every path that ends a contact's follow-ups knew why - the contact replied,
was found to be a client, opted out, was erased - and wrote the reason only
to the log file. sequence_enrollments had nowhere to keep it, so the
dashboard (and anyone answering "why did the follow-ups stop?") saw a
'cancelled' row and nothing else. A cancel_reason column now holds it.
"""

import pytest

from agent import OutreachAgent
from modules.export import DataExporter
from modules.imap_monitor import ImapMonitor


@pytest.fixture
def agent(settings_factory, tmp_path):
    a = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            sequences_enabled=True,
        )
    )
    a._running = True
    a._interruptible_sleep = lambda *_a, **_k: None
    sid = a.db.log_search("q")
    a.cid = a.db.add_contact(sid, "buyer@brand.de", organization="Brand")
    a.db.update_contact_status(a.cid, "emailed")
    a.sequences.enroll_contact(a.cid, a.sequence_id)
    return a


def _reason(a):
    row = a.db.conn.execute(
        "SELECT status, cancel_reason FROM sequence_enrollments WHERE contact_id = ?",
        (a.cid,),
    ).fetchone()
    assert row["status"] == "cancelled"
    return row["cancel_reason"]


def test_a_reply_that_closes_the_contact_is_the_reason(agent):
    agent.db.update_contact_status(agent.cid, "responded")
    agent.db.conn.execute(
        "UPDATE sequence_enrollments SET enrolled_at = '2000-01-01 00:00:00'"
    )
    agent.db.conn.commit()
    agent._process_sequences(dry_run=False)
    assert "responded" in (_reason(agent) or "")


def test_an_opt_out_by_email_is_the_reason(agent):
    ImapMonitor(agent.db, "imap.test", 993, "me@corp.com", "pw").process_unsubscribes(
        [{"email": "buyer@brand.de"}]
    )
    assert "opted out" in (_reason(agent) or "").lower()


def test_an_erasure_is_the_reason(agent):
    DataExporter(agent.db).gdpr_erase("buyer@brand.de")
    assert "erased" in (_reason(agent) or "").lower()
