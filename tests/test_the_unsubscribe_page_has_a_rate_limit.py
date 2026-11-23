"""The public unsubscribe page limits how fast one address may call it, and
never at the cost of a genuine one-click opt-out.

/unsubscribe/<token> answers anyone without a login, and each request reads
the database and may set a file aside. Nothing limited how fast one address
could call it. Now each client address has UNSUBSCRIBE_REQUESTS_PER_MINUTE
requests in any minute; beyond that it is answered 429 with a Retry-After.
Mailbox providers send RFC 8058 one-click POSTs in bursts from a few
addresses and never send one again, so a one-click for a real link is applied
however far over the limit its sender is. The address is the connection's
own: no trusted-proxy setting exists, so no header a caller writes is read.
"""

import os

import pytest

from modules.database import Database
from modules.rate_limit import RequestLimiter
from modules.unsubscribe import UnsubscribeHandler
from tests.test_a_click_after_a_restore_still_opts_out import (
    ANNA,
    SECRET,
    _contact,
    _send,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def s(settings_factory, tmp_path):
    return settings_factory(
        db_path=str(tmp_path / "live" / "agent.db"),
        unsubscribe_secret=SECRET,
        UNSUBSCRIBE_REQUESTS_PER_MINUTE=3,
    )


@pytest.fixture
def db(s):
    os.makedirs(os.path.dirname(s.get_abs_db_path()), exist_ok=True)
    return Database(s.get_abs_db_path())


@pytest.fixture
def client(s, db):
    import main

    return main.create_dashboard_app(s).test_client()


def _junk(n):
    return f"not-a-token-{n}"


def test_the_default_is_a_sensible_number(settings_factory):
    assert 10 <= settings_factory().unsubscribe_requests_per_minute <= 120


def test_requests_under_the_limit_are_answered(client):
    for n in range(3):
        assert client.get(f"/unsubscribe/{_junk(n)}").status_code == 400


def test_a_request_over_the_limit_gets_429_and_a_retry_after(client):
    for n in range(3):
        client.get(f"/unsubscribe/{_junk(n)}")

    reply = client.get(f"/unsubscribe/{_junk(9)}")

    assert reply.status_code == 429
    assert 1 <= int(reply.headers["Retry-After"]) <= 60
    assert reply.mimetype == "text/html"
    assert "Too many requests" in reply.get_data(as_text=True)


def test_a_made_up_one_click_over_the_limit_gets_429(client):
    for n in range(3):
        client.post(f"/unsubscribe/{_junk(n)}")

    assert client.post(f"/unsubscribe/{_junk(9)}").status_code == 429


def test_the_limit_is_per_client_address(client):
    for n in range(3):
        client.get(f"/unsubscribe/{_junk(n)}")

    other = client.get(
        f"/unsubscribe/{_junk(9)}", environ_base={"REMOTE_ADDR": "203.0.113.9"}
    )

    assert other.status_code == 400


def test_a_forwarded_for_header_does_not_buy_more_requests(client):
    for n in range(3):
        client.get(
            f"/unsubscribe/{_junk(n)}", headers={"X-Forwarded-For": f"198.51.100.{n}"}
        )

    reply = client.get(
        f"/unsubscribe/{_junk(9)}", headers={"X-Forwarded-For": "198.51.100.99"}
    )

    assert reply.status_code == 429


def test_a_real_one_click_over_the_limit_still_opts_the_person_out(client, db):
    token = _send(db, _contact(db))
    for n in range(10):
        client.post(f"/unsubscribe/{_junk(n)}")

    reply = client.post(f"/unsubscribe/{token}")

    assert reply.status_code == 200
    assert db.is_suppressed(ANNA)


def test_a_real_link_the_database_lost_is_kept_over_the_limit(client, db):
    from modules import optout_spool

    token = UnsubscribeHandler(db, SECRET).token_for(ANNA)
    for n in range(10):
        client.post(f"/unsubscribe/{_junk(n)}")

    assert client.post(f"/unsubscribe/{token}").status_code == 200
    assert optout_spool.is_set_aside(db.db_path, token)


def test_the_window_slides():
    clock = Clock()
    limiter = RequestLimiter(3, window=60, clock=clock)
    for second in (0, 20, 40):
        clock.now = 1000 + second
        assert limiter.hit("a") is None

    clock.now = 1059
    assert limiter.hit("a") == 1, "the first request is still inside the minute"

    clock.now = 1080.5
    assert limiter.hit("a") is None, "the requests at 0 and 20 have left it"


def test_a_client_that_keeps_calling_stays_limited():
    clock = Clock()
    limiter = RequestLimiter(2, window=60, clock=clock)
    for second in range(0, 300, 10):
        clock.now = 1000 + second
        limiter.hit("a")

    assert limiter.hit("a") is not None


def test_memory_is_bounded():
    clock = Clock()
    limiter = RequestLimiter(5, window=60, max_clients=100, clock=clock)
    for n in range(1000):
        limiter.hit(f"10.0.{n // 256}.{n % 256}")
        assert len(limiter) <= 100
    for _ in range(50):
        limiter.hit("10.9.9.9")
    assert len(limiter._hits["10.9.9.9"]) == 5


def test_clients_whose_requests_left_the_window_are_dropped():
    clock = Clock()
    limiter = RequestLimiter(5, window=60, clock=clock)
    for n in range(50):
        limiter.hit(f"client-{n}")

    clock.now += 61
    limiter.hit("someone-new")

    assert len(limiter) == 1
