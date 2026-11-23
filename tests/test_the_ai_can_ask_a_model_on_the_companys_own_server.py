"""The AI's questions can go to a model at an address the operator sets.

The AI is a model on SUPPLECO's own server (Qwen served by Ollama, which
speaks the OpenAI chat interface at http://<host>:11434/v1), and no other:
the cloud providers Gemini and Groq were removed. LLM_PROVIDER=
openai_compatible sends the chat-completions call to LLM_BASE_URL with
LLM_MODEL. Ollama ignores the key, so none is needed and
no Authorization header goes without one. A redirect is not followed: the
prompt goes to the configured host and nowhere else. The start-up log names
the host and the model, never the key. The processing-agreement gate is the
same as it always was: nothing carrying a recipient's data is asked
while AI_DPA_REFERENCE is blank. The stand-ins answer with the shapes Ollama
0.40.16 returned on 12 November 2026 serving qwen3.5:0.8b; the tests never
need that server.
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import httpx
import pytest

from modules.ai_generator import AIGenerator

ADDRESS = "http://127.0.0.1:11434/v1"
MODEL = "qwen3.5:0.8b"
CONTACT = {
    "organization": "Nordic Wellness",
    "qualification_rationale": "Sells own-brand vitamins across Scandinavia.",
    "email": "buyer@nordicwellness.se",
}


def _answer(text):
    """Ollama 0.40.16's answer with reasoning_effort none, as received."""
    return {
        "id": "chatcmpl-628",
        "object": "chat.completion",
        "created": 1794512295,
        "model": MODEL,
        "system_fingerprint": "fp_ollama",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 44},
            "completion_tokens": 2,
            "total_tokens": 52,
        },
    }


@pytest.fixture
def server(monkeypatch):
    """httpx with every request answered by `server.reply`, and recorded."""
    real_client = httpx.Client
    state = type("Server", (), {})()
    state.requests = []
    state.clients = []
    state.reply = lambda request: httpx.Response(200, json=_answer("YES"))

    def handler(request):
        state.requests.append(request)
        return state.reply(request)

    # A subclass, not a function, so isinstance checks on httpx.Client hold.
    class Client(real_client):
        def __init__(self, **kwargs):
            state.clients.append(kwargs)
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    return state


def _own(**overrides):
    options = {
        "provider": "openai_compatible",
        "base_url": ADDRESS,
        "llm_model": MODEL,
        "personal_data_allowed": True,
    }
    options.update(overrides)
    return AIGenerator(**options)


def test_it_is_configured_by_an_address_and_a_model_without_a_key():
    ai = _own()
    assert ai.is_configured and (ai.base_url, ai.MODEL_NAME) == (ADDRESS, MODEL)


@pytest.mark.parametrize("missing", ["base_url", "llm_model"])
def test_it_is_not_configured_without_the_address_or_the_model(missing):
    assert not _own(**{missing: ""}).is_configured


@pytest.mark.parametrize("provider", ["", "gemini", "groq", "GROQ"])
def test_an_ai_that_is_off_keeps_none_of_the_own_servers_settings(provider):
    """A blank or retired provider name leaves the AI off, holding no
    address, key or reasoning setting that a later reader could take as live."""
    ai = _own(provider=provider, llm_api_key="sk-own", reasoning_effort="high")
    assert not ai.is_configured
    assert (ai.base_url, ai.api_key, ai.reasoning_effort) == (None, None, "")


def test_the_question_goes_to_the_address_with_the_model(server):
    assert _own().classify_relevance(CONTACT) is True
    (request,) = server.requests
    assert str(request.url) == f"{ADDRESS}/chat/completions"
    assert request.method == "POST"
    assert json.loads(request.content)["model"] == MODEL


def test_no_authorization_header_goes_without_a_key(server):
    _own().classify_relevance(CONTACT)
    assert "authorization" not in server.requests[0].headers


def test_the_key_goes_as_a_bearer_token_when_set(server):
    _own(llm_api_key="sk-own").classify_relevance(CONTACT)
    assert server.requests[0].headers["authorization"] == "Bearer sk-own"


def test_the_timeout_setting_is_the_calls_timeout(server):
    _own(timeout=120).classify_relevance(CONTACT)
    assert server.clients[0]["timeout"] == 120


