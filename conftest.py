"""Shared pytest fixtures and import-path setup for the test suite."""

import os
import pathlib
import sys

# Make the project root importable (config, agent, modules.*) regardless of cwd.
ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# The suite tests the agent's rules against one fixed client profile, so a
# change to the live client's words never changes what these tests prove. Set
# before any module is imported, since the modules read the profile at import;
# subprocesses the tests start inherit it.
os.environ["CLIENT_PROFILE_PATH"] = str(
    ROOT / "tests" / "fixtures" / "supplement_profile.json"
)
# The same for the email texts: the fixed client's, not the live client's.
os.environ["CLIENT_TEMPLATES_DIR"] = str(ROOT / "tests" / "fixtures" / "templates")

import pytest

from modules.database import (
    Database,
    set_default_source_channel,
    set_erasure_key,
)

# The key every test runs with, as every process does after start-up. The
# settings factory below gives the same key, so a test that builds an agent
# and opens the database directly sees one key, not two.
TEST_ERASURE_KEY = "test-erasure-key-" + "0" * 32

# The dashboard is never built without a password of 12 characters or more,
# so every test's settings carry this one, and the test client logs in with it.
TEST_DASHBOARD_USERNAME = "admin"
TEST_DASHBOARD_PASSWORD = "test-dashboard-password"
TEST_DASHBOARD_LOGIN = {
    "username": TEST_DASHBOARD_USERNAME,
    "password": TEST_DASHBOARD_PASSWORD,
}


def _send_basic_auth_up_front():
    """A browser without a session gets the sign-in page, not a Basic
    challenge, and Chromium sends http_credentials only after a challenge.
    So a browser test's credentials also go as an Authorization header on
    every request, as a script's would."""
    try:
        from playwright.sync_api._generated import Browser
    except ImportError:
        return
    import base64

    def wrap(original):
        def opened(self, *args, **kwargs):
            login = kwargs.get("http_credentials")
            if login:
                token = base64.b64encode(
                    f"{login['username']}:{login['password']}".encode()
                ).decode()
                headers = dict(kwargs.get("extra_http_headers") or {})
                headers.setdefault("Authorization", f"Basic {token}")
                kwargs["extra_http_headers"] = headers
            return original(self, *args, **kwargs)

        return opened

    for name in ("new_context", "new_page"):
        setattr(Browser, name, wrap(getattr(Browser, name)))


_send_basic_auth_up_front()
# The origin Flask's test client requests as (http://localhost/).
TEST_DASHBOARD_ORIGIN = "http://localhost"


@pytest.fixture(scope="session", autouse=True)
def _the_fixed_client_is_never_written():
    """The fixed client's files are the suite's reference: a test that saved
    into them would change what every later test - and run - checks against."""
    import hashlib

    fixtures = ROOT / "tests" / "fixtures"

    def digest():
        return {
            p.relative_to(fixtures).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in sorted(fixtures.rglob("*"))
            if p.is_file()
        }

    before = digest()
    yield
    after = digest()
    assert after == before, (
        f"a test wrote into tests/fixtures: {sorted(set(after.items()) ^ set(before.items()))}"
    )


@pytest.fixture(autouse=True)
def _the_fixed_client(monkeypatch):
    """The suite's fixed client sends its own subjects, and its sends are
    unattended: these tests check the sending machinery itself, which the
    review queue sits in front of. The tests about review set their stage to
    ``approve`` themselves (tests/test_people_decide_what_is_sent.py)."""
    monkeypatch.setenv("EMAIL_SUBJECT", "Suppleco CDMO | Company Profile e Capabilities")
    monkeypatch.setenv(
        "EMAIL_SUBJECT_IT", "Suppleco CDMO | Company Profile e Capabilities"
    )
    for stage in ("FIRST_EMAIL", "FOLLOWUP", "REPLY"):
        monkeypatch.setenv(f"AUTONOMY_{stage}", "auto")


@pytest.fixture(autouse=True)
def _erasure_key():
    set_erasure_key(TEST_ERASURE_KEY)


