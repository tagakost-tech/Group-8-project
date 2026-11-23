"""
Email Verification Module for the B2B Outreach Agent.

Provides multi-layer email verification:
  1. Format validation   (via email-validator)
  2. MX record lookup    (via dnspython)
  3. SMTP RCPT TO probe  (lightweight, never sends mail)

All methods are safe to call concurrently; no shared mutable state.
"""

import logging
import re
import smtplib

import dns.exception
import dns.resolver
import httpx
from email_validator import EmailNotValidError, validate_email

from modules.search import AddressNotPublic, public_addresses

logger = logging.getLogger(__name__)

# Timeout for SMTP probe connections (seconds)
_SMTP_TIMEOUT = 10


def _is_transient_smtp(detail: str) -> bool:
    """True when an SMTP probe failure says "not now", not "never".

    Covers the 4xx family (greylisting, rate limiting, mailbox temporarily
    unavailable) and the transport-level failures verify_smtp reports in prose
    - a refused connection or a timeout to the MX host tells us about the
    network, not about the address.
    """
    text = (detail or "").lower()
    if re.search(r"\b4\d\d\b", text):
        return True
    return any(
        s in text
        for s in (
            "server disconnected",
            "connection refused",
            "connection timed out",
            "network error",
            "could not resolve mx",
            "unexpected error",
        )
    )


