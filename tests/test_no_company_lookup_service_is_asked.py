"""When a search finds nothing, the agent asks no one else.

Before, a search of up to three words that found no website was sent on to
Clearbit's company-name lookup (autocomplete.clearbit.com), an outside
company that then received the name SUPPLECO was looking for, and its answer
was fetched as the company's site. The owner chose to remove it: a search
that finds nothing now simply has no pages to read.
"""

import re

import httpx

import modules.search as search_mod
from main import PROJECT_ROOT as ROOT
from modules.search import SearchModule


def test_no_running_code_names_clearbit():
    sources = [*ROOT.glob("modules/*.py"), *ROOT.glob("*.py")]
    assert any(p.name == "search.py" for p in sources)
    named = [
        p.name
        for p in sources
        if re.search("clearbit", p.read_text(encoding="utf-8"), re.IGNORECASE)
    ]
    assert named == []


class _NothingMayBeFetched:
    uses_impersonation = False

    def __init__(self):
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        raise AssertionError(f"fetched {url}")

    def close(self):
        pass


def test_a_search_that_finds_nothing_contacts_no_one(db, monkeypatch):
    resolved, clients = [], []

    def resolve(host):
        resolved.append(host)
        raise OSError("no DNS in tests")

    real_client = httpx.Client

    def client(*args, **kwargs):
        clients.append(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(search_mod, "resolve_host", resolve)
    sm = SearchModule(db, rate_limit=0.0)
    fetcher = sm._client = _NothingMayBeFetched()
    monkeypatch.setattr(sm, "_ddg_search", lambda q: [])
    monkeypatch.setattr(search_mod.httpx, "Client", client)

    assert sm.search_and_extract("Brand GmbH") == []

    assert fetcher.asked == []
    assert resolved == []
    assert clients == []
    (row,) = [r for r in db.get_all_searches() if r["search_query"] == "Brand GmbH"]
    assert row["status"] == "no_results"
