"""An empty og:site_name falls through to the page title.

_extract_org_name returned the og:site_name tag's content even when it was
empty, so a page with `<meta property="og:site_name" content="">` - common
in CMS themes that ship the tag unfilled - gave the contact no organisation.
The title a line below would have named the company, and without a name the
client and competitor lists cannot match it at all.
"""

import pytest
from bs4 import BeautifulSoup

from modules.search import SearchModule


@pytest.fixture
def sm(db):
    return SearchModule(db, rate_limit=0.0)


@pytest.mark.parametrize("content", ["", "   "])
def test_a_blank_site_name_falls_through_to_the_title(sm, content):
    html = (
        f"<html><head><meta property='og:site_name' content='{content}'>"
        "<title>Kontakt | VitalBrand GmbH</title></head><body></body></html>"
    )
    name = sm._extract_org_name(
        BeautifulSoup(html, "html.parser"), "https://vitalbrand.de"
    )
    assert name == "VitalBrand GmbH"


def test_a_real_site_name_still_wins(sm):
    html = (
        "<html><head><meta property='og:site_name' content='VitalBrand'>"
        "<title>Kontakt | Something Else</title></head><body></body></html>"
    )
    name = sm._extract_org_name(
        BeautifulSoup(html, "html.parser"), "https://vitalbrand.de"
    )
    assert name == "VitalBrand"
