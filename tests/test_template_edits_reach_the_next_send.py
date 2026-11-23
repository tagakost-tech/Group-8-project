"""A first-email template saved in the editor or by hand reaches the next send.

The main template and its A/B variants were read once, when the agent was built;
only follow-ups were read on each send. So the editor said "Template saved
successfully!" while the dashboard's Direct Send agent, which lives as long as
the process, and a campaign already running kept sending the old text until a
restart, copy fixed at counsel's request included.

Now each send first compares the saved versions of templates/email_template*.html
and re-reads them when one changed, as refresh_lists_if_changed does for the
lists. A file that cannot be used keeps the previous text, never the generic
default nobody reviewed, and is read again at each send until it can be: unlike
a spreadsheet, it costs nothing to look.
"""

import itertools
import logging
import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

import main
from agent import OutreachAgent
from modules.email_sender import EmailSender
from modules.file_handler import FileHandler

REAL = Path(main.__file__).resolve().parent / "templates"
EDIT = "<p>EDITED-AFTER-THE-FIRST-SEND</p>"
# Put in the copy of the template the tests start from.
REVIEWED = "<p>REVIEWED-TEXT</p>"
# In the generic default an agent falls back to when it starts without a file,
# and not in SUPPLECO's own first email, which opens with "I am writing to
# introduce" as the default does.
DEFAULT_TEXT = "Please find our company presentation attached"
# The same for SUPPLECO's Italian first email.
EDIT_IT = "<p>MODIFICATO-DOPO-IL-PRIMO-INVIO</p>"
REVIEWED_IT = "<p>TESTO-RIVISTO</p>"


@pytest.fixture
def templates(tmp_path, monkeypatch):
    """A COPY used by the editor and by every agent: a failing run must not
    overwrite the real template."""
    folder = tmp_path / "templates"
    shutil.copytree(REAL, folder)
    main_file = folder / "email_template.html"
    main_file.write_text(_with(main_file, REVIEWED), encoding="utf-8")
    italian_file = folder / "email_template_it.html"
    italian_file.write_text(_with(italian_file, REVIEWED_IT), encoding="utf-8")
    monkeypatch.setattr(main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("agent.FileHandler", lambda: FileHandler(str(tmp_path)))
    return folder


@pytest.fixture
def wire(monkeypatch):
    """The HTML of every message handed to SMTP (nothing leaves the machine)."""
    sent = []

    def _deliver(self, msg, blocked=None):
        sent.append(msg.get_body(preferencelist=("html",)).get_content())
        return True, "250 2.0.0 Ok", False, False

    monkeypatch.setattr(EmailSender, "_deliver", _deliver)
    return sent


def _settings(settings_factory, tmp_path, **overrides):
    return settings_factory(
        db_path=str(tmp_path / "d.db"),
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
        imap_enabled=False,
        ai_humanize_emails=False,
        warmup_enabled=False,
        attach_profile=False,
        lead_scoring_enabled=False,
        heuristic_discovery_enabled=False,
        target_queries="",
        **overrides,
    )


@pytest.fixture
def agent(settings_factory, tmp_path, templates, wire):
    return OutreachAgent(_settings(settings_factory, tmp_path))


_NEXT = itertools.count(1)


def _address():
    """A new company each time: the per-domain cap is not under test here."""
    return f"buyer@brand{next(_NEXT)}.de"


def _italian_address():
    """An Italian company: SUPPLECO's Italian first email goes to it."""
    return f"acquisti@marca{next(_NEXT)}.it"


def _send(a):
    return a.send_single_email(_address(), organization="Acme", channel="referral")


def _send_it(a):
    return a.send_single_email(
        _italian_address(), organization="Rossi", channel="referral"
    )


def _dashboard_send(client, address=None):
    reply = client.post(
        "/api/send-manual",
        json={
            "email": address or _address(),
            "organization": "Acme",
            "channel": "referral",
        },
    )
    assert reply.get_json()["ok"], reply.get_json()


def _touch(path):
    """A later modification time, as a real edit minutes later would have -
    two saves inside one test can otherwise share a timestamp."""
    st = os.stat(path)
    later = st.st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(later, later))


