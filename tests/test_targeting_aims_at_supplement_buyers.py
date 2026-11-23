"""The agent looks for SUPPLECO's buyers: food supplement and nutraceutical brands.

SUPPLECO makes food supplements for other companies' brands. The scorer, the
default searches and the AI's example were written for a pharmaceutical
outreach product, and nobody re-aimed them:

  The score decides who is emailed first under the daily warm-up cap: the
  relevance filter is off by default and the score threshold of 15 excludes
  no one. Company names earned their 25 points for pharma, hospital, clinic,
  surgical and diagnostics words, and none for supplement, nutrition or
  vitamin. Pharmacists, doctors and nurses earned 20 points for their titles.
  An address on an English /contact page earned 15, the same page in German
  or Italian 5. So a hospital nurse on a /contatti page scored 75 (tier A),
  above a supplement brand's purchasing contact on a /kontakt page at 70.

  A run without --query, from the CLI or the dashboard's Start button,
  searched "pharmaceutical distributor contact email" and "healthcare
  provider procurement contact".

  AI company discovery was shown "Pharma companies in Europe: Sanofi France,
  Bayer Germany, Novartis Switzerland" as its example, and a company with no
  supplement signal was discarded as lacking "supplement/cosmetic/medical
  device/pharma", markets SUPPLECO does not sell to.
"""

import re

import pytest

from agent import OutreachAgent
from modules.ai_generator import AIGenerator
from modules.lead_scoring import LeadScorer
from modules.query_generator import VERTICAL_SEEDS
from modules.relevance import DEFAULT_VERTICALS, RelevanceFilter
from modules.search import SearchModule

HOSPITAL_NURSE = {
    "email": "anna.rossi@citta-hospital.it",
    "role": "Nurse",
    "organization": "Città Hospital",
    "source_url": "https://www.citta-hospital.it/contatti",
}
SUPPLEMENT_BUYER = {
    "email": "jan.weber@vitalbrand.de",
    "role": "Purchasing",
    "organization": "VitalBrand Supplements GmbH",
    "source_url": "https://www.vitalbrand.de/kontakt",
}


def _agent(settings_factory, tmp_path, **kw):
    return OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "t.db"),
            verify_emails=False,
            imap_enabled=False,
            ai_humanize_emails=False,
            **kw,
        )
    )


def _score(contact):
    return LeadScorer().score_contact(contact)["score"]


# ── Who is emailed first ─────────────────────────────────────────────────────


def test_a_supplement_brands_buyer_is_emailed_before_a_hospital_nurse(
    settings_factory, tmp_path
):
    a = _agent(settings_factory, tmp_path)
    a._running = True
    sid = a.db.log_search("q")
    found = []
    for contact in (HOSPITAL_NURSE, SUPPLEMENT_BUYER):
        cid = a.db.add_contact(sid, **contact)
        found.append({"id": cid, **contact})

    a._qualify_contacts(found)

    queue = [c["email"] for c in a.db.get_pending_contacts()]
    assert queue == [SUPPLEMENT_BUYER["email"], HOSPITAL_NURSE["email"]], (
        f"the backlog is emailed highest score first, and it reads {queue}: "
        f"nurse {_score(HOSPITAL_NURSE)}, supplement buyer {_score(SUPPLEMENT_BUYER)}"
    )


def test_a_supplement_brands_mailbox_outranks_a_diagnostics_labs():
    brand = {
        "email": "info@vitalbrand.de",
        "organization": "VitalBrand Nahrungsergänzungsmittel GmbH",
        "source_url": "https://www.vitalbrand.de/kontakt",
    }
    lab = {
        "email": "info@nordlab.se",
        "organization": "Nordic Diagnostics AB",
        "source_url": "https://www.nordlab.se/contact",
    }
    assert _score(brand) > _score(lab), (
        f"supplement brand {_score(brand)}, diagnostics lab {_score(lab)}"
    )


# ── The company name ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "org",
    [
        "Vitalis Supplements Ltd",
        "Nordic Vitamins AB",
        "Probiotici Italia S.r.l.",
        "Nutraceutica Bianchi S.r.l.",
        "Integratori Rossi S.p.A.",
        "Nahrungsergänzungsmittel Müller GmbH",
        "Compléments Alimentaires Dupont SAS",
        "Suplementos Iberia S.L.",
        "Parafarmacia Centrale",
        "Herbalist Botanicals Ltd",
    ],
)
def test_a_supplement_company_is_market_fit_however_it_writes_its_name(org):
    """Plurals, compounds and the local-language names the relevance gate
    already reads. Whole-word matching would score every one of these as a
    company outside the market."""
    assert LeadScorer()._score_organization(org) == 25


