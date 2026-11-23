"""A restore keeps everything that stops or limits sending, not only opt-outs.

The journal held opt-outs, company blocks and lifts, erasures and data
requests. An operator's dashboard Block and Unblock, a bounce, a refusal by
the recipient's server and a bounce over a block were written to the
database alone. Restored from a backup older than them, the agent emailed a
blocked address or a dead one again, and the restore drill could not tell
which was lost: it counted them all as "opt-outs or other suppressions".

Now (stage C2):
- each of them writes a journal line when it changes the database, an
  erased person only as the token;
- replay_journal applies them again in the order they were written: of a
  block and its unblock or lift, whichever came last holds, and a replay
  never lifts anything but an operator's own block;
- a bounce comes back with the time it was recorded, which decides when
  retention releases it and whether it counts in a domain's bounce backoff;
- the replay redacts an erased colleague's company domain in the backup's
  activity log and deletes its daily send counts, as a live erasure does;
- the drill counts what is lost by kind: opt-outs, company blocks, address
  blocks, bounces, erasures.
"""

import json
import os
import shutil
import sqlite3
from datetime import timedelta

import pytest

import main
from conftest import sent_to
from modules import backup, clock, journal
from modules.database import Database, domain_token, erasure_token
from modules.export import DataExporter
from modules.imap_monitor import ImapMonitor

ANNA = "anna.rossi@brandco.de"
LUCA = "luca.verdi@other.de"
COLLEAGUE = "marco.bianchi@brandco.de"


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "live" / "agent.db"))


@pytest.fixture
def db(s):
    os.makedirs(os.path.dirname(s.get_abs_db_path()), exist_ok=True)
    return Database(s.get_abs_db_path())


@pytest.fixture
def dashboard(s, db):
    return main.create_dashboard_app(s).test_client()


def _contact(db, email, status="emailed"):
    cid = db.add_contact(db.log_search("q"), email, organization="Brand")
    db.update_contact_status(cid, status)
    if status == "emailed":
        sent_to(db, email)
    return cid


def _action(client, cid, action):
    response = client.post(f"/api/contacts/{cid}/action", json={"action": action})
    assert response.status_code == 200, response.get_json()


def _bounce(db, email):
    """A bounce report about an email the agent sent: an emailed contact's
    (_contact), or one a restore kept by the token."""
    if db.get_contact_by_email(email) is None:
        sent_to(db, email)
    ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw").process_bounces(
        [{"email": email, "subject": "Undeliverable"}]
    )


def _lines(db):
    return journal.read(journal.journal_path(db.db_path))


def _restored(db, tmp_path, source):
    """The backup at source, put in a folder of its own and replayed with the
    live journal, as an operator restores one."""
    folder = tmp_path / "restored"
    folder.mkdir()
    shutil.copyfile(source, folder / "agent.db")
    copy = Database(str(folder / "agent.db"))
    counts = journal.replay_journal(copy, journal.journal_path(db.db_path))
    return copy, counts


def _reason(db, email):
    return db.get_suppression_reason(email)


def _cli(s, monkeypatch, capsys, *argv):
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", *argv])
    main.main()
    return capsys.readouterr().out


def _row(out, label):
    line = next(line for line in out.splitlines() if line.strip().startswith(label))
    checked, missing = line.split()[-2:]
    return int(checked), int(missing)


# ── Each one is journalled when it changes the database ──


def test_a_dashboard_block_and_unblock_are_journalled(db, dashboard):
    cid = _contact(db, ANNA)

    _action(dashboard, cid, "block")
    _action(dashboard, cid, "unblock")

    assert [(e["kind"], e.get("email"), e.get("reason")) for e in _lines(db)] == [
        ("address_block", ANNA, None),
        ("address_unblock", ANNA, "blocked_via_dashboard"),
    ]


def test_a_bounce_a_refusal_and_a_bounce_over_a_block_are_journalled(db):
    _contact(db, ANNA)
    _contact(db, LUCA)
    _bounce(db, ANNA)
    db.add_to_suppression("refused@third.de", reason="smtp_rejected")
    db.block_address("blocked@third.de")
    _bounce(db, "blocked@third.de")

    assert [(e["kind"], e.get("email"), e.get("reason")) for e in _lines(db)] == [
        ("suppress", ANNA, "bounce"),
        ("suppress", "refused@third.de", "smtp_rejected"),
        ("address_block", "blocked@third.de", None),
        ("suppress", "blocked@third.de", "bounce_blocked"),
    ]