class EmailVerifier:
    """Full-pipeline email verification with MX and optional SMTP probing."""

    # ------------------------------------------------------------------
    # MX record verification
    # ------------------------------------------------------------------

    @staticmethod
    def verify_mx(email: str) -> bool | None:
        """
        Check whether the email's domain has at least one valid MX record.

        Parameters
        ----------
        email : str
            The email address to check.

        Returns
        -------
        bool | None
            True  - the domain has MX records.
            False - the domain definitively has none (NXDOMAIN / NoAnswer).
            None  - INDETERMINATE. The lookup itself failed (timeout, dead
                    nameserver, refused query), which says nothing about the
                    domain. The comments below always distinguished these two
                    cases, but the RETURN VALUE did not - every branch returned
                    False - so the caller could not tell a resolver hiccup from
                    a domain that genuinely cannot receive mail, and discarded
                    a perfectly deliverable prospect permanently.
        """
        try:
            domain = email.rsplit("@", 1)[-1]
            answers = dns.resolver.resolve(domain, "MX")
            has_mx = len(answers) > 0
            logger.debug(
                "MX lookup for '%s': %s (%d records)",
                domain,
                "[OK]" if has_mx else "[FAIL]",
                len(answers),
            )
            return has_mx
        # The two groups below both return False, but they are NOT the same
        # answer and must not be merged: NoAnswer/NXDOMAIN are the definitive
        # "this domain cannot receive mail", while a timeout or a dead
        # nameserver says nothing about the domain at all. Treating the
        # transient group as permanent discards valid leads for good, so those
        # branches log at warning - loud enough to spot a resolver outage that
        # is quietly failing a whole batch.
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN) as exc:
            logger.debug("MX lookup for '%s': [FAIL] - %s", email, exc)
            return False
        except dns.resolver.NoNameservers as exc:
            logger.warning(
                "MX lookup for '%s': [UNKNOWN] - no nameservers (transient): %s",
                email,
                exc,
            )
            return None
        except dns.exception.Timeout:
            logger.warning(
                "MX lookup for '%s': [UNKNOWN] - DNS timeout (transient)", email
            )
            return None
        except dns.exception.DNSException as exc:
            # Any other resolver-level failure (malformed name, refused query,
            # YXDOMAIN...). Also transient-or-config, never proof of no MX.
            logger.warning(
                "MX lookup for '%s': [UNKNOWN] - resolver error: %s", email, exc
            )
            return None

    # ------------------------------------------------------------------
    # SMTP RCPT TO probe
    # ------------------------------------------------------------------

    @staticmethod
    def verify_smtp(email: str) -> tuple[bool, str]:
        """
        Lightweight SMTP RCPT TO probe.

        Connects to the highest-priority MX host, issues EHLO and
        MAIL FROM:<>, then RCPT TO:<email>, and checks for a 250
        response.  The connection is closed immediately afterwards.
        No email is actually sent.

        The MX record is outside input: whoever runs the domain can point
        it at this machine, the office network or a cloud metadata
        address. The host must have only public addresses, by the page
        fetcher's rules, and the connection goes to the address that was
        checked, never to the name resolved again.

        Parameters
        ----------
        email : str
            The email address to probe.

        Returns
        -------
        tuple[bool, str]
            ``(accepted, detail_message)``
        """
        domain = email.rsplit("@", 1)[-1]

        # Resolve MX records, pick highest priority (lowest preference value)
        try:
            mx_records = dns.resolver.resolve(domain, "MX")
            mx_hosts = sorted(mx_records, key=lambda r: r.preference)
            mx_host = str(mx_hosts[0].exchange).rstrip(".")
        except dns.exception.DNSException as exc:
            msg = f"Could not resolve MX for domain '{domain}': {exc}"
            logger.debug("SMTP probe [FAIL] - %s", msg)
            return False, msg

        try:
            addresses, _named = public_addresses(mx_host)
        except AddressNotPublic as exc:
            msg = f"Will not probe this MX host: {exc}"
            logger.warning("SMTP probe for '%s' [FAIL] - %s", email, msg)
            return False, msg
        except httpx.ConnectError as exc:
            msg = f"Could not resolve MX host {mx_host}: {exc}"
            logger.debug("SMTP probe for '%s' [FAIL] - %s", email, msg)
            return False, msg

        try:
            smtp = smtplib.SMTP(timeout=_SMTP_TIMEOUT)
            smtp.connect(str(addresses[0]), 25)
            smtp.ehlo_or_helo_if_needed()

            # MAIL FROM with null sender (standard verification technique)
            code, resp = smtp.mail("")
            if code != 250:
                smtp.quit()
                msg = f"MAIL FROM rejected: {code} {resp.decode(errors='replace')}"
                logger.debug("SMTP probe for '%s' [FAIL] - %s", email, msg)
                return False, msg

            # RCPT TO — the actual verification step
            code, resp = smtp.rcpt(email)
            smtp.quit()

            resp_text = resp.decode(errors="replace")
            if code == 250:
                logger.debug("SMTP probe for '%s' [OK] - 250 accepted", email)
                return True, "RCPT TO accepted (250)"
            else:
                msg = f"RCPT TO rejected: {code} {resp_text}"
                logger.debug("SMTP probe for '%s' [FAIL] - %s", email, msg)
                return False, msg

        except smtplib.SMTPServerDisconnected:
            msg = "Server disconnected during probe"
            logger.debug("SMTP probe for '%s' [FAIL] - %s", email, msg)
            return False, msg
        except smtplib.SMTPConnectError as exc:
            msg = f"Connection refused by {mx_host}: {exc}"
            logger.debug("SMTP probe for '%s' [FAIL] - %s", email, msg)
            return False, msg
        except TimeoutError:
            msg = f"Connection timed out to {mx_host}"
            logger.debug("SMTP probe for '%s' [FAIL] - %s", email, msg)
            return False, msg
        except smtplib.SMTPException as exc:
            # Protocol-level failures (bad greeting, HELO refused, a response
            # the server should never have sent).
            #
            # MUST stay ABOVE the OSError clause: smtplib.SMTPException is a
            # SUBCLASS of OSError, so ordering it after made this branch
            # unreachable - every protocol error was absorbed by the generic
            # network handler and reported as "Network error", hiding the real
            # cause and logging it at debug instead of warning.
            msg = f"Unexpected error during SMTP probe: {exc}"
            logger.warning("SMTP probe for '%s' [FAIL] - %s", email, msg)
            return False, msg
        except OSError as exc:
            # Genuine transport/socket problems only, now that SMTPException is
            # caught above.
            msg = f"Network error probing {mx_host}: {exc}"
            logger.debug("SMTP probe for '%s' [FAIL] - %s", email, msg)
            return False, msg

    # ------------------------------------------------------------------
    # Full verification pipeline
    # ------------------------------------------------------------------

    def verify_email(self, email: str, skip_smtp: bool = False) -> dict:
        """
        Run the full verification pipeline on a single email address.

        Steps:
          1. Format validation (email-validator library)
          2. MX record check
          3. SMTP RCPT TO probe (unless *skip_smtp* is True)

        Parameters
        ----------
        email : str
            The email address to verify.
        skip_smtp : bool
            If True, skip the SMTP probe step.

        Returns
        -------
        dict
            ``{valid, mx_valid, smtp_valid, reason}``
        """
        result = {
            "email": email,
            "valid": False,
            "mx_valid": False,
            "smtp_valid": None,
            "reason": "",
        }

        # Step 1 - Format validation
        try:
            validated = validate_email(email, check_deliverability=False)
            email = validated.normalized
        except EmailNotValidError as exc:
            result["reason"] = f"Invalid format: {exc}"
            logger.info("Verify '%s' [FAIL] - %s", email, result["reason"])
            return result

        # Step 2 - MX record check
        mx_ok = self.verify_mx(email)
        result["mx_valid"] = mx_ok
        if mx_ok is None:
            # We could not ask, so we have learned nothing. Reported as
            # indeterminate so the caller leaves the contact queued for another
            # attempt instead of discarding it: a single DNS timeout used to
            # retire a perfectly deliverable prospect permanently.
            result["indeterminate"] = True
            result["reason"] = "MX lookup failed (transient) - will retry"
            logger.info("Verify '%s' [UNKNOWN] - %s", email, result["reason"])
            return result
        if not mx_ok:
            result["reason"] = "No valid MX records for domain"
            logger.info("Verify '%s' [FAIL] - %s", email, result["reason"])
            return result

        # Step 3 - SMTP probe (optional)
        if not skip_smtp:
            smtp_ok, smtp_detail = self.verify_smtp(email)
            result["smtp_valid"] = smtp_ok
            if not smtp_ok:
                result["valid"] = False
                # A 4xx is the server declining to answer RIGHT NOW - greylisting
                # is the normal, expected response to a first-contact probe from
                # many corporate mail servers. Folding it in with a 550 "no such
                # user" threw away good addresses for being politely deferred.
                if _is_transient_smtp(smtp_detail):
                    result["indeterminate"] = True
                    result["reason"] = f"SMTP probe deferred: {smtp_detail}"
                    logger.info("Verify '%s' [UNKNOWN] - %s", email, result["reason"])
                    return result
                result["reason"] = f"SMTP probe failed: {smtp_detail}"
                logger.info("Verify '%s' [FAIL] - %s", email, result["reason"])
                return result

        # All checks passed
        result["valid"] = True
        result["reason"] = "All checks passed"
        logger.info("Verify '%s' [OK]", email)
        return result

    # ------------------------------------------------------------------
    # Batch verification
    # ------------------------------------------------------------------
