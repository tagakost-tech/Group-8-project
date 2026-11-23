"""Tests for the Phase 1 RelevanceFilter (geo + negative + vertical gates)."""

from modules.relevance import RelevanceFilter


def test_rejects_non_european_cctld():
    r = RelevanceFilter().assess(
        {"email": "contactus@etciso.in", "organization": "ET CISO"}
    )
    assert r["relevant"] is False
    assert "non-European" in r["reason"]


def test_rejects_missing_vertical():
    r = RelevanceFilter(require_vertical=True).assess(
        {"email": "anonymous@cnn.com", "organization": "CNN"}
    )
    assert r["relevant"] is False


def test_accepts_european_vertical():
    # A BUYER: a supplement brand that might outsource production to SUPPLECO.
    r = RelevanceFilter().assess(
        {
            "email": "info@example.de",
            "organization": "BioPharma Nutrition GmbH",
            "qualification_rationale": "Brand selling food supplements and vitamins",
        }
    )
    assert r["relevant"] is True
    assert r["vertical_match"] is True


def test_rejects_competitor_contract_manufacturers():
    """SUPPLECO *is* a contract manufacturer. Rivals match every supplement
    keyword we search for, so without a dedicated gate the agent emails them
    offering to manufacture on their behalf. The AI classifier is asked to catch
    this too, but it fails open - this gate must not depend on it."""
    f = RelevanceFilter(require_vertical=False)
    for org in [
        "Nutrastar Supplement Contract Manufacturing GmbH",
        "BioLab Private Label Nutraceutical Manufacturer",
        "OEM/ODM supplement manufacturing services",
        "Integratori alimentari conto terzi Srl",
        "Nahrungsergänzung Lohnhersteller GmbH",
    ]:
        r = f.assess({"email": "info@example.de", "organization": org})
        assert r["relevant"] is False, org
        assert "competitor" in r["reason"], org


def test_negative_keyword_rejected():
    r = RelevanceFilter().assess(
        {
            "email": "info@example.fr",
            "organization": "Auto Portal",
            "qualification_rationale": "automotive news portal",
        }
    )
    assert r["relevant"] is False


def test_a_newsletter_signup_does_not_make_a_brand_a_news_outlet():
    """The rejection gates match NEGATIVE_KEYWORDS as whole words. Under a bare
    substring test "news" fires inside "newsletter" - and practically every
    company website carries "Subscribe to our newsletter" in its meta
    description or first paragraph, which is precisely the text scraped into
    qualification_rationale.

    That rejects qualified SUPPLECO prospects wholesale, and logs it as an
    ordinary off-target rejection, so the funnel just looks like a market with
    few good leads in it. Nothing errors and nothing is retried: the lead is
    gone.
    """
    r = RelevanceFilter().assess(
        {
            "email": "info@brand.de",
            "organization": "BioPharma Nutrition GmbH",
            "qualification_rationale": (
                "Brand selling food supplements and vitamins. "
                "Subscribe to our newsletter for monthly offers."
            ),
        }
    )
    assert r["relevant"] is True, r["reason"]
    assert r["vertical_match"] is True


def test_a_real_news_outlet_is_still_rejected():
    """The control for the test above. Whole-word matching must not amount to
    switching the negative gate off - SUPPLECO has no reason to cold-email a
    newspaper, and 'news' as an actual word must still reject."""
    r = RelevanceFilter(require_vertical=False).assess(
        {
            "email": "redaktion@verlag.de",
            "organization": "Rheinische Post",
            "qualification_rationale": "Regional news and press release portal",
        }
    )
    assert r["relevant"] is False
    assert "news" in r["reason"]


def test_require_vertical_false_allows_generic_european():
    r = RelevanceFilter(require_vertical=False).assess(
        {"email": "info@example.pl", "organization": "Some Company"}
    )
    assert r["relevant"] is True


# --- Geo gate: non-European region in the mailbox local part (generic TLD) ---


def test_rejects_non_eu_region_in_localpart():
    for email in (
        "sales.thai@intraceuticals.com",
        "sales.au@intraceuticals.com",
        "sales.us@intraceuticals.com",
        "office.kr@brand.com",
    ):
        r = RelevanceFilter(require_vertical=False).assess({"email": email})
        assert r["relevant"] is False, email
        assert "regional" in r["reason"], email


def test_keeps_european_region_in_localpart():
    # European regional mailboxes and ordinary words must NOT be rejected.
    for email in (
        "sales.uk@intraceuticals.com",
        "info.germany@grifols.com",
        "august@company.de",
        "business@nordicpremium.se",
        "customerservice@brand.fr",
    ):
        r = RelevanceFilter(require_vertical=False).assess({"email": email})
        assert r["relevant"] is True, email


def test_rejects_non_eu_consumer_domain_and_subdomain():
    for email in ("chun.hun@hinet.net", "chun.hun@msa.hinet.net"):
        r = RelevanceFilter(require_vertical=False).assess({"email": email})
        assert r["relevant"] is False, email
        assert "mail domain" in r["reason"], email


def test_rejects_placeholder_address():
    for email in ("user@domain.com", "name@example.com", "youremail@yourcompany.com"):
        r = RelevanceFilter(require_vertical=False).assess({"email": email})
        assert r["relevant"] is False, email
        assert "placeholder" in r["reason"], email


def test_rejects_machine_generated_and_subdomain_junk():
    # Sentry event ids scraped from a site's error-tracking config, on a
    # placeholder-domain subdomain - never real leads.
    for email in (
        "271e9fa3230b4eec94b02bf95780f5f2@sentry.wixpress.com",
        "460ff4620fa44cba8df530afde949785@o12345.ingest.sentry.io",
    ):
        r = RelevanceFilter(require_vertical=False).assess({"email": email})
        assert r["relevant"] is False, email


def test_keeps_normal_alphanumeric_localpart():
    # A short alnum mailbox that merely contains hex chars must NOT be flagged.
    for email in ("info@brand.de", "ab12@company.fr", "sales2024@firm.it"):
        r = RelevanceFilter(require_vertical=False).assess({"email": email})
        assert r["relevant"] is True, email


def test_region_token_only_matches_whole_token():
    # 'us'/'in' embedded inside a word must not trigger the region gate.
    for email in ("august@company.de", "business@brand.fr", "info@firm.it"):
        r = RelevanceFilter(require_vertical=False).assess({"email": email})
        assert r["relevant"] is True, email
