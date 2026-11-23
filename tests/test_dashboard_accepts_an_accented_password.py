"""An accented dashboard password must lock the dashboard, not crash it.

hmac.compare_digest refuses str arguments holding non-ASCII characters and
raises TypeError. An Italian operator who set DASHBOARD_PASSWORD=Perché2026!
got a 500 on every page, logged in or not, and the only way back in was to
weaken the password. Both sides are now compared as UTF-8 bytes. (The
password now needs 12 characters, so the one here is longer.)
"""

import base64

import pytest

import main
from conftest import anonymous

PASSWORD = "Perché2026!-Milano"


@pytest.fixture
def client(tmp_path, settings_factory):
    s = settings_factory(
        db_path=str(tmp_path / "auth.db"),
        DASHBOARD_USERNAME="operatore",
        DASHBOARD_PASSWORD=PASSWORD,
    )
    return anonymous(main.create_dashboard_app(s).test_client())


def _basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_the_right_accented_password_gets_in(client):
    assert client.get("/", headers=_basic("operatore", PASSWORD)).status_code == 200


@pytest.mark.parametrize("typed", ["Perche2026!-Milano", "Perché2026?-Milano", "àèìòù"])
def test_a_wrong_password_is_refused_not_crashed(client, typed):
    assert client.get("/", headers=_basic("operatore", typed)).status_code == 401


def test_no_password_is_refused(client):
    assert client.get("/").status_code == 401
