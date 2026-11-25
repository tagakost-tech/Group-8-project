"""Every contact records, when it is stored, where it came from and why it may
be emailed.

Only 282 of the prototype's 1,017 contacts had a basis record, written at
qualification or at a Direct Send, and none said whether the address came
from the company's own website or a third-party directory (decision L1.4).
Art. 14(2)(f) requires telling each person where their data came from. A
Direct Send recorded "legitimate interest" whatever the operator knew about
the contact (L1.5).

Now the contact row itself carries, from the INSERT that stores it:
- source_channel: the company's own website, a contact page on it, a
  third-party directory, or for a manual entry the channel it came through;
- basis: the lawful basis that channel gives (the developer's proposal);
- rule_version: the version of those rules, "rules-draft-1" until counsel
  signs them.
A contact without a channel is refused, and every production call site names
its channel. A live Direct Send needs one of L1.5's channels.
"""

import ast
import pathlib

import pytest

from agent import OutreachAgent
from modules import database
from modules.compliance import MANUAL_CHANNELS, RULE_VERSION, SOURCE_CHANNELS
from modules.database import Database
from modules.search import DEFAULT_BLOCKED_PREFIXES, SearchModule

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def db(settings_factory, tmp_path):
    return Database(settings_factory(db_path=str(tmp_path / "c.db")).get_abs_db_path())


def _row(db, cid):
    return dict(
        db.conn.execute(
            "SELECT source_channel, basis, rule_version FROM contacts WHERE id = ?",
            (cid,),
        ).fetchone()
    )


@pytest.mark.parametrize(
    "channel,basis",
    [
        ("own_website", "legitimate_interest"),
        ("contact_page", "legitimate_interest"),
        ("third_party", "legitimate_interest"),
        ("inbound_request", "consent"),
        ("existing_customer", "existing_customer"),
    ],
)
def test_the_channel_its_basis_and_the_rules_version_are_stored(db, channel, basis):
    cid = db.add_contact(db.log_search("q"), "anna@brand.de", source_channel=channel)
    assert _row(db, cid) == {
        "source_channel": channel,
        "basis": basis,
        "rule_version": RULE_VERSION,
    }
    assert RULE_VERSION == "rules-draft-1"


def test_a_contact_without_a_channel_is_refused(db, monkeypatch):
    monkeypatch.setattr(database, "_default_source_channel", None)
    with pytest.raises(ValueError):
        db.add_contact(db.log_search("q"), "anna@brand.de")
    with pytest.raises(ValueError):
        db.add_contact(db.log_search("q"), "anna@brand.de", source_channel="google")
    assert db.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0


def test_every_production_call_names_its_channel():
    """The test default must never be what a real contact is stored with."""
    calls = []
    for path in [
        *ROOT.joinpath("modules").glob("*.py"),
        ROOT / "agent.py",
        ROOT / "main.py",
    ]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "add_contact"
            ):
                calls.append(path.name)
                assert any(k.arg == "source_channel" for k in node.keywords), (
                    f"{path.name}:{node.lineno} stores a contact without its channel"
                )
    assert sorted(calls) == [
        "ai_proof.py", "dispatch_pipeline.py", "lead_import.py", "search.py"
    ]


def test_the_manual_channels_are_l15s_list():
    assert set(MANUAL_CHANNELS) == {
        "inbound_request",
        "trade_fair",
        "existing_customer",
        "referral",
    }
    assert set(MANUAL_CHANNELS) < set(SOURCE_CHANNELS)


# ── Collection stamps where each address was found ──


class _Resp:
    def __init__(self, body, status=200):
        self.content = body.encode("utf-8")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "err", request=None, response=type("R", (), {"status_code": 404})()
            )


class _Client:
    def __init__(self, pages):
        self.pages = {k.rstrip("/"): v for k, v in pages.items()}

    def get(self, url):
        body = self.pages.get(url.rstrip("/"))
        return _Resp(body) if body is not None else _Resp("", 404)


def _module(pages):
    sm = SearchModule.__new__(SearchModule)
    sm._client = _Client(pages)
    sm.rate_limit = 0.0
    sm.max_results = 10
    sm.blocked_prefixes = DEFAULT_BLOCKED_PREFIXES
    sm._robots_cache = {}
    sm._robots_allows = lambda url: True
    return sm


