"""The Italian email goes to Italian companies, and each contact keeps one language.

On 12 November 2026 SUPPLECO asked for the Italian first email to go to Italian
COMPANIES, not only to .it addresses: an Italian company writing from a .com
or a gmail address got the English email. A contact's language is now, in
this order: the language of the first email it was actually sent, so a thread
never switches; the operator's choice; Italian when its address or its
company's stored country is one ITALIAN_COUNTRIES lists; English otherwise.
Company evidence only adds Italian, since an Italian email to someone who
cannot read it is the worse mistake.

These tests hold the rule, what is stored for it, and what an erasure leaves.
The country lock stays on the address: company evidence never unlocks a send.
"""

import sqlite3

import pytest

from agent import OutreachAgent
from modules import compliance
from modules.compliance import (
    country_refusal,
    language_and_reason,
    notice_language,
    website_country,
)
from modules.database import SCHEMA_SQL, Database
from modules.export import DataExporter


def _rule(settings, email, sent=None, **contact):
    return language_and_reason(settings, email, contact, sent)


# ── The rule ───────────────────────────────────────────────────────────────


def test_an_italian_address_is_italian(settings_factory):
    assert _rule(settings_factory(), "info@rossi.it") == ("it", "address")


@pytest.mark.parametrize("source", ["website", "page", "ai"])
def test_an_italian_company_on_a_com_address_is_italian(settings_factory, source):
    got = _rule(
        settings_factory(),
        "acquisti@gmail.com",
        company_country="IT",
        company_country_source=source,
    )
    assert got == ("it", source)


def test_company_evidence_adds_italian_to_a_foreign_address(settings_factory):
    s = settings_factory()
    italian = _rule(
        s, "einkauf@nordbrand.de", company_country="IT", company_country_source="page"
    )
    assert italian == ("it", "page")
    german = _rule(
        s, "einkauf@nordbrand.de", company_country="DE", company_country_source="page"
    )
    assert german == ("en", "default")


def test_company_evidence_never_takes_italian_away(settings_factory):
    got = _rule(
        settings_factory(),
        "info@rossi.it",
        company_country="DE",
        company_country_source="website",
    )
    assert got == ("it", "address")


def test_a_country_with_no_source_is_not_evidence(settings_factory):
    assert _rule(settings_factory(), "a@brand.com", company_country="IT") == (
        "en",
        "default",
    )


def test_the_operators_choice_comes_before_address_and_company(settings_factory):
    s = settings_factory()
    assert _rule(s, "info@rossi.it", language_choice="en") == ("en", "operator")
    assert _rule(s, "einkauf@brand.de", language_choice="it") == ("it", "operator")


def test_the_first_emails_language_comes_before_everything(settings_factory):
    s = settings_factory()
    assert _rule(s, "info@rossi.it", sent="en", language_choice="it") == (
        "en",
        "sent",
    )
    assert _rule(
        s,
        "a@brand.com",
        sent="it",
        language_choice="en",
        company_country="DE",
        company_country_source="website",
    ) == ("it", "sent")


def test_italian_countries_decides_which_companies_are_italian(settings_factory):
    nobody = settings_factory(ITALIAN_COUNTRIES="")
    assert _rule(
        nobody, "a@brand.com", company_country="IT", company_country_source="ai"
    ) == ("en", "default")
    wider = settings_factory(ITALIAN_COUNTRIES="IT,SM")
    assert _rule(
        wider, "a@brand.com", company_country="SM", company_country_source="ai"
    ) == ("it", "ai")


def test_email_language_given_an_address_alone_is_todays_rule(settings_factory):
    s = settings_factory()
    assert compliance.email_language(s, "info@rossi.it") == "it"
    assert compliance.email_language(s, "info@rossi.com") == "en"


@pytest.mark.parametrize(
    "version,language",
    [
        ("notice-li-1-it", "it"),
        ("notice-consent-1-it", "it"),
        ("notice-li-1", "en"),
        (None, "en"),
        ("", "en"),
    ],
)
def test_the_notice_version_says_the_language_an_email_went_in(version, language):
    assert notice_language(version) == language


@pytest.mark.parametrize(
    "url,country",
    [
        ("https://www.rossi-integratori.it/contatti", "IT"),
        ("https://WWW.BRAND.DE:8443/x", "DE"),
        ("https://www.brand.co.uk/", "GB"),
        ("https://brand.com/", None),
        ("https://brand.eu/", None),
        ("https://brand.io/", None),
        ("https://brand.ai/", None),
        ("http://192.168.0.1/", None),
        ("", None),
        (None, None),
        ("http://[bad", None),
    ],
)
def test_the_website_country_reads_the_company_websites_domain(url, country):
    assert website_country(url) == country


# ── The words the dashboard shows ──────────────────────────────────────────