def _save(path, content):
    """A hand edit: the file changes, and nothing else is told."""
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    _touch(path)


def _with(path, text):
    """The file's content with a line of text added above the legal footer.
    SUPPLECO's first email has no {{body}}: its text is written out in full."""
    return path.read_text(encoding="utf-8").replace(
        "{{legal_footer}}", text + "{{legal_footer}}"
    )


def _rows(a, action):
    return [r for r in a.db.get_recent_activity(200) if r["action"] == action]


# -- The defect: the edit reached the disk and not the next mail --------------


def test_a_save_in_the_editor_reaches_the_next_direct_send(
    settings_factory, tmp_path, templates, wire
):
    """The dashboard's Direct Send agent is built once per process."""
    path = templates / "email_template.html"
    client = main.create_dashboard_app(
        _settings(settings_factory, tmp_path)
    ).test_client()
    _dashboard_send(client)
    saved = client.post("/api/template", json={"content": _with(path, EDIT)})
    assert saved.status_code == 200
    assert EDIT in path.read_text(encoding="utf-8")
    _dashboard_send(client)
    assert EDIT not in wire[0]
    assert EDIT in wire[1], "the editor said saved; the next mail had the old text"


def test_a_restarted_dashboard_has_the_edit_too(
    settings_factory, tmp_path, templates, wire
):
    """The control: a new process always read the file."""
    path = templates / "email_template.html"
    settings = _settings(settings_factory, tmp_path)
    client = main.create_dashboard_app(settings).test_client()
    _dashboard_send(client)
    client.post("/api/template", json={"content": _with(path, EDIT)})
    _dashboard_send(main.create_dashboard_app(settings).test_client())
    assert EDIT in wire[1]


def test_a_hand_edit_reaches_the_next_direct_send(agent, templates, wire):
    path = templates / "email_template.html"
    assert _send(agent)[0]
    _save(path, _with(path, EDIT))
    assert _send(agent)[0]
    assert EDIT not in wire[0]
    assert EDIT in wire[1], "an edit on disk needed a restart to count"


def test_a_running_campaign_sends_the_edit_from_its_next_email(agent, templates, wire):
    """Drives the real loop: the edit lands during the pause after the first
    email of a batch, and the second email of that batch must carry it. A
    batch at the default pace runs for hours."""
    a = agent
    path = templates / "email_template.html"
    # Not pending: a failing run must not go on to run this search for real.
    sid = a.db.log_search("q", status="completed")
    a.db.add_contact(sid, email="anna@one.de", organization="Acme One")
    a.db.add_contact(sid, email="bruno@two.de", organization="Acme Two")
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 1:
            _save(path, _with(path, EDIT))
        if len(wire) >= 2 or len(naps) > 5:
            a._running = False

    a._interruptible_sleep = nap
    a.run_continuous(dry_run=False, loop_delay=60)
    assert len(wire) == 2
    assert EDIT not in wire[0]
    assert EDIT in wire[1], "the campaign kept the text it started with"


def test_a_direct_send_during_a_campaign_carries_the_edit(
    settings_factory, tmp_path, templates, wire, monkeypatch
):
    """While a campaign runs, Direct Send uses the campaign's own agent."""
    path = templates / "email_template.html"
    running, finish = threading.Event(), threading.Event()

    def _campaign(self, **_kw):
        running.set()
        finish.wait(30)

    monkeypatch.setattr(OutreachAgent, "run_continuous", _campaign)
    client = main.create_dashboard_app(
        _settings(settings_factory, tmp_path)
    ).test_client()
    try:
        client.post("/api/agent/start", json={"mode": "continuous"})
        assert running.wait(30)
        client.post("/api/template", json={"content": _with(path, EDIT)})
        _dashboard_send(client)
    finally:
        finish.set()
    assert EDIT in wire[0]


