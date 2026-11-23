"""A lead found on a directory page is the point of the directory page.

Three defects in scrape_site's hop, all of which throw away good prospects or
hammer their websites, and none of which announces itself.

Guard 2 - "an address on a different mail domain is the web agency that built
the site" - only holds when the host IS the company. On a Europages listing, an
exhibitor page or a trade-press article, a different mail domain is the normal
case and the reason the page is worth reading at all. The guard was applied to
the FIRST fetch, so acquisti@nutrivita.it found on a europages listing was
discarded, and seven further requests were then spent probing /contatti at the
DIRECTORY's root, where the prospect's address could never be.

When the landing page fails, the hop had no identity to carry over and left the
contact holding the CONTACT page's own description - "Hier finden Sie das
Impressum". The vertical gate reads that as the company's business and rejects
a real supplement manufacturer for "no SUPPLECO vertical signal": a transient 503
deciding a lead, with a rejection reason that looks like the filter working.

And nothing deduplicated fetches, so one prospect's site could take 18 requests.
"""

from modules.search import SearchModule


class _FakeResponse:
    def __init__(self, body: str, status: int = 200):
        self.content = body.encode("utf-8")
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "err",
                request=None,
                response=type("R", (), {"status_code": self.status_code})(),
            )


class _FakeClient:
    """Serves canned pages and counts every request."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        key = url.rstrip("/")
        for candidate, body in self.pages.items():
            if candidate.rstrip("/") == key:
                if isinstance(body, int):
                    return _FakeResponse("", status=body)
                return _FakeResponse(body)
        return _FakeResponse("", status=404)


def _module(pages):
    """A SearchModule wired to canned pages, with no DB and no network.

    Built via __new__ so no real client, database or robots fetch is created;
    only the attributes the scrape path actually reads are set.
    """
    from modules.search import DEFAULT_BLOCKED_PREFIXES

    sm = SearchModule.__new__(SearchModule)
    sm._client = _FakeClient(pages)
    sm.rate_limit = 0.0
    sm.max_results = 10
    sm.blocked_prefixes = DEFAULT_BLOCKED_PREFIXES
    sm._robots_cache = {}
    sm._robots_allows = lambda url: True
    return sm


DIRECTORY = (
    "<html><head><title>Nutrivita Srl - Produttore di integratori alimentari "
    "| Europages</title></head><body>"
    '<a href="mailto:acquisti@nutrivita.it">acquisti@nutrivita.it</a>'
    "</body></html>"
)


def test_a_lead_on_a_directory_listing_is_kept():
    """The address IS the listing's content. Discarding it and then crawling
    the directory's own contact pages is exactly backwards."""
    url = "https://www.europages.co.uk/Nutrivita/00000012345.html"
    sm = _module({url: DIRECTORY})

    found = sm.scrape_site(url)

    assert [c["email"] for c in found] == ["acquisti@nutrivita.it"], (
        "a prospect published on a directory page was thrown away"
    )


def test_it_does_not_crawl_the_directorys_own_contact_pages():
    """The wasted-request half. /contatti at europages.co.uk will never hold
    the prospect's address, and each probe costs a politeness sleep."""
    url = "https://www.europages.co.uk/Nutrivita/00000012345.html"
    sm = _module({url: DIRECTORY})

    sm.scrape_site(url)

    assert len(sm._client.requested) == 1, (
        f"spent {len(sm._client.requested)} requests on a page that already "
        f"answered: {sm._client.requested}"
    )


def test_an_agency_credit_on_the_companys_own_page_is_still_refused():
    """The guard must keep working where it was right. An Impressum belongs to
    the site owner, so a third-party address on it is the agency that built
    the site - and this is the case the guard exists for."""
    url = "https://www.vitalproducts.de/impressum"
    sm = _module(
        {
            url: (
                "<html><head><title>Impressum</title></head><body>"
                '<a href="mailto:kontakt@webdesign-agentur.de">agency</a>'
                "</body></html>"
            )
        }
    )

    assert sm.scrape_site(url) == []


def test_a_company_page_still_prefers_its_own_address():
    """Control: on the company's own site the guard still picks the company."""
    url = "https://www.goerlich-pharma.de/kontakt"
    sm = _module(
        {
            url: (
                "<html><head><title>Goerlich Pharma - Kontakt</title></head><body>"
                '<a href="mailto:einkauf@goerlich-pharma.de">us</a>'
                '<a href="mailto:hallo@agentur.de">agency</a>'
                "</body></html>"
            )
        }
    )

    assert [c["email"] for c in sm.scrape_site(url)] == ["einkauf@goerlich-pharma.de"]


def test_a_failed_landing_page_does_not_hand_over_the_wrong_description():
    """A 503 must not make the contact inherit the contact page's text. The
    vertical gate reads qualification_rationale, and "Hier finden Sie das
    Impressum" is not a description of a supplement manufacturer."""
    home = "https://www.brand-pharma.de/"
    sm = _module(
        {
            home: 503,
            "https://www.brand-pharma.de/impressum": (
                "<html><head><title>Impressum</title>"
                '<meta name="description" content="Hier finden Sie das Impressum">'
                "</head><body>"
                '<a href="mailto:einkauf@brand-pharma.de">x</a>'
                "</body></html>"
            ),
        }
    )

    found = sm.scrape_site(home)

    assert found, "the address on the contact page was lost entirely"
    rationale = found[0].get("qualification_rationale")
    assert not rationale or "Impressum" not in rationale, (
        f"the contact carried the legal page's own description: {rationale!r}"
    )


def test_a_page_is_fetched_once_per_run():
    """Dedup at the fetch chokepoint. Two calls for the same URL must cost one
    request, however many callers ask for it."""
    url = "https://www.brand.de/kontakt"
    sm = _module({url: "<html><body>a@brand.de</body></html>"})

    sm._scrape_page(url)
    sm._scrape_page(url)
    sm._scrape_page(url.rstrip("/") + "/")

    assert len(sm._client.requested) == 1, sm._client.requested


def test_the_cached_result_is_not_shared_by_reference():
    """scrape_site mutates the dicts it gets - organization, source_url. A
    cache handing out the same objects would let one call corrupt the next."""
    url = "https://www.brand.de/kontakt"
    sm = _module({url: "<html><body>a@brand.de</body></html>"})

    first = sm._scrape_page(url)
    first[0]["organization"] = "MUTATED"
    second = sm._scrape_page(url)

    assert second[0]["organization"] != "MUTATED"
