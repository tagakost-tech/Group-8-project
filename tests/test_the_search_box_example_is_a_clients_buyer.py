"""The example in the search box is a buyer the live client actually looks for.

At a client demo the box at the top read "Pharma distributors in UK": SUPPLECO
makes food supplements for brands and is not pharma, so the first thing the
owner was taught to type was a search for the wrong buyer. The example is now
one of the live client's own English buyer phrases (client/omega.json).
"""

import re

import main
from conftest import _browser_dashboard_settings
from modules.client_profile import DEFAULT_PROFILE_PATH, load_profile


def _placeholder(tmp_path):
    client = main.create_dashboard_app(
        _browser_dashboard_settings(tmp_path)
    ).test_client()
    html = client.get("/").get_data(as_text=True)
    found = re.search(r'id="newQuery"\s+placeholder="([^"]*)"', html)
    assert found, "the search box has no placeholder"
    return found.group(1)


def test_the_example_is_a_buyer_phrase_of_the_live_client(tmp_path):
    text = _placeholder(tmp_path)
    seeds = load_profile(DEFAULT_PROFILE_PATH).buyer_seeds["en"]
    assert any(seed in text for seed in seeds), text


def test_the_example_does_not_mention_pharma(tmp_path):
    assert "pharma" not in _placeholder(tmp_path).lower()
