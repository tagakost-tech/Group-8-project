"""When nothing collected shows a company's country, the AI may be asked.

The address, the company website and its page leave many companies unknown:
a .com site with no VAT number in reach. COMPANY_COUNTRY_AI (on by default)
lets the AI be asked for those, once per contact, just before its first email
is composed. It is sent no more than the fit check (classify_relevance) sends
about a company - its name, its address's domain and 400 characters of its
page description - and nothing at all without a key and AI_DPA_REFERENCE. It
must answer one ISO code or UNKNOWN; anything else is read as unknown. An
error, a timeout or no key never stops a contact being qualified or emailed:
the contact simply gets its address's language. And an AI guess never
unlocks the country lock, which reads only the address.
"""

import pytest
from test_first_email_language import _agent as _sending_agent
from test_first_email_language import _lines

from agent import OutreachAgent
from config import Settings
from modules import compliance
from modules.ai_generator import AIGenerator
from modules.company_country import parse_country_answer, settle_company_country
from modules.database import Database

COM = "acquisti@rossi-integratori.com"


def _ai(answer="IT", allowed=True, fails=False):
    """A real generator whose provider call is scripted, so the prompt,
    the gate and the failure handling are the product's own."""
    ai = AIGenerator.__new__(AIGenerator)
    ai.is_configured = True
    ai.personal_data_allowed = allowed
    ai.provider = "openai_compatible"
    ai.MAX_RETRIES = 1
    ai.prompts = []

    def complete(prompt, temperature=None):
        ai.prompts.append(prompt)
        if fails:
            raise RuntimeError("provider down")
        return answer

    ai._complete = complete
    return ai


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "ai.db"))


def _contact(db, email=COM, **kw):
    fields = {
        "organization": "Rossi Integratori",
        "source_url": "https://www.rossi-integratori.com/contact",
        "qualification_rationale": "Email found on page: x. Page context: integratori",
        "source_channel": "contact_page",
        **kw,
    }
    cid = db.add_contact(db.log_search("q"), email, name="Maria Rossi", **fields)
    return db.get_contact_by_id(cid)


def _country(db, contact):
    row = db.get_contact_by_id(contact["id"])
    return row["company_country"], row["company_country_source"]


