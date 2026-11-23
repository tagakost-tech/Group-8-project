"""doctor tells IT whether the model on the company's own server answers.

With LLM_PROVIDER=openai_compatible the agent's AI questions go to a server
the company runs. doctor names that server's whole address and model (and
warns when the address's path does not end in /v1), and asks it
once for its list of models (GET {LLM_BASE_URL}/models: no personal data,
three seconds in all, the key when one is set). It reports one of three
states: reachable with the model listed; reachable without it (naming up to
five models it does list, so a model never pulled shows); or not reachable or
not understood. None of them refuses live sending - every AI use falls back
to the path without AI - so each is at most a warning. An LLM_BASE_URL set
while LLM_PROVIDER is blank is not used, and doctor says so.
The dashboard's Ready to send card asks no server.

A listed model can still answer nothing in time: Qwen 3.5 on Ollama was
listed, and reasoned for over three minutes on a one-word question. So
doctor then asks the model one fixed question without personal data, by the
agent's own call, and reports: answered as asked (with the seconds), answered
but not as asked (the first 40 characters), no answer within
LLM_TIMEOUT_SECONDS (with what to try), or refused (the status and the
server's own message, 120 characters at most). It is not asked when network
checks are off or the list row did not reach the server.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from main import AI_CHECK_QUESTION, readiness_report, run_doctor_checks

ADDRESS = "http://192.168.1.20:11434/v1"
MODEL = "qwen3.5:0.8b"
# GET /v1/models from Ollama 0.40.16 on 12 November 2026, as received.
MODELS = {
    "object": "list",
    "data": [
        {
            "id": "qwen3.5:0.8b",
            "object": "model",
            "created": 1794511730,
            "owned_by": "library",
        }
    ],
}


@pytest.fixture
def server(monkeypatch):
    real_client = httpx.Client
    state = type("Server", (), {})()
    state.requests = []
    state.clients = []
    state.reply = lambda r: httpx.Response(200, json=MODELS)

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


def _settings(settings_factory, **overrides):
    overrides.setdefault("LLM_BASE_URL", ADDRESS)
    overrides.setdefault("LLM_MODEL", MODEL)
    return settings_factory(llm_provider="openai_compatible", **overrides)


def _row(checks, label):
    return next((c for c in checks if c.label == label), None)


# The other network checks (DNS, SPF, DMARC) are left out: they have their
# own tests, and here they would only reach the guarded network.
AI_ROWS = ("AI server", "AI server reachable")


def _doctor(settings):
    return run_doctor_checks(settings, network=True, only=AI_ROWS)


def _reachable(settings):
    return _row(_doctor(settings), "AI server reachable")


def test_a_row_names_the_address_and_the_model(settings_factory, server):
    """Not [OK]: the row only repeats the settings, and an [OK] read as
    "server OK" above a server that was down, or with nothing asked."""
    row = _row(
        run_doctor_checks(_settings(settings_factory), network=False), "AI server"
    )
    assert row.ok is None
    assert row.detail.startswith(f"{ADDRESS}, model {MODEL}")
    assert "see AI server reachable" in row.detail


def test_the_generator_row_says_the_own_server_was_not_contacted(
    settings_factory, server
):
    checks = run_doctor_checks(
        _settings(settings_factory), network=False, only=("AI generator (optional)",)
    )
    row = _row(checks, "AI generator (optional)")
    assert "not contacted here" in row.detail
    assert server.requests == []


@pytest.mark.parametrize(
    "address", ["http://192.168.1.20:11434", "http://192.168.1.20:11434/api"]
)
def test_an_address_not_ending_in_v1_is_a_warning_that_says_so(
    settings_factory, server, address
):
    """Ollama's own address is http://host:11434, and with it every call
    went to /chat/completions, answered 404 and fell back, while doctor
    showed only host:port, the same as for a correct address."""
    row = _row(
        run_doctor_checks(
            _settings(settings_factory, LLM_BASE_URL=address), network=False
        ),
        "AI server",
    )
    assert row.ok is False and row.blocking is False
    assert row.detail.startswith(f"{address}, model {MODEL}")
    assert "LLM_BASE_URL" in row.detail and "/v1" in row.detail


def test_a_404_from_the_models_list_points_at_the_path(settings_factory, server):
    # Ollama's answer to a path it does not serve, as received.
    server.reply = lambda r: httpx.Response(
        404, headers={"content-type": "text/plain"}, text="404 page not found"
    )
    row = _reachable(_settings(settings_factory))
    assert "LLM_BASE_URL path /v1 answered 404" in row.detail


def test_a_listed_model_is_reported_reachable(settings_factory, server):
    row = _reachable(_settings(settings_factory))
    assert row.ok is True and MODEL in row.detail and "reachable" in row.detail


def test_the_check_is_one_get_of_the_models_within_three_seconds(
    settings_factory, server
):
    _reachable(_settings(settings_factory))
    (request,) = server.requests
    assert (request.method, str(request.url)) == ("GET", f"{ADDRESS}/models")
    assert request.content == b""
    assert server.clients[0]["timeout"] == 3.0
    assert "authorization" not in request.headers


def test_the_check_sends_the_key_when_one_is_set(settings_factory, server):
    _reachable(_settings(settings_factory, LLM_API_KEY="sk-own"))
    assert server.requests[0].headers["authorization"] == "Bearer sk-own"


def test_a_model_not_listed_is_a_warning_naming_five_it_lists(settings_factory, server):
    listed = [f"model-{n}" for n in range(1, 8)]
    server.reply = lambda r: httpx.Response(
        200, json={"object": "list", "data": [{"id": m} for m in listed]}
    )
    row = _reachable(_settings(settings_factory))
    assert row.ok is False and row.blocking is False
    assert f"{MODEL} is not in its list" in row.detail
    assert "model-1, model-2, model-3, model-4, model-5" in row.detail
    assert "model-6" not in row.detail


def test_a_model_not_listed_says_what_to_do(settings_factory, server):
    """The line named the problem and ended "falls back to the path without
    AI": the step DEPLOY.md gives was not in it."""
    server.reply = _lists("qwen3.5:latest")
    row = _reachable(_settings(settings_factory))
    assert (
        f"pull {MODEL} on the server, or set LLM_MODEL to a name it lists"
        in row.detail
    )


