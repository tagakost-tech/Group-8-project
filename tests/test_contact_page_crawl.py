"""One extra hop to the contact page - with the three guards that make it safe.

Measured on 23 real target sites reached through the agent's own discovery
path: the homepage alone yielded 16 addresses, and following to
/contact, /kontakt, /contatti, /impressum or /mentions-legales yielded 27.
Two companies that published nothing on the homepage gave up 7 and 1.

The naive version of this is actively harmful, which is what the guards are
for. Each is pinned by its own test below:

1. CONTEXT STAYS WITH THE HOMEPAGE. The relevance gate and the AI classifier
   judge a company from its page text. Measured: goerlich-pharma's homepage
   says "Hersteller von Nahrungsergaenzungsmitteln", its Impressum says "Hier
   finden Sie das Impressum"; deltha's contact page is base64 page-builder
   junk; naturacare's is "About us". All three are rejected by the vertical
   gate. Harvesting an address and then binning the lead is worse than not
   harvesting it.

2. THE ADDRESS MUST BE THE COMPANY'S OWN. German Impressum pages credit the
   agency that built the site: the only address this hop gained on
   vitalproducts.de was info@memaba-design.de, a "Webdesign und Marketing
   Agentur". Without this guard the change quietly adds a whole category of
   wrong recipients.

3. ONE ADDRESS PER COMPANY. delthapharma publishes seven (info@, ordini@,
   commerciale@, amministrazione@, area_medica@, qualita@...). Each would
   become its own contact, and the domain cap permits three sends a day to
   one company - which reads as spam to the recipient and to the filters.
"""

import httpx
import pytest

from modules.search import SearchModule

ROBOTS_OK = "User-agent: *\nDisallow:\n"

HOME = (
    "<html><head><title>Goerlich Pharma</title>"
    "<meta name='description' content='Hersteller von "
    "Nahrungsergaenzungsmitteln in Bayern'></head>"
    "<body>Willkommen</body></html>"
)
KONTAKT = (
    "<html><head><title>Impressum</title>"
    "<meta name='description' content='Hier finden Sie das Impressum'>"
    "</head><body>E-Mail: <a href='mailto:einkauf@goerlich-pharma.com'>"
    "einkauf@goerlich-pharma.com</a></body></html>"
)


@pytest.fixture
def sm(db):
    return SearchModule(db, rate_limit=0.0)


def _serve(monkeypatch, sm, pages, robots=ROBOTS_OK):
    """Serve a fixed site. `pages` maps path -> html; anything else 404s."""
    fetched = []

    def fake_get(url, *a, **kw):
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=robots, request=httpx.Request("GET", url))
        fetched.append(url)
        path = url.split("goerlich-pharma.com", 1)[-1] or "/"
        path = path.split("brand.de", 1)[-1] or "/"
        if path in pages:
            return httpx.Response(
                200, text=pages[path], request=httpx.Request("GET", url)
            )
        return httpx.Response(404, text="nope", request=httpx.Request("GET", url))

    monkeypatch.setattr(sm._client, "get", fake_get)
    return fetched


# ── The hop itself ────────────────────────────────────────────────────────


def test_an_address_only_on_the_contact_page_is_found(monkeypatch, sm):
    """The whole point: the homepage publishes nothing, the Kontakt page does."""
    _serve(monkeypatch, sm, {"/": HOME, "/kontakt": KONTAKT})
    contacts = sm.scrape_site("https://goerlich-pharma.com/")
    assert [c["email"] for c in contacts] == ["einkauf@goerlich-pharma.com"]


def test_a_site_publishing_on_its_homepage_needs_no_extra_hop(monkeypatch, sm):
    """Politeness: do not spend four requests on a site that already answered."""
    home_with_email = HOME.replace(
        "<body>Willkommen", "<body>Kontakt: einkauf@goerlich-pharma.com"
    )
    fetched = _serve(monkeypatch, sm, {"/": home_with_email, "/kontakt": KONTAKT})
    contacts = sm.scrape_site("https://goerlich-pharma.com/")
    assert [c["email"] for c in contacts] == ["einkauf@goerlich-pharma.com"]
    assert len(fetched) == 1, f"probed contact pages unnecessarily: {fetched}"


def test_the_hop_stops_at_the_first_page_that_answers(monkeypatch, sm):
    """Four probes per site at a 2s politeness delay is 8s; stop when done."""
    fetched = _serve(monkeypatch, sm, {"/": HOME, "/kontakt": KONTAKT})
    sm.scrape_site("https://goerlich-pharma.com/")
    assert not any("/impressum" in u for u in fetched), (
        f"kept probing after finding an address: {fetched}"
    )


