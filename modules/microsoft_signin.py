"""Signing in to Microsoft 365 mailboxes with a certificate, never a password.

Exchange Online already refuses a password for IMAP, and turns password SMTP
AUTH off by default at the end of December 2026. A mailbox password kept on
this machine is also the one thing that lets whoever copies it send spam or
phishing from SUPPLECO's domain. So the agent signs in as an app registered in
SUPPLECO's Microsoft Entra (OAuth 2.0 client credentials with a certificate):
it signs a short assertion with the certificate's private key, Microsoft
answers with an access token for Exchange Online, and IMAP and SMTP accept
that token (SASL XOAUTH2) for the mailboxes SUPPLECO's IT gave the app.

The token comes only from login.microsoftonline.com, over verified TLS, with
a time limit, no redirect and no proxy from the environment. It is kept in
memory until shortly before it expires and never logged.

Microsoft's documents this follows:
https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth
https://learn.microsoft.com/en-us/entra/identity-platform/certificate-credentials
https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
"""

import base64
import imaplib
import json
import logging
import os
import re
import ssl
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from modules import clock
from modules.private_files import owner_only

logger = logging.getLogger(__name__)

TOKEN_HOST = "login.microsoftonline.com"
# The one scope Microsoft names for IMAP, POP and SMTP with client credentials.
SCOPE = "https://outlook.office365.com/.default"
SMTP_HOST, SMTP_PORT = "smtp.office365.com", 587
IMAP_HOST, IMAP_PORT = "outlook.office365.com", 993

# Microsoft: keep the assertion short, 5 to 10 minutes after nbf at most.
ASSERTION_LIFETIME = 300
# A token is asked for again this long before it expires, so a login never
# starts with one about to run out.
RENEW_BEFORE = 300
TOKEN_TIMEOUT = 30
MIN_KEY_BITS = 2048

_GUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_DOMAIN = re.compile(
    r"(?=.{4,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)
# What may stand in an access token: it goes into the XOAUTH2 string, whose
# fields a control character (\x01) would end.
_TOKEN_TEXT = re.compile(r"[A-Za-z0-9\-._~+/=]+")


class SignInRefused(Exception):
    """Microsoft refused the sign-in, or the certificate cannot be used: a
    setting is wrong, and asking again changes nothing. Trips the same
    circuit breaker as a wrong password."""


class SignInUnavailable(OSError):
    """Microsoft could not be reached, or answered with nothing usable. Like
    a mail server that does not answer: nothing was sent, try later."""


def is_microsoft_host(host: str) -> bool:
    """True for the Microsoft 365 SMTP server, the host a mailbox signing
    in with a token sends through."""
    return (host or "").strip().lower() == SMTP_HOST


# Microsoft's mail servers answer under these names too: Microsoft 365 takes
# SMTP on outlook.office365.com, Outlook.com on smtp-mail.outlook.com. A
# mailbox reached through any of them is a Microsoft mailbox, and its password
# is as good to a thief as one kept for smtp.office365.com.
_MICROSOFT_MAIL_DOMAINS = ("office365.com", "outlook.com", "office.com")


def is_microsoft_mail_host(host) -> bool:
    """True for any Microsoft mail server name, not only the one the agent
    sends through."""
    host = str(host or "").strip().lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in _MICROSOFT_MAIL_DOMAINS)


def tenant_problem(tenant: str) -> str:
    """What is wrong with a tenant id, or "". Never quotes it."""
    tenant = (tenant or "").strip()
    if _GUID.fullmatch(tenant) or _DOMAIN.fullmatch(tenant):
        return ""
    return (
        "MICROSOFT_TENANT_ID must be the Directory (tenant) ID, a GUID such as "
        "00000000-0000-0000-0000-000000000000, or SUPPLECO's verified domain "
        "such as suppleco.onmicrosoft.com"
    )


def client_id_problem(client_id: str) -> str:
    """What is wrong with an app's client id, or "". Never quotes it."""
    if _GUID.fullmatch((client_id or "").strip()):
        return ""
    return (
        "MICROSOFT_CLIENT_ID must be the app registration's Application "
        "(client) ID, a GUID such as 00000000-0000-0000-0000-000000000000"
    )


def _mode(path: Path) -> int:
    return os.stat(path).st_mode