def test_a_block_over_a_bounce_is_journalled(db):
    _bounce(db, ANNA)
    db.block_address(ANNA)
    assert _reason(db, ANNA) == "bounce_blocked"
    assert [e["kind"] for e in _lines(db)] == ["suppress", "address_block"]


def test_nothing_is_journalled_when_nothing_changed(db):
    db.record_opt_out(ANNA, "Opted out by email")
    before = _lines(db)

    db.add_to_suppression(ANNA, reason="bounce")
    db.block_address(ANNA)
    db.replace_block(ANNA, "bounce_blocked")
    assert not db.remove_from_suppression(ANNA, only_reasons=("blocked_via_dashboard",))

    assert _lines(db) == before


def test_an_erased_persons_block_and_bounce_lines_keep_only_the_token(db):
    _contact(db, ANNA)
    db.block_address(ANNA)
    _bounce(db, ANNA)

    DataExporter(db).gdpr_erase(ANNA)

    raw = journal.journal_path(db.db_path).read_text(encoding="utf-8")
    assert ANNA not in raw
    kinds = {(e["kind"], e.get("token")) for e in _lines(db)}
    assert ("address_block", erasure_token(ANNA)) in kinds
    assert ("suppress", erasure_token(ANNA)) in kinds


# ── A backup taken before the change, plus the journal after it ──


def _block(db, dashboard):
    _action(dashboard, _contact(db, ANNA), "block")


def _bounced(db, dashboard):
    _contact(db, ANNA)
    _bounce(db, ANNA)


def _refused(db, dashboard):
    db.add_to_suppression(ANNA, reason="smtp_rejected")


@pytest.mark.parametrize(
    "change, reason",
    [
        (_block, "blocked_via_dashboard"),
        (_bounced, "bounce"),
        (_refused, "smtp_rejected"),
    ],
    ids=["address-block", "bounce", "server-refusal"],
)
def test_a_restore_keeps_what_was_suppressed_since_the_backup(
    db, dashboard, tmp_path, change, reason
):
    source = backup.make_backup(db)
    change(db, dashboard)

    copy, _ = _restored(db, tmp_path, source)

    assert _reason(copy, ANNA) == reason
    assert copy.is_blocked(ANNA)


def test_a_restore_keeps_a_bounce_over_a_block(db, dashboard, tmp_path):
    cid = _contact(db, ANNA)
    _action(dashboard, cid, "block")
    source = backup.make_backup(db)
    _bounce(db, ANNA)

    copy, counts = _restored(db, tmp_path, source)

    assert _reason(copy, ANNA) == "bounce_blocked", "Unblock could undo a bounce"
    assert counts["suppressions"] == 1


def test_a_restore_keeps_a_block_over_a_bounce(db, tmp_path):
    _contact(db, ANNA)
    _bounce(db, ANNA)
    source = backup.make_backup(db)
    db.block_address(ANNA)

    copy, counts = _restored(db, tmp_path, source)

    assert _reason(copy, ANNA) == "bounce_blocked", "retention would release it"
    assert counts["address_blocks"] == 1


def test_a_restore_keeps_an_unblock_made_since_the_backup(db, dashboard, tmp_path):
    cid = _contact(db, ANNA)
    _action(dashboard, cid, "block")
    source = backup.make_backup(db)
    _action(dashboard, cid, "unblock")

    copy, counts = _restored(db, tmp_path, source)

    assert not copy.is_suppressed(ANNA)
    assert counts["unblocks"] == 1


def test_the_contact_shows_what_the_replay_put_back(db, dashboard, tmp_path):
    """As live: a Block marks a contact 'blocked', which the dashboard's
    Unblock reopens, and a bounce marks it 'bounced'. A restored contact still
    'pending' and suppressed could not be unblocked from the dashboard."""
    blocked = _contact(db, ANNA)
    reopened = _contact(db, LUCA)
    _action(dashboard, reopened, "block")
    bounced = _contact(db, "dead@third.de")
    source = backup.make_backup(db)
    _action(dashboard, blocked, "block")
    _action(dashboard, reopened, "unblock")
    _bounce(db, "dead@third.de")

    copy, _ = _restored(db, tmp_path, source)

    assert copy.get_contact_by_id(blocked)["status"] == "blocked"
    assert copy.get_contact_by_id(reopened)["status"] == "pending"
    assert copy.get_contact_by_id(bounced)["status"] == "bounced"


