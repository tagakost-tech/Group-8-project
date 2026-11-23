"""A reply marks the contact 'responded' and says so - but never undoes a
client, an opt-out or a block.

process_replies set 'responded' on any status but 'responded'. A contact the
operator had just marked as a client, one who had opted out, and one the
operator had blocked all became 'responded' - and because each scan re-reads
the last day of mail, marking a client was reverted on the next scan. That
contradicted the rule the bounce handler states beside it: a client status is
the more important fact and is left alone. A reply also cleared nothing and
logged nothing per contact, so the contact kept a stale reason from before
the reply ("Score 12 below threshold") and only an aggregate IMAP count said
anything had happened.
"""

import pytest

from modules.database import Database
from modules.imap_monitor import ImapMonitor


@pytest.fixture
def monitor(tmp_path):
    m = ImapMonitor(
        Database(str(tmp_path / "i.db")), "imap.test", 993, "me@corp.com", "pw"
    )
    m.cid = m.db.add_contact(m.db.log_search("q"), "buyer@brand.de")
    return m


def _reply(m):
    m.process_replies([{"email": "Buyer@Brand.de"}])


def _logged(m, action):
    return [r for r in m.db.get_recent_activity(limit=50) if r["action"] == action]


def test_a_reply_is_recorded_once_with_its_reason(monitor):
    m = monitor
    m.db.update_contact_status(m.cid, "emailed", discard_reason="stale reason")
    _reply(m)
    _reply(m)  # the next scan re-reads the same mail
    row = m.db.get_contact_by_id(m.cid)
    assert row["status"] == "responded"
    assert row["discard_reason"] == "Replied by email"
    rows = _logged(m, "REPLY_RECEIVED")
    assert len(rows) == 1 and "buyer@brand.de" in rows[0]["details"]


@pytest.mark.parametrize("status", ["active_client", "unsubscribed", "blocked"])
def test_a_reply_does_not_undo_a_closure(monitor, status):
    m = monitor
    m.db.update_contact_status(m.cid, status)
    _reply(m)
    assert m.db.get_contact_by_id(m.cid)["status"] == status