def test_a_retry_carries_the_edit(agent, templates, wire):
    """A retry sends the first email again, in whichever text is current."""
    a = agent
    path = templates / "email_template.html"
    sid = a.db.log_search("q")
    cid = a.db.add_contact(sid, email="anna@one.de", organization="Acme One")
    log_id = a.db.log_email(cid, "anna@one.de", "s", delivery_status="failed")
    a.retry_queue.add_to_retry(log_id, cid, "Initial send failed")
    a.db.conn.execute("UPDATE retry_queue SET next_retry_at = '2000-01-01 00:00:00'")
    a.db.conn.commit()
    _save(path, _with(path, EDIT))
    a._running = True
    a._interruptible_sleep = lambda _seconds: None
    a._process_retries()
    assert len(wire) == 1
    assert EDIT in wire[0]


# -- A/B variants ---------------------------------------------------------------


@pytest.fixture
def ab_agent(settings_factory, tmp_path, templates, wire, monkeypatch):
    """Two arms, and the draw always takes the last one, so a test knows which
    arm the next mail came from. A variant sorts after the main template."""
    (templates / "email_template_b.html").write_text(
        _with(templates / "email_template.html", "<p>ARM-B-AS-STARTED</p>"),
        encoding="utf-8",
    )
    monkeypatch.setattr("modules.dispatch_pipeline.random.choice", lambda seq: seq[-1])
    return OutreachAgent(_settings(settings_factory, tmp_path, ab_testing_enabled=True))


def test_an_edited_variant_reaches_the_next_send(ab_agent, templates, wire):
    """The main template is not touched: the variant alone has to count."""
    assert _send(ab_agent)[0]
    assert "ARM-B-AS-STARTED" in wire[0]
    _save(
        templates / "email_template_b.html",
        _with(templates / "email_template.html", "<p>ARM-B-EDITED</p>"),
    )
    assert _send(ab_agent)[0]
    assert "ARM-B-EDITED" in wire[1], "the A/B arm kept the text it started with"


def test_an_edit_to_the_main_template_reaches_its_ab_arm(
    ab_agent, templates, wire, monkeypatch
):
    """With A/B on, the main template is drawn from the variants as well."""
    path = templates / "email_template.html"
    monkeypatch.setattr("modules.dispatch_pipeline.random.choice", lambda seq: seq[0])
    _save(path, _with(path, EDIT))
    assert _send(ab_agent)[0]
    assert EDIT in wire[0]


def test_a_variant_added_or_removed_counts(ab_agent, templates, wire):
    arm_c = templates / "email_template_c.html"
    _save(arm_c, _with(templates / "email_template.html", "<p>ARM-C-ADDED</p>"))
    assert _send(ab_agent)[0]
    assert "ARM-C-ADDED" in wire[0], "a new arm needed a restart"
    arm_c.unlink()
    assert _send(ab_agent)[0]
    assert "ARM-C-ADDED" not in wire[1], "a retired arm went on being sent"
    assert "ARM-B-AS-STARTED" in wire[1]


def test_a_variant_that_cannot_be_used_keeps_its_text(ab_agent, templates, wire):
    _blank(templates / "email_template_b.html")
    assert _send(ab_agent)[0]
    assert "ARM-B-AS-STARTED" in wire[0]


def test_a_new_variant_that_cannot_be_used_stays_out_of_the_draw(
    ab_agent, templates, wire
):
    _blank(templates / "email_template_c.html")
    assert _send(ab_agent)[0], "the draw took an empty arm and the send was refused"
    assert "ARM-B-AS-STARTED" in wire[0]


def test_a_broken_variant_does_not_hold_back_an_edit_to_the_main_template(
    agent, templates, wire
):
    """A/B is off by default: a stray file nothing sends must not cost the edit
    the operator was told was saved."""
    path = templates / "email_template.html"
    _blank(templates / "email_template_b.html")
    _save(path, _with(path, EDIT))
    assert _send(agent)[0]
    assert EDIT in wire[0]
    assert len(_rows(agent, "TEMPLATE_RELOAD_FAILED")) == 1


