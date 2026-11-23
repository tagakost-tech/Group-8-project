"""Stopping all email to a company after one person says no (decision L1.6).

An opt-out stopped only its own address, so the agent went on to email a
colleague it found on the same website - after the company had said no.
Under art. 130 Codice Privacy the company is itself a subscriber.

A company is blocked by its domain, through block_company only. Shared mail
domains are refused here: blocking gmail.com, or a PEC provider, would stop
email to every unrelated person on it. Those addresses are never collected
or emailed anyway (modules/search.py), so they keep an address-level block.
"""

from modules.database import email_domain
from modules.search import PERSONAL_DOMAINS, SearchModule


def blockable_domain(email: str) -> str | None:
    """The company domain to block for this address, or None for a shared one."""
    domain = email_domain(email)
    if (
        not domain
        or domain in PERSONAL_DOMAINS
        or SearchModule._is_certified_mail(domain)
    ):
        return None
    return domain


def block_company(db, email: str, reason: str, *, keyed: bool = False) -> bool:
    """Block the company of an address we emailed. Returns True if newly blocked.

    keyed after an erasure: the domain is kept only as its token. An address
    already erased is always blocked keyed: once an operator has lifted the
    block its erasure left, its one-click would otherwise write the domain,
    which can name the person, back in clear.
    """
    domain = blockable_domain(email)
    if domain is None:
        return False
    return db.block_domain(domain, reason, keyed=keyed or db.is_erased(email))
