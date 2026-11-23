"""Certified mail (PEC) is not a marketing channel, and one company is one contact.

Observed live on revalfarma.com, which yielded TWO contacts for one company:

    info@revalfarma.com
    info.revalfarma@pec.it

Two separate problems in one page.

PEC - Posta Elettronica Certificata - is Italy's legally registered certified
mail. It carries the evidential weight of a registered letter and exists for
formal and legal correspondence: contracts, disputes, filings, dealings with
the public administration. Cold marketing sent there is both ineffective and
conspicuously wrong to an Italian recipient, and SUPPLECO is itself Italian, so
its prospects will read it as a company that does not know the conventions.
These addresses must never be contacted at all.

Separately, guard 3 ("one address per company") grouped by email DOMAIN, so
one company publishing two mail domains still produced two contacts. Grouping
by the BRAND LABEL - the same comparison guard 2 already uses - collapses
brand.com and brand.de into one.
"""

import pytest

from modules.search import SearchModule


@pytest.fixture
def sm(db):
    return SearchModule(db, rate_limit=0.0)


PEC_PAGE = (
    "<html><head><title>Revalfarma</title></head><body>"
    "<a href='mailto:info@revalfarma.com'>a</a>"
    "<a href='mailto:info.revalfarma@pec.it'>b</a>"
    "</body></html>"
)


# ── PEC addresses are never harvested ─────────────────────────────────────


def test_a_pec_address_is_not_harvested(sm):
    found = sm._extract_emails(PEC_PAGE)
    assert "info@revalfarma.com" in found
    assert "info.revalfarma@pec.it" not in found


@pytest.mark.parametrize(
    "address",
    [
        "azienda@pec.it",
        "azienda@legalmail.it",
        "azienda@pec.aruba.it",
        "azienda@arubapec.it",
        "azienda@postacert.it",
        "comune@postacert.comune.milano.it",
        "studio@cert.legalmail.it",
        "impresa@pec.brand.it",  # a company's own PEC subdomain
    ],
)
def test_every_common_certified_mail_domain_is_refused(sm, address):
    html = f"<html><body><a href='mailto:{address}'>x</a></body></html>"
    assert sm._extract_emails(html) == []


@pytest.mark.parametrize(
    "address",
    [
        "buyer@brand.it",
        "info@concert.it",  # contains "cert", is not certified mail
        "info@specialita.it",  # contains "pec", is not certified mail
        "info@pecorino-shop.it",  # starts with "pec", is a cheese shop
        "sales@postacomunale.it",  # contains "posta", is not postacert
    ],
)
def test_an_ordinary_address_that_merely_looks_like_pec_is_kept(sm, address):
    """Matched on whole domain LABELS, never substrings. This codebase's most
    frequent defect is a substring standing in for a token - "cert" inside
    "concert", "pec" inside "specialita" - and here it would silently discard
    real Italian prospects."""
    html = f"<html><body><a href='mailto:{address}'>x</a></body></html>"
    assert sm._extract_emails(html) == [address]


# ── One company is one contact, across mail domains ───────────────────────


def test_the_same_company_on_two_mail_domains_yields_one_contact(sm):
    """A company publishing info@brand.com and einkauf@brand.de is one
    company. Grouping by domain sent it two emails; grouping by brand label
    sends it one - and picks the buying mailbox."""
    contacts = [
        {"email": "info@brand.com"},
        {"email": "einkauf@brand.de"},
    ]
    kept = sm._one_per_company(contacts)
    assert [c["email"] for c in kept] == ["einkauf@brand.de"]


def test_two_genuinely_different_companies_are_both_kept(sm):
    """The dedupe must not over-merge: these are two prospects, not one."""
    contacts = [
        {"email": "info@alpha.de"},
        {"email": "info@beta.de"},
    ]
    kept = sm._one_per_company(contacts)
    assert sorted(c["email"] for c in kept) == ["info@alpha.de", "info@beta.de"]