def key_file_problem(path) -> str:
    """Why the certificate file cannot be used as it is, or ""."""
    path = Path(path)
    if not path.is_file():
        return "MICROSOFT_CERTIFICATE_PATH names no file"
    if owner_only() and _mode(path) & 0o077:
        return (
            "MICROSOFT_CERTIFICATE_PATH can be read by other accounts on this "
            "machine. It holds the private key that signs in to SUPPLECO's "
            "mailboxes: make it readable by the agent's account only "
            "(chmod 600)"
        )
    return ""


def load_credential(path, passphrase: str = ""):
    """The private key and certificate in a PEM file, as (key, certificate).

    Raises SignInRefused when they cannot be used. No message quotes the
    file's content or the passphrase."""
    problem = key_file_problem(path)
    if problem:
        raise SignInRefused(problem)
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise SignInRefused(
            f"MICROSOFT_CERTIFICATE_PATH cannot be read ({type(e).__name__})"
        ) from None
    try:
        key = serialization.load_pem_private_key(
            data, password=passphrase.encode() if passphrase else None
        )
    except TypeError:
        raise SignInRefused(
            "the private key in MICROSOFT_CERTIFICATE_PATH is encrypted: set "
            "MICROSOFT_CERTIFICATE_PASSPHRASE"
            if not passphrase
            else "MICROSOFT_CERTIFICATE_PASSPHRASE is set, but the private key "
            "in MICROSOFT_CERTIFICATE_PATH is not encrypted: blank it"
        ) from None
    except ValueError:
        raise SignInRefused(
            "MICROSOFT_CERTIFICATE_PATH holds no private key this agent can "
            "read, or MICROSOFT_CERTIFICATE_PASSPHRASE is wrong. The file must "
            "be PEM, with the private key and the certificate in it"
        ) from None
    try:
        certificate = x509.load_pem_x509_certificate(data)
    except ValueError:
        raise SignInRefused(
            "MICROSOFT_CERTIFICATE_PATH holds no certificate: put the "
            "certificate (BEGIN CERTIFICATE) in the same PEM file as the key"
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < MIN_KEY_BITS:
        raise SignInRefused(
            f"the key in MICROSOFT_CERTIFICATE_PATH must be RSA of "
            f"{MIN_KEY_BITS} bits or more"
        )
    if certificate.public_key().public_numbers() != key.public_key().public_numbers():
        raise SignInRefused(
            "the certificate in MICROSOFT_CERTIFICATE_PATH does not belong to "
            "the private key in it"
        )
    return key, certificate


def certificate_expiry(path, passphrase: str = "") -> datetime:
    """When the certificate in the PEM file stops being valid (UTC)."""
    _key, certificate = load_credential(path, passphrase)
    return certificate.not_valid_after_utc


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def token_endpoint(tenant: str) -> str:
    return f"https://{TOKEN_HOST}/{tenant.strip()}/oauth2/v2.0/token"


def client_assertion(
    tenant: str, client_id: str, key, certificate, now: float | None = None
) -> str:
    """The signed JWT that proves to Microsoft the request comes from the app.

    PS256 with the certificate's SHA-256 thumbprint in x5t#S256, as Microsoft's
    certificate-credentials page asks; a new jti every time; valid from now
    for ASSERTION_LIFETIME seconds."""
    now = int(time.time() if now is None else now)
    header = {
        "alg": "PS256",
        "typ": "JWT",
        "x5t#S256": _b64url(certificate.fingerprint(hashes.SHA256())),
    }
    claims = {
        "aud": token_endpoint(tenant),
        "iss": client_id.strip(),
        "sub": client_id.strip(),
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "iat": now,
        "exp": now + ASSERTION_LIFETIME,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in (header, claims)
    )
    signature = key.sign(
        signing_input.encode("ascii"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256(),
    )
    return f"{signing_input}.{_b64url(signature)}"


def xoauth2(user: str, token: str) -> str:
    """The SASL XOAUTH2 answer, before base64: imaplib and smtplib encode it."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01"


# Microsoft's AADSTS codes, in words an IT person can act on.
_TOKEN_ERRORS = {
    "AADSTS700016": (
        "Microsoft does not know this app in this tenant. Check "
        "MICROSOFT_CLIENT_ID (the app registration's Application (client) ID) "
        "and MICROSOFT_TENANT_ID (its Directory (tenant) ID)"
    ),
    "AADSTS90002": "Microsoft does not know this tenant. Check MICROSOFT_TENANT_ID",
    "AADSTS900023": "Microsoft does not know this tenant. Check MICROSOFT_TENANT_ID",
    "AADSTS7000215": (
        "Microsoft refused the app's credential. Upload the certificate from "
        "MICROSOFT_CERTIFICATE_PATH to the app registration (Certificates & "
        "secrets > Certificates); a client secret is not used"
    ),
    "AADSTS700027": (
        "Microsoft could not check the signature: the certificate in "
        "MICROSOFT_CERTIFICATE_PATH is not uploaded to the app registration "
        "(Certificates & secrets > Certificates), or the one there has expired "
        "or was replaced"
    ),
    "AADSTS700024": (
        "Microsoft says the signed request is outside its valid time. Set this "
        "computer's clock right (automatic time)"
    ),
    "AADSTS7000112": "the app is disabled in Microsoft Entra",
}


def explain_token_error(answer: dict) -> str:
    """Microsoft's refusal of a token request, in words, with its code."""
    description = str(answer.get("error_description") or "")
    found = re.search(r"AADSTS\d+", description)
    codes = answer.get("error_codes") or []
    code = found.group(0) if found else (f"AADSTS{codes[0]}" if codes else "")
    words = _TOKEN_ERRORS.get(code) or (
        'Microsoft refused the sign-in. See docs/DEPLOY.md, "Connect to '
        'Microsoft 365 without a password"'
    )
    named = code or str(answer.get("error") or "no code")
    return f"{words} ({named})"


def explain_mailbox_refusal(protocol: str, server_text: str) -> str:
    """Why Exchange refused a token Microsoft had just issued, in words."""
    text = server_text or ""
    if "5.7.139" in text or "smtpclientauthentication is disabled" in text.lower():
        return (
            "SMTP AUTH is switched off for this mailbox or the whole tenant, "
            "and Microsoft needs it on even with a token. In Exchange Online "
            "PowerShell: Set-CASMailbox -Identity <mailbox> "
            "-SmtpClientAuthenticationDisabled $false"
        )
    extra = (
        ", and that IMAP is on for the mailbox (Set-CASMailbox -Identity "
        "<mailbox> -ImapEnabled $true)"
        if protocol == "IMAP"
        else ""
    )
    return (
        f"Microsoft issued a token, but the mailbox refused it over {protocol}: "
        "the app has no access to this mailbox. In Exchange Online PowerShell "
        "check that New-ServicePrincipal used the Enterprise application's "
        "Object ID (not the App registration's), that Add-MailboxPermission "
        "-AccessRights FullAccess was given for this mailbox, that "
        "IMAP.AccessAsApp and SMTP.SendAsApp have admin consent"
        f"{extra}. A change can take a while to reach Exchange"
    )


class MicrosoftSignIn:
    """Asks Microsoft for an access token and keeps it until shortly before
    it expires. One app serves every mailbox: each signs in under its own
    name with the same token. Safe to share between threads."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        certificate_path,
        passphrase: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
        clock=time.time,
    ):
        self.tenant_id = tenant_id.strip()
        self.client_id = client_id.strip()
        self.certificate_path = Path(certificate_path)
        self._passphrase = passphrase
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._token = ""
        self._good_until = 0.0

    def __repr__(self) -> str:
        return f"MicrosoftSignIn(tenant={self.tenant_id!r}, client={self.client_id!r})"

    def token(self) -> str:
        """A valid access token, from memory while it has time left."""
        with self._lock:
            if self._token and self._clock() < self._good_until:
                return self._token
            self._token = ""
            token, lifetime = self._request()
            now = self._clock()
            self._token = token
            self._good_until = now + (
                lifetime - RENEW_BEFORE if lifetime > 2 * RENEW_BEFORE else lifetime / 2
            )
            return token

    def forget(self) -> None:
        """Drop the token, so the next login asks Microsoft again."""
        with self._lock:
            self._token = ""
            self._good_until = 0.0

    def _request(self) -> tuple[str, float]:
        # Read every time a token is asked for, about once an hour: a renewed
        # certificate put in the file is used without a restart.
        key, certificate = load_credential(self.certificate_path, self._passphrase)
        form = {
            "client_id": self.client_id,
            "scope": SCOPE,
            "client_assertion_type": (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            ),
            "client_assertion": client_assertion(
                self.tenant_id, self.client_id, key, certificate, self._clock()
            ),
            "grant_type": "client_credentials",
        }
        try:
            with httpx.Client(
                transport=self._transport,
                verify=True,
                timeout=TOKEN_TIMEOUT,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(token_endpoint(self.tenant_id), data=form)
        except httpx.HTTPError as e:
            raise SignInUnavailable(
                f"Microsoft's sign-in server could not be reached ({type(e).__name__})"
            ) from None
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            answer = {}
        if response.status_code in (400, 401) and answer.get("error"):
            reason = explain_token_error(answer)
            logger.error("Microsoft sign-in refused: %s", reason)
            raise SignInRefused(reason)
        if response.status_code != 200:
            raise SignInUnavailable(
                f"Microsoft's sign-in server answered HTTP {response.status_code}"
            )
        token = answer.get("access_token")
        lifetime = answer.get("expires_in")
        if (
            str(answer.get("token_type", "")).lower() != "bearer"
            or not isinstance(token, str)
            or not _TOKEN_TEXT.fullmatch(token)
            or isinstance(lifetime, bool)
            or not isinstance(lifetime, (int, float, str))
        ):
            raise SignInUnavailable("Microsoft's answer held no usable token")
        try:
            lifetime = float(lifetime)
        except ValueError:
            raise SignInUnavailable("Microsoft's answer held no usable token") from None
        if lifetime <= 0:
            raise SignInUnavailable("Microsoft's answer held no usable token")
        return token, lifetime


def from_settings(settings) -> MicrosoftSignIn | None:
    """The sign-in for the settings' Microsoft 365 mailboxes, or None when
    Microsoft sign-in is not set up."""
    if not settings.microsoft_signin_on():
        return None
    return MicrosoftSignIn(
        settings.microsoft_tenant_id,
        settings.microsoft_client_id,
        settings.get_abs_microsoft_certificate_path(),
        settings.microsoft_certificate_passphrase,
    )


def imap_authenticate(mail, user: str, token: str) -> None:
    """AUTHENTICATE XOAUTH2 on an open IMAP connection. Raises imaplib's
    error when the server refuses. A second challenge is the server's error
    report, answered with a cancel."""
    answer = xoauth2(user, token).encode("utf-8")
    mail.authenticate("XOAUTH2", lambda challenge: None if challenge else answer)


def smtp_authenticate(smtp, user: str, token: str) -> None:
    """AUTH XOAUTH2 on an SMTP connection after STARTTLS and EHLO. Raises
    smtplib.SMTPAuthenticationError when the server refuses. A challenge is
    the server's error report, answered empty as XOAUTH2 asks."""
    answer = xoauth2(user, token)
    smtp.auth(
        "XOAUTH2",
        lambda challenge=None: answer if challenge is None else "",
        initial_response_ok=True,
    )


def check_imap(signin: MicrosoftSignIn, host: str, port: int, user: str, timeout=15):
    """Sign in to one mailbox over IMAP with a token, then NOOP and log out.
    Raises what a login would: SignInRefused, OSError or imaplib's error."""
    token = signin.token()
    mail = imaplib.IMAP4_SSL(
        host, port, ssl_context=ssl.create_default_context(), timeout=timeout
    )
    try:
        imap_authenticate(mail, user, token)
        mail.noop()
    finally:
        try:
            mail.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def expires_soon(path, passphrase: str = "", days: int = 30) -> tuple[bool, str]:
    """Whether the certificate still has more than `days` days, and when it
    ends."""
    ends = certificate_expiry(path, passphrase).astimezone(clock.get_timezone())
    left = ends - clock.now()
    when = ends.strftime("%Y-%m-%d")
    if left.total_seconds() <= 0:
        return (
            False,
            f"the certificate expired on {when}: sign-in fails until it is renewed",
        )
    if left.days < days:
        return False, (
            f"the certificate expires on {when}, in {left.days} day(s): make a "
            "new one and upload it (docs/DEPLOY.md)"
        )
    return True, f"certificate valid until {when}"