@pytest.mark.parametrize(
    "language,reason,words",
    [
        ("en", "default", "English"),
        ("it", "address", "Italian (its address ends in .it)"),
        ("it", "website", "Italian (its company website ends in .it)"),
        ("it", "page", "Italian (its company's page shows an Italian company)"),
        ("it", "ai", "Italian (the AI's reading of its company)"),
        ("it", "operator", "Italian (set by you)"),
        ("en", "operator", "English (set by you)"),
        ("it", "sent", "Italian (the language of its first email)"),
        ("en", "sent", "English (the language of its first email)"),
    ],
)
def test_the_reason_is_said_in_plain_words(language, reason, words):
    contact = {"source_url": "https://www.rossi.it/contatti"}
    said = compliance.language_reason_words(language, reason, "a@rossi.it", contact)
    assert said == words


# ── What was sent decides ──────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "lang.db"))


def _contact(db, email="buyer@brand.com", **kw):
    return db.add_contact(db.log_search("q", source="test"), email, **kw)


def test_nothing_sent_has_no_first_language(db):
    cid = _contact(db)
    assert db.first_sent_language(cid) is None
    for status in ("dry_run", "failed", "rejected"):
        db.log_email(
            cid,
            "buyer@brand.com",
            "s",
            delivery_status=status,
            notice_version="notice-li-1-it",
        )
    assert db.first_sent_language(cid) is None


def test_a_send_under_way_fixes_the_language(db):
    cid = _contact(db)
    db.log_email(
        cid,
        "buyer@brand.com",
        "s",
        delivery_status="preparing",
        notice_version="notice-li-1-it",
    )
    assert db.first_sent_language(cid) == "it"


def test_the_first_email_decides_not_the_latest(db):
    cid = _contact(db)
    db.log_email(
        cid,
        "buyer@brand.com",
        "s",
        delivery_status="sent",
        notice_version="notice-li-1-it",
    )
    db.log_email(
        cid,
        "buyer@brand.com",
        "s2",
        delivery_status="sent",
        notice_version="notice-li-1",
    )
    assert db.first_sent_language(cid) == "it"
    other = _contact(db, "x@other.com")
    db.log_email(
        other, "x@other.com", "s", delivery_status="sent", notice_version="notice-li-1"
    )
    db.log_email(
        other,
        "x@other.com",
        "s2",
        delivery_status="sent_unconfirmed",
        notice_version="notice-li-1-it",
    )
    assert db.first_sent_language(other) == "en"


def test_a_sent_email_with_no_notice_version_went_in_english(db):
    cid = _contact(db)
    db.log_email(cid, "buyer@brand.com", "s", delivery_status="sent")
    assert db.first_sent_language(cid) == "en"


def test_the_list_answer_agrees_with_the_single_one(db):
    a, b, c = (_contact(db, f"p{i}@brand{i}.com") for i in range(3))
    db.log_email(
        a, "p0@brand0.com", "s", delivery_status="sent", notice_version="notice-li-1-it"
    )
    db.log_email(
        b, "p1@brand1.com", "s", delivery_status="sent", notice_version="notice-li-1"
    )
    many = db.first_sent_languages([a, b, c])
    assert many == {a: "it", b: "en"}
    assert [db.first_sent_language(i) for i in (a, b, c)] == ["it", "en", None]


def test_language_for_reads_the_stored_contact_and_its_first_email(
    settings_factory, db
):
    s = settings_factory()
    cid = _contact(
        db,
        company_country="IT",
        company_country_source="website",
        source_url="https://www.brand.it/",
    )
    assert compliance.language_for(s, db, {"id": cid}) == ("it", "website")
    # Looked up by address, as the unsubscribe page does.
    assert compliance.language_for(s, db, email="BUYER@brand.com") == (
        "it",
        "website",
    )
    db.log_email(
        cid,
        "buyer@brand.com",
        "s",
        delivery_status="sent",
        notice_version="notice-li-1",
    )
    assert compliance.language_for(s, db, {"id": cid}) == ("en", "sent")
    # An address the agent does not hold: its address decides.
    assert compliance.language_for(s, db, email="new@rossi.it") == ("it", "address")
    assert compliance.language_for(s, db, email="new@rossi.com") == ("en", "default")


# ── The operator's choice ──────────────────────────────────────────────────


def _choice(db, cid):
    return db.get_contact_by_id(cid)["language_choice"]


def test_the_operator_chooses_before_any_send(db):
    cid = _contact(db)
    assert db.set_language_choice(cid, "it") is True
    assert _choice(db, cid) == "it"
    assert db.set_language_choice(cid, None) is True
    assert _choice(db, cid) is None


def test_the_operator_cannot_choose_once_an_email_may_have_gone(db):
    cid = _contact(db)
    db.log_email(cid, "buyer@brand.com", "s", delivery_status="preparing")
    assert db.set_language_choice(cid, "en") is False
    assert _choice(db, cid) is None


def test_a_dry_run_does_not_stop_the_choice(db):
    cid = _contact(db)
    db.log_email(cid, "buyer@brand.com", "s", delivery_status="dry_run")
    assert db.set_language_choice(cid, "en") is True


def test_an_erased_contact_takes_no_choice(db):
    cid = _contact(db)
    DataExporter(db).gdpr_erase("buyer@brand.com")
    assert db.set_language_choice(cid, "it") is False