def test_the_question_asks_for_no_reasoning_by_default(server):
    _own().classify_relevance(CONTACT)
    assert json.loads(server.requests[0].content)["reasoning_effort"] == "none"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_the_reasoning_effort_setting_goes_in_the_request(server, effort):
    _own(reasoning_effort=effort).classify_relevance(CONTACT)
    assert json.loads(server.requests[0].content)["reasoning_effort"] == effort


def test_a_blank_reasoning_effort_sends_no_such_field(server):
    """For a server that refuses the field: it is left out, not sent empty."""
    _own(reasoning_effort="").classify_relevance(CONTACT)
    assert set(json.loads(server.requests[0].content)) == {
        "model",
        "messages",
        "temperature",
    }


def test_text_in_the_reasoning_field_is_never_the_answer(server):
    """Ollama's real answer when the reply was cut short while the model
    reasoned: content "" and the thinking in message.reasoning. A YES in the
    reasoning is not a YES the model gave."""
    cut_short = _answer("")
    cut_short["choices"][0]["message"]["reasoning"] = "Thinking Process:\n\nYES"
    cut_short["choices"][0]["finish_reason"] = "length"
    server.reply = lambda request: httpx.Response(200, json=cut_short)
    ai = _own()
    assert ai.classify_relevance(CONTACT) is None
    assert ai.classify_reply_intent("Re: hello", "Tell me more") is None
    assert ai.generate_opening_line(CONTACT) == ""


def test_a_busy_server_is_asked_again_after_the_wait_it_names(server, monkeypatch):
    """The refusal's body is now read so doctor can quote it; the wait a 429
    names must still reach the retry."""
    from modules import ai_generator

    waits = []
    monkeypatch.setattr(ai_generator.time, "sleep", waits.append)
    answers = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}, text="busy"),
            httpx.Response(200, json=_answer("YES")),
        ]
    )
    server.reply = lambda request: next(answers)
    assert _own().classify_relevance(CONTACT) is True
    assert waits == [7.0]


def test_a_redirect_is_not_followed(server):
    def reply(request):
        if request.url.host == "127.0.0.1":
            return httpx.Response(
                307, headers={"location": "http://elsewhere.test/v1/chat/completions"}
            )
        return httpx.Response(200, json=_answer("YES"))

    server.reply = reply
    assert _own().classify_relevance(CONTACT) is None
    assert [r.url.host for r in server.requests] == ["127.0.0.1"]


def test_ollamas_redirect_for_a_doubled_slash_is_no_answer(server):
    """Ollama answers POST /v1//chat/completions with 307, an empty body and
    no content type."""
    server.reply = lambda request: httpx.Response(
        307, headers={"location": "/v1/chat/completions"}
    )
    ai = _own()
    ai.base_url = ADDRESS + "/"
    assert ai.classify_relevance(CONTACT) is None
    assert [r.url.path for r in server.requests] == ["/v1//chat/completions"]


def test_the_start_up_log_names_the_host_and_the_model_not_the_key(caplog):
    with caplog.at_level(logging.INFO, logger="modules.ai_generator"):
        _own(llm_api_key="sk-own-secret")
    logged = caplog.text
    assert "127.0.0.1:11434" in logged and MODEL in logged
    assert "sk-own-secret" not in logged


def test_nothing_carrying_a_recipients_data_is_asked_without_an_agreement(server):
    ai = _own(personal_data_allowed=False)
    assert ai.classify_relevance(CONTACT) is None
    assert ai.classify_company_country(CONTACT) is None
    assert ai.classify_reply_intent("Re: hello", "yes please") is None
    assert ai.generate_opening_line(CONTACT) == ""
    assert ai.generate_email_body(CONTACT, "Anna", "SUPPLECO") == ""
    assert server.requests == []


def test_the_agent_builds_the_generator_from_the_settings(settings_factory):
    from agent import OutreachAgent

    ai = OutreachAgent(
        settings_factory(
            llm_provider="openai_compatible",
            LLM_BASE_URL=ADDRESS + "/",
            LLM_MODEL=MODEL,
            LLM_API_KEY="sk-own",
            LLM_TIMEOUT_SECONDS=90,
            LLM_REASONING_EFFORT="low",
        )
    ).ai_gen
    assert (ai.provider, ai.base_url, ai.MODEL_NAME) == (
        "openai_compatible",
        ADDRESS,
        MODEL,
    )
    assert (ai.api_key, ai.TIMEOUT_SECONDS) == ("sk-own", 90)
    assert ai.reasoning_effort == "low"


