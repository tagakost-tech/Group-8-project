"""No secret is printed by a settings repr or written to a log in clear.

SENDER_ACCOUNTS, a JSON list that holds each extra mailbox's password, was
printed by any repr of the settings: only the fields someone remembered had
repr=False. The repr test listed the secrets by hand, so a new one could be
missed. The log masking knew addresses, unsubscribe tokens and IPv4
addresses only: an API key in an error message, an Authorization header, a
password=... in a URL and an IPv6 address went to the log file as they were.

Now (2026-11 security hardening, stage 3):
- every setting named like a password, secret, key, token or accounts list
  is found by its name and must stay out of a repr;
- keys with a known prefix (sk-, gsk_, AIza), long values after key=,
  token= and secret=, any value after password=, Authorization header values
  and IPv6 addresses are masked; ordinary log text is left as it was.
"""

import json
import re

import pytest

import main
from config import Settings
from modules.compliance import mask_personal
from modules.database import erasure_token

SECRET_NAME = re.compile(
    r"password|passwd|passphrase|secret|key|token|credential|accounts"
)


def _secret_fields():
    return {
        name: field
        for name, field in Settings.model_fields.items()
        if SECRET_NAME.search(name)
    }


def test_the_finder_sees_every_secret_setting_known_today():
    found = set(_secret_fields())
    assert {
        "smtp_password",
        "sender_2_password",
        "sender_5_password",
        "imap_password",
        "unsubscribe_secret",
        "erasure_key",
        "dashboard_password",
        "llm_api_key",
        "sender_accounts",
        "microsoft_certificate_passphrase",
        "lists_key",
    } <= found


def test_every_secret_setting_stays_out_of_a_repr(settings_factory):
    values = {}
    for name, field in _secret_fields().items():
        sentinel = f"S3cret-{name}-7Hq2ZpW9"
        if name == "sender_accounts":
            sentinel = json.dumps(
                [{"username": "a@example.test", "password": sentinel}]
            )
        values[field.alias or name] = sentinel
    s = settings_factory(**values)
    shown = repr(s) + str(s)
    leaked = [key for key, value in values.items() if value in shown]
    assert not leaked


@pytest.mark.parametrize(
    "line,expected",
    [
        (
            "AI call failed: Incorrect API key provided: sk-proj-AbCdEf0123456789XyZ",
            "AI call failed: Incorrect API key provided: sk-[secret]",
        ),
        ("key gsk_abcdefghijklmnop0123456789 refused", "key gsk_[secret] refused"),
        (
            "GET /v1beta/models?key=AIzaSyA-abcdefghijklmnopqrstuvwxyz0123456",
            "GET /v1beta/models?key=AIza[secret]",
        ),
        (
            "GET /x?token=0123456789abcdef0123456789abcdef HTTP/1.1",
            "GET /x?token=[secret] HTTP/1.1",
        ),
        (
            "secret=c2VjcmV0LXZhbHVlLWhlcmUtMTIzNDU2Nzg5MA== rejected",
            "secret=[secret] rejected",
        ),
        ("api_key=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b", "api_key=[secret]"),
        ("login failed: smtp_password=hunter2 user=x", "login failed: smtp_password=[secret] user=x"),
        ('{"username": "x", "password": "pw!"}', '{"username": "x", "password": "[secret]"}'),
        (
            "headers={'Authorization': 'Bearer abc.def-ghi_jkl', 'Accept': 'x'}",
            "headers={'Authorization': 'Bearer [secret]', 'Accept': 'x'}",
        ),
        ("Authorization: Basic YW5uYUBicmFuZC5kZQ==", "Authorization: Basic [secret]"),
        ("sent with bearer eyJhbGciOiJIUzI1NiJ9.e30", "sent with bearer [secret]"),
        (
            "login from 2a02:1210:4e2c:9d00:1c3a:5b6e:7f80:9a1b failed",
            "login from [ip] failed",
        ),
        ("2001:db8::1 - - [16/Nov/2026 10:30:00] GET /", "[ip] - - [16/Nov/2026 10:30:00] GET /"),
        ("open from [2001:db8:85a3::8a2e:370:7334]:5000", "open from [[ip]]:5000"),
        # Review of stage 3: a key printed as JSON or a dict, an api-key
        # header, and a quoted password with spaces kept all or part of it.
        (
            'body {"api_key": "9f86d081884c7d659a2feaa0c55ad015"}',
            'body {"api_key": "[secret]"}',
        ),
        (
            "Settings {'llm_api_key': 'own-0123456789abcdefXYZ'}",
            "Settings {'llm_api_key': '[secret]'}",
        ),
        ("X-Api-Key: 0123456789abcdef0123", "X-Api-Key: [secret]"),
        ("headers={'api-key': 'abc0123456789'}", "headers={'api-key': '[secret]'}"),
        ('login with password="correct horse battery" failed', 'login with password="[secret]" failed'),
    ],
)
def test_keys_passwords_and_ipv6_addresses_are_masked(line, expected):
    assert mask_personal(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "Sent 5 emails in 00:12:30; next at 2026-11-16 14:05:00,123",
        "agent_state key=last_run value=2026-11-16T10:30:00+01:00",
        "key=followup_it:followup_1_it.html",
        "Dashboard password: set (12 characters)",
        "ERASURE_KEY is blank - set it in .env",
        "sha256 deadbeefdeadbeefdeadbeefdeadbeef matches",
        "rows[1::2] and x[::-1] at 12:30:45.123",
        "open from fe80::1 at 10:00",
        "MAC aa:bb:cc:dd:ee:ff",
        "Traceback (most recent call last): File \"main.py\", line 12, in <module>",
        "the desk-top skeleton key opens a monkey's token box",
        "Batch 3/7 done: 42 sent, 0 failed, 3 skipped (score < 40)",
        '{"key": "followup_1_it", "token": "short"}',
        "LLM_API_KEY is set; the x-api-key header is not used",
    ],
)
def test_ordinary_log_text_is_left_as_it_was(line):
    assert mask_personal(line) == line


def test_an_erasure_token_after_token_equals_stays_readable():
    token = erasure_token("anna@brand.de")
    assert mask_personal(f"Erased token={token}") == f"Erased token={token}"


def test_doctor_sees_a_key_left_in_a_log_file(settings_factory, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.log").write_text(
        "10:00:00 ok\nAI call failed: Bearer sk-own0123456789abcdefXYZ\n",
        encoding="utf-8",
    )
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    (row,) = main.run_doctor_checks(
        s, network=False, log_dir=logs, only={"Log files masked"}
    )
    assert row.ok is False and "key, password" in row.detail