def test_the_editors_backup_never_becomes_an_ab_arm(
    settings_factory, tmp_path, templates, wire
):
    """The control: the save leaves email_template.html.bak in the same folder."""
    path = templates / "email_template.html"
    settings = _settings(settings_factory, tmp_path, ab_testing_enabled=True)
    a = OutreachAgent(settings)
    client = main.create_dashboard_app(settings).test_client()
    client.post("/api/template", json={"content": _with(path, EDIT)})
    assert (templates / "email_template.html.bak").exists()
    assert _send(a)[0]
    assert [name for name, _html in a._template_variants] == ["email_template"]


# -- The follow-up fallback ---------------------------------------------------


def test_a_follow_up_without_its_file_falls_back_to_the_edit(agent, templates):
    """Follow-ups read their own file on each send; only this fallback used the
    copy cached at startup."""
    path = templates / "email_template.html"
    _save(path, _with(path, EDIT))
    assert EDIT in agent._load_followup_template("no_such_step")


def test_a_follow_up_edit_is_not_a_first_email_reload(agent, templates):
    """The control: follow-up files already count, and are none of this."""
    path = templates / "followup_1.html"
    _save(path, path.read_text(encoding="utf-8") + "<!-- edited -->")
    assert _send(agent)[0]
    assert _rows(agent, "TEMPLATE_RELOADED") == []


# -- What counts as a new version --------------------------------------------


def test_an_edit_of_the_same_length_is_noticed(agent, templates, wire):
    """One word swapped for another of the same length: only the time differs."""
    path = templates / "email_template.html"
    before = path.read_text(encoding="utf-8")
    after = before.replace("REVIEWED-TEXT", "REWORDED-TEXT")
    assert after != before and len(after) == len(before)
    _save(path, after)
    assert _send(agent)[0]
    assert "REWORDED-TEXT" in wire[0]


def test_an_edit_with_the_same_timestamp_is_noticed(agent, templates, wire):
    """A coarse clock: only the length differs."""
    path = templates / "email_template.html"
    st = os.stat(path)
    path.write_text(_with(path, EDIT), encoding="utf-8")
    os.utime(path, ns=(st.st_mtime_ns, st.st_mtime_ns))
    assert _send(agent)[0]
    assert EDIT in wire[0]


def test_an_older_version_put_back_is_noticed(agent, templates, wire):
    """Changed is not the same as newer: a backup restored with its own date counts."""
    path = templates / "email_template.html"
    earlier = os.stat(path).st_mtime_ns - 3_600_000_000_000
    path.write_text(_with(path, EDIT), encoding="utf-8")
    os.utime(path, ns=(earlier, earlier))
    assert _send(agent)[0]
    assert EDIT in wire[0]


def test_an_unchanged_template_is_not_re_read(agent, monkeypatch):
    reads = []
    real = Path.read_text

    def spy(self, *args, **kw):
        if self.name.startswith("email_template"):
            reads.append(self.name)
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", spy)
    assert _send(agent)[0]
    assert _send(agent)[0]
    assert reads == []
    assert _rows(agent, "TEMPLATE_RELOADED") == []


def test_an_edit_is_read_once_and_not_at_every_later_send(
    agent, templates, wire, monkeypatch
):
    """After one edit the next send re-reads; the ones after it must not, or a
    campaign reads the files and logs a reload for every contact."""
    path = templates / "email_template.html"
    _save(path, _with(path, EDIT))
    assert _send(agent)[0]
    reads = []
    real = Path.read_text

    def spy(self, *args, **kw):
        if self.name.startswith("email_template"):
            reads.append(self.name)
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", spy)
    assert _send(agent)[0]
    assert _send(agent)[0]
    assert reads == []
    assert all(EDIT in html for html in wire) and len(wire) == 3
    assert len(_rows(agent, "TEMPLATE_RELOADED")) == 1


