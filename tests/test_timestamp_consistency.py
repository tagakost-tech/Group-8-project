"""One column, one clock.

sequence_enrollments.completed_at has three writers: SequenceEngine, the
unsubscribe link, and GDPR erasure. Two of them called datetime.now(), which is
the SYSTEM clock in the SYSTEM timezone - not the app clock everything else
goes through. Same event, same column, two different wall-clock times (an hour
apart on a UTC+3 host running a Europe/Rome app).

Same defect Batch 7 fixed for opened_at, in code Batch 7 never touched.

The stamps SQLite wrote itself were a second clock of the same kind. Each stamp
column's DEFAULT reads the host's clock, and a container sets no time zone: on
one, an opt-out read an hour or two earlier than the email it answered, in the
Art. 21 record itself, and the bounce windows mixed the two clocks at their
edges. Nothing showed on a PC set to Italian time, so those tests run in a
process of their own, on a host in another zone.
"""

import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from modules import clock, journal
from modules.database import (
    SCHEMA_SQL,
    Database,
    domain_token,
    erasure_token,
    set_erasure_key,
)
from modules.export import DataExporter
from modules.retry_queue import RetryQueue
from modules.sequences import SequenceEngine
from modules.unsubscribe import UnsubscribeHandler

SECRET = "k" * 32
ROOT = pathlib.Path(__file__).resolve().parent.parent
ROME = ZoneInfo(clock.DEFAULT_TIMEZONE)
# Hosts as TZ names them: UTC, where a container runs unless told otherwise, and
# one ahead of Rome, whose errors at a window's edge go the other way.
OTHER_HOSTS = ["UTC0", "JST-9"]
DB_STAMP = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def _enrol(db, email):
    """A contact with one active enrollment. Returns (contact_id, enrollment_id)."""
    engine = SequenceEngine(db)
    seq_id = engine.create_sequence(
        f"seq-{email}",
        [{"delay_days": 3, "subject": "Following up", "template_name": "followup_1"}],
    )
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, email)
    enrollment_id = engine.enroll_contact(cid, seq_id)
    return cid, enrollment_id


def _completed_at(db, cid):
    raw = sqlite3.connect(db.db_path)
    row = raw.execute(
        "SELECT completed_at FROM sequence_enrollments WHERE contact_id = ?", (cid,)
    ).fetchone()
    raw.close()
    return row[0] if row else None


def test_the_unsubscribe_link_stamps_the_same_shape_as_the_engine(db):
    """Both cancel an enrollment; both must record it the same way."""
    cid_engine, enrollment_id = _enrol(db, "engine@brand.de")
    cid_link, _ = _enrol(db, "link@brand.de")

    SequenceEngine(db).cancel_enrollment(enrollment_id, "replied")

    handler = UnsubscribeHandler(db, SECRET)
    ok, msg = handler.process_unsubscribe(handler.generate_token("link@brand.de"))
    assert ok, msg

    by_engine = _completed_at(db, cid_engine)
    by_link = _completed_at(db, cid_link)
    assert by_engine and by_link, f"engine={by_engine!r} link={by_link!r}"

    # The offset is the tell: the engine writes "+02:00", the link wrote a naive
    # local string - and on a host that is not in the app's timezone, a
    # different hour entirely.
    assert ("+" in by_engine) == ("+" in by_link), (
        f"two writers, two timestamp shapes in one column: "
        f"engine={by_engine!r} link={by_link!r}"
    )
    assert len(by_engine) == len(by_link), f"engine={by_engine!r} link={by_link!r}"


def test_gdpr_erasure_stamps_the_same_shape_as_the_engine(db):
    cid, _ = _enrol(db, "erase@brand.de")

    DataExporter(db).gdpr_erase("erase@brand.de")

    stamped = _completed_at(db, cid)
    assert stamped, "erasure did not cancel the enrollment"
    reference = clock.now_iso()
    assert ("+" in stamped) == ("+" in reference), (
        f"GDPR erasure wrote {stamped!r}, the app clock produces {reference!r}"
    )