def _lists(*names):
    """Ollama's list of models, naming these."""
    entry = MODELS["data"][0]
    return lambda r: httpx.Response(
        200,
        json={"object": "list", "data": [{**entry, "id": n} for n in names]},
    )


@pytest.mark.parametrize(
    "model,listed",
    [
        ("qwen3.5", ["qwen3.5:latest", "qwen3.5:0.8b"]),
        ("qwen3.5:latest", ["qwen3.5"]),
        ("library/qwen3.5", ["library/qwen3.5:latest"]),
        ("registry.local:5000/qwen3.5", ["registry.local:5000/qwen3.5:latest"]),
    ],
)
def test_a_name_without_a_tag_is_the_latest_tag(settings_factory, server, model, listed):
    """Ollama 0.40.16 answered a chat question for LLM_MODEL=qwen3.5 as for
    qwen3.5:latest, but lists only the tagged name, so doctor warned "not in
    its list" of a model the agent used without trouble."""
    server.reply = _lists(*listed)
    row = _reachable(_settings(settings_factory, LLM_MODEL=model))
    assert row.ok is True and f"{model} is listed" in row.detail


@pytest.mark.parametrize(
    "model,listed",
    [
        ("qwen3.5", ["qwen3.5:0.8b"]),
        ("qwen3.5:0.8b", ["qwen3.5:latest"]),
        ("qwen3.5:latest", ["qwen3.5:0.8b"]),
        ("qwen3", ["qwen3.5:latest"]),
        ("Qwen3.5", ["qwen3.5:latest"]),
    ],
)
def test_no_other_name_counts_as_listed(settings_factory, server, model, listed):
    server.reply = _lists(*listed)
    row = _reachable(_settings(settings_factory, LLM_MODEL=model))
    assert row.ok is False and f"{model} is not in its list" in row.detail