def test_a_save_during_startup_is_still_noticed(
    settings_factory, tmp_path, templates, wire, monkeypatch
):
    """The saved version is noted BEFORE the reads, so a save between the two
    looks new at the next send and not like what was read."""
    path = templates / "email_template.html"
    real = FileHandler.get_template_variants

    def saved_meanwhile(self):
        variants = real(self)
        _save(path, _with(path, EDIT))
        return variants

    monkeypatch.setattr(FileHandler, "get_template_variants", saved_meanwhile)
    a = OutreachAgent(_settings(settings_factory, tmp_path))
    monkeypatch.setattr(FileHandler, "get_template_variants", real)
    assert EDIT not in a._email_template
    assert _send(a)[0]
    assert EDIT in wire[0]


def test_a_save_during_a_reload_is_still_noticed(agent, templates, wire, monkeypatch):
    path = templates / "email_template.html"
    _save(path, _with(path, "<p>THE-EARLIER-EDIT</p>"))
    real = FileHandler.get_template_variants

    def saved_meanwhile(self):
        variants = real(self)
        monkeypatch.setattr(FileHandler, "get_template_variants", real)
        _save(path, _with(path, EDIT))
        return variants

    monkeypatch.setattr(FileHandler, "get_template_variants", saved_meanwhile)
    assert _send(agent)[0]
    assert "THE-EARLIER-EDIT" in wire[0] and EDIT not in wire[0]
    assert _send(agent)[0]
    assert EDIT in wire[1], "the save landed during the read and was never noticed"


def test_a_template_created_after_startup_replaces_the_default(
    settings_factory, tmp_path, templates, wire
):
    """The default is only what the agent had to start with."""
    path = templates / "email_template.html"
    content = path.read_text(encoding="utf-8")
    path.unlink()
    a = OutreachAgent(_settings(settings_factory, tmp_path))
    assert _send(a)[0]
    assert DEFAULT_TEXT in wire[0]
    _save(path, content)
    assert _send(a)[0]
    assert DEFAULT_TEXT not in wire[1]
    assert REVIEWED in wire[1]


# -- A file that cannot be used ------------------------------------------------


def _blank(path):
    """How a save looks between its truncate and its write."""
    _save(path, b"")


def _spaces(path):
    _save(path, b"  \r\n\t ")


def _not_utf8(path):
    """A hand save in ANSI, the older default of Notepad on Windows."""
    _save(path, "Caffè {{legal_footer}} {{unsubscribe_url}}".encode("cp1252"))


def _deleted(path):
    path.unlink()


@pytest.mark.parametrize("damage", [_blank, _spaces, _not_utf8, _deleted])
def test_a_template_that_cannot_be_used_keeps_the_previous_text(
    agent, templates, wire, damage
):
    """Reviewed text beats a generic default nobody chose. The operator is told
    once per saved version, and the next good save is picked up."""
    path = templates / "email_template.html"
    good = _with(path, EDIT)
    damage(path)
    assert _send(agent)[0]
    assert _send(agent)[0]
    assert all(REVIEWED in html and DEFAULT_TEXT not in html for html in wire)
    assert len(_rows(agent, "TEMPLATE_RELOAD_FAILED")) == 1
    _save(path, good)
    assert _send(agent)[0]
    assert EDIT in wire[-1], "the completed save was not picked up"
    assert len(_rows(agent, "TEMPLATE_RELOADED")) == 1


def test_a_file_locked_when_first_read_counts_without_another_save(
    agent, templates, wire, monkeypatch
):
    """Another program held the file for a moment, and the file will not change
    again. Marking that version broken would lose the edit."""
    path = templates / "email_template.html"
    _save(path, _with(path, EDIT))
    real = Path.read_text
    locked = [True]

    def read(self, *args, **kw):
        if locked and self.name == "email_template.html":
            locked.clear()
            raise PermissionError("in use by another process")
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", read)
    assert _send(agent)[0]
    assert EDIT not in wire[0]
    assert _send(agent)[0]
    assert EDIT in wire[1], "the edit waited for a save that was not coming"


