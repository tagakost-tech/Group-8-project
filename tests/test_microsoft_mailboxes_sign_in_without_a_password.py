"""Microsoft 365 mailboxes sign in with a certificate, and no password exists.

Exchange Online refuses a password for IMAP already and turns password SMTP
AUTH off by default at the end of December 2026, and a stored mailbox
password is the main way a thief sends phishing from SUPPLECO's domain. With
Microsoft sign-in set up, the agent signs a short JWT with the app's
certificate, gets an access token from login.microsoftonline.com, and signs in
to IMAP and SMTP with it (SASL XOAUTH2). These tests run against a fake token
endpoint (httpx.MockTransport) and fake IMAP/SMTP servers, with throwaway
certificates made here: no key file is in the repository.
"""

import base64
import datetime as dt
import imaplib
import json
import logging
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from typing import ClassVar

import httpx
import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from pydantic import ValidationError

import main
from config import config_error_text
from modules import microsoft_signin as ms
from modules.compliance import mask_personal
from modules.email_sender import EmailSender
from modules.imap_monitor import ImapMonitor

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
MAILBOX = "outreach@sender.example"
PASSPHRASE = "a-passphrase-for-the-test-9Qz"

_KEYS = {}


def _key(slot=0, bits=2048):
    if (slot, bits) not in _KEYS:
        _KEYS[slot, bits] = rsa.generate_private_key(
            public_exponent=65537, key_size=bits
        )
    return _KEYS[slot, bits]


def _certificate(key, *, signer=None, days=730):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "outreach agent test")])
    now = dt.datetime.now(dt.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=days))
        .sign(signer or key, hashes.SHA256())
    )


def _pem(folder, *, key=None, certificate=None, passphrase="", name="agent.pem"):
    key = key or _key()
    certificate = certificate or _certificate(key)
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    path = folder / name
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
        )
        + certificate.public_bytes(serialization.Encoding.PEM)
    )
    return path


def _ms_settings(settings_factory, pem, **overrides):
    values = {
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_username": MAILBOX,
        "microsoft_tenant_id": TENANT,
        "microsoft_client_id": CLIENT,
        "microsoft_certificate_path": str(pem),
    }
    values.update(overrides)
    return settings_factory(**values)


def _signin(pem, **kw):
    return ms.MicrosoftSignIn(TENANT, CLIENT, pem, **kw)


def _part(segment):
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


class TokenServer:
    """login.microsoftonline.com, as far as the agent can tell."""

    def __init__(self):
        self.requests = []
        self.client_kwargs = []
        self.answers = []
        self.issued = []

    def handle(self, request):
        self.requests.append(request)
        if self.answers:
            status, body, *headers = self.answers.pop(0)
            if isinstance(body, Exception):
                raise body
            return httpx.Response(status, json=body, headers=dict(*headers))
        token = f"eyJ0eXAiOiJKV1QifQ.eyJmYWtlIjoi{len(self.issued)}x.c2lnbmF0dXJl{len(self.issued)}"
        self.issued.append(token)
        return httpx.Response(
            200,
            json={"token_type": "Bearer", "expires_in": 3599, "access_token": token},
        )

    def form(self, i=-1):
        return dict(httpx.QueryParams(self.requests[i].content.decode()))

    def refuse(self, code, words="refused"):
        self.answers.append(
            (
                401 if code == "AADSTS7000215" else 400,
                {
                    "error": "invalid_client",
                    "error_description": f"{code}: {words}. Trace ID: x",
                    "error_codes": [int(code[6:])],
                },
            )
        )


@pytest.fixture
def token_server(monkeypatch):
    server = TokenServer()
    real = httpx.Client

    def client(**kwargs):
        server.client_kwargs.append(kwargs)
        return real(**{**kwargs, "transport": httpx.MockTransport(server.handle)})

    monkeypatch.setattr(httpx, "Client", client)
    return server