def _page(title, address):
    return (
        f"<html><head><title>{title}</title></head><body>"
        f'<a href="mailto:{address}">{address}</a></body></html>'
    )


def test_an_address_on_the_companys_home_page_is_its_own_website():
    url = "https://www.brandco.de/"
    found = _module({url: _page("Brandco", "info@brandco.de")}).scrape_site(url)
    assert [c["source_channel"] for c in found] == ["own_website"]


def test_an_address_on_the_companys_contact_page_is_a_contact_page():
    url = "https://www.brandco.de/kontakt"
    found = _module({url: _page("Brandco", "info@brandco.de")}).scrape_site(url)
    assert [c["source_channel"] for c in found] == ["contact_page"]


def test_an_address_found_one_hop_away_is_a_contact_page():
    home = "https://www.brandco.de/"
    pages = {
        home: "<html><head><title>Brandco</title></head><body>Hello</body></html>",
        "https://www.brandco.de/kontakt": _page("Kontakt", "info@brandco.de"),
    }
    found = _module(pages).scrape_site(home)
    assert [c["source_channel"] for c in found] == ["contact_page"]


def test_an_address_on_a_directory_is_third_party():
    url = "https://www.europages.co.uk/Nutrivita/00000012345.html"
    title = "Nutrivita Srl - Produttore di integratori alimentari | Europages"
    found = _module({url: _page(title, "acquisti@nutrivita.it")}).scrape_site(url)
    assert [c["source_channel"] for c in found] == ["third_party"]


def test_a_searched_contact_is_stored_with_its_channel(db, monkeypatch):
    sm = _module({})
    sm.db = db
    sm._ddg_search = lambda q: ["https://www.europages.co.uk/x"]
    sm.scrape_site = lambda url: [
        {
            "email": "acquisti@nutrivita.it",
            "organization": "Nutrivita",
            "source_url": url,
            "source_channel": "third_party",
        }
    ]
    monkeypatch.setattr(database, "_default_source_channel", None)

    stored = sm.search_and_extract("nutrivita integratori")

    assert _row(db, stored[0]["id"])["source_channel"] == "third_party"


# ── A live Direct Send needs one of L1.5's channels ──


def _agent(settings_factory, tmp_path, monkeypatch):
    agent = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "a.db"),
            verify_emails=False,
            send_window_enabled=False,
            blacklist_check_enabled=False,
            bounce_guard_enabled=False,
            ai_humanize_emails=False,
        )
    )
    monkeypatch.setattr(agent.email_sender, "dispatch_to_contact", lambda **k: 1)
    return agent


def test_a_direct_send_without_a_channel_is_refused(
    settings_factory, tmp_path, monkeypatch
):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    monkeypatch.setattr(database, "_default_source_channel", None)

    ok, message = agent.send_single_email("anna@brand.de")
    assert ok is False and "channel" in message.lower()
    assert agent.db.get_contact_by_email("anna@brand.de") is None

    ok, _ = agent.send_single_email("anna@brand.de", dry_run=True)
    assert ok is True, "a preview contacts nobody and needs no channel"


def test_a_direct_send_stores_its_channel(settings_factory, tmp_path, monkeypatch):
    agent = _agent(settings_factory, tmp_path, monkeypatch)
    monkeypatch.setattr(database, "_default_source_channel", None)

    ok, _ = agent.send_single_email("anna@brand.de", channel="trade_fair")

    assert ok is True
    stored = agent.db.get_contact_by_email("anna@brand.de")
    assert _row(agent.db, stored["id"])["source_channel"] == "trade_fair"


def test_the_dashboard_passes_the_channel(settings_factory, tmp_path, monkeypatch):
    import main

    seen = {}
    monkeypatch.setattr(
        OutreachAgent,
        "send_single_email",
        lambda self, **k: seen.update(k) or (True, "sent"),
    )
    client = main.create_dashboard_app(
        settings_factory(db_path=str(tmp_path / "d.db"))
    ).test_client()
    client.post(
        "/api/send-manual", json={"email": "anna@brand.de", "channel": "referral"}
    )
    assert seen["channel"] == "referral"
