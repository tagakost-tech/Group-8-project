"""Personal mailboxes are screened across Europe, not only in the US.

PERSONAL_DOMAINS held ten providers, all American: gmail, yahoo.com,
hotmail.com and so on. A small Italian supplement shop publishing
mario@libero.it, or a German one listing info-shop@gmx.de, passed as a
"business" address. It is not: it belongs to a natural person, and Italian
law (Art. 130 Codice Privacy) needs that person's prior consent for
marketing email. Legitimate interest - the basis this agent relies on -
covers business addresses only, which is exactly what COMPLIANCE.md claims.

Matched on the whole domain, never a substring: gmx-industrie.de is a
company that happens to contain "gmx".
"""

import pytest

from modules.search import SearchModule


@pytest.fixture
def sm(db):
    return SearchModule(db, rate_limit=0.0)


def _page(*emails):
    links = "".join(f"<a href='mailto:{e}'>x</a>" for e in emails)
    return f"<html><head><title>Shop</title></head><body>{links}</body></html>"


@pytest.mark.parametrize(
    "email",
    [
        "mario@libero.it",
        "anna@virgilio.it",
        "shop@tiscali.it",
        "info@alice.it",
        "vendite@tin.it",
        "hans@gmx.de",
        "laden@web.de",
        "kontakt@t-online.de",
        "pierre@orange.fr",
        "marie@wanadoo.fr",
        "boutique@free.fr",
        "contact@laposte.net",
        "maria@yahoo.it",
        "juan@hotmail.es",
        "jan@live.nl",
        "sam@outlook.de",
        "joe@googlemail.com",
        "eva@proton.me",
        "tom@btinternet.com",
        "piet@ziggo.nl",
        "petr@seznam.cz",
        "adam@wp.pl",
    ],
)
def test_a_european_personal_mailbox_is_not_harvested(sm, email):
    assert email not in sm._extract_emails(_page(email, "info@brand.de"))


@pytest.mark.parametrize("email", ["info@brand.de", "sales@gmx-industrie.de"])
def test_a_business_address_is_kept(sm, email):
    """The control, including a company whose name merely contains a
    provider's: whole-domain matching, not substring."""
    assert email in sm._extract_emails(_page(email))
