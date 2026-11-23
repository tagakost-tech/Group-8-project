"""The company's country is read from the company, deterministic evidence first.

An Italian company writing from a .com or a gmail address got the English
email, because only the address was read. At collection the company's own
website now counts (its domain), and so does the page the address was found
on: an Italian VAT number, a legal form only Italy uses, or two weaker signs
together (a legal form Italy shares, a +39 number, a page in Italian). One
weak sign alone is not enough, since an Italian email to someone who cannot
read it is the worse mistake: a wellness "Spa", a Romanian S.R.L., a French
SAS and a +39 number on an English page stay unknown. A directory's page is
not the company's, so its domain and its footer count for nothing.
"""

import pytest

from modules.company_country import collected_country, page_country, stored_evidence
from modules.database import Database
from modules.search import DEFAULT_BLOCKED_PREFIXES, SearchModule

# ── What a page shows ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,lang,organization",
    [
        ("Rossi Integratori - P.IVA 01234567897 - Milano", "en", None),
        ("Partita IVA: IT01234567897", None, None),
        ("Sede legale Milano P. IVA 01234567890", None, None),
        ("P.IVA/C.F. 01234567897", None, None),
        ("p.iva 01234567897", None, None),
        ("Contatti", None, "Rossi Integratori S.p.A."),
        ("Verdi S.P.A. - Milano", None, None),
        ("Verdi S.r.l.s.", None, None),
        ("Bianchi Integratori S.r.l.", "it", None),
        ("Chiamaci: +39 02 1234567", "it-IT", None),
        ("Bianchi Srl - tel. 0039 02 1234567", None, None),
        ("Bianchi SpA - tel. +39 333 1234567", None, None),
        ("Contatti", "it_IT", "Bianchi Integratori Snc"),
    ],
)
def test_an_italian_company_page_is_italian(text, lang, organization):
    assert page_country(text, lang, organization) == "IT"


