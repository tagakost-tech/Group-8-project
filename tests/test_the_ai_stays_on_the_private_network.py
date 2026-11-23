"""No AI question leaves this machine and the private network.

SUPPLECO's AI runs only on a model on its own server. The settings refuse an
LLM_BASE_URL whose host resolves outside loopback and the private ranges
when they load; but a name can resolve one way at start-up and another an
hour later, and every prompt carries a company's name, domain and page text,
or a prospect's reply. So the host is resolved and checked again before
every call, and when any address it resolves to is outside, nothing is sent:
the caller takes its path without AI, and doctor says why. Calls follow no
redirect, use no proxy from the environment and always have a timeout.
"""

import ipaddress
import logging

import httpx
import pytest

import config
from modules import ai_generator
from modules.ai_generator import AddressNotLocal, AIGenerator, own_server_models

ADDRESS = "http://ai.internal:11434/v1"
MODEL = "qwen2.5:14b"
CONTACT = {
    "organization": "Vitalis Nordica",
    "email": "info@vitalis-nordica.example",
    "qualification_rationale": "Vitamin brand",
}
MODELS = {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@pytest.fixture
def dns(monkeypatch):
    """The stand-in resolver: what ai.internal resolves to, changed by a test."""
    answer = {"ai.internal": ["10.0.0.7"]}

    def resolve(host):
        if host not in answer:
            raise OSError("no such name")
        return [ipaddress.ip_address(a) for a in answer[host]]

    monkeypatch.setattr(config, "resolve_host", resolve)
    return answer


@pytest.fixture
def server(monkeypatch):
    real_client = httpx.Client
    state = type("Server", (), {})()
    state.requests = []
    state.clients = []

    def handler(request):
        state.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        return httpx.Response(200, json={"choices": [{"message": {"content": "YES"}}]})

    class Client(real_client):
        def __init__(self, **kwargs):
            state.clients.append(kwargs)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(
        ai_generator.time,
        "sleep",
        lambda s: pytest.fail(f"a refused call waited {s}s to be retried"),
    )
    return state


def _own():
    return AIGenerator(
        provider="openai_compatible",
        base_url=ADDRESS,
        llm_model=MODEL,
        personal_data_allowed=True,
    )


def test_a_private_address_is_asked(dns, server):
    assert _own().classify_relevance(CONTACT) is True
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "now",
    [
        ["93.184.215.14"],
        ["10.0.0.7", "93.184.215.14"],
        ["169.254.169.254"],
        ["224.0.0.251"],
        ["0.0.0.0"],
        ["2606:4700::6810:85e5"],
    ],
)
def test_a_dns_answer_that_changes_after_start_up_sends_nothing(
    dns, server, caplog, now
):
    ai = _own()
    assert ai.classify_relevance(CONTACT) is True
    dns["ai.internal"] = now
    with caplog.at_level(logging.ERROR, logger="modules.ai_generator"):
        assert ai.classify_relevance(CONTACT) is None
        assert ai.generate_opening_line(CONTACT) == ""
        assert ai.classify_reply_intent("Re: hello", "Send prices") is None
    assert len(server.requests) == 1, "a prompt went to an address outside"
    assert "nothing sent" in caplog.text
    assert "this machine or the private network" in caplog.text
    assert "ai.internal" not in caplog.text
    # One line each, not a traceback: nothing failed, the address is refused.
    refused = [r for r in caplog.records if "nothing sent" in r.getMessage()]
    assert len(refused) == 3 and all(r.exc_info is None for r in refused)


def test_a_name_that_stops_resolving_sends_nothing(dns, server):
    ai = _own()
    del dns["ai.internal"]
    assert ai.classify_relevance(CONTACT) is None
    assert server.requests == []


def test_a_refused_address_is_not_a_stall_and_is_not_retried(dns, server):
    """It is no fault of the server's and no retry changes it."""
    ai = _own()
    dns["ai.internal"] = ["8.8.8.8"]
    for _ in range(ai.STALL_LIMIT + 1):
        assert ai.classify_relevance(CONTACT) is None
    assert ai.stalls_in_a_row == 0 and not ai.paused
    assert server.requests == []


def test_doctors_list_of_models_is_refused_too(dns, server):
    assert own_server_models(ADDRESS, None) == [MODEL]
    dns["ai.internal"] = ["93.184.215.14"]
    with pytest.raises(AddressNotLocal, match="nothing sent"):
        own_server_models(ADDRESS, "sk-own")
    assert len(server.requests) == 1