class _FakeSMTP(smtplib.SMTP):
    reply = (235, b"2.7.0 Authentication successful")
    made: ClassVar[list] = []

    def __init__(self, host="", port=0, *args, timeout=None, **kwargs):
        super().__init__(local_hostname="agent.test")
        self.commands = []
        self.context = None
        type(self).made.append(self)

    def ehlo(self, name=""):
        self.commands.append(("EHLO",))
        return 250, b"ok"

    def starttls(self, *args, context=None, **kwargs):
        self.commands.append(("STARTTLS",))
        self.context = context
        return 220, b"ready"

    def docmd(self, cmd, args=""):
        self.commands.append((cmd, args))
        if cmd.lower() == "quit":
            return 221, b"bye"
        return type(self).reply

    def login(self, *a, **k):
        raise AssertionError("a password login was tried")

    def noop(self):
        return 250, b"ok"

    def send_message(self, msg, *a, **k):
        self.commands.append(("DATA",))
        return {}

    def quit(self):
        return 221, b"bye"

    def close(self):
        pass


@pytest.fixture
def fake_smtp(monkeypatch):
    class Fake(_FakeSMTP):
        made: ClassVar[list] = []
        reply = _FakeSMTP.reply

    monkeypatch.setattr(smtplib, "SMTP", Fake)
    return Fake


class _FakeIMAP:
    made: ClassVar[list] = []
    refuse = False

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port = host, port
        self.ssl_context = ssl_context
        self.answers = []
        self.nooped = False
        type(self).made.append(self)

    def authenticate(self, mechanism, authobject):
        self.mechanism = mechanism
        exchange = imaplib._Authenticator(authobject)
        self.answers.append(exchange.process(b""))
        self.answers.append(exchange.process(base64.b64encode(b'{"status":"401"}')))
        if self.refuse:
            raise imaplib.IMAP4.error("AUTHENTICATE failed.")
        return "OK", [b"AUTHENTICATE completed."]

    def login(self, *a, **k):
        raise AssertionError("a password login was tried")

    def noop(self):
        self.nooped = True
        return "OK", [b"NOOP completed."]

    def logout(self):
        return "BYE", [b""]


@pytest.fixture
def fake_imap(monkeypatch):
    class Fake(_FakeIMAP):
        made: ClassVar[list] = []
        refuse = False

    monkeypatch.setattr(imaplib, "IMAP4_SSL", Fake)
    return Fake


# -- The signed assertion ------------------------------------------------------


def test_the_assertion_carries_the_claims_microsoft_asks_for(tmp_path):
    key, certificate = ms.load_credential(_pem(tmp_path))
    jwt = ms.client_assertion(TENANT, CLIENT, key, certificate, now=1_800_000_000)
    header, claims, _signature = jwt.split(".")
    header, claims = _part(header), _part(claims)
    der = certificate.public_bytes(serialization.Encoding.DER)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(der)
    assert header == {
        "alg": "PS256",
        "typ": "JWT",
        "x5t#S256": base64.urlsafe_b64encode(digest.finalize()).rstrip(b"=").decode(),
    }
    assert claims["aud"] == (
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    )
    assert claims["iss"] == claims["sub"] == CLIENT
    assert claims["nbf"] == claims["iat"] == 1_800_000_000
    assert claims["exp"] == 1_800_000_000 + 300
    assert claims["exp"] - claims["nbf"] <= 600, "Microsoft: 5-10 minutes at most"
    assert uuid.UUID(claims["jti"])


def test_every_assertion_has_its_own_jti(tmp_path):
    key, certificate = ms.load_credential(_pem(tmp_path))
    jtis = {
        _part(ms.client_assertion(TENANT, CLIENT, key, certificate, 0).split(".")[1])[
            "jti"
        ]
        for _ in range(20)
    }
    assert len(jtis) == 20


def test_the_signature_verifies_with_the_certificates_public_key(tmp_path):
    key, certificate = ms.load_credential(_pem(tmp_path))
    jwt = ms.client_assertion(TENANT, CLIENT, key, certificate)
    signed, _dot, signature = jwt.rpartition(".")
    signature = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    pss = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
    certificate.public_key().verify(signature, signed.encode(), pss, hashes.SHA256())
    with pytest.raises(InvalidSignature):
        certificate.public_key().verify(
            signature, signed.encode() + b"x", pss, hashes.SHA256()
        )


# -- The token request ---------------------------------------------------------


