"""The optional SMTP probe connects only to mail servers on the public internet.

VERIFY_SMTP_PROBE (off by default) opens a connection to the server a
prospect's MX record names. That record is outside input: whoever runs the
domain can point it at this machine, the office network or a cloud metadata
address, and the agent connected to whatever it named. Now the MX host must
have only public addresses, by the page fetcher's rules, and the connection
goes to the address that was checked. Nothing here touches the network.
"""

import ipaddress
from typing import ClassVar

import pytest

import modules.search as search_mod
from modules import verification
from modules.verification import EmailVerifier, _is_transient_smtp

PUBLIC = "93.184.216.34"
EMAIL = "anna@brand.de"


class _MX:
    def __init__(self, exchange, preference=10):
        self.exchange = exchange
        self.preference = preference


class _FakeSMTP:
    instances: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        self.connected_to = None
        _FakeSMTP.instances.append(self)

    def connect(self, host, port):
        self.connected_to = (host, port)
        return 220, b"ready"

    def ehlo_or_helo_if_needed(self):
        pass

    def mail(self, sender):
        return 250, b"ok"

    def rcpt(self, address):
        return 250, b"ok"

    def quit(self):
        pass


@pytest.fixture
def mx(monkeypatch):
    """mx(exchange, *addresses): the domain's MX names `exchange`, which
    resolves to `addresses`."""
    _FakeSMTP.instances = []
    monkeypatch.setattr(verification.smtplib, "SMTP", _FakeSMTP)
    asked = []

    def set_up(exchange, *addresses):
        monkeypatch.setattr(
            verification.dns.resolver, "resolve", lambda domain, kind: [_MX(exchange)]
        )

        def resolve(host):
            asked.append(host)
            if not addresses:
                raise OSError("[Errno 11001] getaddrinfo failed")
            return [ipaddress.ip_address(a) for a in addresses]

        monkeypatch.setattr(search_mod, "resolve_host", resolve)
        return asked

    return set_up


@pytest.mark.parametrize(
    "exchange,addresses",
    [
        ("mx.brand.de.", ["127.0.0.1"]),
        ("mx.brand.de.", ["::1"]),
        ("mx.brand.de.", ["10.0.0.5"]),
        ("mx.brand.de.", ["172.16.4.4"]),
        ("mx.brand.de.", ["192.168.1.1"]),
        ("mx.brand.de.", ["fd12:3456::1"]),
        ("mx.brand.de.", ["169.254.169.254"]),
        ("mx.brand.de.", ["fe80::1"]),
        ("mx.brand.de.", ["224.0.0.251"]),
        ("mx.brand.de.", ["240.0.0.1"]),
        ("mx.brand.de.", ["0.0.0.0"]),
        ("mx.brand.de.", ["100.64.0.7"]),
        ("mx.brand.de.", ["::ffff:127.0.0.1"]),
        ("mx.brand.de.", [PUBLIC, "10.0.0.5"]),
        ("127.0.0.1.", []),
        ("2130706433.", []),
        ("[10.0.0.1].", []),
        (".", []),
    ],
)
def test_an_mx_host_off_the_public_internet_is_never_probed(mx, exchange, addresses):
    mx(exchange, *addresses)

    accepted, detail = EmailVerifier.verify_smtp(EMAIL)

    assert accepted is False
    assert _FakeSMTP.instances == [] or all(
        s.connected_to is None for s in _FakeSMTP.instances
    )
    assert not _is_transient_smtp(detail), detail


def test_a_public_mx_host_is_probed_at_the_address_that_was_checked(mx):
    asked = mx("mx.brand.de.", PUBLIC)

    accepted, _detail = EmailVerifier.verify_smtp(EMAIL)

    assert accepted is True
    assert asked == ["mx.brand.de"]
    (smtp,) = _FakeSMTP.instances
    assert smtp.connected_to == (PUBLIC, 25)


def test_a_dns_answer_that_changes_after_the_check_moves_nothing(mx, monkeypatch):
    mx("mx.brand.de.", PUBLIC)
    answers = iter([[PUBLIC], ["127.0.0.1"]])
    monkeypatch.setattr(
        search_mod,
        "resolve_host",
        lambda host: [ipaddress.ip_address(a) for a in next(answers)],
    )

    EmailVerifier.verify_smtp(EMAIL)

    (smtp,) = _FakeSMTP.instances
    assert smtp.connected_to == (PUBLIC, 25)


def test_an_mx_host_that_does_not_resolve_is_a_retry_not_a_verdict(mx):
    mx("mx.brand.de.")

    accepted, detail = EmailVerifier.verify_smtp(EMAIL)

    assert accepted is False
    assert _FakeSMTP.instances == []
    assert _is_transient_smtp(detail), detail


def test_a_refused_mx_host_fails_the_address_in_the_full_check(mx):
    mx("mx.brand.de.", "10.0.0.5")

    result = EmailVerifier().verify_email(EMAIL, skip_smtp=False)

    assert result["valid"] is False
    assert not result.get("indeterminate")
    assert result["reason"].startswith("SMTP probe failed")
    assert _FakeSMTP.instances == []
