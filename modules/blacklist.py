"""
Blacklist (DNSBL) monitoring for the B2B Outreach Agent.

Checks whether the sending domain - or a sending IP - has been listed on a
public spam blocklist. Getting blocklisted is silent and devastating: mail keeps
"sending" while recipients never receive it, so this surfaces it early.

Uses dnspython (already a dependency); no external service or API key.

A DNSBL lookup is just a DNS A query:
  domain check -> <domain>.dbl.spamhaus.org
  IP check     -> <reversed-ip>.zen.spamhaus.org
A returned 127.0.0.x means LISTED; NXDOMAIN means clean.

IMPORTANT: public resolvers (Google 8.8.8.8, Cloudflare) are blocked by Spamhaus
and answer 127.255.255.x. That is an ERROR, not "clean" - reporting it as clean
would be dangerously misleading, so it is surfaced as 'unavailable'.
"""

import logging
import time

logger = logging.getLogger(__name__)

# Domain-based blocklists - the ones that matter when sending from a hosted
# provider (Microsoft 365 / Google), where the IP belongs to the provider.
DOMAIN_ZONES = [
    ("Spamhaus DBL", "dbl.spamhaus.org"),
    ("SURBL", "multi.surbl.org"),
]

# IP-based blocklists - relevant when sending from your own server / dedicated IP.
IP_ZONES = [
    ("Spamhaus ZEN", "zen.spamhaus.org"),
    ("SpamCop", "bl.spamcop.net"),
    ("Barracuda", "b.barracudacentral.org"),
]

LISTED = "listed"
CLEAN = "clean"
UNAVAILABLE = "unavailable"
# Some lists answered, some didn't: no listing found, but coverage was
# incomplete, so this is not an all-clear.
PARTIAL = "partial"


class BlacklistChecker:
    """Queries public DNSBLs for a domain or IP, with a short result cache."""

    def __init__(self, timeout: float = 4.0, cache_ttl: float = 3600.0):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._cache = {}  # key -> (checked_at, results)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolver(self):
        import dns.resolver

        r = dns.resolver.Resolver()
        r.timeout = r.lifetime = self.timeout
        return r

    def _query(self, name: str, list_name: str, zone: str) -> dict:
        """Look up one name on one blocklist zone."""
        import dns.exception
        import dns.resolver

        result = {"list": list_name, "zone": zone, "status": CLEAN, "detail": ""}
        try:
            answers = self._resolver().resolve(name, "A")
            codes = [r.address for r in answers]

            # A real listing is always inside 127.0.0.0/8. Anything else is a
            # resolver hijacking NXDOMAIN (consumer ISP, captive portal, some
            # corporate DNS) - treating that as LISTED would report every domain
            # on earth as blocklisted.
            if not all(c.startswith("127.") for c in codes):
                result["status"] = UNAVAILABLE
                result["detail"] = (
                    f"resolver returned a non-127/8 answer "
                    f"({', '.join(codes)}) - it is probably "
                    f"hijacking NXDOMAIN"
                )
            # Documented "your query was refused" codes. These are NOT listings:
            #   127.255.255.x  Spamhaus: public resolver / over quota
            #   127.0.0.1      SURBL: query blocked
            #   127.0.1.255    Spamhaus DBL: IP queries prohibited
            elif any(
                c.startswith("127.255.255.") or c in ("127.0.0.1", "127.0.1.255")
                for c in codes
            ):
                result["status"] = UNAVAILABLE
                result["detail"] = (
                    f"blocklist refused the query ({', '.join(codes)}) "
                    f"- public DNS resolver, rate limit, or an "
                    f"unsupported query type"
                )
            else:
                result["status"] = LISTED
                result["detail"] = ", ".join(codes)
        except dns.resolver.NXDOMAIN:
            result["status"] = CLEAN  # not listed - the good case
        except dns.exception.DNSException as e:
            # Timeout, no nameservers, refused query - the ordinary way a DNSBL
            # probe fails. NXDOMAIN above is the ONLY answer that means clean.
            result["status"] = UNAVAILABLE
            result["detail"] = str(e)[:120]
            logger.debug("DNSBL %s lookup for '%s' unavailable: %s", zone, name, e)
        except Exception as e:
            # Deliberately broad, and it must stay that way. The one outcome
            # this module exists to prevent is reporting a listed domain as
            # CLEAN, so anything that is not a definitive NXDOMAIN has to land
            # on UNAVAILABLE - including failures from below dnspython (socket,
            # ssl, a resolver misconfiguration) that are not DNSExceptions.
            # Logged with a traceback because reaching here is unexpected.
            result["status"] = UNAVAILABLE
            result["detail"] = str(e)[:120]
            logger.exception("DNSBL %s lookup for '%s' failed unexpectedly", zone, name)
        return result

    def _cached(self, key: str):
        hit = self._cache.get(key)
        if hit and (time.time() - hit[0]) < self.cache_ttl:
            return hit[1]
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_domain(self, domain: str) -> list:
        """Check a sending domain against the domain blocklists."""
        domain = (domain or "").strip().lower().rstrip(".")
        if not domain or "." not in domain:
            return []
        cached = self._cached(f"d:{domain}")
        if cached is not None:
            return cached
        results = [
            self._query(f"{domain}.{zone}", name, zone) for name, zone in DOMAIN_ZONES
        ]
        self._cache[f"d:{domain}"] = (time.time(), results)
        return results

    def check_ip(self, ip: str) -> list:
        """Check a sending IP against the IP blocklists."""
        ip = (ip or "").strip()
        parts = ip.split(".")
        if len(parts) != 4 or not all(p.isdigit() for p in parts):
            return []  # IPv4 only; DNSBLs rarely cover IPv6 usefully
        cached = self._cached(f"i:{ip}")
        if cached is not None:
            return cached
        rev = ".".join(reversed(parts))
        results = [self._query(f"{rev}.{zone}", name, zone) for name, zone in IP_ZONES]
        self._cache[f"i:{ip}"] = (time.time(), results)
        return results

    @staticmethod
    def summarize(results: list) -> dict:
        """Roll per-list results into an overall verdict.

        Fails LOUD, not silent: if every list errored we report 'unavailable'
        rather than implying the domain is clean, and a run where only some
        lists answered is 'partial' - the silent ones may well be the ones
        listing us, so calling that CLEAN is an unearned all-clear."""
        listed = [r for r in results if r["status"] == LISTED]
        checked = [r for r in results if r["status"] != UNAVAILABLE]
        if listed:
            status = LISTED
        elif not checked:
            status = UNAVAILABLE
        elif len(checked) < len(results):
            status = PARTIAL
        else:
            status = CLEAN
        return {
            "status": status,
            "listed_on": [r["list"] for r in listed],
            "checked": len(checked),
            "total": len(results),
            "results": results,
        }
