"""A rival contract manufacturer must not be pitched contract manufacturing.

COMPETITOR_KEYWORDS deliberately holds stems - "contract manufactur", "white
label produc" - so they cover manufacturer/manufacturing and producer/
production. They were matched with a two-sided \\b, and a trailing \\b needs a
word/non-word transition: the character after "produc" in real text is always
"e" or "t", so the boundary never matched and every stem entry was dead.

Six of the seven were masked by a separate `"manufactur" in haystack` fallback,
so nothing looked broken. "white label produc" was not covered by it, because
that fallback only fires when the word "manufactur" appears at all. A company
describing itself as a white label PRODUCER - never using the word manufacture
- passed the gate as an ordinary lead, and would have been sent an offer to
manufacture on its behalf.
"""

import pytest

from modules.relevance import RelevanceFilter


@pytest.fixture
def rf():
    return RelevanceFilter(require_vertical=True)


def _assess(rf, org, rationale):
    return rf.assess(
        {
            "email": "info@example.de",
            "organization": org,
            "qualification_rationale": rationale,
        }
    )


# ── Competitors must be rejected ───────────────────────────────────────────


@pytest.mark.parametrize(
    "org,rationale",
    [
        (
            "Nutriva Brand GmbH",
            (
                "Nutriva is a white label producer of dietary supplements and "
                "vitamins, producing private label products for European brands."
            ),
        ),
        (
            "CM GmbH",
            "We are a contract manufacturer of food supplements.",
        ),
        (
            "Alpha Produzione",
            "White label production of nutraceuticals for third-party brands.",
        ),
    ],
)
def test_a_rival_manufacturer_is_rejected(rf, org, rationale):
    result = _assess(rf, org, rationale)
    assert result["relevant"] is False, (
        f"a competitor passed the gate as a lead: {result['reason']!r}"
    )
    assert "competitor" in result["reason"]


def test_the_private_label_producer_phrasing_is_caught(rf):
    """Its own case, using no other competitor wording. "white label produc"
    was covering for it: every rival in the suite happened to say "white", so
    deleting the private-label entry broke nothing and the gap was invisible."""
    rationale = "Private label production of nutraceuticals for third-party brands."
    assert "white label" not in rationale.lower()
    assert "manufactur" not in rationale.lower()
    result = _assess(rf, "PL Produzione", rationale)
    assert result["relevant"] is False
    assert "private label produc" in result["reason"]


def test_the_producer_stem_matches_without_the_word_manufacture(rf):
    """The specific gap: the "manufactur" fallback cannot catch a company that
    never uses the word."""
    rationale = (
        "We are a white label producer of supplements, producing private "
        "label ranges for retailers across Europe."
    )
    assert "manufactur" not in rationale.lower(), (
        "the fixture must not leak the fallback"
    )
    assert _assess(rf, "PL Producers", rationale)["relevant"] is False


# ── Genuine prospects must still pass ──────────────────────────────────────


@pytest.mark.parametrize(
    "org,rationale",
    [
        (
            "Nordic Wellness AB",
            (
                "A Swedish brand selling its own range of vitamins and dietary "
                "supplements to retailers."
            ),
        ),
        (
            "Bio Shop Berlin",
            "Online retailer of organic food supplements and nutraceuticals.",
        ),
        (
            "Sport Nutrition Store",
            "We sell sports nutrition products, protein powders and vitamins.",
        ),
        (
            "Herbal Brands Ltd",
            "Our brand produces a line of herbal supplements sold through pharmacies.",
        ),
    ],
)
def test_a_real_prospect_still_passes(rf, org, rationale):
    """The control that matters most. Tightening a rejection gate is how you
    silently delete your own pipeline - "Herbal Brands ... produces a line of
    supplements" is a customer, not a competitor, and the word "produces"
    alone must not condemn it."""
    result = _assess(rf, org, rationale)
    assert result["relevant"] is True, (
        f"a genuine prospect was rejected: {result['reason']!r}"
    )