def test_the_token_request_carries_the_assertion_and_the_exchange_scope(
    tmp_path, token_server
):
    token = _signin(_pem(tmp_path)).token()
    assert token == token_server.issued[0]
    (request,) = token_server.requests
    assert request.method == "POST"
    assert str(request.url) == (
        f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
    )
    form = token_server.form()
    assert set(form) == {
        "client_id",
        "scope",
        "client_assertion_type",
        "client_assertion",
        "grant_type",
    }
    assert form["client_id"] == CLIENT
    assert form["scope"] == "https://outlook.office365.com/.default"
    assert form["grant_type"] == "client_credentials"
    assert form["client_assertion_type"] == (
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    )
    assert _part(form["client_assertion"].split(".")[1])["iss"] == CLIENT


def test_the_token_request_checks_tls_follows_no_redirect_and_uses_no_proxy(
    tmp_path, token_server
):
    _signin(_pem(tmp_path)).token()
    (kwargs,) = token_server.client_kwargs
    assert kwargs.get("verify", True) is True
    assert kwargs.get("follow_redirects") is False
    assert kwargs.get("trust_env") is False
    assert kwargs.get("timeout") is not None


def test_a_redirect_is_not_followed(tmp_path, token_server):
    token_server.answers.append(
        (302, {"error": "moved"}, {"Location": "https://login.elsewhere.example/t"})
    )
    with pytest.raises(ms.SignInUnavailable):
        _signin(_pem(tmp_path)).token()
    assert [r.url.host for r in token_server.requests] == [ms.TOKEN_HOST]


def test_the_token_is_kept_until_shortly_before_it_expires(tmp_path, token_server):
    now = [1_800_000_000.0]
    signin = _signin(_pem(tmp_path), clock=lambda: now[0])
    first = signin.token()
    now[0] += 3599 - ms.RENEW_BEFORE - 1
    assert signin.token() == first
    assert len(token_server.requests) == 1
    now[0] += 2
    second = signin.token()
    assert second != first
    assert len(token_server.requests) == 2


@pytest.mark.parametrize(
    "code,words",
    [
        ("AADSTS700016", "does not know this app"),
        ("AADSTS7000215", "Certificates & secrets"),
        ("AADSTS700027", "not uploaded to the app registration"),
    ],
)
def test_microsofts_common_refusals_are_explained(tmp_path, token_server, code, words):
    token_server.refuse(code)
    with pytest.raises(ms.SignInRefused) as refused:
        _signin(_pem(tmp_path)).token()
    assert words in str(refused.value)
    assert code in str(refused.value)


@pytest.mark.parametrize(
    "answer",
    [
        (503, {"error": "temporarily_unavailable"}),
        (200, {"token_type": "Bearer", "expires_in": 3599}),
        (200, {"token_type": "Bearer", "expires_in": 3599, "access_token": "a\x01b"}),
        (200, {"token_type": "mac", "expires_in": 3599, "access_token": "abc"}),
        (200, {"token_type": "Bearer", "expires_in": 0, "access_token": "abc"}),
        (200, ["not", "an", "object"]),
    ],
)
def test_an_unusable_answer_is_not_a_refusal(tmp_path, token_server, answer):
    token_server.answers.append(answer)
    with pytest.raises(ms.SignInUnavailable):
        _signin(_pem(tmp_path)).token()


def test_an_unreachable_server_is_not_a_refusal(tmp_path, token_server):
    token_server.answers.append((0, httpx.ConnectError("no route")))
    with pytest.raises(ms.SignInUnavailable):
        _signin(_pem(tmp_path)).token()


# -- XOAUTH2 -------------------------------------------------------------------


def test_the_xoauth2_answer_is_exactly_microsofts_format():
    assert (
        ms.xoauth2("a@b.example", "tok")
        == "user=a@b.example\x01auth=Bearer tok\x01\x01"
    )
    # The worked example on Microsoft's page.
    encoded = base64.b64encode(
        ms.xoauth2(
            "test@contoso.onmicrosoft.com", "EwBAAl3BAAUFFpUAo7J3Ve0bjLBWZWCclRC3EoAA"
        ).encode()
    ).decode()
    assert encoded == (
        "dXNlcj10ZXN0QGNvbnRvc28ub25taWNyb3NvZnQuY29tAWF1dGg9QmVhcmVy"
        "IEV3QkFBbDNCQUFVRkZwVUFvN0ozVmUwYmpMQldaV0NjbFJDM0VvQUEBAQ=="
    )