def _refused(request):
    raise httpx.ConnectError("connection refused", request=request)


def _timed_out(request):
    raise httpx.ConnectTimeout("timed out", request=request)


@pytest.mark.parametrize(
    "reply",
    [
        _refused,
        _timed_out,
        lambda r: httpx.Response(404, text="404 page not found"),
        lambda r: httpx.Response(500, text="Internal Server Error"),
        lambda r: httpx.Response(200, text="<html>router login</html>"),
        lambda r: httpx.Response(200, json={"error": "unauthorised"}),
        lambda r: httpx.Response(
            302, headers={"location": "http://login.example.test/"}
        ),
        # Ollama's answer to GET /v1//models, as received.
        lambda r: httpx.Response(
            307,
            headers={
                "content-type": "text/html; charset=utf-8",
                "location": "/v1/models",
            },
            text='<a href="/v1/models">Temporary Redirect</a>.\n\n',
        ),
    ],
    ids=[
        "refused",
        "timeout",
        "404",
        "500",
        "html",
        "other json",
        "redirect",
        "ollama 307",
    ],
)
def test_a_server_not_reachable_or_not_understood_is_a_warning(
    settings_factory, server, reply
):
    server.reply = reply
    row = _reachable(_settings(settings_factory))
    assert row.ok is False and row.blocking is False
    assert row.detail.startswith("not reachable or not understood at 192.168.1.20")
    assert len(server.requests) == 1


@pytest.mark.parametrize("reply", [_refused, _timed_out], ids=["refused", "timeout"])
def test_a_server_not_reachable_says_what_to_check(settings_factory, server, reply):
    server.reply = reply
    row = _reachable(_settings(settings_factory))
    assert (
        "check that the model server is running and that its host, port and "
        "firewall let this machine reach it" in row.detail
    )


@pytest.mark.parametrize(
    "reply",
    [
        lambda r: httpx.Response(500, text="Internal Server Error"),
        lambda r: httpx.Response(200, text="<html>router login</html>"),
        lambda r: httpx.Response(200, json={"error": "unauthorised"}),
    ],
    ids=["500", "html", "other json"],
)
def test_a_server_not_understood_says_to_check_the_address(
    settings_factory, server, reply
):
    """Something answered, so "is it running" would send IT the wrong way."""
    server.reply = reply
    row = _reachable(_settings(settings_factory))
    assert "check that LLM_BASE_URL is the model server's own address" in row.detail
    assert "firewall" not in row.detail


class _Trickle(BaseHTTPRequestHandler):
    """Answers 200 at once, then one byte of the list every 0.3 s for 6 s."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "20")
        self.end_headers()
        try:
            for _ in range(20):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.3)
        except OSError:
            pass

    def log_message(self, *args):
        pass


def test_a_server_that_trickles_its_list_is_cut_off_at_three_seconds(
    settings_factory,
):
    """The 3 s were per read, so a half-hung server held doctor for 11 s
    while DEPLOY.md said three seconds at most. The check's own thread stops
    reading at the 3 s too, not when the server stops writing."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        settings = _settings(
            settings_factory, LLM_BASE_URL=f"http://127.0.0.1:{httpd.server_port}/v1"
        )
        before = {t for t in threading.enumerate() if t.name == "ai-server-call"}
        started = time.monotonic()
        row = _reachable(settings)
        took = time.monotonic() - started
        for thread in {
            t for t in threading.enumerate() if t.name == "ai-server-call"
        } - before:
            thread.join(1.5)
            assert not thread.is_alive()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert row.ok is False and row.detail.startswith("not reachable or not understood")
    assert took < 4.5