@pytest.fixture(autouse=True)
def _the_test_client_is_the_operators_browser(monkeypatch):
    """Every dashboard test client is the operator's own browser: logged in
    with the test password, and writing from the dashboard's own page (an
    Origin header, as a browser sends on every POST). A request's own headers
    win over these, and anonymous() takes both away: tests ABOUT the login and
    the origin check say so explicitly."""
    import base64

    import flask
    from flask.testing import FlaskClient

    token = base64.b64encode(
        f"{TEST_DASHBOARD_USERNAME}:{TEST_DASHBOARD_PASSWORD}".encode()
    ).decode()

    class OperatorClient(FlaskClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.environ_base["HTTP_AUTHORIZATION"] = f"Basic {token}"
            self.environ_base["HTTP_ORIGIN"] = TEST_DASHBOARD_ORIGIN

    monkeypatch.setattr(flask.Flask, "test_client_class", OperatorClient)


def sent_to(db, email, message_id=None):
    """What a send to email leaves of itself, for a bounce report about it to
    be acted on (Database.bounce_matches_a_send): an email_log row, for a
    contact the database holds, or the row a restore keeps by the token, for
    an address it does not. Returns the send's Message-ID."""
    import uuid

    from modules import clock
    from modules.database import token_of

    message_id = message_id or f"<sent-{uuid.uuid4().hex}@suppleco.test>"
    contact = db.get_contact_by_email(email)
    if contact:
        db.log_email(
            contact["id"],
            email,
            "A first email",
            delivery_status="sent",
            message_id=message_id,
        )
    else:
        with db._transaction() as conn:
            conn.execute(
                "INSERT INTO prior_sends (token, message_id, sent_at, "
                "delivery_status) VALUES (?, ?, ?, 'sent')",
                (token_of(email), message_id, clock.db_now()),
            )
    return message_id


def anonymous(client):
    """A test client for the same app with no login and no Origin: a
    stranger, a script or a mail server, as the dashboard sees them. The
    client passed in keeps both."""
    stranger = client.application.test_client()
    stranger.environ_base.pop("HTTP_AUTHORIZATION", None)
    stranger.environ_base.pop("HTTP_ORIGIN", None)
    return stranger


@pytest.fixture(autouse=True)
def _source_channel():
    """Production names a channel at every call site and refuses a contact
    without one; tests store theirs as a referral unless they say otherwise.
    A referral is also an L1.5 channel, so a test's Direct Send may go out."""
    set_default_source_channel("referral")
    yield
    set_default_source_channel(None)


@pytest.fixture(scope="session", autouse=True)
def _the_real_database_is_never_touched():
    """No test may open the product's own database file.

    Tests that built settings without a db_path opened data/agent.db, created
    it, and stamped it with the test key's fingerprint - so the product would
    then have refused the real ERASURE_KEY. settings_factory now defaults to a
    temporary path; this fails the run if anything reaches the real file.
    """
    real = ROOT / "data" / "agent.db"

    def state():
        return (real.stat().st_size, real.stat().st_mtime_ns) if real.exists() else None

    before = state()
    yield
    assert state() == before, f"a test opened or changed {real}"


@pytest.fixture(scope="session", autouse=True)
def _no_setting_comes_from_the_shell():
    """No setting exported in the shell reaches a test.

    pydantic-settings reads the process environment as well as .env, and
    _env_file=None turns off only the file. So SMTP credentials exported in the
    shell that ran the suite reached every test that did not pass its own, and
    a live-path test that reached SMTP would send with them; DRY_RUN and the
    rest changed what the tests test. They are removed for the whole run,
    before any fixture builds settings, the browser tests' module-wide
    dashboard included. A test can still set one with monkeypatch.setenv.
    """
    from config import Settings

    # The retired cloud AI settings too: exported, each stops every Settings.
    names = {
        (field.alias or name).lower() for name, field in Settings.model_fields.items()
    } | {name.lower() for name in Settings.RETIRED_SETTINGS}
    with pytest.MonkeyPatch.context() as mp:
        for key in list(os.environ):
            if key.lower() in names:
                mp.delenv(key)
        yield


_THIS_MACHINE = ("127.0.0.1", "::1", "localhost")
_HTTP_VERBS = ("request", "get", "head", "options", "delete", "post", "put", "patch")


def _guard_the_network(mp, attempts):
    """Refuse, and record, every way a test can reach another machine."""
    import socket

    def refuse(what):
        attempts.append(what)
        raise ConnectionRefusedError(f"a test may not reach the network: {what}")

    def local(address):
        return not isinstance(address, tuple) or address[0] in _THIS_MACHINE

    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    sendto = socket.socket.sendto
    getaddrinfo = socket.getaddrinfo

    def guarded_connect(sock, address):
        if local(address):
            return connect(sock, address)
        return refuse(f"connect {address!r}")

    def guarded_connect_ex(sock, address):
        if local(address):
            return connect_ex(sock, address)
        return refuse(f"connect_ex {address!r}")

    def guarded_sendto(sock, data, *flags_and_address):
        if local(flags_and_address[-1]):
            return sendto(sock, data, *flags_and_address)
        return refuse(f"sendto {flags_and_address[-1]!r}")

    def guarded_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        # A numeric host is parsed, never looked up; connect still guards it.
        if host in _THIS_MACHINE or flags & socket.AI_NUMERICHOST:
            return getaddrinfo(host, port, family, type, proto, flags)
        return refuse(f"getaddrinfo {host!r}")

    mp.setattr(socket.socket, "connect", guarded_connect)
    mp.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    mp.setattr(socket.socket, "sendto", guarded_sendto)
    mp.setattr(socket, "getaddrinfo", guarded_getaddrinfo)

    def refused(what):
        return lambda *args, **kwargs: refuse(what)

    # These open their own connections from native code, past Python's sockets:
    # ddgs searches through primp, and the page fetcher uses curl_cffi.
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        curl_requests = None
    if curl_requests is not None:
        for session in (curl_requests.Session, curl_requests.AsyncSession):
            mp.setattr(session, "request", refused(f"curl_cffi {session.__name__}.request"))
    try:
        import primp
    except ImportError:
        primp = None
    if primp is not None:
        for verb in _HTTP_VERBS:
            for client in (primp.Client, primp.AsyncClient):
                mp.setattr(client, verb, refused(f"primp {client.__name__}.{verb}"))
            if hasattr(primp, verb):
                mp.setattr(primp, verb, refused(f"primp.{verb}"))


# The running test's list of refused attempts, for what records its own: the
# browser tests' Chromium (tests/ui/test_dashboard_ui.py, _browser).
_RUNNING_TEST_ATTEMPTS = []


def browser_reached(what):
    """Chromium asked for another machine: the running test fails."""
    if _RUNNING_TEST_ATTEMPTS:
        _RUNNING_TEST_ATTEMPTS[-1].append(what)


@pytest.fixture(autouse=True)
def _no_network():
    """No test may reach another machine.

    The suite sent the default prospecting queries to public search engines,
    opened an SMTP session with smtp.gmail.com and made live DNS lookups:
    slow or red on a locked-down network, and with credentials in the
    environment, real sends. A refused attempt fails its test even when the
    code under test swallows the error, as code that degrades gracefully
    does. This machine stays reachable: the SMTP sink and the browser tests'
    dashboard listen on 127.0.0.1. The browser tests (-m ui) are guarded too:
    the dashboard serves its own libraries, and their Chromium resolves no
    other host's name and reports each request to another machine here; see
    tests/test_no_test_reaches_the_network.py.
    """
    attempts = []
    with pytest.MonkeyPatch.context() as mp:
        _guard_the_network(mp, attempts)
        _RUNNING_TEST_ATTEMPTS.append(attempts)
        try:
            yield attempts
        finally:
            _RUNNING_TEST_ATTEMPTS.remove(attempts)
    if attempts:
        pytest.fail("the test reached for the network: " + "; ".join(attempts), pytrace=False)


@pytest.fixture
def db(tmp_path):
    """A fresh, isolated SQLite Database in a temp directory."""
    return Database(str(tmp_path / "test_agent.db"))


def _invented_client_list(folder):
    """A client list in the format modules/clients.py reads: names in the
    first column of the first sheet. One invented company, so the screen
    loads and matches as it does in production, and no test's prospect is on
    it. Written once per test, so a client a test marks stays marked."""
    path = folder / "settings_factory_clients.xlsx"
    if not path.exists():
        from openpyxl import Workbook

        wb = Workbook()
        wb.active.append(["Fictitious Client S.r.l."])
        wb.save(path)
    return path


def _invented_profile(folder):
    """A stand-in for the Company Profile PDF the first email attaches."""
    path = folder / "settings_factory_profile.pdf"
    if not path.exists():
        path.write_bytes(b"%PDF-1.4 an invented company profile\n%%EOF\n")
    return path


def _browser_dashboard_settings(folder):
    """Settings for the dashboard the browser tests serve (tests/ui). It lives
    for a whole module, so it cannot come from settings_factory, and its list
    is invented too: "Mark as client" writes to the dashboard's client list,
    and the default is SUPPLECO's real client register."""
    from config import Settings

    return Settings(
        _env_file=None,
        db_path=str(folder / "ui.db"),
        ERASURE_KEY=TEST_ERASURE_KEY,
        DASHBOARD_USERNAME=TEST_DASHBOARD_USERNAME,
        DASHBOARD_PASSWORD=TEST_DASHBOARD_PASSWORD,
        client_list_path=str(_invented_client_list(folder)),
    )


def _dry_run_logged(db, contact_id) -> bool:
    """Was a simulated send recorded for this contact?

    Asserted on the EMAIL LOG rather than contacts.status. Three tests used to
    check `contact["status"] == "dry_run"`, which codified a real bug: a dry
    run mutated the contact, get_pending_contacts selects only 'pending', and
    nothing moves a contact back - so one dry run against a live database
    silently rendered the whole backlog unsendable. Those tests would have
    blocked the fix, so the assertion moved to where the record belongs.
    """
    import sqlite3

    raw = sqlite3.connect(db.db_path)
    rows = raw.execute(
        "SELECT delivery_status FROM email_log WHERE contact_id = ?", (contact_id,)
    ).fetchall()
    raw.close()
    return any(r[0] == "dry_run" for r in rows)


@pytest.fixture
def settings_factory(tmp_path):
    """Factory building isolated Settings (ignoring the real .env) with overrides.

    Pacing is switched off by default. The send loops call
    _interruptible_sleep(_send_delay()) between contacts, so with production
    defaults every multi-contact test spends real wall-clock seconds asleep -
    5s per send under delay_between_emails alone, and 120s once emails_per_hour
    is honoured as a floor. A suite that sleeps is a suite people stop running.

    Tests that are ABOUT pacing pass their own values, so this hides nothing:
    see tests/test_settings_reach_enforcement.py.
    """
    from config import Settings

    def _make(**overrides):
        # _env_file=None keeps tests independent of the developer's real .env,
        # and the default db_path keeps them off the product's own database.
        overrides.setdefault("db_path", str(tmp_path / "settings_factory.db"))
        overrides.setdefault("delay_between_emails", 0.0)
        overrides.setdefault("emails_per_hour", 0)
        overrides.setdefault("send_jitter_seconds", 0.0)
        # A blank legal identity refuses every live send, which would turn
        # every live-path test into a test of that refusal. Tests that are
        # ABOUT it pass blanks explicitly: see
        # tests/test_live_send_needs_a_legal_identity.py.
        overrides.setdefault("legal_entity_name", "Test Manufacturing S.r.l.")
        overrides.setdefault(
            "legal_registered_office", "Via Roma 1, 00100 Roma RM, Italy"
        )
        overrides.setdefault(
            "legal_company_registration",
            "Reg. Imprese di Roma n. 00000000000 - P.IVA IT00000000000",
        )
        overrides.setdefault("privacy_notice_url", "https://example.test/privacy")
        overrides.setdefault("privacy_contact_email", "privacy@example.test")
        overrides.setdefault("legal_share_capital", "EUR 10.000,00 i.v.")
        overrides.setdefault("legal_single_member", "no")
        # Likewise the opt-out route: with BASE_URL on this machine and no
        # inbox monitor, nothing would read an opt-out, and every live send
        # is refused. Tests that are ABOUT it pass a local BASE_URL: see
        # tests/test_live_send_needs_an_opt_out_route.py.
        overrides.setdefault("base_url", "https://outreach.example.test")
        overrides.setdefault("ERASURE_KEY", TEST_ERASURE_KEY)
        # And the dashboard password, without which the dashboard is not
        # built. Tests ABOUT it pass their own: see
        # tests/test_the_dashboard_always_needs_a_password.py.
        overrides.setdefault("DASHBOARD_PASSWORD", TEST_DASHBOARD_PASSWORD)
        # And the interim country lock: every country the tests address, and
        # no-country domains, so live-path tests are not tests of the lock.
        # Tests ABOUT it pass their own: see
        # tests/test_live_send_needs_an_enabled_country.py.
        overrides.setdefault("ENABLED_COUNTRIES", "DE,IT,SE,FR,PL,NL,GB,AU")
        overrides.setdefault("UNKNOWN_COUNTRY_ALLOWED", True)
        # And the client list: while the screen is on and its list is not
        # loaded, every live send is refused, and the default path is
        # SUPPLECO's real client register, which git does not hold. A fresh
        # clone failed 94 tests on that refusal, and wherever the file was
        # present, every live-path test read SUPPLECO's customers. Tests ABOUT
        # the list pass their own: see
        # tests/test_client_list_cannot_stop_startup.py.
        if "client_list_path" not in overrides:
            overrides["client_list_path"] = str(_invented_client_list(tmp_path))
        # And what SUPPLECO's first email says of itself: what the sender is
        # responsible for, and the Company Profile it says is attached. Blank
        # or missing, every live first email is refused, and the default PDF
        # path is SUPPLECO's real profile, which git does not hold. Tests ABOUT
        # both pass their own: see
        # tests/test_a_first_email_needs_its_title_and_its_attachment.py.
        overrides.setdefault("sender_title", "Business Development")
        # The fixed client's first email says its profile is attached.
        overrides.setdefault("attach_profile", True)
        if "pdf_path" not in overrides:
            overrides["pdf_path"] = str(_invented_profile(tmp_path))
        return Settings(_env_file=None, **overrides)

    return _make


def open_tab(page, target):
    """Open a dashboard tab as a person does: the technical tabs fold away
    under "Advanced", which is unfolded first when the tab lives there."""
    item = page.locator(f".nav-links li[data-target='{target}']")
    if not item.is_visible():
        page.click("#navMore")
    item.click()