def test_smtp_signs_in_with_xoauth2_after_starttls(
    db, tmp_path, token_server, fake_smtp
):
    sender = EmailSender(
        db, ms.SMTP_HOST, ms.SMTP_PORT, MAILBOX, "", signin=_signin(_pem(tmp_path))
    )
    sender._new_connection()
    (smtp,) = fake_smtp.made
    token = token_server.issued[0]
    expected = base64.b64encode(
        f"user={MAILBOX}\x01auth=Bearer {token}\x01\x01".encode()
    ).decode()
    commands = smtp.commands
    assert commands.index(("STARTTLS",)) < commands.index(
        ("AUTH", f"XOAUTH2 {expected}")
    )
    assert smtp.context.verify_mode == ssl.CERT_REQUIRED
    assert smtp.context.check_hostname is True


def test_imap_signs_in_with_xoauth2(db, tmp_path, token_server, fake_imap):
    monitor = ImapMonitor(
        db, ms.IMAP_HOST, ms.IMAP_PORT, MAILBOX, "", signin=_signin(_pem(tmp_path))
    )
    assert monitor._connect() is not None
    (imap,) = fake_imap.made
    token = token_server.issued[0]
    assert imap.mechanism == "XOAUTH2"
    assert imap.answers[0] == base64.b64encode(
        f"user={MAILBOX}\x01auth=Bearer {token}\x01\x01".encode()
    )
    assert imap.answers[1] == b"*", "the server's error challenge must be cancelled"
    assert imap.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert imap.ssl_context.check_hostname is True


def test_one_token_serves_every_login_until_it_is_due(
    db, tmp_path, token_server, fake_smtp, fake_imap
):
    signin = _signin(_pem(tmp_path))
    for user in (MAILBOX, "second@sender.example"):
        EmailSender(db, ms.SMTP_HOST, 587, user, "", signin=signin)._new_connection()
        ImapMonitor(db, ms.IMAP_HOST, 993, user, "", signin=signin)._connect()
    assert len(token_server.requests) == 1
    assert len(fake_smtp.made) == 2 and len(fake_imap.made) == 2


# -- Failures trip the authentication breaker ----------------------------------


def _send(db, sender, email="buyer@brand.de"):
    sid = db.log_search("q", source="t")
    cid = db.add_contact(sid, email)
    sender.dispatch_to_contact(
        contact={"id": cid, "email": email, "name": "N"},
        subject="s",
        html_body="<p>hi</p>",
        plain_body="hi",
    )
    return email


def test_a_refused_token_trips_the_breaker_and_leaves_the_contact_pending(
    db, tmp_path, token_server, fake_smtp
):
    token_server.refuse("AADSTS700027", "Client assertion failed signature validation")
    sender = EmailSender(
        db, ms.SMTP_HOST, 587, MAILBOX, "", signin=_signin(_pem(tmp_path))
    )
    email = _send(db, sender)
    assert sender.auth_failed is True
    assert email in [c["email"] for c in db.get_pending_contacts()]
    assert not db.is_suppressed(email)
    assert fake_smtp.made == [], "a refused sign-in must open no connection"
    actions = [r["action"] for r in db.get_recent_activity(20)]
    assert "SMTP_AUTH_FAILED" in actions


def test_a_refused_xoauth2_login_trips_the_breaker_and_says_why(
    db, tmp_path, token_server, fake_smtp
):
    fake_smtp.reply = (
        535,
        (
            b"5.7.139 Authentication unsuccessful, SmtpClientAuthentication is "
            b"disabled for the Mailbox."
        ),
    )
    sender = EmailSender(
        db, ms.SMTP_HOST, 587, MAILBOX, "", signin=_signin(_pem(tmp_path))
    )
    ok, message, permanent, _unsure = sender._deliver(EmailMessage())
    assert (ok, permanent, sender.auth_failed) == (False, False, True)
    assert "SmtpClientAuthenticationDisabled $false" in message
    assert "535 5.7.139 Authentication unsuccessful" in message, (
        "the server's own words must stay beside the explanation"
    )


def test_an_unreachable_token_server_does_not_trip_the_breaker(
    db, tmp_path, token_server, fake_smtp
):
    token_server.answers.append((0, httpx.ConnectTimeout("slow")))
    sender = EmailSender(
        db, ms.SMTP_HOST, 587, MAILBOX, "", signin=_signin(_pem(tmp_path))
    )
    ok, _message, permanent, unsure = sender._deliver(EmailMessage())
    assert (ok, permanent, unsure, sender.auth_failed) == (False, False, False, False)