class _TrickledHeaders(BaseHTTPRequestHandler):
    """Sends its status line at once, then one header line every 0.3 s for
    6 s, then the list."""

    def do_GET(self):
        body = json.dumps(MODELS).encode()
        try:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            for n in range(20):
                self.wfile.write(f"X-Wait-{n}: 1\r\n".encode())
                self.wfile.flush()
                time.sleep(0.3)
            self.wfile.write(
                b"Content-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
        except OSError:
            pass

    def log_message(self, *args):
        pass


def test_a_server_that_trickles_its_headers_is_cut_off_at_three_seconds(
    settings_factory,
):
    """The deadline was checked only between body chunks, and each header
    line reset the 3 s per-read timeout: a stand-in sending one header line
    a second held the list check 20 s."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TrickledHeaders)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        settings = _settings(
            settings_factory, LLM_BASE_URL=f"http://127.0.0.1:{httpd.server_port}/v1"
        )
        started = time.monotonic()
        row = _reachable(settings)
        took = time.monotonic() - started
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert row.ok is False and row.detail.startswith("not reachable or not understood")
    assert took < 4.5


def test_without_network_checks_the_server_is_not_asked(settings_factory, server):
    row = _row(
        run_doctor_checks(_settings(settings_factory), network=False),
        "AI server reachable",
    )
    assert row.ok is None and server.requests == []


def test_an_address_set_without_the_ai_is_a_warning(settings_factory, server):
    checks = _doctor(settings_factory(llm_provider="", LLM_BASE_URL=ADDRESS))
    row = _row(checks, "AI server")
    assert row.ok is False and row.blocking is False
    assert "LLM_PROVIDER is blank" in row.detail
    assert _row(checks, "AI server reachable") is None
    assert server.requests == []


def test_no_address_and_no_ai_adds_no_row(settings_factory, server):
    checks = _doctor(settings_factory())
    assert _row(checks, "AI server") is None
    assert _row(checks, "AI server reachable") is None


def test_the_ready_to_send_card_asks_no_server(settings_factory, server):
    readiness_report(_settings(settings_factory))
    assert server.requests == []


# ── The one question doctor asks the model ─────────────────────────────────


def _chat(content):
    """Ollama 0.40.16's answer to a chat question, as it was received."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-628",
            "object": "chat.completion",
            "created": 1794512295,
            "model": MODEL,
            "system_fingerprint": "fp_ollama",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 44},
                "completion_tokens": 2,
                "total_tokens": 52,
            },
        },
    )


def _models_then(chat):
    """The model list for GET, chat(request) for the question."""

    def reply(request):
        if request.method == "GET":
            return httpx.Response(200, json=MODELS)
        return chat(request)

    return reply


def _answers(settings):
    checks = run_doctor_checks(
        settings, network=True, only=(*AI_ROWS, "AI server answers")
    )
    return _row(checks, "AI server answers")


def _questions(server):
    return [r for r in server.requests if r.method == "POST"]


def test_a_model_that_answers_as_asked_is_ok_with_the_seconds(
    settings_factory, server
):
    server.reply = _models_then(lambda r: _chat("YES"))
    row = _answers(_settings(settings_factory))
    assert row.ok is True
    assert row.detail.startswith("answered as asked in ") and row.detail.endswith(" s")


def test_the_question_goes_by_the_agents_own_call(settings_factory, server):
    """Same address, model, key, reasoning setting and timeout as every AI
    use, and nothing in it about anyone."""
    server.reply = _models_then(lambda r: _chat("YES"))
    _answers(
        _settings(
            settings_factory,
            LLM_API_KEY="sk-own",
            LLM_TIMEOUT_SECONDS=45,
            LLM_REASONING_EFFORT="low",
        )
    )
    (question,) = _questions(server)
    assert str(question.url) == f"{ADDRESS}/chat/completions"
    assert question.headers["authorization"] == "Bearer sk-own"
    sent = json.loads(question.content)
    assert (sent["model"], sent["reasoning_effort"]) == (MODEL, "low")
    assert sent["messages"] == [{"role": "user", "content": AI_CHECK_QUESTION}]
    assert server.clients[-1]["timeout"] == 45


def test_a_blank_reasoning_setting_sends_no_field_in_the_question(
    settings_factory, server
):
    server.reply = _models_then(lambda r: _chat("YES"))
    _answers(_settings(settings_factory, LLM_REASONING_EFFORT=""))
    (question,) = _questions(server)
    assert "reasoning_effort" not in json.loads(question.content)