def test_only_italian_or_english_can_be_chosen(db):
    cid = _contact(db)
    with pytest.raises(ValueError):
        db.set_language_choice(cid, "fr")


# ── Storing the company country ────────────────────────────────────────────


def _country(db, cid):
    row = db.get_contact_by_id(cid)
    return row["company_country"], row["company_country_source"]


def test_a_contact_is_stored_with_its_company_country(db):
    cid = _contact(db, company_country="it", company_country_source="website")
    assert _country(db, cid) == ("IT", "website")
    plain = _contact(db, "x@plain.com")
    assert _country(db, plain) == (None, None)


def test_a_country_and_its_source_come_together(db):
    with pytest.raises(ValueError):
        _contact(db, company_country_source="page")
    with pytest.raises(ValueError):
        _contact(db, "y@brand.com", company_country="IT")
    with pytest.raises(ValueError):
        _contact(
            db, "z@brand.com", company_country="IT", company_country_source="guess"
        )


def test_a_country_that_is_not_a_code_is_not_stored(db):
    cid = _contact(db, company_country="Italy", company_country_source="page")
    assert _country(db, cid) == (None, None)


def test_the_first_evidence_stands(db):
    cid = _contact(db)
    assert db.set_company_country(cid, "IT", "page") is True
    assert db.set_company_country(cid, "DE", "ai") is False
    assert _country(db, cid) == ("IT", "page")


def test_an_unknown_answer_is_recorded_so_it_is_not_asked_again(db):
    cid = _contact(db)
    assert db.set_company_country(cid, None, "ai") is True
    assert _country(db, cid) == (None, "ai")
    assert db.contacts_needing_company_country() == []


def test_contacts_needing_a_country_leave_out_the_emailed_and_the_erased(db):
    pending = _contact(db, "a@one.com")
    emailed = _contact(db, "b@two.com")
    db.log_email(emailed, "b@two.com", "s", delivery_status="sent")
    _contact(db, "c@three.com")
    DataExporter(db).gdpr_erase("c@three.com")
    known = _contact(
        db, "d@four.com", company_country="IT", company_country_source="website"
    )
    dry = _contact(db, "e@five.com")
    db.log_email(dry, "e@five.com", "s", delivery_status="dry_run")
    ids = [c["id"] for c in db.contacts_needing_company_country()]
    assert ids == [pending, dry]
    assert known not in ids


# ── An existing database gains the columns ─────────────────────────────────


def test_a_database_made_before_the_change_gains_the_columns(tmp_path):
    path = tmp_path / "old.db"
    old = SCHEMA_SQL
    for column in ("company_country", "company_country_source", "language_choice"):
        line = next(
            ln for ln in old.splitlines() if ln.strip().startswith(column + " ")
        )
        old = old.replace(line + "\n", "")
    raw = sqlite3.connect(path)
    raw.executescript(old)
    columns = {r[1] for r in raw.execute("PRAGMA table_info(contacts)")}
    raw.close()
    assert "language_choice" not in columns
    db = Database(str(path))
    columns = {r[1] for r in db.conn.execute("PRAGMA table_info(contacts)")}
    assert {"company_country", "company_country_source", "language_choice"} <= columns


# ── Erasure ────────────────────────────────────────────────────────────────


def test_an_erasure_clears_the_country_and_the_choice(db):
    cid = _contact(db, company_country="IT", company_country_source="website")
    db.set_language_choice(cid, "it")
    DataExporter(db).gdpr_erase("buyer@brand.com")
    row = db.get_contact_by_id(cid)
    assert row["status"] == "erased"
    assert (row["company_country"], row["company_country_source"]) == (None, None)
    assert row["language_choice"] is None


# ── The country lock stays on the address ──────────────────────────────────


def test_an_italian_company_does_not_unlock_a_com_address(
    settings_factory, tmp_path, monkeypatch
):
    s = settings_factory(
        db_path=str(tmp_path / "a.db"),
        ENABLED_COUNTRIES="IT",
        UNKNOWN_COUNTRY_ALLOWED=False,
        verify_emails=False,
        send_window_enabled=False,
        blacklist_check_enabled=False,
        bounce_guard_enabled=False,
    )
    assert country_refusal(s, "acquisti@gmail.com") is not None
    agent = OutreachAgent(s)
    sent = []
    monkeypatch.setattr(
        agent.email_sender, "dispatch_to_contact", lambda **k: sent.append(k) or 1
    )
    cid = agent.db.add_contact(
        agent.db.log_search("q"),
        "acquisti@gmail.com",
        organization="Rossi Integratori S.p.A.",
        company_country="IT",
        company_country_source="page",
    )
    contact = agent.db.get_contact_by_id(cid)
    assert compliance.language_for(s, agent.db, contact) == ("it", "page")
    assert agent._process_contact(contact, dry_run=False) is False
    assert sent == []
    # The same send path does email an address in an enabled country.
    italian = agent.db.add_contact(agent.db.log_search("q"), "acquisti@rossi.it")
    assert agent._process_contact(agent.db.get_contact_by_id(italian), dry_run=False)
    assert [k["contact"]["email"] for k in sent] == ["acquisti@rossi.it"]
