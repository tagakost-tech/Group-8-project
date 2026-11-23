"""Wrong dashboard passwords are limited per address.

Nothing limited how fast one address could try passwords against the
dashboard's Basic login: a script could guess for as long as it liked. Now an
address that sends 10 wrong passwords within 15 minutes is answered 429, with
Retry-After, until the oldest of them leaves the window - its right password
too, or the guessing would simply go on. The address is the connection's
own; X-Forwarded-For is never read. A request with no credentials (the
browser asking for the login box) and a successful login count for nothing.
The recipient-facing pages stay reachable. Memory is bounded and the count
is locked, as for the unsubscribe page's limit.
"""

import base64
import time

import pytest

import main
from conftest import TEST_DASHBOARD_PASSWORD, anonymous
from modules.rate_limit import RequestLimiter

ATTACKER = "203.0.113.7"
OPERATOR = "198.51.100.20"


def _basic(password, user="admin"):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(settings_factory):
    return anonymous(main.create_dashboard_app(settings_factory()).test_client())


def _get(client, password=None, addr=ATTACKER, path="/api/stats", **headers):
    if password is not None:
        headers.update(_basic(password))
    return client.get(path, headers=headers, environ_base={"REMOTE_ADDR": addr})


def _fail(client, times, addr=ATTACKER, **headers):
    for i in range(times):
        assert _get(client, f"guess-{i}", addr, **headers).status_code == 401


def test_ten_wrong_passwords_lock_the_address(client):
    _fail(client, main.LOGIN_FAILURES_ALLOWED)
    locked = _get(client, "guess-11")
    assert locked.status_code == 429
    wait = int(locked.headers["Retry-After"])
    assert 0 < wait <= main.LOGIN_FAILURE_WINDOW_SECONDS


def test_the_lockout_is_logged_once_it_starts(client, caplog):
    _fail(client, main.LOGIN_FAILURES_ALLOWED - 1)
    assert "wrong passwords" not in caplog.text
    _fail(client, 1)
    assert f"wrong passwords from {ATTACKER}" in caplog.text
    assert "guess-" not in caplog.text, "a tried password reached the log"


def test_nine_wrong_passwords_do_not(client):
    _fail(client, main.LOGIN_FAILURES_ALLOWED - 1)
    assert _get(client, TEST_DASHBOARD_PASSWORD).status_code == 200


def test_the_right_password_waits_too_while_locked(client):
    _fail(client, main.LOGIN_FAILURES_ALLOWED)
    resp = _get(client, TEST_DASHBOARD_PASSWORD)
    assert resp.status_code == 429, "a right guess answered 200 while locked"


def test_another_address_is_not_locked(client):
    _fail(client, main.LOGIN_FAILURES_ALLOWED)
    assert _get(client, TEST_DASHBOARD_PASSWORD, addr=OPERATOR).status_code == 200


def test_x_forwarded_for_is_never_read(client):
    for i in range(main.LOGIN_FAILURES_ALLOWED):
        resp = _get(
            client, f"guess-{i}", ATTACKER, **{"X-Forwarded-For": f"10.0.0.{i}"}
        )
        assert resp.status_code == 401
    assert (
        _get(client, "again", ATTACKER, **{"X-Forwarded-For": "10.9.9.9"}).status_code
        == 429
    )
    # Nor can a forged header lock out somebody else.
    assert _get(client, TEST_DASHBOARD_PASSWORD, addr=OPERATOR).status_code == 200


def test_no_credentials_and_good_logins_count_for_nothing(client):
    for _ in range(3 * main.LOGIN_FAILURES_ALLOWED):
        assert _get(client).status_code == 401
        assert _get(client, TEST_DASHBOARD_PASSWORD).status_code == 200
    _fail(client, main.LOGIN_FAILURES_ALLOWED - 1)
    assert _get(client, TEST_DASHBOARD_PASSWORD).status_code == 200


def test_the_lock_ends_when_the_window_passes(settings_factory, monkeypatch):
    monkeypatch.setattr(main, "LOGIN_FAILURE_WINDOW_SECONDS", 0.5)
    client = anonymous(main.create_dashboard_app(settings_factory()).test_client())
    _fail(client, main.LOGIN_FAILURES_ALLOWED)
    assert _get(client, TEST_DASHBOARD_PASSWORD).status_code == 429
    time.sleep(0.6)
    assert _get(client, TEST_DASHBOARD_PASSWORD).status_code == 200


def test_the_recipient_pages_stay_reachable_while_locked(client):
    _fail(client, main.LOGIN_FAILURES_ALLOWED)
    resp = _get(client, path="/unsubscribe/not-a-token")
    assert resp.status_code not in (401, 429)


# ── The limiter's peek ──


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_peeking_counts_nothing():
    clock = _Clock()
    limiter = RequestLimiter(3, window=60, clock=clock)
    for _ in range(50):
        assert limiter.retry_after("a") is None
    for _ in range(3):
        limiter.hit("a")
    clock.now += 10
    assert limiter.retry_after("a") == 50
    assert limiter.retry_after("b") is None
    clock.now += 50
    assert limiter.retry_after("a") is None


def test_the_failure_table_is_bounded():
    limiter = RequestLimiter(10, window=900, max_clients=100, clock=_Clock())
    for i in range(1000):
        limiter.hit(f"198.51.100.{i}")
    assert len(limiter) == 100
