"""The inbox monitor reads the junk folder too.

Only INBOX was scanned. Exchange's default anti-spam action moves suspected
spam to Junk Email, so an opt-out, a data request or a bounce filed there
was never processed (GDPR Art. 21). Now the monitor also reads the folder the
server marks as junk (RFC 6154 \\Junk), or else one with a known junk name,
read-only and with its own watermark, and processes what it finds exactly
like INBOX. A server without one is read as before. While a junk folder that
exists cannot be read, the account's read does not count as complete, so the
send check holds live mail as it does for an unread inbox.
"""

from modules.database import Database
from modules.imap_monitor import ImapMonitor, watermark_key
from tests.test_imap_inbox_scan import _raw

ANNA = "anna@brand.it"
OPT_OUT = "Please remove me from your mailing list."


class FolderIMAP:
    """A fake server with folders. Every command is recorded, so a test can
    show that nothing is ever moved, flagged or deleted."""

    def __init__(self, folders, junk_flag=None, select_fails=()):
        self.folders = folders  # name -> list of raw messages
        self.junk_flag = junk_flag
        self.select_fails = set(select_fails)
        self.commands = []
        self.current = None

    def list(self, *args):
        self.commands.append("LIST")
        lines = []
        for name in self.folders:
            flag = " \\Junk" if name == self.junk_flag else ""
            lines.append(f'(\\HasNoChildren{flag}) "/" "{name}"'.encode())
        return "OK", lines

    def select(self, folder, readonly=False):
        name = folder.strip('"')
        self.commands.append(("SELECT", name, readonly))
        if name in self.select_fails or name not in self.folders:
            return "NO", [b"no such folder"]
        self.current = name
        return "OK", [str(len(self.folders[name])).encode()]

    def search(self, charset, criteria):
        self.commands.append("SEARCH")
        ids = b" ".join(str(i + 1).encode() for i in range(len(self.folders[self.current])))
        return "OK", [ids]

    def fetch(self, msg_id, parts):
        self.commands.append("FETCH")
        return "OK", [(b"1 (RFC822 {1})", self.folders[self.current][int(msg_id) - 1])]

    def logout(self):
        self.commands.append("LOGOUT")
        return "BYE", [b""]


def _monitor(tmp_path, fake):
    db = Database(str(tmp_path / "j.db"))
    monitor = ImapMonitor(db, "imap.test", 993, "me@corp.com", "pw")
    monitor._connect = lambda: fake
    return monitor, db


def _opt_out():
    return _raw(ANNA, "Re: hello", OPT_OUT)


def test_an_opt_out_filed_as_junk_is_honoured(tmp_path):
    fake = FolderIMAP({"INBOX": [], "Junk Email": [_opt_out()]}, junk_flag="Junk Email")
    monitor, db = _monitor(tmp_path, fake)

    summary = monitor.run_check()

    assert db.is_suppressed(ANNA)
    assert summary["ok"] is True
    assert ("SELECT", "Junk Email", True) in fake.commands


def test_the_folder_the_server_marks_as_junk_is_read_whatever_its_name(tmp_path):
    fake = FolderIMAP({"INBOX": [], "Filtrata": [_opt_out()]}, junk_flag="Filtrata")
    monitor, db = _monitor(tmp_path, fake)

    monitor.run_check()

    assert db.is_suppressed(ANNA)


def test_a_junk_folder_the_server_does_not_mark_is_found_by_its_name(tmp_path):
    fake = FolderIMAP({"INBOX": [], "Junk": [_opt_out()]})
    monitor, db = _monitor(tmp_path, fake)

    monitor.run_check()

    assert db.is_suppressed(ANNA)


def test_every_folder_is_only_read(tmp_path):
    fake = FolderIMAP({"INBOX": [_opt_out()], "Junk Email": [_opt_out()]}, junk_flag="Junk Email")
    monitor, _ = _monitor(tmp_path, fake)

    monitor.run_check()

    selects = [c for c in fake.commands if isinstance(c, tuple)]
    assert selects and all(readonly for _, _, readonly in selects)
    assert {c if isinstance(c, str) else c[0] for c in fake.commands} <= {
        "LIST",
        "SELECT",
        "SEARCH",
        "FETCH",
        "LOGOUT",
    }


def test_a_server_without_a_junk_folder_is_read_as_before(tmp_path):
    fake = FolderIMAP({"INBOX": [_opt_out()]})
    monitor, db = _monitor(tmp_path, fake)

    summary = monitor.run_check()

    assert db.is_suppressed(ANNA)
    assert summary["ok"] is True
    assert db.get_state(watermark_key("me@corp.com"))


def test_each_folder_keeps_its_own_watermark(tmp_path):
    fake = FolderIMAP({"INBOX": [], "Junk Email": []}, junk_flag="Junk Email")
    monitor, db = _monitor(tmp_path, fake)

    monitor.run_check()

    inbox_mark = db.get_state(watermark_key("me@corp.com"))
    junk_mark = db.get_state(watermark_key("me@corp.com") + "::junk")
    assert inbox_mark and junk_mark


def test_an_unreadable_junk_folder_keeps_the_read_incomplete(tmp_path):
    fake = FolderIMAP(
        {"INBOX": [], "Junk Email": [_opt_out()]},
        junk_flag="Junk Email",
        select_fails={"Junk Email"},
    )
    monitor, db = _monitor(tmp_path, fake)

    summary = monitor.run_check()

    assert summary["ok"] is False
    assert not db.get_state(watermark_key("me@corp.com"))