def test_a_refused_token_leaves_the_inbox_closed(db, tmp_path, token_server, fake_imap):
    token_server.refuse("AADSTS700016")
    monitor = ImapMonitor(
        db, ms.IMAP_HOST, 993, MAILBOX, "", signin=_signin(_pem(tmp_path))
    )
    assert monitor._connect() is None
    assert fake_imap.made == []


def test_an_inbox_that_refuses_the_token_is_reported(
    db, tmp_path, token_server, fake_imap, caplog
):
    fake_imap.refuse = True
    monitor = ImapMonitor(
        db, ms.IMAP_HOST, 993, MAILBOX, "", signin=_signin(_pem(tmp_path))
    )
    with caplog.at_level(logging.ERROR):
        assert monitor._connect() is None
    assert "Add-MailboxPermission" in caplog.text


# -- The token never reaches a log ---------------------------------------------


def test_the_token_and_the_assertion_never_reach_a_log(
    db, tmp_path, token_server, fake_smtp, fake_imap, caplog
):
    caplog.set_level(logging.DEBUG)
    signin = _signin(_pem(tmp_path))
    EmailSender(db, ms.SMTP_HOST, 587, MAILBOX, "", signin=signin)._new_connection()
    ImapMonitor(db, ms.IMAP_HOST, 993, MAILBOX, "", signin=signin)._connect()
    fake_smtp.reply = (535, b"5.7.3 Authentication unsuccessful")
    signin.forget()
    sender = EmailSender(db, ms.SMTP_HOST, 587, MAILBOX, "", signin=signin)
    _send(db, sender)
    token_server.refuse("AADSTS700027")
    signin.forget()
    with pytest.raises(ms.SignInRefused):
        signin.token()

    secrets = [*token_server.issued]
    secrets += [token_server.form(i)["client_assertion"] for i in range(3)]
    secrets += [
        base64.b64encode(ms.xoauth2(MAILBOX, t).encode()).decode()
        for t in token_server.issued
    ]
    rows = db.conn.execute("SELECT * FROM activity_log").fetchall()
    written = caplog.text + repr(signin) + " ".join(str(tuple(r)) for r in rows)
    rows = db.conn.execute("SELECT * FROM email_log").fetchall()
    written += " ".join(str(tuple(r)) for r in rows)
    assert not [s for s in secrets if s in written]


@pytest.mark.parametrize(
    "line",
    [
        "token eyJ0eXAiOiJKV1QifQ.eyJhdWQiOiJodHRwcyJ9.c2lnbmF0dXJlLXZhbHVl issued",
        "client_assertion=eyJhbGciOiJQUzI1NiJ9.eyJpc3MiOiJhYmMifQ.c2ln",
        "send: 'AUTH XOAUTH2 dXNlcj1vdXRyZWFjaEBzZW5kZXIuZXhhbXBsZQFhdXRo'",
        "> AUTHENTICATE answer dXNlcj1vdXRyZWFjaEBzZW5kZXIuZXhhbXBsZQFhdXRo==",
    ],
)
def test_the_log_mask_hides_tokens_and_xoauth2_answers(line):
    masked = mask_personal(line)
    assert "[secret]" in masked
    assert "eyJhdWQ" not in masked and "eyJpc3M" not in masked
    assert "dXNlcj1vdXRyZWFj" not in masked


# -- Settings ------------------------------------------------------------------


def _refusal(settings_factory, pem, **overrides) -> str:
    with pytest.raises(ValidationError) as refused:
        _ms_settings(settings_factory, pem, **overrides)
    return config_error_text(refused.value)


def test_microsoft_signin_alone_is_accepted(settings_factory, tmp_path):
    s = _ms_settings(
        settings_factory,
        _pem(tmp_path),
        imap_enabled=True,
        imap_host="outlook.office365.com",
        imap_port=993,
    )
    assert s.microsoft_signin_problems() == []
    (primary,) = s.get_sender_accounts()
    assert primary["microsoft"] is True
    assert primary["password"] == primary["imap_password"] == ""