# ── Guard 1: the AI must still see the homepage's description ─────────────


def test_the_company_description_comes_from_the_homepage(monkeypatch, sm):
    """Measured degradation this prevents: the vertical gate keeps
    "Hersteller von Nahrungsergaenzungsmitteln" and rejects "Hier finden Sie
    das Impressum". Taking context from the contact page would harvest the
    address and then bin the lead."""
    _serve(monkeypatch, sm, {"/": HOME, "/kontakt": KONTAKT})
    c = sm.scrape_site("https://goerlich-pharma.com/")[0]
    assert "Nahrungsergaenzungsmitteln" in (c["qualification_rationale"] or "")
    assert "Impressum" not in (c["qualification_rationale"] or "")


def test_the_organisation_name_comes_from_the_homepage(monkeypatch, sm):
    """The contact page's <title> is "Impressum". That string would otherwise
    become the organisation - and the organisation goes into the subject line
    of the email we send."""
    _serve(monkeypatch, sm, {"/": HOME, "/kontakt": KONTAKT})
    c = sm.scrape_site("https://goerlich-pharma.com/")[0]
    assert c["organization"] == "Goerlich Pharma"


# ── Guard 2: the address must belong to the company ───────────────────────


def test_the_web_agencys_address_in_the_impressum_is_not_harvested(monkeypatch, sm):
    """Observed live on vitalproducts.de, whose Impressum credits
    info@memaba-design.de - a web design agency, not a supplement company."""
    impressum = (
        "<html><head><title>Impressum</title></head><body>"
        "Umsetzung: <a href='mailto:info@memaba-design.de'>memaba</a>"
        "</body></html>"
    )
    _serve(monkeypatch, sm, {"/": HOME, "/impressum": impressum})
    assert sm.scrape_site("https://goerlich-pharma.com/") == []


def test_a_subdomain_of_the_company_still_counts_as_its_own(monkeypatch, sm):
    """mail.goerlich-pharma.com is the company; treat it as such."""
    kontakt = KONTAKT.replace(
        "einkauf@goerlich-pharma.com", "einkauf@mail.goerlich-pharma.com"
    )
    _serve(monkeypatch, sm, {"/": HOME, "/kontakt": kontakt})
    contacts = sm.scrape_site("https://goerlich-pharma.com/")
    assert [c["email"] for c in contacts] == ["einkauf@mail.goerlich-pharma.com"]


# ── Guard 3: one address per company ──────────────────────────────────────


def test_only_one_address_per_company_is_kept(monkeypatch, sm):
    """delthapharma publishes seven. Emailing several people at one company on
    one day reads as spam - to the recipient and to the filters."""
    many = (
        "<html><head><title>Contatti</title></head><body>"
        "<a href='mailto:info@goerlich-pharma.com'>a</a>"
        "<a href='mailto:ordini@goerlich-pharma.com'>b</a>"
        "<a href='mailto:qualita@goerlich-pharma.com'>c</a>"
        "</body></html>"
    )
    _serve(monkeypatch, sm, {"/": HOME, "/kontakt": many})
    contacts = sm.scrape_site("https://goerlich-pharma.com/")
    assert len(contacts) == 1, [c["email"] for c in contacts]


def test_a_buying_contact_is_preferred_over_a_generic_inbox(monkeypatch, sm):
    """Given a choice, write to the person who buys, not to reception."""
    many = (
        "<html><head><title>Contatti</title></head><body>"
        "<a href='mailto:info@goerlich-pharma.com'>a</a>"
        "<a href='mailto:einkauf@goerlich-pharma.com'>b</a>"
        "</body></html>"
    )
    _serve(monkeypatch, sm, {"/": HOME, "/kontakt": many})
    contacts = sm.scrape_site("https://goerlich-pharma.com/")
    assert [c["email"] for c in contacts] == ["einkauf@goerlich-pharma.com"]


# ── It must still obey robots.txt ─────────────────────────────────────────


def test_the_hop_respects_robots(monkeypatch, sm):
    fetched = _serve(
        monkeypatch,
        sm,
        {"/": HOME, "/kontakt": KONTAKT},
        robots="User-agent: *\nDisallow: /kontakt\n",
    )
    sm.scrape_site("https://goerlich-pharma.com/")
    assert not any("/kontakt" in u for u in fetched), (
        f"fetched a disallowed contact page: {fetched}"
    )