def test_a_replayed_block_leaves_a_closed_contact_as_it_is(db, tmp_path):
    cid = _contact(db, ANNA, status="responded")
    source = backup.make_backup(db)
    db.block_address(ANNA)

    copy, _ = _restored(db, tmp_path, source)

    assert copy.get_contact_by_id(cid)["status"] == "responded"
    assert copy.is_suppressed(ANNA)


def test_a_restored_bounce_keeps_the_time_it_was_recorded(db, tmp_path):
    """The time decides when retention releases it (24 months) and whether
    it counts in its domain's bounce backoff (the last 7 days)."""
    written = clock.now() - timedelta(days=3)
    journal.journal_path(db.db_path).write_text(
        json.dumps(
            {
                "at": written.isoformat(),
                "kind": "suppress",
                "email": ANNA,
                "reason": "bounce",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    journal.replay_journal(db)

    (stamp,) = db.conn.execute(
        "SELECT timestamp FROM suppression_list WHERE email = ?", (ANNA,)
    ).fetchone()
    assert stamp == written.strftime("%Y-%m-%d %H:%M:%S")


# ── Order: whichever came last holds ──


def test_block_then_unblock_since_the_backup_leaves_it_unblocked(
    db, dashboard, tmp_path
):
    cid = _contact(db, ANNA)
    source = backup.make_backup(db)
    _action(dashboard, cid, "block")
    _action(dashboard, cid, "unblock")

    copy, _ = _restored(db, tmp_path, source)

    assert not copy.is_suppressed(ANNA)


def test_unblock_then_block_since_the_backup_leaves_it_blocked(db, dashboard, tmp_path):
    cid = _contact(db, ANNA)
    _action(dashboard, cid, "block")
    source = backup.make_backup(db)
    _action(dashboard, cid, "unblock")
    _action(dashboard, cid, "block")

    copy, _ = _restored(db, tmp_path, source)

    assert _reason(copy, ANNA) == "blocked_via_dashboard"


def test_lift_then_block_of_a_company_leaves_it_blocked(db, tmp_path):
    db.block_domain("third.de", "objection")
    source = backup.make_backup(db)
    db.lift_domain_block("third.de")
    db.block_domain("third.de", "objection again")

    copy, _ = _restored(db, tmp_path, source)

    assert copy.company_blocked("x@third.de")


def test_block_then_lift_of_a_company_leaves_it_lifted(db, tmp_path):
    source = backup.make_backup(db)
    db.block_domain("third.de", "objection")
    db.lift_domain_block("third.de")

    copy, _ = _restored(db, tmp_path, source)

    assert not copy.company_blocked("x@third.de")


def test_a_replay_never_lifts_an_objection_whatever_a_line_says(db):
    db.record_opt_out(ANNA, "Opted out by email")
    with open(journal.journal_path(db.db_path), "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "at": clock.now_iso(),
                    "kind": "address_unblock",
                    "email": ANNA,
                    "reason": "unsubscribe",
                }
            )
            + "\n"
        )

    journal.replay_journal(db)

    assert _reason(db, ANNA) == "unsubscribe"


def test_a_line_whose_reason_is_not_text_is_skipped_and_the_rest_replayed(db):
    path = journal.journal_path(db.db_path)
    path.write_text(
        "".join(
            json.dumps({"at": clock.now_iso(), **line}) + "\n"
            for line in (
                {"kind": "suppress", "email": LUCA, "reason": ["bounce"]},
                {"kind": "suppress", "email": ANNA, "reason": "bounce"},
            )
        ),
        encoding="utf-8",
    )

    counts = journal.replay_journal(db)

    assert _reason(db, ANNA) == "bounce"
    assert counts["suppressions"] == 1


# ── An erased person or company never comes back ──


def test_an_erased_persons_block_and_bounce_are_not_written_back(db, tmp_path):
    """Lines written before the erasure, read by a replay that runs before
    the journal is rewritten: the erasure still wins."""
    path = journal.journal_path(db.db_path)
    path.write_text(
        "".join(
            json.dumps({"at": clock.now_iso(), **line}) + "\n"
            for line in (
                {"kind": "address_block", "email": ANNA},
                {"kind": "suppress", "email": ANNA, "reason": "bounce"},
                {"kind": "erasure", "token": erasure_token(ANNA)},
            )
        ),
        encoding="utf-8",
    )

    journal.replay_journal(db)

    held = [r[0] for r in db.conn.execute("SELECT email FROM suppression_list")]
    assert held == [erasure_token(ANNA)]


def _a_colleague_erased_after_the_backup(db):
    """A backup whose activity log and daily send counts name brandco.de; then
    a colleague there it never held is added and erased. Returns the backup."""
    _contact(db, ANNA)
    db.log_activity("DOMAIN_BACKOFF", "Backing off brandco.de: 3 of 5 bounced")
    db.log_activity("NOTE", "Not a match: mybrandco.de, www.brandco.de, ANNA")
    db.conn.execute(
        "INSERT INTO domain_send_counts (date, domain, count) VALUES (?, ?, 4)",
        (clock.today_str(), "brandco.de"),
    )
    db.conn.commit()
    source = backup.make_backup(db)
    # A lead never emailed: _contact's send is not journalled, as a real one is.
    _contact(db, COLLEAGUE, status="pending")
    DataExporter(db).gdpr_erase(COLLEAGUE)
    return source


def _activity(db):
    return [r[0] for r in db.conn.execute("SELECT details FROM activity_log")]


def test_the_replay_redacts_an_erased_colleagues_company_as_a_live_erasure_does(
    db, tmp_path
):
    source = _a_colleague_erased_after_the_backup(db)
    live = _activity(db)

    copy, counts = _restored(db, tmp_path, source)

    details = _activity(copy)
    token = domain_token("brandco.de")
    assert f"Backing off {token}: 3 of 5 bounced" in details
    assert f"Backing off {token}: 3 of 5 bounced" in live, "as the live erasure did"
    assert "Not a match: mybrandco.de, www.brandco.de, ANNA" in details
    assert not any(" brandco.de" in d for d in details)
    assert (
        copy.conn.execute(
            "SELECT COUNT(*) FROM domain_send_counts WHERE domain = 'brandco.de'"
        ).fetchone()[0]
        == 0
    )
    assert counts["redacted"] == 2


def test_a_lifted_erased_company_is_redacted_too(db, tmp_path):
    source = _a_colleague_erased_after_the_backup(db)
    assert db.lift_domain_block("brandco.de") == "keyed", "setup"

    copy, _ = _restored(db, tmp_path, source)

    assert not any(" brandco.de" in d for d in _activity(copy))
    (counted,) = copy.conn.execute("SELECT COUNT(*) FROM domain_send_counts")
    assert counted[0] == 0


def test_a_company_that_was_never_erased_is_left_alone(db, tmp_path):
    db.log_activity("DOMAIN_BACKOFF", "Backing off third.de: 3 of 5 bounced")
    source = backup.make_backup(db)
    db.block_domain("third.de", "objection")

    copy, counts = _restored(db, tmp_path, source)

    assert "Backing off third.de: 3 of 5 bounced" in _activity(copy)
    assert counts["redacted"] == 0


# ── Old journals ──


def test_a_journal_written_before_these_lines_existed_still_replays(db, tmp_path):
    _contact(db, ANNA)
    _contact(db, LUCA)
    source = backup.make_backup(db)
    path = journal.journal_path(db.db_path)
    path.write_text(
        "".join(
            json.dumps({"at": "2026-11-01T10:00:00+01:00", **line}) + "\n"
            for line in (
                {"kind": "opt_out", "email": ANNA},
                {"kind": "block", "domain": "third.de"},
                {"kind": "lift", "domain": "third.de"},
                {"kind": "block", "domain": "fourth.de"},
                {"kind": "erasure", "token": erasure_token(LUCA)},
            )
        ),
        encoding="utf-8",
    )

    copy, counts = _restored(db, tmp_path, source)

    assert _reason(copy, ANNA) == "unsubscribe"
    assert not copy.company_blocked("x@third.de")
    assert copy.company_blocked("x@fourth.de")
    assert copy.get_contact_by_email(LUCA) is None
    assert counts["erased"] == 1
    assert counts["address_blocks"] == counts["unblocks"] == 0
    assert counts["suppressions"] == counts["redacted"] == 0


# ── The drill counts what is lost by kind ──


def _everything_since_the_backup(db, dashboard):
    blocked = _contact(db, "blocked@third.de")
    unblocked = _contact(db, "unblocked@third.de")
    _action(dashboard, unblocked, "block")
    backup.make_backup(db)
    db.record_opt_out(ANNA, "Opted out by email")
    db.block_domain("fourth.de", "objection")
    _action(dashboard, blocked, "block")
    _action(dashboard, unblocked, "unblock")
    _bounce(db, "dead@fifth.de")
    db.add_to_suppression("refused@fifth.de", reason="smtp_rejected")


def test_a_restore_the_journal_fully_repairs_passes_the_drill(
    s, db, dashboard, monkeypatch, capsys
):
    _everything_since_the_backup(db, dashboard)

    out = _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill PASSED" in out
    assert _row(out, "opt-outs") == (1, 0)
    assert _row(out, "company blocks") == (1, 0)
    assert _row(out, "address blocks") == (1, 0)
    assert _row(out, "bounces") == (2, 0)
    assert "1 address block(s), 1 unblock(s), 2 bounce(s)" in out


@pytest.mark.parametrize(
    "lost, label, problem",
    [
        (
            lambda db: db.block_address("blocked@third.de"),
            "address blocks",
            "1 address block(s) missing",
        ),
        (lambda db: _bounce(db, "dead@fifth.de"), "bounces", "1 bounce(s) missing"),
        (
            lambda db: db.add_to_suppression("refused@fifth.de", "smtp_rejected"),
            "bounces",
            "1 bounce(s) missing",
        ),
        (
            lambda db: db.block_domain("fourth.de", "objection"),
            "company blocks",
            "1 company block(s) missing",
        ),
        (
            lambda db: db.record_opt_out(ANNA, "Opted out by email"),
            "opt-outs",
            "1 opt-out(s) missing",
        ),
    ],
    ids=["address-block", "bounce", "server-refusal", "company-block", "opt-out"],
)
def test_the_drill_names_the_kind_the_journal_lost(
    s, db, monkeypatch, capsys, lost, label, problem
):
    backup.make_backup(db)
    db.journaling = False
    lost(db)

    with pytest.raises(SystemExit):
        _cli(s, monkeypatch, capsys, "restore-drill", "--newest")
    out = capsys.readouterr().out

    assert _row(out, label) == (1, 1)
    assert problem in out
    kinds = {"opt-outs", "company blocks", "address blocks", "bounces", "erasures"}
    for other in kinds - {label}:
        assert _row(out, other)[1] == 0
    assert "blocked@third.de" not in out and ANNA not in out


def test_the_drill_names_a_lost_erasure_as_an_erasure(s, db, monkeypatch, capsys):
    """An erasure the journal lost is counted as an erasure, not as an opt-out:
    the report must say which kind a restore would lose."""
    backup.make_backup(db)
    db.journaling = False
    DataExporter(db).gdpr_erase(ANNA)

    with pytest.raises(SystemExit):
        _cli(s, monkeypatch, capsys, "restore-drill", "--newest")
    out = capsys.readouterr().out

    assert _row(out, "erasures") == (1, 1)
    assert "1 erasure(s) missing" in out
    assert _row(out, "company blocks") == (1, 1), "the erasure's company block"
    for other in ("opt-outs", "address blocks", "bounces"):
        assert _row(out, other) == (0, 0)
    assert ANNA not in out


@pytest.mark.parametrize("company", [False, True], ids=["address", "company"])
def test_a_block_made_again_after_its_lift_with_its_line_lost_fails_the_drill(
    s, db, dashboard, monkeypatch, capsys, company
):
    """The journal's lift took the block out of what the drill expected, though
    the live database holds it again: the restore would lose it, and the drill
    passed."""
    cid = _contact(db, ANNA)
    backup.make_backup(db)
    if company:
        db.block_domain("brandco.de", "objection")
        db.lift_domain_block("brandco.de")
        db.journaling = False
        db.block_domain("brandco.de", "objection again")
    else:
        _action(dashboard, cid, "block")
        _action(dashboard, cid, "unblock")
        db.journaling = False
        db.block_address(ANNA)

    with pytest.raises(SystemExit):
        _cli(s, monkeypatch, capsys, "restore-drill", "--newest")
    out = capsys.readouterr().out

    assert _row(out, "company blocks" if company else "address blocks") == (1, 1)


@pytest.mark.parametrize("where", ["live", "live-gone"])
def test_a_company_blocked_again_in_clear_after_its_erasure_and_lift_passes(
    s, db, monkeypatch, capsys, tmp_path, where
):
    """A colleague's erasure kept brandco.de as its token; the operator lifted
    that block; then another person there opted out, which blocks the company
    in clear, as the live database then holds it. The drill counted the
    restored copy's plain block as an erased company back in clear and failed
    a good restore, with or without the live database. The backup comes
    first, so the erasure's token lines are never trimmed from the journal,
    and does not hold the colleague, whom the replay would erase again."""
    _contact(db, ANNA)
    source = backup.make_backup(db)
    # A lead never emailed: _contact's send is not journalled, as a real one is.
    _contact(db, COLLEAGUE, status="pending")
    DataExporter(db).gdpr_erase(COLLEAGUE)
    assert db.lift_domain_block("brandco.de") == "keyed", "setup"
    db.record_opt_out(ANNA, "Opted out by email")
    assert db.block_domain("brandco.de", "Opted out by email"), "setup"
    tokens = [e.get("token") for e in _lines(db) if e["kind"] in ("block", "lift")]
    assert domain_token("brandco.de") in tokens, "setup: the erasure's lines"
    argv = ["restore-drill", "--newest"]
    if where == "live-gone":
        off_machine = tmp_path / "off-machine.db"
        shutil.copyfile(source, off_machine)
        db.close_thread_conn()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db.db_path + suffix):
                os.remove(db.db_path + suffix)
        argv = ["restore-drill", "--backup", str(off_machine)]

    out = _cli(s, monkeypatch, capsys, *argv)

    assert "Restore drill PASSED" in out
    assert "Erased and back in clear: 0" in out
    assert _row(out, "company blocks") == (1, 0)