def test_doctors_question_is_refused_too(dns, server):
    ai = _own()
    dns["ai.internal"] = ["93.184.215.14"]
    with pytest.raises(AddressNotLocal):
        ai.ask_once("Reply with the single word YES.")
    assert server.requests == []


def test_every_call_has_a_timeout_and_no_redirect_or_proxy(dns, server):
    _own().classify_relevance(CONTACT)
    own_server_models(ADDRESS, None)
    assert len(server.clients) == 2
    for options in server.clients:
        assert options["timeout"] in (30.0, 3.0)
        assert options["follow_redirects"] is False
        assert options["trust_env"] is False


def test_a_redirect_is_not_followed(dns, monkeypatch):
    real_client = httpx.Client
    asked = []

    def handler(request):
        asked.append((str(request.url), request.headers["Host"]))
        return httpx.Response(307, headers={"Location": "https://outside.example/v1"})

    class Client(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    assert _own().classify_relevance(CONTACT) is None
    # Once, to the address the check found, for the configured name.
    assert asked == [("http://10.0.0.7:11434/v1/chat/completions", "ai.internal:11434")]


def _doctor(settings_factory):
    from main import run_doctor_checks

    settings = settings_factory(
        llm_provider="openai_compatible", LLM_BASE_URL=ADDRESS, LLM_MODEL=MODEL
    )
    return lambda: {
        c.label: c
        for c in run_doctor_checks(
            settings,
            network=True,
            only=(
                "AI server stays on this machine or private network",
                "AI server reachable",
            ),
        )
    }


def test_doctor_resolves_the_address_again(settings_factory, dns, server):
    doctor = _doctor(settings_factory)
    row = doctor()["AI server stays on this machine or private network"]
    assert row.ok is True
    assert len(server.requests) == 1
    dns["ai.internal"] = ["10.0.0.7", "93.184.215.14"]
    rows = doctor()
    row = rows["AI server stays on this machine or private network"]
    assert row.ok is False and "a public address" in row.detail
    assert "ai.internal" not in row.detail
    reachable = rows["AI server reachable"]
    assert reachable.ok is False and "nothing sent" in reachable.detail
    assert len(server.requests) == 1


def test_a_dns_hiccup_keeps_the_dashboard_overrides(settings_factory, db, dns, server):
    """Every rebuild for a runtime override resolved the AI server again, so
    while DNS did not answer each override was refused, and the agent ran on
    .env's bounce limit and pacing. The address cannot be changed at
    runtime and is resolved again before every AI call."""
    from config import Settings, effective_settings

    base = settings_factory(
        llm_provider="openai_compatible", LLM_BASE_URL=ADDRESS, LLM_MODEL=MODEL
    )
    db.set_runtime_override("max_bounce_rate", 0.02)
    db.set_runtime_override("delay_between_emails", 7.0)
    del dns["ai.internal"]
    eff = effective_settings(base, db)
    assert eff.max_bounce_rate == 0.02
    assert eff.delay_between_emails == 7.0
    # A name that cannot be looked up does not stop the settings loading:
    # the agent must still start and read opt-outs.
    assert Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        LLM_BASE_URL=ADDRESS,
        LLM_MODEL=MODEL,
    )
    # The call still resolves it, and sends nothing while it cannot.
    assert _own().classify_relevance(CONTACT) is None
    assert server.requests == []


def test_a_dns_answer_that_changes_keeps_the_dashboard_overrides(
    settings_factory, db, dns, server, monkeypatch
):
    """The other DNS fault: the name answers, with an address outside. A
    rebuild that looked the name up again refused every override, and the
    agent ran on .env's bounce limit and pacing. A rebuild asks no name
    server; the AI call does, and sends nothing."""
    from config import effective_settings

    base = settings_factory(
        llm_provider="openai_compatible", LLM_BASE_URL=ADDRESS, LLM_MODEL=MODEL
    )
    db.set_runtime_override("max_bounce_rate", 0.02)
    db.set_runtime_override("delay_between_emails", 7.0)
    dns["ai.internal"] = ["93.184.216.34"]
    asked = []
    resolve = config.resolve_host

    def counted(host):
        asked.append(host)
        return resolve(host)

    monkeypatch.setattr(config, "resolve_host", counted)
    eff = effective_settings(base, db)
    assert eff.max_bounce_rate == 0.02
    assert eff.delay_between_emails == 7.0
    assert asked == [], "a rebuild for an override looked the AI server up"
    assert _own().classify_relevance(CONTACT) is None
    assert server.requests == []
    assert asked == ["ai.internal"]