def test_a_file_that_cannot_be_checked_does_not_stop_the_send(agent, wire, monkeypatch):
    real = Path.stat

    def stat(self, **kw):
        if self.name == "email_template.html":
            raise OSError("the share went away")
        return real(self, **kw)

    monkeypatch.setattr(Path, "stat", stat)
    assert _send(agent)[0]
    assert REVIEWED in wire[0]


def test_a_variant_that_cannot_be_checked_does_not_hold_back_an_edit(
    agent, templates, wire, monkeypatch
):
    path = templates / "email_template.html"
    _save(templates / "email_template_b.html", _with(path, "<p>ARM-B</p>"))
    _save(path, _with(path, EDIT))
    real = Path.stat

    def stat(self, **kw):
        if self.name == "email_template_b.html":
            raise OSError("the share went away")
        return real(self, **kw)

    monkeypatch.setattr(Path, "stat", stat)
    assert _send(agent)[0]
    assert EDIT in wire[0]


def test_a_folder_that_cannot_be_listed_does_not_stop_the_send(
    agent, wire, monkeypatch
):
    real = Path.glob

    def glob(self, pattern, **kw):
        if pattern.startswith("email_template"):
            raise OSError("the share went away")
        return real(self, pattern, **kw)

    monkeypatch.setattr(Path, "glob", glob)
    assert _send(agent)[0]
    assert REVIEWED in wire[0]


# -- The legal footer guard still has the last word --------------------------


def _without_footer(path):
    return path.read_text(encoding="utf-8").replace("{{legal_footer}}", "")


def test_a_hand_edit_that_loses_the_footer_is_refused_at_the_send(
    agent, templates, wire
):
    """The editor refuses such a save; a hand edit never meets the editor."""
    path = templates / "email_template.html"
    _save(path, _without_footer(path))
    assert _send(agent)[0] is False
    assert wire == []
    assert len(_rows(agent, "LEGAL_FOOTER_MISSING")) == 1


def test_one_hand_edit_without_the_footer_is_reported_once(agent, templates, wire):
    """The file itself is fine, so it is one new version, not one per send:
    each refused contact must not re-read it and clear the guard's latch."""
    path = templates / "email_template.html"
    _save(path, _without_footer(path))
    for _ in range(3):
        assert _send(agent)[0] is False
    assert wire == []
    assert len(_rows(agent, "LEGAL_FOOTER_MISSING")) == 1
    assert len(_rows(agent, "TEMPLATE_RELOADED")) == 1


def test_each_version_that_loses_the_footer_is_reported(agent, templates):
    """The guard logs once while it stays tripped. Fixing the file ends that,
    or a second bad save would be refused with nothing on the dashboard."""
    path = templates / "email_template.html"
    good = path.read_text(encoding="utf-8")
    _save(path, _without_footer(path))
    assert _send(agent)[0] is False
    _save(path, good)
    assert _send(agent)[0]
    _save(path, _without_footer(path))
    assert _send(agent)[0] is False
    assert len(_rows(agent, "LEGAL_FOOTER_MISSING")) == 2


def test_rereading_a_broken_file_does_not_repeat_the_reports(agent, templates):
    """While a file stays broken every send reads again, which is not a new
    version: a campaign refused contact after contact must not bury the log."""
    path = templates / "email_template.html"
    _blank(templates / "email_template_b.html")
    _save(path, _without_footer(path))
    for _ in range(3):
        assert _send(agent)[0] is False
    assert len(_rows(agent, "LEGAL_FOOTER_MISSING")) == 1
    assert len(_rows(agent, "TEMPLATE_RELOAD_FAILED")) == 1


# -- The operator sees it ------------------------------------------------------


def test_the_reload_is_logged(agent, templates):
    """The operator sees that the edit reached the agent."""
    path = templates / "email_template.html"
    _save(path, _with(path, EDIT))
    assert _send(agent)[0]
    rows = _rows(agent, "TEMPLATE_RELOADED")
    assert len(rows) == 1
    assert "template" in rows[0]["details"].lower()