@pytest.mark.parametrize("left", ["line", "record"])
def test_a_line_in_clear_older_than_the_erasure_still_counts_as_erased(
    s, db, monkeypatch, capsys, tmp_path, left
):
    """Only a line in clear after the erasure's token names the company again.
    One before it is a line the erasure's rewrite has not reached yet: the
    record of that rewrite is pending, or was lost with its token line kept.
    The drill must still fail a copy that holds the company in clear."""
    db.block_domain("brandco.de", "objection")
    off_machine = tmp_path / "off-machine.db"
    shutil.copyfile(backup.make_backup(db), off_machine)
    if left == "line":
        journal.append(db.db_path, "block", token=domain_token("brandco.de"))
    else:
        assert journal.keep_pending(
            db.db_path, erasure_token(COLLEAGUE), domain_token("brandco.de")
        ), "setup"
    db.close_thread_conn()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db.db_path + suffix):
            os.remove(db.db_path + suffix)
    # The replay drops the plain block; the drill is the check behind it.
    monkeypatch.setattr(journal, "_drop_plain_block", lambda db, token: None)

    with pytest.raises(SystemExit):
        _cli(s, monkeypatch, capsys, "restore-drill", "--backup", str(off_machine))
    out = capsys.readouterr().out

    assert "1 erased address(es) or company domain(s) back in clear" in out
    assert "brandco" not in out


def test_the_drill_passes_an_erased_persons_journalled_block_and_bounce(
    s, db, monkeypatch, capsys
):
    _contact(db, ANNA)
    backup.make_backup(db)
    db.block_address(ANNA)
    _bounce(db, ANNA)
    DataExporter(db).gdpr_erase(ANNA)

    out = _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert "Restore drill PASSED" in out
    assert "Erased and back in clear: 0" in out
    assert ANNA not in out


def test_the_live_journal_is_read_not_changed_by_a_drill(s, db, monkeypatch, capsys):
    _contact(db, ANNA)
    backup.make_backup(db)
    _bounce(db, ANNA)
    kept = journal.journal_path(db.db_path).read_bytes()

    _cli(s, monkeypatch, capsys, "restore-drill", "--newest")

    assert journal.journal_path(db.db_path).read_bytes() == kept
    raw = sqlite3.connect(db.db_path)
    try:
        assert raw.execute(
            "SELECT reason FROM suppression_list WHERE email = ?", (ANNA,)
        ).fetchone() == ("bounce",)
    finally:
        raw.close()
