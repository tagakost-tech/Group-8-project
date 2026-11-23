"""What gets scraped becomes the org name, the relevance verdict and the email.

`organization` and `qualification_rationale` are not just record-keeping. They
feed the relevance gate that decides whether a company is contacted at all,
and they are interpolated into the AI prompt that writes the opening line of a
real cold email. Garbage in either one is visible to the prospect.

Two extraction defects did exactly that, both confirmed against real markup:
a page title split the wrong way produced the organisation "Contact Us", and
the first <p> on a European site is routinely the cookie banner.
"""

import pytest
from bs4 import BeautifulSoup

from modules.database import Database
from modules.search import SearchModule


@pytest.fixture
def sm(tmp_path):
    return SearchModule(Database(str(tmp_path / "s.db")))


def _soup(html):
    return BeautifulSoup(html, "html.parser")


# ── The company name, not the page name ────────────────────────────────────


@pytest.mark.parametrize(
    "title,expected",
    [
        # WordPress/Yoast default is "%%title%% %%sep%% %%sitename%%" - page
        # first, brand last - and it is everywhere on smaller European sites.
        ("Contact Us - Acme Nutrition GmbH", "Acme Nutrition GmbH"),
        ("Chi siamo | Vitalis Srl", "Vitalis Srl"),
        ("Startseite - Gruene Naehrstoffe GmbH", "Gruene Naehrstoffe GmbH"),
        ("Accueil | Sante Plus SARL", "Sante Plus SARL"),
        # The other convention must keep working exactly as before.
        ("Acme Nutrition GmbH - Contact Us", "Acme Nutrition GmbH"),
        ("Acme Nutrition GmbH | Integratori alimentari", "Acme Nutrition GmbH"),
    ],
)
def test_the_company_side_of_the_title_is_chosen(sm, title, expected):
    soup = _soup(f"<html><head><title>{title}</title></head></html>")
    assert sm._extract_org_name(soup, "https://acme.de/contact") == expected


def test_a_title_with_no_generic_half_is_unchanged(sm):
    """When neither side is a page name, keep the old behaviour - the first
    part. Guessing between two plausible halves would be worse than the
    convention we already assume."""
    soup = _soup(
        "<html><head><title>Acme Nutrition - Premium Supplements</title></head></html>"
    )
    assert sm._extract_org_name(soup, "https://acme.de/") == "Acme Nutrition"


def test_og_site_name_still_wins(sm):
    """The explicit signal beats any title heuristic."""
    soup = _soup(
        '<html><head><meta property="og:site_name" content="Acme Nutrition GmbH">'
        "<title>Contact Us - Something Else</title></head></html>"
    )
    assert sm._extract_org_name(soup, "https://acme.de/") == "Acme Nutrition GmbH"


# ── The company description, not the consent notice ────────────────────────


def test_the_cookie_banner_is_not_the_company_description(sm):
    """The banner is the first <p> in document order on most EU sites. Taken
    as the description it fed the vertical gate - so a real supplement company
    was dropped for showing no supplement signal - and went into the AI prompt,
    which would have opened a cold email by remarking on their cookie policy."""
    html = """
    <html><body>
    <div class="cookie-banner"><p>Wir verwenden Cookies, um Ihnen die beste
    Nutzererfahrung zu bieten.</p></div>
    <main><p>Gruene Naehrstoffe GmbH ist ein Hersteller von
    Nahrungsergaenzungsmitteln fuer den europaeischen Markt.</p></main>
    </body></html>
    """
    ctx = sm._extract_page_context(_soup(html)) or ""
    assert "Cookies" not in ctx
    assert "Nahrungsergaenzung" in ctx


@pytest.mark.parametrize(
    "markup",
    [
        '<div class="cookie-banner"><p>We use cookies to improve your visit.</p></div>',
        '<div id="cookie-consent"><p>Questo sito utilizza cookie tecnici.</p></div>',
        '<div class="gdpr-notice"><p>Ce site utilise des cookies.</p></div>',
        "<p>We use cookies on this website.</p>",
    ],
)
def test_consent_furniture_is_skipped_in_every_language(sm, markup):
    """Matched on the markup's class/id AND on the wording, because plenty of
    banners carry neither a helpful class name nor English text."""
    html = (
        f"<html><body>{markup}<p>We manufacture herbal supplements.</p></body></html>"
    )
    assert sm._extract_page_context(_soup(html)) == "We manufacture herbal supplements."


def test_a_page_with_only_a_banner_yields_nothing(sm):
    """Returning None is right: it means "we learned nothing about them",
    which is true. Returning the banner asserts something false."""
    html = '<html><body><div id="cookie-consent"><p>We use cookies.</p></div></body></html>'
    assert sm._extract_page_context(_soup(html)) is None


def test_the_meta_description_still_wins(sm):
    """Guard: the boilerplate filter must not disturb the preferred source."""
    html = (
        '<html><head><meta name="description" content="We make vitamins."></head>'
        "<body><p>We use cookies.</p></body></html>"
    )
    assert sm._extract_page_context(_soup(html)) == "We make vitamins."


def test_an_ordinary_first_paragraph_is_still_used(sm):
    """The common case must be untouched."""
    html = (
        "<html><body><p>We distribute food supplements across Italy.</p></body></html>"
    )
    assert (
        sm._extract_page_context(_soup(html))
        == "We distribute food supplements across Italy."
    )