@pytest.mark.parametrize(
    "overrides,named",
    [
        ({"smtp_password": "Pw-in-the-env-1"}, "SMTP_PASSWORD"),
        ({"imap_password": "Pw-in-the-env-1"}, "IMAP_PASSWORD"),
        (
            {
                "sender_3_email": "three@sender.example",
                "sender_3_password": "Pw-in-the-env-1",
            },
            "SENDER_3_PASSWORD",
        ),
        (
            {
                "sender_accounts": json.dumps(
                    [{"username": "x@sender.example", "password": "Pw-in-the-env-1"}]
                )
            },
            "SENDER_ACCOUNTS entry 1",
        ),
    ],
)
def test_a_password_beside_microsoft_signin_is_refused(
    settings_factory, tmp_path, overrides, named
):
    message = _refusal(settings_factory, _pem(tmp_path), **overrides)
    assert named in message
    assert "Pw-in-the-env-1" not in message


def test_every_kept_password_is_named_at_once(settings_factory, tmp_path):
    message = _refusal(
        settings_factory,
        _pem(tmp_path),
        smtp_password="Pw-in-the-env-1",
        sender_2_email="two@sender.example",
        sender_2_password="Pw-in-the-env-2",
    )
    assert "SMTP_PASSWORD" in message and "SENDER_2_PASSWORD" in message


def test_a_mailbox_elsewhere_keeps_its_password(settings_factory, tmp_path):
    s = _ms_settings(
        settings_factory,
        _pem(tmp_path),
        sender_accounts=json.dumps(
            [
                {"username": "two@sender.example"},
                {
                    "username": "other@elsewhere.example",
                    "password": "elsewhere-pw",
                    "smtp_host": "smtp.elsewhere.example",
                },
            ]
        ),
        sender_4_email="four@sender.example",
    )
    accounts = {a["username"]: a for a in s.get_sender_accounts()}
    assert accounts[MAILBOX]["microsoft"] is True
    assert accounts["four@sender.example"]["microsoft"] is True, (
        "a numbered slot without a password was dropped"
    )
    assert accounts["two@sender.example"]["microsoft"] is True
    assert accounts["other@elsewhere.example"]["microsoft"] is False
    assert accounts["other@elsewhere.example"]["password"] == "elsewhere-pw"


@pytest.mark.parametrize(
    "entry",
    [
        {"smtp_host": "outlook.office365.com"},
        {"smtp_host": "smtp-mail.outlook.com"},
        {"smtp_host": "smtp.elsewhere.example", "imap_host": "outlook.office365.com"},
    ],
)
def test_a_password_for_a_microsoft_mailbox_under_another_name_is_refused(
    settings_factory, tmp_path, entry
):
    entry = {"username": "x@sender.example", "password": "Pw-in-the-env-1", **entry}
    message = _refusal(
        settings_factory,
        _pem(tmp_path),
        imap_enabled=True,
        imap_host="outlook.office365.com",
        imap_port=993,
        sender_accounts=json.dumps([entry]),
    )
    assert "the password of SENDER_ACCOUNTS entry 1" in message
    assert "must send through smtp.office365.com port 587" in message
    assert "Pw-in-the-env-1" not in message


@pytest.mark.parametrize(
    "entry,named",
    [
        ({"smtp_port": 465}, "must send through smtp.office365.com port 587"),
        ({"smtp_port": "25"}, "must send through smtp.office365.com port 587"),
        ({"imap_port": 143}, "must read mail from outlook.office365.com port 993"),
    ],
)
def test_a_microsoft_mailbox_on_another_port_is_refused(
    settings_factory, tmp_path, entry, named
):
    message = _refusal(
        settings_factory,
        _pem(tmp_path),
        imap_enabled=True,
        imap_host="outlook.office365.com",
        imap_port=993,
        sender_accounts=json.dumps([{"username": "x@sender.example", **entry}]),
    )
    assert named in message


def test_every_microsoft_mailbox_signs_in_through_the_one_app(
    settings_factory, tmp_path
):
    from agent import OutreachAgent

    s = _ms_settings(
        settings_factory,
        _pem(tmp_path),
        db_path=str(tmp_path / "a.db"),
        imap_enabled=True,
        imap_host="outlook.office365.com",
        imap_port=993,
        sender_2_email="two@sender.example",
        sender_accounts=json.dumps(
            [
                {
                    "username": "other@elsewhere.example",
                    "password": "elsewhere-pw",
                    "smtp_host": "smtp.elsewhere.example",
                    "imap_host": "imap.elsewhere.example",
                }
            ]
        ),
    )
    agent = OutreachAgent(s)
    by_name = {es.smtp_username: es for _k, _n, es in agent.senders}
    signins = {id(by_name[n].signin) for n in (MAILBOX, "two@sender.example")}
    assert len(signins) == 1 and agent.microsoft_signin is not None
    assert by_name[MAILBOX].signin is agent.microsoft_signin
    assert by_name["other@elsewhere.example"].signin is None
    inboxes = {m.username: m for m in agent.imap_monitors}
    assert inboxes["two@sender.example"].signin is agent.microsoft_signin
    assert inboxes["other@elsewhere.example"].signin is None