def test_no_module_reaches_for_the_system_clock_directly():
    """The whole point of modules/clock.py is that "now" has exactly one
    definition. A datetime.now() anywhere in the package silently reintroduces
    a second one, in whatever timezone the host happens to be set to.

    Matching any argument list, not just the bare call. This guard used to look
    for the literal "datetime.now()" and so walked straight past
    datetime.now(timezone.utc) in analytics.get_chart_data - which built the
    chart's day boundaries in UTC while the rows it bucketed were stamped in
    Rome, so anything logged in the hour or two after local midnight vanished
    from the chart until UTC caught up. utcnow() is included too: it returns a
    NAIVE datetime, which compares against local timestamps without complaint
    and is wrong by the offset.
    """
    offenders = []
    pattern = re.compile(r"datetime\.(now\s*\(|utcnow\s*\()")
    root = pathlib.Path(__file__).resolve().parent.parent / "modules"
    # retry_queue.py keeps its own clock ON PURPOSE, and is exempt rather than
    # fixed. It stamps next_retry_at as UTC ISO-8601 ("...T...Z") and only ever
    # compares that column against cutoffs it builds itself in the same format,
    # so it is internally consistent. Converting it to clock.db_now() would
    # change the stored format, and the comparison is a STRING one: an existing
    # row's "T" sorts above a new cutoff's " ", so every retry already queued in
    # production would read as not-yet-due and never be sent again. That is a
    # migration with a backfill, not a one-line change.
    allowed = {"clock.py", "retry_queue.py"}
    for path in sorted(root.glob("*.py")):
        if path.name in allowed:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(stripped):
                offenders.append(f"{path.name}:{i}")
    assert not offenders, (
        f"these bypass modules/clock.py and use the host timezone: {offenders}"
    )


# ── A host in another time zone ──


def _on_a_host_in(zone: str, job: str, folder) -> dict:
    """Run one of the jobs below where the host's time zone is `zone`.

    In a process of its own: Windows has no time.tzset, and SQLite reads the
    zone through the C runtime, once.
    """
    program = (
        "import json, sys\n"
        f"from tests.test_timestamp_consistency import {job}\n"
        f"print(json.dumps({job}(sys.argv[1])))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", program, str(folder)],
        cwd=ROOT,
        env={**os.environ, "TZ": zone},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,  # a failure is reported with what the process said
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.splitlines()[-1])


def _stamp_columns() -> dict[str, str]:
    """Each table whose stamp defaults to the host's clock, and that column."""
    columns = {}
    for table, body in re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", SCHEMA_SQL, flags=re.DOTALL
    ):
        for column in re.findall(
            r"(\w+)\s+TEXT NOT NULL DEFAULT \(datetime\('now','localtime'\)\)", body
        ):
            columns[table] = column
    return columns


def _one_record_of_each_kind(folder: str) -> dict:
    """Every kind of stamped record, written through the product's own calls."""
    set_erasure_key(SECRET)
    folder = pathlib.Path(folder)
    db = Database(str(folder / "host.db"))
    address = "reader@brand.de"

    search = db.log_search("q", source="test")
    manual = db.get_or_create_manual_search()
    contact = db.add_contact(search, address, source_channel="referral")
    email = db.log_email(contact, address, "Hello")
    db.log_activity("stamp probe")
    db.set_state("stamp probe", "1")
    db.record_lead_alert(address)
    retry = RetryQueue(db).add_to_retry(email, contact, "refused")
    engine = SequenceEngine(db)
    sequence = engine.create_sequence(
        "probe", [{"delay_days": 3, "subject": "S", "template_name": "followup_1"}]
    )
    enrolment = engine.enroll_contact(contact, sequence)
    db.register_unsubscribe_token("a-token", address)
    db.log_consent(address, "legitimate_interest")

    db.add_to_suppression("bounced@brand.de", "bounce")
    db.block_address("blocked@brand.de")
    db.record_opt_out("optout@brand.de", "link")
    db.suppress_erased("erased@brand.de")
    db.block_domain("plain.example", "an objection")
    db.block_domain("keyed.example", "an erasure", keyed=True)
    lines = folder / "older_journal.jsonl"
    lines.write_text(
        json.dumps({"kind": "block", "token": domain_token("restored.example")})
        + "\n"
        + json.dumps({"kind": "erasure", "token": erasure_token("restored@brand.de")})
        + "\n",
        encoding="utf-8",
    )
    journal.replay_journal(db, lines)

    written = {
        "a search": ("search_log", "id", search),
        "the manual search": ("search_log", "id", manual),
        "a contact": ("contacts", "id", contact),
        "an email": ("email_log", "id", email),
        "an activity": ("activity_log", "action", "stamp probe"),
        "a state": ("agent_state", "key", "stamp probe"),
        "a lead alert": ("lead_alerts", "email", address),
        "a retry": ("retry_queue", "id", retry),
        "a sequence": ("sequences", "id", sequence),
        "an enrolment": ("sequence_enrollments", "id", enrolment),
        "an unsubscribe link": ("unsubscribe_tokens", "token", "a-token"),
        "a consent record": ("consent_log", "email", address),
        "a bounce": ("suppression_list", "email", "bounced@brand.de"),
        "an operator's block": ("suppression_list", "email", "blocked@brand.de"),
        "an opt-out": ("suppression_list", "email", "optout@brand.de"),
        "an erasure": ("suppression_list", "email", erasure_token("erased@brand.de")),
        "an erasure restored from the journal": (
            "suppression_list",
            "email",
            erasure_token("restored@brand.de"),
        ),
        "a company block": ("suppressed_domains", "domain", "plain.example"),
        "a company block after an erasure": (
            "suppressed_domains",
            "domain",
            domain_token("keyed.example"),
        ),
        "a company block restored from the journal": (
            "suppressed_domains",
            "domain",
            domain_token("restored.example"),
        ),
    }
    columns = _stamp_columns()
    conn = db.conn
    return {
        "host_clock": conn.execute("SELECT datetime('now','localtime')").fetchone()[0],
        "tables": sorted({table for table, _, _ in written.values()}),
        "stamps": {
            what: conn.execute(
                f"SELECT {columns[table]} FROM {table} WHERE {key_column} = ?", (key,)
            ).fetchone()[0]
            for what, (table, key_column, key) in written.items()
        },
    }