def test_the_dashboard_config_says_the_own_server_is_set(tmp_path, settings_factory):
    """It once showed only the cloud providers, both false, while the AI was
    configured on the company's own server; they are gone from it now."""
    import main

    app = main.create_dashboard_app(
        settings_factory(
            db_path=str(tmp_path / "dash.db"),
            blacklist_check_enabled=False,
            llm_provider="openai_compatible",
            LLM_BASE_URL=ADDRESS,
            LLM_MODEL=MODEL,
            LLM_API_KEY="sk-own-secret",
        )
    )
    data = app.test_client().get("/api/config").get_json()
    assert data["settings"]["llm_provider"] == "openai_compatible"
    assert data["settings"]["own_server_configured"] is True
    assert "sk-own-secret" not in str(data)
    assert not {"gemini_configured", "groq_configured"} & set(data["settings"])


def test_the_dashboard_config_says_no_own_server_without_one(
    tmp_path, settings_factory
):
    import main

    app = main.create_dashboard_app(
        settings_factory(db_path=str(tmp_path / "dash.db"), blacklist_check_enabled=False)
    )
    data = app.test_client().get("/api/config").get_json()
    assert data["settings"]["own_server_configured"] is False


@pytest.mark.parametrize("provider", ["groq", "gemini", ""])
def test_a_cloud_provider_name_asks_nobody(server, provider):
    """The Groq backend sent every prompt to api.groq.com whatever
    LLM_BASE_URL said. No provider but the own server builds a generator."""
    ai = AIGenerator(
        provider=provider,
        personal_data_allowed=True,
        base_url=ADDRESS,
        llm_model=MODEL,
        llm_api_key="gsk-test",
    )
    assert ai.is_configured is False and ai.base_url is None
    assert ai.classify_relevance(CONTACT) is None
    assert server.requests == []


def test_every_call_goes_without_a_proxy_and_without_redirects(server):
    _own().classify_relevance(CONTACT)
    (options,) = server.clients
    assert options["trust_env"] is False and options["follow_redirects"] is False


class _StandIn(BaseHTTPRequestHandler):
    """Answers as Ollama 0.40.16 did: with reasoning_effort none the answer
    alone; without it, the thinking in message.reasoning and the answer in
    content."""

    seen: ClassVar[list] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(
            (self.path, body["model"], self.headers.get("Authorization"))
        )
        answer = _answer("YES")
        if body.get("reasoning_effort") != "none":
            answer["choices"][0]["message"]["reasoning"] = (
                "Thinking Process:\n\nA vitamin brand: a buyer, not a rival. NO"
            )
        payload = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class _Proxy(BaseHTTPRequestHandler):
    """A proxy from the environment: records what reaches it, answers 502."""

    seen: ClassVar[list] = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).seen.append(self.path)
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST

    def log_message(self, *args):
        pass


def _serve(handler):
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_a_proxy_from_the_environment_is_not_used(monkeypatch):
    """httpx read HTTP_PROXY by default, so on a machine with a system or
    company proxy every plain-http prompt - company, domain, notes - went to
    that proxy in clear, not only to the configured server."""
    from modules.ai_generator import own_server_models

    _StandIn.seen.clear()
    _Proxy.seen.clear()
    model_server, proxy = _serve(_StandIn), _serve(_Proxy)
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    address = f"http://127.0.0.1:{model_server.server_port}/v1"
    try:
        assert _own(base_url=address).classify_relevance(CONTACT) is True
        try:
            own_server_models(address, None)
        except httpx.HTTPError:
            pass
    finally:
        for httpd in (model_server, proxy):
            httpd.shutdown()
            httpd.server_close()
    at_proxy, at_server = list(_Proxy.seen), [path for path, *_ in _StandIn.seen]
    _StandIn.seen.clear()
    assert at_proxy == []
    assert at_server == ["/v1/chat/completions"]


@pytest.mark.parametrize("effort", ["none", ""])
def test_a_stand_in_server_on_this_machine_answers_the_question(effort):
    """With reasoning left on, the NO in message.reasoning is not read."""
    _StandIn.seen.clear()
    httpd = HTTPServer(("127.0.0.1", 0), _StandIn)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        ai = _own(
            base_url=f"http://127.0.0.1:{httpd.server_port}/v1",
            reasoning_effort=effort,
        )
        assert ai.classify_relevance(CONTACT) is True
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert _StandIn.seen == [("/v1/chat/completions", MODEL, None)]
    _StandIn.seen.clear()