@pytest.mark.parametrize(
    "overrides,named",
    [
        ({"microsoft_tenant_id": "common"}, "MICROSOFT_TENANT_ID"),
        ({"microsoft_client_id": "not-a-guid"}, "MICROSOFT_CLIENT_ID"),
        ({"microsoft_client_id": ""}, "MICROSOFT_CLIENT_ID"),
        ({"smtp_host": "smtp.gmail.com"}, "SMTP_HOST=smtp.office365.com"),
        ({"smtp_port": 465}, "SMTP_PORT=587"),
        ({"imap_enabled": True}, "IMAP_HOST=outlook.office365.com"),
    ],
)
def test_ids_and_hosts_must_fit_microsoft(settings_factory, tmp_path, overrides, named):
    assert named in _refusal(settings_factory, _pem(tmp_path), **overrides)


def test_a_verified_domain_is_a_tenant(settings_factory, tmp_path):
    s = _ms_settings(
        settings_factory, _pem(tmp_path), microsoft_tenant_id="suppleco.onmicrosoft.com"
    )
    assert s.microsoft_signin_on()


def test_a_key_file_others_can_read_is_refused(settings_factory, tmp_path, monkeypatch):
    pem = _pem(tmp_path)
    monkeypatch.setattr(ms, "owner_only", lambda: True)
    monkeypatch.setattr(ms, "_mode", lambda path: 0o100644)
    assert "chmod 600" in _refusal(settings_factory, pem)
    monkeypatch.setattr(ms, "_mode", lambda path: 0o100600)
    assert _ms_settings(settings_factory, pem).microsoft_signin_problems() == []


@pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="POSIX only")
def test_on_posix_a_key_file_with_group_read_is_refused(settings_factory, tmp_path):
    pem = _pem(tmp_path)
    pem.chmod(0o640)
    assert "chmod 600" in _refusal(settings_factory, pem)
    pem.chmod(0o600)
    assert _ms_settings(settings_factory, pem).microsoft_signin_problems() == []


def test_an_unusable_certificate_file_is_refused_without_printing_it(
    settings_factory, tmp_path
):
    other = _key(slot=1)
    cases = {
        "names no file": tmp_path / "missing.pem",
        "does not belong": _pem(
            tmp_path, certificate=_certificate(other), name="mismatch.pem"
        ),
        "RSA of 2048 bits": _pem(tmp_path, key=_key(bits=1024), name="small.pem"),
        "MICROSOFT_CERTIFICATE_PASSPHRASE": _pem(
            tmp_path, passphrase=PASSPHRASE, name="encrypted.pem"
        ),
    }
    for words, path in cases.items():
        message = _refusal(settings_factory, path)
        assert words in message, message
        assert "PRIVATE KEY" not in message and "BEGIN" not in message
    message = _refusal(
        settings_factory,
        cases["MICROSOFT_CERTIFICATE_PASSPHRASE"],
        microsoft_certificate_passphrase="the-wrong-one",
    )
    assert "the-wrong-one" not in message and "PASSPHRASE is wrong" in message
    s = _ms_settings(
        settings_factory,
        cases["MICROSOFT_CERTIFICATE_PASSPHRASE"],
        microsoft_certificate_passphrase=PASSPHRASE,
    )
    assert PASSPHRASE not in repr(s)


# -- Doctor --------------------------------------------------------------------


def _doctor(settings, label, live=True):
    checks = main.run_doctor_checks(
        settings, live_smtp=live, network=False, only={label}
    )
    return checks[0] if checks else None


def _imap_settings(settings_factory, tmp_path, **kw):
    return _ms_settings(
        settings_factory,
        _pem(tmp_path),
        imap_enabled=True,
        imap_host="outlook.office365.com",
        imap_port=993,
        **kw,
    )


