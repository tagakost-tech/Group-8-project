"""A sender with an apostrophe is heard: d'amico@brand.it opts out and asks.

The inbox takes the sender from the parsed From header and accepts it only if
is_address does (PRIV-07). Before PRIV-04, is_address refused an apostrophe,
so d'amico@brand.it was kept for review instead of filed: her opt-out was not
honoured and she kept getting follow-ups (GDPR Art. 21). The two fixes were
written apart; this drives the merged inbox pass with her address in each
form a From header gives it, in the inbox and in the junk folder.
"""

import pytest

from modules.sequences import SequenceEngine
from tests.test_imap_inbox_scan import _dsn_raw, _monitor, _raw
from tests.test_one_rule_decides_what_an_address_is import _stored_before_the_rule
from tests.test_the_junk_folder_is_read_too import FolderIMAP
from tests.test_the_junk_folder_is_read_too import _monitor as _junk_monitor
from tests.test_the_sender_is_read_from_any_from_header import (
    OPT_OUT,
    REQUEST,
    _review_rows,
    _suppressed,
)

APOS = "d'amico@brand.it"
ACCESS = "What personal data do you hold about me?"

SENDERS = {
    "quoted name": f'"Luca D\'Amico" <{APOS}>',
    "bare": APOS,
    "encoded name": f"=?utf-8?q?Luca_D=27Am=C3=ACco?= <{APOS}>",
}


def _emailed_and_enrolled(db):
    # Stored directly, so a rule that refuses the apostrophe fails the inbox
    # pass and not this setup.
    cid = _stored_before_the_rule(db, APOS, organization="Brand")
    db.log_email(cid, APOS, "Hello", delivery_status="sent")
    engine = SequenceEngine(db)
    seq = engine.create_sequence(
        "s", [{"delay_days": 3, "subject": "Following up", "template_name": "t"}]
    )
    engine.enroll_contact(cid, seq)
    return cid


def _active_enrollments(db, cid):
    return db.conn.execute(
        "SELECT COUNT(*) FROM sequence_enrollments "
        "WHERE contact_id = ? AND status = 'active'",
        (cid,),
    ).fetchone()[0]


@pytest.mark.parametrize("sender", SENDERS.values(), ids=SENDERS.keys())
def test_an_opt_out_from_the_inbox_is_honoured(tmp_path, sender):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", OPT_OUT)])
    cid = _emailed_and_enrolled(monitor.db)
    assert _active_enrollments(monitor.db, cid) == 1

    summary = monitor.run_check()

    assert _suppressed(monitor.db) == [APOS]
    assert monitor.db.get_contact_by_id(cid)["status"] == "unsubscribed"
    assert _active_enrollments(monitor.db, cid) == 0
    assert summary["ok"] is True
    assert summary["unsubscribes"] == 1
    assert _review_rows(monitor.db) == []


@pytest.mark.parametrize("sender", SENDERS.values(), ids=SENDERS.keys())
def test_an_opt_out_filed_as_junk_is_honoured(tmp_path, sender):
    fake = FolderIMAP(
        {"INBOX": [], "Junk Email": [_raw(sender, "Re: hello", OPT_OUT)]},
        junk_flag="Junk Email",
    )
    monitor, db = _junk_monitor(tmp_path, fake)
    cid = _emailed_and_enrolled(db)

    summary = monitor.run_check()

    assert _suppressed(db) == [APOS]
    assert db.get_contact_by_id(cid)["status"] == "unsubscribed"
    assert _active_enrollments(db, cid) == 0
    assert summary["ok"] is True
    assert summary["unsubscribes"] == 1
    assert _review_rows(db) == []


@pytest.mark.parametrize(("body", "kind"), [(REQUEST, "erasure"), (ACCESS, "access")])
@pytest.mark.parametrize("sender", SENDERS.values(), ids=SENDERS.keys())
def test_a_data_request_is_registered_under_the_address(tmp_path, sender, body, kind):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", body)])

    summary = monitor.run_check()

    assert [(r["email"], r["kind"]) for r in monitor.db.list_dsrs()] == [(APOS, kind)]
    assert _suppressed(monitor.db) == [APOS]
    assert summary["ok"] is True
    assert summary["requests"] == 1
    assert _review_rows(monitor.db) == []


def test_a_bounce_is_recorded_for_the_address(tmp_path):
    monitor, _ = _monitor(tmp_path, [_dsn_raw(APOS, "failed", "5.1.1")])
    cid = _emailed_and_enrolled(monitor.db)

    summary = monitor.run_check()

    assert _suppressed(monitor.db) == [APOS]
    assert monitor.db.get_contact_by_id(cid)["status"] == "bounced"
    assert summary["ok"] is True
    assert summary["bounces"] == 1
    assert _review_rows(monitor.db) == []


@pytest.mark.parametrize(
    "sender",
    ["Luca D'Amico <d'amico@brand>", "d'amico at brand.it"],
    ids=["no top-level domain", "no at sign"],
)
def test_a_sender_the_rule_refuses_is_still_kept_for_review(tmp_path, sender):
    monitor, _ = _monitor(tmp_path, [_raw(sender, "Re: hello", OPT_OUT)])

    summary = monitor.run_check()

    assert len(_review_rows(monitor.db)) == 1
    assert _suppressed(monitor.db) == []
    assert summary["unsubscribes"] == 0
