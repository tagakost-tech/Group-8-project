"""main.py company-country: the company country of contacts already stored.

Contacts collected before the company counted for the language have no
country stored, so an Italian company on a .com address would still get the
English email. The command reads what is stored about each contact not yet
emailed (its website, organisation and page description) and records what
it shows. It calls the AI only when asked to (--ai), so a plain run makes no
network call, and --dry-run changes nothing. A contact already emailed keeps
its first email's language, and an erased one is never touched.
"""

import argparse

import pytest

import main
from modules import ai_generator
from modules.database import Database
from modules.export import DataExporter


@pytest.fixture
def setup(settings_factory, tmp_path):
    s = settings_factory(db_path=str(tmp_path / "cmd.db"))
    return s, Database(s.get_abs_db_path())


def _add(db, email, url, channel="own_website", **kw):
    return db.add_contact(
        db.log_search("q"), email, source_url=url, source_channel=channel, **kw
    )


def _run(s, capsys, ai=False, dry_run=False):
    main.cmd_company_country(argparse.Namespace(ai=ai, dry_run=dry_run), s)
    return capsys.readouterr().out


def _country(db, cid):
    row = db.get_contact_by_id(cid)
    return row["company_country"], row["company_country_source"]


class _NoAI:
    def __init__(self, *a, **k):
        raise AssertionError("the AI was built without --ai")


class _FakeAI:
    calls = 0

    def __init__(self, *a, **k):
        self.is_configured = True

    def classify_company_country(self, contact):
        type(self).calls += 1
        return "IT"


def test_stored_contacts_get_the_country_their_company_shows(
    setup, capsys, monkeypatch
):
    s, db = setup
    monkeypatch.setattr(ai_generator, "AIGenerator", _NoAI)
    website = _add(db, "info@rossi.com", "https://www.rossi.it/")
    page = _add(
        db, "a@bianchi.com", "https://www.bianchi.com/", organization="Bianchi S.p.A."
    )
    emailed = _add(db, "b@verdi.com", "https://www.verdi.it/")
    db.log_email(emailed, "b@verdi.com", "s", delivery_status="sent")
    erased = _add(db, "c@neri.com", "https://www.neri.it/")
    DataExporter(db).gdpr_erase("c@neri.com")
    directory = _add(
        db, "d@brandco.de", "https://www.europages.it/x", channel="third_party"
    )
    plain = _add(db, "e@plain.com", "https://www.plain.com/")

    out = _run(s, capsys)

    assert _country(db, website) == ("IT", "website")
    assert _country(db, page) == ("IT", "page")
    for untouched in (emailed, erased, directory, plain):
        assert _country(db, untouched) == (None, None)
    lines = {ln.split()[0]: ln.split()[-1] for ln in out.splitlines()[1:]}
    assert lines["checked"] == "4"
    assert lines["website"] == "1" and lines["page"] == "1"
    assert lines["still"] == "2"
    assert lines["get"] == "2"
    for label in ("ai country", "ai no answer"):
        assert label in out


def test_the_ai_is_asked_only_with_ai_and_only_once(setup, capsys, monkeypatch):
    s, db = setup
    _FakeAI.calls = 0
    monkeypatch.setattr(ai_generator, "AIGenerator", _FakeAI)
    cid = _add(db, "info@brand.com", "https://www.brand.com/")
    _run(s, capsys, ai=True)
    assert _FakeAI.calls == 1
    assert _country(db, cid) == ("IT", "ai")
    _run(s, capsys, ai=True)
    assert _FakeAI.calls == 1


def test_no_contact_closed_to_outreach_goes_to_the_ai(setup, capsys, monkeypatch):
    """An objector, a client, a bounced or discarded address will never get
    a first email, yet --ai sent each one's company to the provider."""
    s, db = setup
    asked = []

    class _Recording(_FakeAI):
        def classify_company_country(self, contact):
            asked.append(contact["email"])
            return "IT"

    monkeypatch.setattr(ai_generator, "AIGenerator", _Recording)
    closed = {}
    for status in ("unsubscribed", "blocked", "active_client", "bounced", "irrelevant"):
        cid = _add(db, f"buyer@{status.replace('_', '')}.com", "https://x.com/")
        db.update_contact_status(cid, status)
        closed[status] = cid
    objector = _add(db, "buyer@objector.com", "https://objector.com/")
    db.add_to_suppression("buyer@objector.com", reason="unsubscribe")
    colleague = _add(db, "buyer@saidno.com", "https://saidno.com/")
    db.block_domain("saidno.com", "manual")
    pending = _add(db, "buyer@pending.com", "https://pending.com/")

    out = _run(s, capsys, ai=True)

    assert asked == ["buyer@pending.com"]
    assert _country(db, pending) == ("IT", "ai")
    for cid in (*closed.values(), objector, colleague):
        assert _country(db, cid) == (None, None)
    lines = {ln.split()[0]: ln.split()[-1] for ln in out.splitlines()[1:]}
    assert lines["checked"] == "1"


def test_without_ai_no_client_is_built(setup, capsys, monkeypatch):
    s, db = setup
    monkeypatch.setattr(ai_generator, "AIGenerator", _NoAI)
    cid = _add(db, "info@brand.com", "https://www.brand.com/")
    _run(s, capsys)
    assert _country(db, cid) == (None, None)


def test_the_setting_off_keeps_the_ai_out(
    settings_factory, tmp_path, capsys, monkeypatch
):
    s = settings_factory(db_path=str(tmp_path / "off.db"), company_country_ai=False)
    db = Database(s.get_abs_db_path())
    monkeypatch.setattr(ai_generator, "AIGenerator", _NoAI)
    _add(db, "info@brand.com", "https://www.brand.com/")
    assert "COMPANY_COUNTRY_AI is off" in _run(s, capsys, ai=True)


def test_a_dry_run_changes_nothing_and_still_counts(setup, capsys, monkeypatch):
    s, db = setup
    monkeypatch.setattr(ai_generator, "AIGenerator", _NoAI)
    _add(db, "info@rossi.com", "https://www.rossi.it/")
    _add(db, "a@brand.com", "https://www.brand.com/")
    before = db.conn.execute("SELECT * FROM contacts ORDER BY id").fetchall()
    out = _run(s, capsys, ai=True, dry_run=True)
    after = db.conn.execute("SELECT * FROM contacts ORDER BY id").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert "Dry run" in out
    lines = {ln.split()[0]: ln.split()[-1] for ln in out.splitlines()[1:]}
    assert lines["website"] == "1"


def test_the_command_is_on_the_command_line(setup, monkeypatch, capsys):
    s, db = setup
    monkeypatch.setattr(ai_generator, "AIGenerator", _NoAI)
    cid = _add(db, "info@rossi.com", "https://www.rossi.it/")
    monkeypatch.setattr(main, "load_settings", lambda: s)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["main.py", "company-country"])
    main.main()
    assert "Company countries worked out" in capsys.readouterr().out
    assert _country(db, cid) == ("IT", "website")