def test_a_prospect_writing_in_its_own_language_still_qualifies(rf):
    """The vertical gate was English-only, so an Italian distributor of
    "integratori alimentari" was rejected for showing no supplement signal -
    and SUPPLECO is itself Italian, making those its nearest prospects."""
    for org, rationale in [
        # Deliberately carries ONE recognisable term each, so a test that
        # passes proves that term is doing the work. The first version said
        # "integratori alimentari e nutraceutici" and stayed green when
        # "integratori" was deleted, because "nutraceutic" covered it.
        (
            "Vitalis Distribuzione",
            "Distributore italiano di integratori per farmacie.",
        ),
        (
            "Sante Plus SARL",
            "Distributeur de complement alimentaire pour les pharmacies.",
        ),
        (
            "Salud Natural SL",
            "Vendemos suplemento y complemento alimenticio en Espana.",
        ),
    ]:
        assert _assess(rf, org, rationale)["relevant"] is True, org


def test_the_spanish_market_describes_itself_in_the_plural(rf):
    """ "Complementos alimenticios" is how the Spanish industry actually writes
    it - the plural is the trade term, and a company says "fabricante de
    complementos alimenticios", never the singular.

    The alias was stored singular as "complemento aliment", and the plural's
    's' lands between the two words, so the two-word substring never matched:
        "complemento aliment" in "complementos alimenticios" -> False
    Every Spanish prospect was therefore rejected for showing no supplement
    signal. Spain is a target market, so this was a whole country silently
    filtered out.

    Each rationale below carries exactly ONE recognisable term. The existing
    multilingual test's Spanish line says "suplemento y complemento
    alimenticio", which stays green even with the Spanish alias deleted
    because "suplemento" covers it - so it could never have caught this.
    """
    for org, rationale in [
        ("Nutrisalud SL", "Fabricante de complementos alimenticios naturales."),
        ("BioEspana SA", "Somos fabricantes de complementos alimenticios."),
        # The singular must keep working - this is a widening, not a swap.
        ("Vida Natural SL", "Elaboramos complemento alimenticio de calidad."),
    ]:
        assert _assess(rf, org, rationale)["relevant"] is True, org


def test_a_plain_manufacturer_is_not_assumed_to_be_a_competitor(rf):
    """German "Hersteller" just means manufacturer. A brand that makes its own
    supplements may still outsource, so it is a prospect - only the contract
    forms (Lohnhersteller, conto terzi) identify a rival."""
    assert (
        _assess(
            rf,
            "Gruene GmbH",
            "Hersteller von Nahrungserganzung und Vitaminpraparat fuer Europa.",
        )["relevant"]
        is True
    )
    assert (
        _assess(
            rf,
            "Lohn GmbH",
            "Lohnhersteller von Nahrungserganzung fuer andere Marken.",
        )["relevant"]
        is False
    )


def test_a_cdmo_is_recognised_as_a_competitor(rf):
    """Found by a live discovery run, not by a unit test.

    A real query returned naturacare.com, whose own site says "a European CDMO
    and pharmaceutical dietary supplement manufacturer". CDMO - Contract
    Development and Manufacturing Organisation - is THE industry term for
    exactly what SUPPLECO is, and it was absent from the keyword list, so the
    company passed the gate as an ordinary prospect. The agent would have
    offered to manufacture for a direct competitor.

    The list caught two siblings in the same run ("conto terzi", "produzione
    conto terzi") purely because those firms happen to use Italian phrasing.
    A company writing in English about itself was invisible.
    """
    for org, rationale in [
        ("Naturacare", "A European CDMO and dietary supplement manufacturer."),
        ("Nutra Partners", "Leading cdmo for nutraceutical brands."),
        ("BioProd", "We are a contract development and manufacturing organisation."),
        ("BioProd US", "A contract development and manufacturing organization."),
    ]:
        result = _assess(rf, org, rationale)
        assert result["relevant"] is False, f"{org} was treated as a prospect"
        assert "competitor" in result["reason"], org


def test_a_chief_marketing_officer_is_not_a_contract_manufacturer(rf):
    """The trap in the fix above: CMO is also Chief Marketing Officer, and a
    supplement BRAND naming its CMO is a prime prospect. Only the unambiguous
    CDMO and the spelled-out phrase are matched - never a bare "cmo"."""
    for org, rationale in [
        ("VitalBrand", "Our CMO leads the supplement brand's marketing team."),
        ("NutriCo", "Interview with our cmo about our new vitamin range."),
    ]:
        assert _assess(rf, org, rationale)["relevant"] is True, org