def test_a_locked_database_costs_neither_the_edit_nor_the_send(
    agent, templates, wire, monkeypatch
):
    path = templates / "email_template.html"
    real = agent.db.log_activity

    def log(action, *args, **kw):
        if action.startswith("TEMPLATE_"):
            raise sqlite3.OperationalError("database is locked")
        return real(action, *args, **kw)

    monkeypatch.setattr(agent.db, "log_activity", log)
    _save(path, _with(path, EDIT))
    assert _send(agent)[0]
    assert EDIT in wire[0]


# -- SUPPLECO's Italian first email ----------------------------------------------
#
# templates/email_template_it.html is a first-email template like the main one,
# and never an A/B arm. Counted among the saved versions and never read with the
# arms, it looked like a broken arm at every send: the templates were read again
# at each one, and an edit to the Italian email waited for a restart.


def test_an_edit_to_the_italian_first_email_reaches_the_next_direct_send(
    agent, templates, wire
):
    path = templates / "email_template_it.html"
    assert _send_it(agent)[0]
    _save(path, _with(path, EDIT_IT))
    assert _send_it(agent)[0]
    assert REVIEWED_IT in wire[0] and EDIT_IT not in wire[0]
    assert EDIT_IT in wire[1], "an edit to the Italian email needed a restart"
    assert len(_rows(agent, "TEMPLATE_RELOADED")) == 1
    assert _rows(agent, "TEMPLATE_RELOAD_FAILED") == []


def test_a_dashboard_direct_send_carries_the_italian_edit(
    settings_factory, tmp_path, templates, wire
):
    """The dashboard's Direct Send agent lives as long as the process."""
    path = templates / "email_template_it.html"
    client = main.create_dashboard_app(
        _settings(settings_factory, tmp_path)
    ).test_client()
    _dashboard_send(client, _italian_address())
    _save(path, _with(path, EDIT_IT))
    _dashboard_send(client, _italian_address())
    assert EDIT_IT not in wire[0]
    assert EDIT_IT in wire[1], "Direct Send kept the Italian text it started with"


def test_a_running_campaign_sends_the_italian_edit_from_its_next_email(
    agent, templates, wire
):
    a = agent
    path = templates / "email_template_it.html"
    sid = a.db.log_search("q", status="completed")
    a.db.add_contact(sid, email="anna@uno.it", organization="Rossi Uno")
    a.db.add_contact(sid, email="bruno@due.it", organization="Rossi Due")
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 1:
            _save(path, _with(path, EDIT_IT))
        if len(wire) >= 2 or len(naps) > 5:
            a._running = False

    a._interruptible_sleep = nap
    a.run_continuous(dry_run=False, loop_delay=60)
    assert len(wire) == 2
    assert EDIT_IT not in wire[0]
    assert EDIT_IT in wire[1], "the campaign kept the Italian text it started with"


@pytest.mark.parametrize("damage", [_blank, _spaces, _not_utf8])
def test_an_italian_file_that_cannot_be_used_keeps_its_text(
    agent, templates, wire, damage
):
    path = templates / "email_template_it.html"
    good = _with(path, EDIT_IT)
    damage(path)
    assert _send_it(agent)[0]
    assert _send_it(agent)[0]
    assert all(REVIEWED_IT in html for html in wire), "the Italian text was lost"
    assert len(_rows(agent, "TEMPLATE_RELOAD_FAILED")) == 1
    _save(path, good)
    assert _send_it(agent)[0]
    assert EDIT_IT in wire[-1], "the completed save was not picked up"
    assert len(_rows(agent, "TEMPLATE_RELOADED")) == 1


def test_an_italian_file_locked_when_first_read_counts_without_another_save(
    agent, templates, wire, monkeypatch
):
    path = templates / "email_template_it.html"
    _save(path, _with(path, EDIT_IT))
    real = Path.read_text
    locked = [True]

    def read(self, *args, **kw):
        if locked and self.name == "email_template_it.html":
            locked.clear()
            raise PermissionError("in use by another process")
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", read)
    assert _send_it(agent)[0]
    assert REVIEWED_IT in wire[0] and EDIT_IT not in wire[0]
    assert _send_it(agent)[0]
    assert EDIT_IT in wire[1], "the Italian edit waited for a save that was not coming"