def test_an_answer_not_as_asked_is_a_warning_quoting_40_characters(
    settings_factory, server
):
    said = "Sure! Here is the word you asked me for: YES. Anything else?"
    server.reply = _models_then(lambda r: _chat(said))
    row = _answers(_settings(settings_factory))
    assert row.ok is False and row.blocking is False
    assert f"but not as asked: {said[:40]!r}" in row.detail
    assert said[:41] not in row.detail
    assert "may not follow instructions well enough" in row.detail


def test_reasoning_with_no_answer_is_not_answered_as_asked(settings_factory, server):
    """Ollama's real answer when the reply ends while the model reasons."""

    def cut_short(request):
        answer = _chat("").json()
        answer["choices"][0]["message"]["reasoning"] = "Thinking Process:\n\nYES"
        answer["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=answer)

    server.reply = _models_then(cut_short)
    row = _answers(_settings(settings_factory))
    assert row.ok is False and "but not as asked: ''" in row.detail


def test_no_answer_in_time_names_what_to_try(settings_factory, server):
    def hangs(request):
        raise httpx.ReadTimeout("timed out", request=request)

    server.reply = _models_then(hangs)
    row = _answers(_settings(settings_factory, LLM_TIMEOUT_SECONDS=60))
    assert row.ok is False and row.blocking is False
    assert row.detail.startswith("no answer within 60 s (LLM_TIMEOUT_SECONDS)")
    for remedy in (
        "LLM_REASONING_EFFORT=none",
        "a longer LLM_TIMEOUT_SECONDS",
        "a smaller or faster model",
    ):
        assert remedy in row.detail


def test_a_refusal_shows_the_status_and_the_servers_own_message(
    settings_factory, server
):
    server.reply = _models_then(
        lambda r: httpx.Response(
            404,
            json={
                "error": {
                    "message": f"model '{MODEL}' not found",
                    "type": "not_found_error",
                    "param": None,
                    "code": None,
                }
            },
        )
    )
    row = _answers(_settings(settings_factory))
    assert row.ok is False and row.blocking is False
    assert row.detail == (
        f"refused: HTTP 404 \"model '{MODEL}' not found\" - pull {MODEL} on the "
        "server, or set LLM_MODEL to a name it lists"
    )


def test_the_servers_message_is_cut_to_120_characters(settings_factory, server):
    long = "x" * 119 + "YZ"
    server.reply = _models_then(
        lambda r: httpx.Response(400, json={"error": {"message": long}})
    )
    row = _answers(_settings(settings_factory))
    assert repr(long[:120]) in row.detail and "Z" not in row.detail


def test_a_refusal_that_mentions_reasoning_says_the_setting_can_be_blank(
    settings_factory, server
):
    server.reply = _models_then(
        lambda r: httpx.Response(
            400,
            json={"error": {"message": "unknown field: reasoning_effort"}},
        )
    )
    row = _answers(_settings(settings_factory))
    assert "LLM_REASONING_EFFORT can be left blank" in row.detail


def test_another_refusal_does_not_mention_the_reasoning_setting(
    settings_factory, server
):
    server.reply = _models_then(
        lambda r: httpx.Response(500, json={"error": {"message": "out of memory"}})
    )
    row = _answers(_settings(settings_factory))
    assert row.detail == "refused: HTTP 500 'out of memory'"


def test_not_asked_when_the_list_row_did_not_reach_the_server(
    settings_factory, server
):
    server.reply = _refused
    row = _answers(_settings(settings_factory))
    assert row.ok is None and row.detail.startswith("not asked")
    assert _questions(server) == []


def test_not_asked_without_network_checks(settings_factory, server):
    row = _row(
        run_doctor_checks(_settings(settings_factory), network=False),
        "AI server answers",
    )
    assert row.ok is None and row.detail.startswith("not asked")
    assert server.requests == []


def test_no_question_without_the_ai(settings_factory, server):
    checks = run_doctor_checks(
        settings_factory(llm_provider=""), network=True, only=("AI server answers",)
    )
    assert _row(checks, "AI server answers") is None and server.requests == []