# ── Reading the answer ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,code",
    [
        ("IT", "IT"),
        (" DE\n", "DE"),
        ("UK", "GB"),
        ("EL", "GR"),
        ("GR", "GR"),
        ("Italy", ""),
        ("it", ""),
        ("IT.", ""),
        ("IT (Italy)", ""),
        ("I think IT", ""),
        ("UNKNOWN", ""),
        ("XX", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_only_a_bare_code_is_an_answer(text, code):
    assert parse_country_answer(text) == code


# ── Asked, and what is recorded ────────────────────────────────────────────


def test_an_italian_answer_makes_a_foreign_address_italian(settings_factory, db):
    s = settings_factory()
    contact = _contact(db, "einkauf@nordbrand.de")
    ai = _ai("IT")
    assert settle_company_country(s, db, contact, ai) == ("IT", "ai")
    assert _country(db, contact) == ("IT", "ai")
    assert contact["company_country"] == "IT"
    assert compliance.language_for(s, db, contact) == ("it", "ai")


def test_an_answer_naming_no_country_is_recorded_and_not_paid_for_again(
    settings_factory, db
):
    s = settings_factory()
    contact = _contact(db)
    ai = _ai("Italy, probably")
    assert settle_company_country(s, db, contact, ai) == (None, "ai")
    assert _country(db, contact) == (None, "ai")
    again = db.get_contact_by_id(contact["id"])
    assert settle_company_country(s, db, again, ai) == (None, None)
    assert len(ai.prompts) == 1
    assert compliance.language_for(s, db, contact) == ("en", "default")


def test_a_failed_call_records_nothing_and_never_raises(settings_factory, db):
    s = settings_factory()
    contact = _contact(db)
    ai = _ai(fails=True)
    assert settle_company_country(s, db, contact, ai) == (None, None)
    assert len(ai.prompts) == 1
    assert _country(db, contact) == (None, None)


def test_without_an_agreement_on_record_nothing_is_sent(settings_factory, db):
    contact = _contact(db)
    ai = _ai(allowed=False)
    assert settle_company_country(settings_factory(), db, contact, ai) == (None, None)
    assert ai.prompts == []
    assert _country(db, contact) == (None, None)


@pytest.mark.parametrize(
    "why,email,fields,setting,configured",
    [
        ("the setting is off", COM, {}, False, True),
        ("there is no key", COM, {}, True, False),
        ("the address is Italian already", "acquisti@rossi.it", {}, True, True),
    ],
)
def test_not_asked(settings_factory, db, why, email, fields, setting, configured):
    s = settings_factory(company_country_ai=setting)
    contact = _contact(db, email, **fields)
    ai = _ai("IT")
    ai.is_configured = configured
    settle_company_country(s, db, contact, ai)
    assert ai.prompts == [], why


def test_not_asked_once_the_operator_chose(settings_factory, db):
    contact = _contact(db)
    db.set_language_choice(contact["id"], "en")
    ai = _ai("IT")
    settle_company_country(
        settings_factory(), db, db.get_contact_by_id(contact["id"]), ai
    )
    assert ai.prompts == []


def test_not_asked_when_no_country_is_written_to_in_italian(settings_factory, db):
    ai = _ai("IT")
    settle_company_country(settings_factory(ITALIAN_COUNTRIES=""), db, _contact(db), ai)
    assert ai.prompts == []


def test_not_asked_when_the_stored_evidence_shows_the_country(settings_factory, db):
    ai = _ai("DE")
    website = _contact(db, source_url="https://www.rossi-integratori.it/contatti")
    assert settle_company_country(settings_factory(), db, website, ai) == (
        "IT",
        "website",
    )
    page = _contact(db, "x@verdi.com", organization="Verdi S.p.A.")
    assert settle_company_country(settings_factory(), db, page, ai) == ("IT", "page")
    collected = _contact(
        db, "y@bianchi.com", company_country="IT", company_country_source="page"
    )
    assert settle_company_country(settings_factory(), db, collected, ai) == (
        None,
        None,
    )
    assert ai.prompts == []


def test_a_dry_run_asks_nothing_and_writes_nothing(settings_factory, db):
    ai = _ai("IT")
    contact = _contact(db, organization="Verdi S.p.A.")
    assert settle_company_country(settings_factory(), db, contact, ai, write=False) == (
        "IT",
        "page",
    )
    assert _country(db, contact) == (None, None)
    plain = _contact(db, "z@plain.com")
    assert settle_company_country(settings_factory(), db, plain, ai, write=False) == (
        None,
        None,
    )
    assert ai.prompts == []


# ── What the prompt carries ────────────────────────────────────────────────


def test_the_prompt_carries_no_more_than_the_fit_check(settings_factory, db):
    contact = _contact(
        db,
        role="Head of Purchasing",
        source_url="https://www.rossi-integratori.com/team/maria-rossi?ref=x",
        qualification_rationale="Supplements brand in Milan, private label.",
    )
    ai = _ai("IT")
    ai.classify_company_country(contact)
    relevance = _ai("YES")
    relevance.classify_relevance(contact)
    prompt, fit = ai.prompts[0], relevance.prompts[0]
    for sent in ("Company: Rossi Integratori", "Domain: rossi-integratori.com"):
        assert sent in prompt and sent in fit
    assert "Supplements brand in Milan, private label." in prompt
    for kept_back in ("Maria", "Head of Purchasing", "team/maria-rossi", "acquisti@"):
        assert kept_back not in prompt, kept_back
    assert "Address:" not in prompt and "[email]" not in prompt
    assert "ISO 3166-1 alpha-2" in prompt


def test_the_page_description_is_cut_and_fenced(db):
    contact = _contact(db, qualification_rationale="x" * 900 + "TAIL")
    ai = _ai("IT")
    ai.classify_company_country(contact)
    assert "TAIL" not in ai.prompts[0]
    assert "<<<NOTES" in ai.prompts[0]


# ── In a campaign ──────────────────────────────────────────────────────────


def _campaign(settings_factory, tmp_path, ai, **kw):
    a, sent = _sending_agent(settings_factory, tmp_path, **kw)
    a.ai_gen = ai
    return a, sent


def _stored(a, email=COM):
    cid = a.db.add_contact(
        a.db.log_search("q"),
        email,
        name="Maria Rossi",
        organization="Rossi Integratori",
        source_url="https://www.rossi-integratori.com/contact",
        source_channel="contact_page",
    )
    return cid


def test_the_ai_is_asked_before_the_first_email_and_decides_it(
    settings_factory, tmp_path
):
    a, sent = _campaign(settings_factory, tmp_path, _ai("IT"))
    cid = _stored(a)
    a._process_pending_contacts(dry_run=False)
    assert len(sent) == 1, "the first email did not go"
    assert _lines(sent[0])[0] == "Buongiorno,"
    assert sent[0]["notice_version"].endswith("-it")
    assert a.db.get_contact_by_id(cid)["company_country_source"] == "ai"


def test_a_failing_ai_still_qualifies_and_emails_in_english(settings_factory, tmp_path):
    a, sent = _campaign(settings_factory, tmp_path, _ai(fails=True))
    cid = _stored(a)
    a._process_pending_contacts(dry_run=False)
    assert len(sent) == 1, "the first email did not go"
    assert _lines(sent[0])[0] == "Dears,"
    assert a.db.get_contact_by_id(cid)["qualified_at"]


def test_a_contact_waiting_for_a_slot_is_asked_once(settings_factory, tmp_path):
    ai = _ai("UNKNOWN")
    a, sent = _campaign(settings_factory, tmp_path, ai)
    a._pick_sender = lambda: None
    _stored(a)
    a._process_pending_contacts(dry_run=False)
    a._process_pending_contacts(dry_run=False)
    assert sent == []
    assert len(ai.prompts) == 1


def test_an_ai_answer_never_unlocks_the_country_lock(settings_factory, tmp_path):
    a, sent = _campaign(
        settings_factory,
        tmp_path,
        _ai("IT"),
        ENABLED_COUNTRIES="IT",
        UNKNOWN_COUNTRY_ALLOWED=False,
    )
    cid = _stored(a)
    a.db.set_company_country(cid, "IT", "ai")
    a._process_pending_contacts(dry_run=False)
    assert a.db.get_contact_by_id(cid)["company_country"] == "IT"
    assert compliance.country_refusal(a.settings, COM) is not None
    assert sent == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_contact_the_country_lock_refuses_is_not_sent_to_the_ai(
    settings_factory, tmp_path, dry_run
):
    """Its company went to the provider, and its answer was stored, for a
    contact no live email could reach."""
    ai = _ai("IT")
    a, _sent = _campaign(
        settings_factory,
        tmp_path,
        ai,
        ENABLED_COUNTRIES="IT",
        UNKNOWN_COUNTRY_ALLOWED=False,
    )
    cid = _stored(a)
    a._process_pending_contacts(dry_run=dry_run)
    assert ai.prompts == []
    assert a.db.get_contact_by_id(cid)["company_country_source"] is None


# ── The setting ────────────────────────────────────────────────────────────


def test_the_setting_is_on_by_default_and_not_an_ai_switch():
    assert Settings(_env_file=None).company_country_ai is True
    assert "company_country_ai" not in compliance.AI_SWITCHES


def test_the_agents_own_client_is_what_the_campaign_asks(settings_factory, tmp_path):
    """A guard on the harness: the agent's real generator has the method the
    qualify step calls."""
    a = OutreachAgent(settings_factory(db_path=str(tmp_path / "g.db")))
    assert callable(getattr(a.ai_gen, "classify_company_country", None))