def test_every_term_the_searches_and_the_gate_use_is_market_fit_for_the_scorer():
    """One definition of SUPPLECO's market: the words the discovery queries are
    built from and the words the relevance gate accepts must also earn the
    company points that put a contact ahead in the queue."""
    missed = [
        term
        for term in [*DEFAULT_VERTICALS, *VERTICAL_SEEDS]
        if LeadScorer()._score_organization(f"Acme {term} GmbH") != 25
    ]
    assert not missed, f"the scorer does not count these market terms: {missed}"


def test_every_built_in_search_seed_passes_the_relevance_gate():
    """The other half of one vocabulary: a company named by a word the agent
    searches for must not then be discarded for lacking a market signal.
    SUPPLECO's own "azienda di alimenti dietetici" and "azienda di prodotti per
    il benessere" were searched and then rejected."""
    gate = RelevanceFilter(require_vertical=True)
    rejected = [
        seed
        for seed in VERTICAL_SEEDS
        if not gate.assess({"email": "info@acme.it", "organization": f"Acme {seed}"})[
            "relevant"
        ]
    ]
    assert not rejected, f"searched for, then rejected by the gate: {rejected}"


def test_every_suppleco_buyer_phrase_is_market_fit_for_the_scorer():
    from modules.query_generator import BUYER_SEEDS

    phrases = [p for ps in BUYER_SEEDS.values() for p in ps]
    missed = [
        p for p in phrases if LeadScorer()._score_organization(f"Acme {p} S.r.l.") != 25
    ]
    assert not missed, f"the scorer does not count SUPPLECO's own words: {missed}"


@pytest.mark.parametrize(
    "org", ["Integratori Rossi S.r.l.", "Acme azienda di alimenti dietetici"]
)
def test_the_agents_own_gate_reads_the_built_in_vocabulary(
    settings_factory, tmp_path, org
):
    """The running gate is built from RELEVANCE_VERTICALS. Its default was an
    English-only list that replaced the built-in vocabulary, so with the
    filter on every Italian prospect, SUPPLECO's nearest market, was rejected."""
    a = _agent(settings_factory, tmp_path, relevance_filter_enabled=True)
    verdict = a.relevance_filter.assess({"email": "info@acme.it", "organization": org})
    assert verdict["relevant"], verdict["reason"]


NARROW = ["supplement", "nutraceutical", "integratori"]
SPA = {
    "email": "info@wellnessspa.it",
    "organization": "Wellness Spa Milano",
    "qualification_rationale": "Centro benessere con piscina, massaggi e trattamenti viso",
}


@pytest.mark.parametrize(
    "org", ["Rossi Alimenti Dietetici S.r.l.", "Bio Prodotti per il Benessere Snc"]
)
def test_the_built_in_words_read_supplecos_terms_in_a_company_name(org):
    """SUPPLECO's phrases are always read whole ("azienda di alimenti
    dietetici"); a company names itself with the words inside them."""
    verdict = RelevanceFilter(require_vertical=True).assess(
        {"email": "info@rossi.it", "organization": org}
    )
    assert verdict["relevant"], verdict["reason"]


def test_an_operator_can_narrow_the_market_words():
    """Adding the whole built-in list back to a narrowed RELEVANCE_VERTICALS
    let a spa through on "wellness" after the operator had taken it out."""
    gate = RelevanceFilter(verticals=NARROW, require_vertical=True)
    assert not gate.assess(SPA)["relevant"]
    assert gate.assess({"email": "info@acme.it", "organization": "Integratori Rossi"})[
        "relevant"
    ]


@pytest.mark.parametrize(
    "org", ["Acme wellness brand", "Acme azienda di alimenti dietetici"]
)
def test_supplecos_own_buyer_words_count_however_the_list_is_narrowed(org):
    gate = RelevanceFilter(verticals=NARROW, require_vertical=True)
    verdict = gate.assess({"email": "info@acme.it", "organization": org})
    assert verdict["relevant"], verdict["reason"]


def test_a_spa_is_not_market_fit():
    """SUPPLECO's "prodotti per il benessere" is counted as the whole phrase:
    bare "benessere" would let every centro benessere through."""
    org = "Centro Benessere Aurora"
    assert not RelevanceFilter(require_vertical=True).assess(
        {"email": "info@aurora.it", "organization": org}
    )["relevant"]
    assert LeadScorer()._score_organization(org) == 10


@pytest.mark.parametrize(
    "org",
    [
        "Città Hospital",
        "Westpark Clinic",
        "Nordic Diagnostics AB",
        "Surgical Instruments Ltd",
        "Bayer Laboratories",
        "Pharma Distribution Ltd",
        "Vitalis Pharmaceuticals",
        "Healthcare Supplies SpA",
        "Medical Devices GmbH",
        "Biotech Labs",
    ],
)
def test_a_healthcare_provider_is_not_market_fit(org):
    assert LeadScorer()._score_organization(org) == 10