@pytest.mark.parametrize(
    "text,lang,organization",
    [
        ("Wellness Spa & Resort - book your stay", "en", "Alpen Spa Resort"),
        ("SPA DAY OFFERS", "en", "SPA Center"),
        ("our spa and sauna", "en", None),
        ("Call us: +39 02 1234567", "en", None),
        ("Fiterman Pharma S.R.L. - Iasi", "ro", None),
        ("Fiterman Pharma S.R.L. - Iasi", None, None),
        ("Contact", None, "Fiterman Pharma S.r.l."),
        ("Laboratoires Dupont SAS - Lyon", "fr", None),
        ("Nutri SpA - Santiago de Chile", "es", None),
        ("P.IVA 012345678901", None, None),
        ("P.IVA 0123456789", None, None),
        ("Prices include IVA at 22 percent", "en", None),
        ("Order 12345678901 shipped", "en", None),
        ("Benvenuti nel nostro negozio", "it", None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_one_weak_sign_or_a_foreign_one_is_not_enough(text, lang, organization):
    assert page_country(text, lang, organization) is None


@pytest.mark.parametrize(
    "text,lang,organization",
    [
        ("Sports nutrition from S.P.A.R.K.", None, None),
        ("Contact", None, "S.P.A.R.K. Nutrition"),
        ("Contact", None, "s.r.l.s.x Labs"),
        ("Our S.P.A. (Salus Per Aquam) menu. Book a massage.", "en", "Alpen Wellness"),
    ],
)
def test_an_acronym_or_a_spa_in_capitals_is_no_legal_form(text, lang, organization):
    """S.P.A.R.K. matched as S.P.A. followed by more letters, and a spa
    writing S.P.A. on an English page read as an Italian company: the
    Italian email, for life."""
    assert page_country(text, lang, organization) is None


@pytest.mark.parametrize(
    "text,lang,organization",
    [
        ("ROSSI S.P.A. - Milano", "it", None),
        ("Contact us", "en", "ROSSI INTEGRATORI S.P.A."),
        ("Rossi Integratori S.p.A. - export", "en", None),
    ],
)
def test_an_italian_name_in_capitals_still_counts(text, lang, organization):
    assert page_country(text, lang, organization) == "IT"


@pytest.mark.parametrize(
    "text,lang,organization",
    [
        (
            (
                "Nordbrand GmbH, Berlin. Impressum. Website: Pixel Web S.r.l. - "
                "P.IVA 01234567890"
            ),
            "de",
            "Nordbrand GmbH",
        ),
        ("Nordbrand GmbH. Design: Pixel S.r.l.s.", None, "Nordbrand GmbH"),
        ("Site by Studio Verdi S.p.A. - tel. +39 02 1234567", "it", "Acme Ltd."),
        ("Credits: Rossi Web S.r.l.s.", None, "Northwind Foods Sp. z o.o."),
    ],
)
def test_a_foreign_company_crediting_an_italian_agency_is_not_italian(
    text, lang, organization
):
    """An Italian web agency's VAT number or legal form in a German firm's
    footer made the firm Italian: its email, and every one after it, in
    Italian."""
    assert page_country(text, lang, organization) is None


@pytest.mark.parametrize(
    "organization",
    ["Integratori Rossi", "Incanto S.r.l.s.", "Agrifood S.p.A.", "Corpo Sano S.r.l.s."],
)
def test_an_italian_name_is_not_read_as_a_foreign_form(organization):
    assert page_country("P.IVA 01234567890", None, organization) == "IT"


# ── Only the company's own pages ───────────────────────────────────────────


@pytest.mark.parametrize(
    "url,channel,page,expected",
    [
        ("https://www.rossi.it/contatti", "contact_page", None, ("IT", "website")),
        ("https://www.rossi.it/", "own_website", "IT", ("IT", "website")),
        ("https://www.brand.com/", "own_website", "IT", ("IT", "page")),
        ("https://www.brand.de/", "own_website", "IT", ("DE", "website")),
        ("https://www.brand.co.uk/", "contact_page", None, ("GB", "website")),
        ("https://www.brand.eu/", "own_website", None, (None, None)),
        ("https://www.brand.com/", "own_website", None, (None, None)),
        ("https://brand.io/", "own_website", None, (None, None)),
        ("https://www.europages.it/x", "third_party", "IT", (None, None)),
        ("https://www.rossi.it/", None, "IT", (None, None)),
        ("https://www.rossi.it/", "referral", None, (None, None)),
    ],
)
def test_only_the_companys_own_pages_count(url, channel, page, expected):
    assert collected_country(url, channel, page) == expected


def test_stored_evidence_reads_the_organisation_and_the_description():
    contact = {
        "source_url": "https://www.rossi-integratori.com/",
        "source_channel": "own_website",
        "organization": "Rossi Integratori S.p.A.",
        "qualification_rationale": "Email found on page: x. Page context: N/A",
    }
    assert stored_evidence(contact) == ("IT", "page")
    assert stored_evidence({**contact, "source_channel": "third_party"}) == (
        None,
        None,
    )
    assert stored_evidence({**contact, "organization": "Rossi"}) == (None, None)


# ── At collection, through the scraper ─────────────────────────────────────


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


def _module(pages, db=None, urls=()):
    sm = SearchModule.__new__(SearchModule)
    sm._client = _Client(pages)
    sm.rate_limit = 0.0
    sm.max_results = 10
    sm.blocked_prefixes = DEFAULT_BLOCKED_PREFIXES
    sm._robots_cache = {}
    sm._robots_allows = lambda url: True
    sm.db = db
    sm._ddg_search = lambda q: list(urls)
    return sm


def _html(title, body, lang="en"):
    return (
        f'<html lang="{lang}"><head><title>{title}</title></head>'
        f"<body>{body}</body></html>"
    )


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "c.db"))


def _stored(db, stored):
    row = db.get_contact_by_id(stored["id"])
    return row["company_country"], row["company_country_source"]


def test_the_home_pages_vat_number_reaches_a_contact_found_one_hop_away(db):
    home = "https://www.rossi-integratori.com/"
    pages = {
        home: _html("Rossi Integratori", "Benvenuti. P.IVA 01234567897"),
        "https://www.rossi-integratori.com/contact": _html(
            "Contact", '<a href="mailto:acquisti@rossi-integratori.com">write</a>'
        ),
    }
    stored = _module(pages, db, [home]).search_and_extract("integratori")
    assert [c["source_channel"] for c in stored] == ["contact_page"]
    assert _stored(db, stored[0]) == ("IT", "page")


def test_an_italian_website_is_stored_as_italian(db):
    url = "https://www.nordbrand.it/"
    pages = {url: _html("Nordbrand", '<a href="mailto:einkauf@nordbrand.de">x</a>')}
    stored = _module(pages, db, [url]).search_and_extract("nordbrand")
    assert _stored(db, stored[0]) == ("IT", "website")


def test_a_directory_listing_stores_no_country(db):
    url = "https://www.europages.it/Brandco/00000012345.html"
    body = (
        '<a href="mailto:einkauf@brandco.de">x</a> Europages S.r.l. P.IVA 01234567897'
    )
    pages = {url: _html("Brandco GmbH - Europages", body, lang="it")}
    stored = _module(pages, db, [url]).search_and_extract("brandco")
    assert [c["source_channel"] for c in stored] == ["third_party"]
    assert _stored(db, stored[0]) == (None, None)


def test_a_wellness_spa_on_an_english_page_stores_no_country(db):
    url = "https://www.alpenspa.com/"
    body = '<a href="mailto:info@alpenspa.com">x</a> Alpen Spa Resort. +39 0471 123456'
    pages = {url: _html("Alpen Spa Resort", body)}
    stored = _module(pages, db, [url]).search_and_extract("spa")
    assert _stored(db, stored[0]) == (None, None)


def test_a_page_in_italian_with_an_italian_number_is_stored_as_italian(db):
    url = "https://www.verdi-nutrizione.com/"
    body = '<a href="mailto:ordini@verdi-nutrizione.com">x</a> Tel. +39 02 1234567'
    pages = {url: _html("Verdi Nutrizione", body, lang="it")}
    stored = _module(pages, db, [url]).search_and_extract("verdi")
    assert _stored(db, stored[0]) == ("IT", "page")