def test_doctor_signs_in_to_every_inbox_and_runs_a_noop(
    settings_factory, tmp_path, token_server, fake_imap
):
    s = _imap_settings(settings_factory, tmp_path, sender_2_email="two@sender.example")
    row = _doctor(s, "Microsoft sign-in (live)")
    assert row.ok is True, row.detail
    assert {i.answers[0] for i in fake_imap.made} == {
        base64.b64encode(ms.xoauth2(u, token_server.issued[0]).encode())
        for u in (MAILBOX, "two@sender.example")
    }
    assert all(i.nooped for i in fake_imap.made)


def test_doctor_explains_a_mailbox_the_app_cannot_open(
    settings_factory, tmp_path, token_server, fake_imap
):
    fake_imap.refuse = True
    row = _doctor(
        _imap_settings(settings_factory, tmp_path), "Microsoft sign-in (live)"
    )
    assert row.ok is False
    assert "Add-MailboxPermission" in row.detail and "FullAccess" in row.detail


def test_doctor_explains_an_unknown_app(
    settings_factory, tmp_path, token_server, fake_imap
):
    token_server.refuse("AADSTS700016")
    row = _doctor(
        _imap_settings(settings_factory, tmp_path), "Microsoft sign-in (live)"
    )
    assert row.ok is False and "does not know this app" in row.detail
    assert fake_imap.made == []


def test_doctors_live_smtp_login_explains_smtp_auth_switched_off(
    settings_factory, tmp_path, token_server, fake_smtp
):
    fake_smtp.reply = (
        535,
        (
            b"5.7.139 Authentication unsuccessful, SmtpClientAuthentication is "
            b"disabled for the Tenant."
        ),
    )
    row = _doctor(_ms_settings(settings_factory, _pem(tmp_path)), "SMTP login (live)")
    assert row.ok is False
    assert "Set-CASMailbox" in row.detail


def test_doctor_opens_nothing_outside_its_live_mode(
    settings_factory, tmp_path, token_server, fake_imap
):
    s = _imap_settings(settings_factory, tmp_path)
    assert _doctor(s, "Microsoft sign-in (live)", live=False) is None
    assert token_server.requests == [] and fake_imap.made == []


def test_doctor_warns_before_the_certificate_runs_out(settings_factory, tmp_path):
    key = _key()
    pem = _pem(tmp_path, certificate=_certificate(key, days=10))
    row = _doctor(_ms_settings(settings_factory, pem), "Microsoft sign-in", live=False)
    assert row.ok is False and "expires on" in row.detail
    row = _doctor(
        _ms_settings(settings_factory, _pem(tmp_path, name="long.pem")),
        "Microsoft sign-in",
        live=False,
    )
    assert row.ok is True and "valid until" in row.detail


def test_doctor_flags_a_microsoft_mailbox_kept_on_a_password(settings_factory):
    s = settings_factory(
        smtp_host="smtp.office365.com",
        smtp_port=587,
        smtp_username=MAILBOX,
        smtp_password="Pw-in-the-env-1",
    )
    row = _doctor(s, "Microsoft sign-in", live=False)
    assert row.ok is False and "DEPLOY.md" in row.detail
    other = settings_factory(smtp_host="smtp.elsewhere.example", smtp_password="pw")
    assert _doctor(other, "Microsoft sign-in", live=False).ok is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"smtp_host": "outlook.office365.com"},
        {
            "smtp_host": "smtp.elsewhere.example",
            "imap_enabled": True,
            "imap_host": "outlook.office365.com",
            "imap_password": "Pw-in-the-env-2",
        },
    ],
)
def test_doctor_flags_a_password_for_microsoft_under_another_name(
    settings_factory, overrides
):
    s = settings_factory(
        **{
            "smtp_port": 587,
            "smtp_username": MAILBOX,
            "smtp_password": "Pw-in-the-env-1",
            **overrides,
        }
    )
    row = _doctor(s, "Microsoft sign-in", live=False)
    assert row.ok is False and "signs in with a password" in row.detail


def test_doctor_counts_a_microsoft_mailbox_as_having_its_credentials(
    settings_factory, tmp_path
):
    s = _imap_settings(settings_factory, tmp_path)
    assert _doctor(s, "SMTP credentials set", live=False).ok is True
    assert _doctor(s, "IMAP credentials", live=False).ok is True