def test_a_deleted_italian_file_sends_the_english_email_whole(
    agent, templates, wire, caplog
):
    """As at startup: Italian recipients get the English first email, said once."""
    path = templates / "email_template_it.html"
    content = path.read_text(encoding="utf-8")
    path.unlink()
    with caplog.at_level(logging.WARNING):
        assert _send_it(agent)[0]
        assert _send_it(agent)[0]
    assert all(REVIEWED in html and REVIEWED_IT not in html for html in wire)
    assert all('lang="en"' in html for html in wire)
    said = [r for r in caplog.records if "email_template_it.html" in r.getMessage()]
    assert len(said) == 1, [r.getMessage() for r in said]
    assert _rows(agent, "TEMPLATE_RELOAD_FAILED") == []
    _save(path, content)
    assert _send_it(agent)[0]
    assert REVIEWED_IT in wire[-1], "the Italian file put back was not picked up"


def test_the_italian_first_email_never_becomes_an_ab_arm(ab_agent, templates, wire):
    path = templates / "email_template_it.html"
    _save(path, _with(path, EDIT_IT))
    assert _send(ab_agent)[0]
    names = [name for name, _html in ab_agent._template_variants]
    assert names == ["email_template", "email_template_b"]
    assert "ARM-B-AS-STARTED" in wire[0] and EDIT_IT not in wire[0]
    assert _send_it(ab_agent)[0]
    assert EDIT_IT in wire[1]


def test_an_unchanged_italian_file_costs_one_listing_and_no_read(agent, monkeypatch):
    reads, listings = [], []
    real_read, real_glob = Path.read_text, Path.glob

    def read(self, *args, **kw):
        if self.name.startswith("email_template"):
            reads.append(self.name)
        return real_read(self, *args, **kw)

    def glob(self, pattern, **kw):
        if pattern.startswith("email_template"):
            listings.append(pattern)
        return real_glob(self, pattern, **kw)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "glob", glob)
    assert _send_it(agent)[0]
    assert _send(agent)[0]
    assert reads == [], "the templates were read again with nothing changed"
    assert len(listings) == 2, listings
    assert _rows(agent, "TEMPLATE_RELOAD_FAILED") == []


def test_an_italian_edit_is_read_once_and_not_at_every_later_send(
    agent, templates, wire, monkeypatch
):
    path = templates / "email_template_it.html"
    _save(path, _with(path, EDIT_IT))
    assert _send_it(agent)[0]
    reads = []
    real = Path.read_text

    def spy(self, *args, **kw):
        if self.name.startswith("email_template"):
            reads.append(self.name)
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", spy)
    assert _send_it(agent)[0]
    assert _send_it(agent)[0]
    assert reads == []
    assert all(EDIT_IT in html for html in wire) and len(wire) == 3
    assert len(_rows(agent, "TEMPLATE_RELOADED")) == 1


def test_an_italian_first_email_saved_in_the_editor_reaches_the_next_italian_send(
    settings_factory, tmp_path, templates, wire
):
    path = templates / "email_template_it.html"
    client = main.create_dashboard_app(
        _settings(settings_factory, tmp_path)
    ).test_client()
    _dashboard_send(client, _italian_address())
    saved = client.post(
        "/api/template",
        json={"name": "email_template_it", "content": _with(path, EDIT_IT)},
    )
    assert saved.status_code == 200, saved.get_json()
    english = (templates / "email_template.html").read_text(encoding="utf-8")
    assert EDIT_IT not in english, "the Italian text was saved over the English one"
    _dashboard_send(client, _italian_address())
    assert EDIT_IT not in wire[0]
    assert EDIT_IT in wire[1], "the editor said saved; the next Italian mail had the old text"
