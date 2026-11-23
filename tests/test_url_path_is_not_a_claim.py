"""Where a contact was found says nothing about what the company does.

The relevance gates searched one haystack built from the organisation, its
description, the email domain AND the full source URL. So the path of the
page the address was found on could condemn the company:

- a brand's address found on /news/new-vitamin-range was rejected as
  "non-target signal: 'news'";
- a brand listed on a directory page /suppliers/contract-manufacturing/
  was rejected as a competitor ("contract" + "manufactur").

That undid the directory-page fix (b0a969f), which exists precisely because
listing pages publish good leads. The rejection gates now read the host of
the source URL but not its path. The vertical gate - a POSITIVE signal - keeps
the full URL: a /supplements/ path is fair evidence of the sector.
"""

import pytest

from modules.relevance import RelevanceFilter


@pytest.fixture
def rf():
    return RelevanceFilter(require_vertical=True)


def _assess(rf, url, org="VitalBrand GmbH", rationale="Dietary supplements brand."):
    return rf.assess(
        {
            "email": "info@vitalbrand.de",
            "organization": org,
            "qualification_rationale": rationale,
            "source_url": url,
        }
    )


def test_a_news_page_does_not_make_a_brand_a_news_site(rf):
    r = _assess(rf, "https://www.vitalbrand.de/news/new-vitamin-range")
    assert r["relevant"] is True, r["reason"]


def test_a_directory_path_does_not_make_a_brand_a_competitor(rf):
    r = _assess(
        rf, "https://www.nutra-directory.eu/suppliers/contract-manufacturing/vitalbrand"
    )
    assert r["relevant"] is True, r["reason"]


def test_a_news_host_is_still_rejected(rf):
    """The control: the host is still read. A newspaper's own domain is a
    real signal about who we would be writing to. The organisation name is
    neutral on purpose - with "Supplement News" this passed on the name
    alone, and dropping the host from the gates survived the suite."""
    r = _assess(rf, "https://www.supplement-news.com/articles/x", org="Vitamin Weekly")
    assert r["relevant"] is False


def test_a_competitor_host_is_still_rejected(rf):
    r = _assess(rf, "https://www.contract-manufacturing-italia.it/", org="Italia Nutra")
    assert "competitor" in r["reason"]


def test_a_vertical_term_in_the_path_still_counts(rf):
    """The positive gate keeps the full URL."""
    r = _assess(
        rf,
        "https://www.vitalbrand.de/supplements/omega-3",
        rationale="Online shop.",
    )
    assert r["relevant"] is True, r["reason"]