@pytest.mark.parametrize(
    "org",
    [
        "Malnutrition Research Clinic",
        "Lipoprotein Diagnostics GmbH",
        "Geomineral Mining Ltd",
    ],
)
def test_a_market_term_inside_another_word_is_not_market_fit(org):
    """ "nutrition" sits inside "Malnutrition", "protein" inside "Lipoprotein",
    "mineral" inside "Geomineral": a term counts only at the start of a word."""
    assert LeadScorer()._score_organization(org) == 10


# ── The job title ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role",
    [
        "Nurse",
        "Pharmacist",
        "Pharm.D",
        "Physician",
        "Doctor",
        "Dr.",
        "MD",
        "Clinical Research Associate",
        "Medical Officer",
    ],
)
def test_a_clinical_title_earns_no_more_than_an_unknown_one(role):
    scorer = LeadScorer()
    assert scorer._score_role(role) <= scorer._score_role(None), (
        f"{role!r} is not who buys contract manufacturing for a supplement brand"
    )


# ── The page the address was found on ────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/kontakt",
        "/contatti",
        "/contattaci",
        "/impressum",
        "/mentions-legales",
        "/aviso-legal",
    ],
)
def test_a_contact_page_scores_the_same_in_every_language(path):
    scorer = LeadScorer()
    assert scorer._score_source(f"https://www.brand.example{path}") == (
        scorer._score_source("https://www.brand.example/contact")
    )


@pytest.mark.parametrize(
    "path",
    [
        "/chi-siamo",
        "/ueber-uns",
        "/uber-uns",
        "/a-propos",
        "/quienes-somos",
        "/over-ons",
    ],
)
def test_an_about_page_scores_the_same_in_every_language(path):
    scorer = LeadScorer()
    assert scorer._score_source(f"https://www.brand.example{path}") == (
        scorer._score_source("https://www.brand.example/about")
    )


def test_every_page_the_crawler_reads_an_address_from_is_scored_as_one():
    """The crawler visits these pages, in these languages, to find a company's
    address; the score must not then treat them as unknown pages."""
    paths = {
        *SearchModule.CONTACT_PATHS_DEFAULT,
        *(p for ps in SearchModule.CONTACT_PATHS_BY_TLD.values() for p in ps),
    }
    scorer = LeadScorer()
    unknown = scorer._score_source("https://www.brand.example/products")
    missed = [
        p
        for p in sorted(paths)
        if scorer._score_source(f"https://www.brand.example{p}") <= unknown
    ]
    assert not missed, f"scored like an unknown page: {missed}"


# ── What a run searches ──────────────────────────────────────────────────────


def test_a_run_without_a_query_searches_for_supplement_buyers(
    settings_factory, tmp_path
):
    """`python main.py run` and the dashboard's Start button pass no query."""
    a = _agent(settings_factory, tmp_path)
    searched = []
    a.search.search_and_extract = lambda query, **_k: searched.append(query) or []
    a._process_pending_contacts = lambda *_a, **_k: None
    a._process_sequences = lambda *_a, **_k: None
    a._maybe_apply_retention = lambda *_a, **_k: None

    a.run_single(dry_run=True)

    off_market = [
        q
        for q in searched
        if not any(q.startswith(f"{seed} ") for seed in VERTICAL_SEEDS)
    ]
    assert searched and not off_market, (
        f"a run with no query searched {searched}, not the supplement queries "
        "modules/query_generator.py builds"
    )


def test_ai_company_discovery_is_shown_a_supplement_example():
    ai = AIGenerator(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1", llm_model="test-model")
    prompts = []
    ai._complete_with_retry = lambda prompt, label="": prompts.append(prompt) or ""

    ai.generate_companies("Sports nutrition brands in Spain", count=3)

    prompt = prompts[0].lower()
    pharma = re.findall(r"pharma|sanofi|bayer|novartis|novo nordisk|gsk", prompt)
    assert not pharma, f"the example primes the model with pharma companies: {pharma}"
    example = prompts[0].split("Example output for", 1)[1]
    topic = example.split(":", 1)[0]
    assert "supplement" in topic.lower(), f"the example's topic is {topic!r}"
    rows = [line.strip() for line in example.split(":", 1)[1].splitlines()]
    named = [row for row in rows if row and not row.startswith("[brand name] ")]
    assert not named, f"the example names a company, not a placeholder: {named}"


def test_a_company_outside_the_market_is_told_what_the_market_is():
    reason = RelevanceFilter(require_vertical=True).assess(
        {"email": "info@nordwind.de", "organization": "Nordwind Handels GmbH"}
    )["reason"]
    market = re.search(r"\(([^)]*)\)", reason)
    assert market, f"the discard reason names no market: {reason!r}"
    named = set(re.split(r"[/,]\s*", market.group(1)))
    assert named == {"food supplements", "nutraceuticals"}, (
        f"the discard reason names {sorted(named)} as SUPPLECO's market"
    )