def _bounces_either_side_of_the_window(folder: str) -> dict:
    """Two addresses, mailed and bounced half an hour either side of where a
    seven-day window starts by the app clock."""
    set_erasure_key(SECRET)
    db = Database(str(pathlib.Path(folder) / "host.db"))
    search = db.log_search("q", source="test")
    for address, minutes in (("inside@brand.de", -30), ("outside@brand.de", 30)):
        contact = db.add_contact(search, address, source_channel="referral")
        db.log_email(contact, address, "Hello", delivery_status="sent")
        db.add_to_suppression(address, "bounce")
        stamp = (clock.now() - timedelta(days=7, minutes=minutes)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.conn.execute(
            "UPDATE email_log SET timestamp = ? WHERE recipient_email = ?",
            (stamp, address),
        )
        db.conn.execute(
            "UPDATE suppression_list SET timestamp = ? WHERE email = ?",
            (stamp, address),
        )
        db.conn.commit()  # the next contact writes through another connection
    metrics = db.get_deliverability_metrics(window_days=7)
    domain = db.get_domain_bounce_stats(window_days=7, min_sends=1).get(
        "brand.de", {"sent": 0, "bounced": 0}
    )
    return {
        "sent": metrics["recent_sent"],
        "bounced": metrics["recent_bounced"],
        "domain_sent": domain["sent"],
        "domain_bounced": domain["bounced"],
    }


def _reads_between(stamp: str, started: float, finished: float) -> bool:
    """Whether a stored stamp names a moment in this time, as instants.

    Without an offset a stamp is a wall-clock reading in Rome, which in the
    repeated hour of October means either of two. It holds whole seconds, so
    it can read up to a second before `started`.
    """
    when = datetime.fromisoformat(stamp)
    readings = (
        [when]
        if when.tzinfo
        else [when.replace(tzinfo=ROME, fold=fold) for fold in (0, 1)]
    )
    return any(int(started) <= r.timestamp() <= finished for r in readings)


@pytest.mark.parametrize("zone", OTHER_HOSTS)
def test_every_record_is_stamped_by_the_app_clock_on_a_host_in_another_zone(
    tmp_path, zone
):
    started = time.time()
    result = _on_a_host_in(zone, "_one_record_of_each_kind", tmp_path)
    finished = time.time()

    assert not _reads_between(result["host_clock"], started, finished), (
        f"TZ={zone} did not move SQLite's clock ({result['host_clock']}), so this "
        "test would prove nothing"
    )
    assert result["tables"] == sorted(_stamp_columns()), (
        "a table has a stamp this test writes no record to"
    )
    off_the_clock = {
        what: stamp
        for what, stamp in result["stamps"].items()
        if not _reads_between(stamp, started, finished)
    }
    assert not off_the_clock, (
        f"on a host in {zone}, whose clock reads {result['host_clock']}, these are "
        f"not in the app clock's time: {off_the_clock}"
    )
    # The windows compare stamps as strings against clock.db_days_ago(), so the
    # right instant in another shape is still read wrong: "2026-11-04T20:03"
    # sorts after "2026-11-04 20:33", because "T" sorts after a space. Three
    # keep a shape of their own on purpose and are only ever compared with
    # stamps in it: the retry queue its UTC "...Z" (see the exemption in
    # test_no_module_reaches_for_the_system_clock_directly), and the sequences
    # and their enrolments clock.now_iso(), offset included.
    own_shape = {"a retry", "a sequence", "an enrolment"}
    misshapen = {
        what: stamp
        for what, stamp in result["stamps"].items()
        if what not in own_shape and not DB_STAMP.fullmatch(stamp)
    }
    assert not misshapen, (
        f"these are not in clock.db_now()'s shape, so the windows misread them: "
        f"{misshapen}"
    )


@pytest.mark.parametrize("zone", OTHER_HOSTS)
def test_the_bounce_windows_start_by_the_app_clock_on_a_host_in_another_zone(
    tmp_path, zone
):
    counts = _on_a_host_in(zone, "_bounces_either_side_of_the_window", tmp_path)

    assert counts == {"sent": 1, "bounced": 1, "domain_sent": 1, "domain_bounced": 1}


def test_the_bounce_windows_read_the_stamps_the_product_writes(db, monkeypatch):
    """The test above writes its stamps with UPDATE, in the shape it expects.
    Here the product stamps the send and the bounce itself, with its clock
    stopped half an hour either side of where the seven-day window starts."""
    real_now = clock.now
    search = db.log_search("q", source="test")
    for address, minutes in (("inside@brand.de", -30), ("outside@brand.de", 30)):
        then = real_now() - timedelta(days=7, minutes=minutes)
        monkeypatch.setattr(clock, "now", lambda then=then: then)
        contact = db.add_contact(search, address, source_channel="referral")
        db.log_email(contact, address, "Hello", delivery_status="sent")
        db.add_to_suppression(address, "bounce")
    monkeypatch.setattr(clock, "now", real_now)

    metrics = db.get_deliverability_metrics(window_days=7)
    domain = db.get_domain_bounce_stats(window_days=7, min_sends=1).get(
        "brand.de", {"sent": 0, "bounced": 0}
    )

    stamps = dict(db.conn.execute("SELECT email, timestamp FROM suppression_list"))
    assert {
        "sent": metrics["recent_sent"],
        "bounced": metrics["recent_bounced"],
        "domain_sent": domain["sent"],
        "domain_bounced": domain["bounced"],
    } == {"sent": 1, "bounced": 1, "domain_sent": 1, "domain_bounced": 1}, (
        f"window starts {clock.db_days_ago(7)!r}; the bounces read {stamps}"
    )


def _product_sources() -> list[pathlib.Path]:
    return [
        *sorted((ROOT / "modules").glob("*.py")),
        ROOT / "agent.py",
        ROOT / "main.py",
    ]


def test_no_insert_leaves_its_stamp_to_the_hosts_clock():
    """The DEFAULT cannot be taken away: a database already made keeps its own.
    So an insert that names no stamp is on the host's clock, unseen on a PC set
    to Italian time."""
    columns = _stamp_columns()
    assert columns
    inserted, offenders = set(), []
    for path in _product_sources():
        source = path.read_text(encoding="utf-8")
        for table, named in re.findall(
            r"INSERT(?: OR \w+)? INTO (\w+)\s*\(([^)]*)\)", source
        ):
            inserted.add(table)
            if table in columns and columns[table] not in re.findall(r"\w+", named):
                offenders.append(f"{path.name}: {table}")
    assert not offenders, f"these leave the stamp to SQLite's default: {offenders}"
    assert set(columns) <= inserted, "this scan no longer finds the inserts"


def test_no_query_reads_the_hosts_clock():
    """The windows start where the app clock says: every row they judge is
    stamped by it."""
    offenders = [
        path.name
        for path in _product_sources()
        if "'localtime'" in path.read_text(encoding="utf-8").replace(SCHEMA_SQL, "")
    ]
    assert not offenders, f"these ask SQLite for the host's time: {offenders}"


def test_the_image_runs_in_the_apps_time_zone():
    """For what still reads the host's clock, the log lines among them."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    zones = re.findall(r"^ENV\s+TZ=(\S+)", dockerfile, flags=re.MULTILINE)
    assert zones == [clock.DEFAULT_TIMEZONE]