def test_the_same_brand_on_a_different_tld_is_still_the_company(monkeypatch, sm):
    """Observed live: delthapharma.com publishes seven addresses, all
    @delthapharma.it. Comparing whole domains rejected every one of them, so
    the richest site in the sample yielded nothing.

    The guard exists to exclude the web AGENCY credited in an Impressum, and
    an agency has a different NAME - not merely a different TLD. Compare the
    brand label, so delthapharma.com/.it matches while
    vitalproducts.de/memaba-design.de still does not.
    """
    contatti = (
        "<html><head><title>Contatti</title></head><body>"
        "<a href='mailto:export@goerlich-pharma.it'>e</a></body></html>"
    )
    _serve(monkeypatch, sm, {"/": HOME, "/kontakt": contatti})
    contacts = sm.scrape_site("https://goerlich-pharma.com/")
    assert [c["email"] for c in contacts] == ["export@goerlich-pharma.it"]


def test_brand_label_extraction_handles_real_domain_shapes():
    """Includes co.uk, where taking the last two labels yields "co.uk" and
    would make every British company look like every other."""
    from modules.search import SearchModule as S

    assert S._brand_label("delthapharma.com") == "delthapharma"
    assert S._brand_label("delthapharma.it") == "delthapharma"
    assert S._brand_label("www.goerlich-pharma.com") == "goerlich-pharma"
    assert S._brand_label("mail.goerlich-pharma.com") == "goerlich-pharma"
    assert S._brand_label("brand.co.uk") == "brand"
    assert S._brand_label("memaba-design.de") == "memaba-design"
    # Two different British companies must not collapse onto "co.uk".
    assert S._brand_label("alpha.co.uk") != S._brand_label("beta.co.uk")


# ── Guard 2 must cover the FIRST fetch, not only the hop ──────────────────


def test_an_agency_address_on_the_landing_page_is_refused(monkeypatch, sm):
    """Guard 2 was wired only into the contact-page loop, so an address found
    on the very first fetch skipped it entirely.

    That path is not hypothetical: _ddg_search retries with a "<query>
    contact" variant, which makes a thin, agency-credited contact page the
    FIRST thing scraped. Reproduced: the
    agency address came back with the organisation set to "Impressum", which
    is what goes into the subject line of the mail we send.
    """
    impressum = (
        "<html><head><title>Impressum</title></head><body>"
        "Umsetzung: <a href='mailto:info@memaba-design.de'>memaba</a>"
        "</body></html>"
    )
    _serve(monkeypatch, sm, {"/": impressum, "/impressum": impressum})
    assert sm.scrape_site("https://goerlich-pharma.com/") == []


def test_the_hop_still_runs_when_the_landing_page_held_only_an_agency(monkeypatch, sm):
    """Rejecting the agency address must not end the site: the company's own
    address may still be one hop away."""
    agency_home = (
        "<html><head><title>Goerlich Pharma</title>"
        "<meta name='description' content='Hersteller von "
        "Nahrungsergaenzungsmitteln'></head><body>"
        "Umsetzung: <a href='mailto:info@memaba-design.de'>m</a>"
        "</body></html>"
    )
    _serve(monkeypatch, sm, {"/": agency_home, "/kontakt": KONTAKT})
    contacts = sm.scrape_site("https://goerlich-pharma.com/")
    assert [c["email"] for c in contacts] == ["einkauf@goerlich-pharma.com"]


# ── The landing page must not be fetched twice ────────────────────────────


def test_the_landing_page_is_fetched_only_once(monkeypatch, sm):
    """_page_identity re-fetched the page _scrape_page had just read, because
    _scrape_page only computes the organisation AFTER finding an address. That
    is one wasted request against every site that needs the hop - which is
    exactly the sites we are already asking the most of - and it arrived with
    no politeness delay between the two."""
    fetched = _serve(monkeypatch, sm, {"/": HOME, "/kontakt": KONTAKT})
    sm.scrape_site("https://goerlich-pharma.com/")
    landing = [u for u in fetched if u.rstrip("/").endswith("goerlich-pharma.com")]
    assert len(landing) == 1, f"landing page fetched {len(landing)}x: {fetched}"


def test_the_identity_survives_without_a_second_request(monkeypatch, sm):
    """The organisation and description must still come from the landing page
    even though it published no address - that is the whole of guard 1, and it
    must not depend on a second fetch that could fail."""
    fetched = _serve(monkeypatch, sm, {"/": HOME, "/kontakt": KONTAKT})
    c = sm.scrape_site("https://goerlich-pharma.com/")[0]
    assert c["organization"] == "Goerlich Pharma"
    assert "Nahrungsergaenzungsmitteln" in (c["qualification_rationale"] or "")
    # The landing page is read once and once only. Asserting a total request
    # count instead would pin the contact-path ORDER, which is a tuning knob:
    # a .com uses the default order and legitimately probes /contact and
    # /contact-us before reaching /kontakt.
    assert fetched.count("https://goerlich-pharma.com/") == 1, fetched
