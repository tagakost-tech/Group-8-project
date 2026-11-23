"""A company shopping for a manufacturer is a customer, not a competitor.

Two defects with the same shape: a signal that identifies a rival when it
appears in the company's NAME was being read from scraped prose and from the
source URL as well.

"cdmo" is one short token, unlike every other competitor entry, which are
multi-word phrases nobody writes into a URL slug by accident. The haystack the
competitor gate reads includes qualification_rationale and source_url, both
scraped, so it fired on a conference listing and - the expensive case - on a
Spanish brand writing "Buscamos un CDMO para fabricar nuestros complementos
alimenticios". That company is asking for precisely what SUPPLECO sells, and it
was discarded with a reason that reads like the filter working.

The second is the address ranking. _rank_address's own docstring calls info@
reception, but "info" was the last BUYING_PREFIXES entry and so scored better
than any unrecognised prefix - and an unrecognised prefix is a NAMED mailbox.
A page offering both info@ and m.mueller@ labelled "Leiter Einkauf" kept
reception and discarded the buyer.
"""

import pytest

from modules.relevance import RelevanceFilter
from modules.search import SearchModule


@pytest.fixture
def rf():
    return RelevanceFilter(require_vertical=True)


def _assess(rf, org, rationale, url="https://example.de/about"):
    return rf.assess(
        {
            "email": "info@example.de",
            "organization": org,
            "qualification_rationale": rationale,
            "source_url": url,
        }
    )


# ── The competitor signal belongs to the name, not the prose ───────────────


def test_a_brand_looking_for_a_cdmo_is_a_prospect(rf):
    """The most expensive false positive available: a company advertising that
    it wants exactly this service."""
    result = _assess(
        rf,
        "Marca Espanola SL",
        "Buscamos un CDMO para fabricar nuestros complementos alimenticios.",
    )
    assert result["relevant"] is True, (
        f"a brand shopping for a manufacturer was binned as one: {result['reason']!r}"
    )


def test_a_conference_url_does_not_condemn_the_exhibitor(rf):
    """source_url is in the haystack. An exhibitor listed under a 'cdmo-zone'
    path is not thereby a CDMO."""
    result = _assess(
        rf,
        "BrandX GmbH",
        "We sell our own range of dietary supplements.",
        url="https://www.cphi.com/europe/exhibitors/cdmo-zone/brandx",
    )
    assert result["relevant"] is True, result["reason"]


@pytest.mark.parametrize(
    "rationale",
    [
        "Buscamos un CDMO para fabricar nuestros complementos alimenticios.",
        "Cerchiamo un CDMO per la produzione dei nostri integratori.",
        "Wir suchen einen CDMO fuer unsere Nahrungsergaenzungsmittel.",
        "We are looking for a CDMO to produce our supplement range.",
        "Nous recherchons un CDMO pour nos complements alimentaires.",
        "Seeking a CDMO partner for our vitamin line.",
    ],
)
def test_a_want_ad_is_not_a_self_description(rf, rationale):
    """The distinction is VOICE, not location. The same word in the same field
    means opposite things depending on whether the company is saying what it
    IS or what it WANTS - and this agent's whole market is companies that want
    one."""
    result = _assess(rf, "Marca SL", rationale)
    assert "competitor" not in result["reason"], (
        f"a company advertising for a manufacturer was binned as one: "
        f"{result['reason']!r}"
    )


def test_a_seeking_phrase_does_not_leak_across_fields(rf):
    """Name and description are checked separately. A want-ad in the rationale
    must not excuse a competitor's NAME."""
    result = _assess(
        rf,
        "Rheinland CDMO GmbH",
        "We are looking for new distribution partners in Spain.",
    )
    assert result["relevant"] is False


def test_a_real_cdmo_is_still_rejected(rf):
    """Control. The gate exists because emailing a rival an offer to
    manufacture for them is worse than wasting the send."""
    result = _assess(
        rf,
        "Rheinland CDMO GmbH",
        "We produce supplements for other brands.",
    )
    assert result["relevant"] is False
    assert "cdmo" in result["reason"]


def test_the_multi_word_competitor_phrases_still_read_the_prose(rf):
    """Only the single-token entry moved to name-only matching. A company
    describing itself as a white label producer in its own copy must still be
    caught, because that is where such a company says it."""
    result = _assess(
        rf,
        "Nutriva Brand GmbH",
        "Nutriva is a white label producer of dietary supplements for brands.",
    )
    assert result["relevant"] is False
    assert "competitor" in result["reason"]


# ── A named mailbox beats reception ────────────────────────────────────────


def test_a_named_mailbox_outranks_reception():
    """Lower is better. The buyer must win."""
    rank = SearchModule._rank_address
    assert rank("m.mueller@brand.de") < rank("info@brand.de"), (
        "reception was preferred over a named decision-maker"
    )


def test_a_buying_mailbox_still_beats_both():
    """Control: the recognised prefixes must keep their priority."""
    rank = SearchModule._rank_address
    assert rank("einkauf@brand.de") < rank("m.mueller@brand.de")
    assert rank("acquisti@brand.de") < rank("info@brand.de")


def test_reception_is_still_used_when_it_is_all_there_is():
    """info@ is a real fallback, not something to discard - it just must not
    win against a person."""
    rank = SearchModule._rank_address
    assert rank("info@brand.de") < float("inf")


def test_one_address_per_company_keeps_the_person(monkeypatch):
    """The ranking only matters through _one_per_company, which is what
    actually drops the loser."""
    sm = SearchModule.__new__(SearchModule)
    chosen = sm._one_per_company(
        [
            {"email": "info@brand.de", "name": None},
            {"email": "m.mueller@brand.de", "name": "Markus Mueller"},
        ]
    )
    assert [c["email"] for c in chosen] == ["m.mueller@brand.de"]
