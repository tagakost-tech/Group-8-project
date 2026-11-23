"""SUPPLECO's supplier-side keywords screen rivals, and never screen its buyers.

Half of the keyword list SUPPLECO sent on 10 November 2026 describes what
SUPPLECO itself is: "CDMO specializzata in integratori alimentari", "produzione
conto terzi integratori alimentari", "contract manufacturer food supplements".
A company that describes ITSELF that way is a competitor, and the competitor
gate already reads every one of those phrases.

The same list phrases a buyer's need in the first person singular: "cerco
CDMO integratori", "cerco produzione conto terzi". The want-ad rule knew
"cerchiamo", "in cerca di" and "alla ricerca di", but not "cerco", so a brand
writing exactly the way SUPPLECO's own document does was discarded as a rival.

Left out on purpose, with the reason pinned below where it can be: bare
"terzista" (a brand names its own: "il nostro terzista"), bare "private
label" and "white label supplements" (a retailer's own-label line is a
buyer), and service words such as turnkey, in-licensing, ready to market,
formulation development, packaging or capsule manufacturing, which say what a
company buys or sells, not what it is.
"""

import pytest

from modules.relevance import RelevanceFilter


@pytest.fixture
def rf():
    return RelevanceFilter(require_vertical=True)


def _assess(rf, org, rationale):
    return rf.assess(
        {
            "email": "info@example.it",
            "organization": org,
            "qualification_rationale": rationale,
        }
    )


@pytest.mark.parametrize(
    "rationale",
    [
        "Specialized CDMO for food supplements",
        "CDMO specializzata in integratori alimentari",
        "Produzione conto terzi integratori alimentari",
        "Contract manufacturing di qualità per integratori",
        "Private label supplements manufacturing",
        "Third party manufacturer supplements",
        "Contract manufacturer food supplements",
        "CDMO nutraceuticals",
    ],
)
def test_a_company_describing_itself_in_supplecos_supplier_words_is_a_rival(
    rf, rationale
):
    verdict = _assess(rf, "Acme S.r.l.", rationale)
    assert not verdict["relevant"]
    assert verdict["reason"].startswith("competitor"), verdict["reason"]


@pytest.mark.parametrize(
    "rationale",
    [
        "Cerco un CDMO per i nostri integratori",
        "Cerco produzione conto terzi per i miei integratori",
        "Sto cercando un partner per la produzione conto terzi dei nostri integratori",
        "Stiamo cercando un terzista: produzione conto terzi per la nostra linea",
        "Seeking CDMO for our supplement brand",
        "Looking for private label manufacturer for our supplements",
        "Seeking third party manufacturer for our supplement brand",
    ],
)
def test_a_brand_asking_in_supplecos_own_words_is_a_prospect(rf, rationale):
    verdict = _assess(rf, "Rossi Integratori S.r.l.", rationale)
    assert verdict["relevant"], verdict["reason"]


@pytest.mark.parametrize(
    "rationale",
    [
        "Stiamo cercando nuovi brand partner: siamo contract manufacturer di integratori alimentari",
        "Cerchiamo nuovi brand partner: siamo contract manufacturer di integratori alimentari",
        "Sto cercando di presentarvi la nostra azienda, produzione conto terzi di integratori",
        "Cerchiamo di offrire ai brand la produzione conto terzi di integratori",
        "Ricerchiamo e sviluppiamo formule: contract manufacturer di integratori",
        "We are looking for new brands, we are a contract manufacturer of supplements",
        "Buscamos nuevos clientes, somos fabricante por contrato de complementos",
    ],
)
def test_a_rival_whose_sentence_opens_like_a_want_ad_is_still_a_rival(rf, rationale):
    """A want-ad opening followed by what the company IS, "cercare di" (to
    try to), and a want-ad word inside a longer one are all claims."""
    verdict = _assess(rf, "Acme S.r.l.", rationale)
    assert not verdict["relevant"]
    assert verdict["reason"].startswith("competitor"), verdict["reason"]


def test_a_brand_naming_its_terzista_is_a_prospect(rf):
    """Why bare "terzista" is not a competitor word."""
    verdict = _assess(
        rf,
        "Rossi Integratori S.r.l.",
        "Il nostro terzista produce i nostri integratori",
    )
    assert verdict["relevant"], verdict["reason"]


def test_a_rivals_hook_that_opens_with_cerco_is_still_a_rival(rf):
    """A rival's question to buyers is a hook, not a want-ad."""
    verdict = _assess(
        rf, "Acme S.r.l.", "Cerco un produttore conto terzi? Produciamo per i brand."
    )
    assert not verdict["relevant"]
    assert verdict["reason"].startswith("competitor"), verdict["reason"]
