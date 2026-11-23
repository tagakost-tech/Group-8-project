"""An AI call connects to the address that was checked, not to the name again.

Before every AI call the host in LLM_BASE_URL is resolved and every address
it gives must be on this machine or the private network. The call itself then
handed httpx the name, which looked it up a second time: a DNS answer that
changed between the check and the connection sent the prompt, with a
company's details or a prospect's reply, wherever that second answer
pointed. Now the request goes to an address the check found, as page fetches
do (modules/search.py): the Host header and the TLS server name stay the
configured name. The next checked address is tried only when nothing could
be connected to, so "localhost" still reaches a server on 127.0.0.1 when ::1
is tried first; once anything may have been sent, no other address is tried.
"""

import http.server
import ipaddress
import json
import socket
import threading

import httpx
import pytest

import config
from modules import ai_generator
from modules.ai_generator import AIGenerator, own_server_models

MODEL = "qwen2.5:14b"
CONTACT = {
    "organization": "Vitalis Nordica",
    "email": "info@vitalis-nordica.example",
    "qualification_rationale": "Vitamin brand",
}


@pytest.fixture
def dns(monkeypatch):
    """What the check's resolver answers for ai.internal, changed by a test."""
    answer = {"ai.internal": ["10.0.0.7"]}
    asked = []

    def resolve(host):
        asked.append(host)
        return [ipaddress.ip_address(a) for a in answer[host]]

    monkeypatch.setattr(config, "resolve_host", resolve)
    answer["asked"] = asked
    return answer


@pytest.fixture
def server(monkeypatch):
    """A stand-in AI server behind httpx.MockTransport: what it was asked,
    and a handler a test can replace."""
    real_client = httpx.Client
    state = type("Server", (), {})()
    state.requests = []

    def answer(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": MODEL}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "YES"}}]})

    state.answer = answer

    def handler(request):
        state.requests.append(request)
        return state.answer(request)

    class Client(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(ai_generator.time, "sleep", lambda _s: None)
    return state


def _own(address="http://ai.internal:11434/v1"):
    return AIGenerator(
        provider="openai_compatible",
        base_url=address,
        llm_model=MODEL,
        personal_data_allowed=True,
    )


def test_the_prompt_goes_to_the_checked_address_for_the_configured_name(dns, server):
    assert _own().classify_relevance(CONTACT) is True

    (request,) = server.requests
    assert request.url.host == "10.0.0.7"
    assert request.url.port == 11434 and request.url.path == "/v1/chat/completions"
    assert request.headers["Host"] == "ai.internal:11434"
    assert request.extensions["sni_hostname"] == "ai.internal"
    assert dns["asked"] == ["ai.internal"], "the name was looked up once, checked"


def test_https_keeps_the_name_for_the_certificate(dns, server):
    _own("https://ai.internal/v1").classify_relevance(CONTACT)

    (request,) = server.requests
    assert str(request.url) == "https://10.0.0.7/v1/chat/completions"
    assert request.headers["Host"] == "ai.internal"
    assert request.extensions["sni_hostname"] == "ai.internal"


def test_doctors_list_of_models_goes_to_the_checked_address(dns, server):
    assert own_server_models("http://ai.internal:11434/v1", None) == [MODEL]

    (request,) = server.requests
    assert request.url.host == "10.0.0.7"
    assert request.headers["Host"] == "ai.internal:11434"


def test_an_ip_address_is_asked_as_it_is(server):
    _own("http://192.168.1.20:11434/v1").classify_relevance(CONTACT)

    (request,) = server.requests
    assert str(request.url) == "http://192.168.1.20:11434/v1/chat/completions"
    assert request.headers["Host"] == "192.168.1.20:11434"


def test_the_next_checked_address_is_tried_only_when_nothing_was_connected(dns, server):
    """localhost resolves to ::1 and 127.0.0.1, and Ollama listens on
    127.0.0.1 only."""
    dns["ai.internal"] = ["10.0.0.8", "10.0.0.7"]
    plain = server.answer

    def first_refuses(request):
        if request.url.host == "10.0.0.8":
            raise httpx.ConnectError("connection refused", request=request)
        return plain(request)

    server.answer = first_refuses

    assert _own().classify_relevance(CONTACT) is True
    assert [r.url.host for r in server.requests] == ["10.0.0.8", "10.0.0.7"]
    assert all(r.headers["Host"] == "ai.internal:11434" for r in server.requests)


def test_no_other_address_is_tried_once_the_prompt_may_have_gone(dns, server):
    dns["ai.internal"] = ["10.0.0.8", "10.0.0.7"]

    def first_fails(request):
        raise httpx.ReadError("connection reset", request=request)

    server.answer = first_fails

    assert _own().classify_relevance(CONTACT) is None
    assert {r.url.host for r in server.requests} == {"10.0.0.8"}


def test_when_no_checked_address_answers_the_call_fails(dns, server):
    dns["ai.internal"] = ["10.0.0.8", "10.0.0.7"]

    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    server.answer = refused

    with pytest.raises(httpx.ConnectError):
        own_server_models("http://ai.internal:11434/v1", None)
    assert [r.url.host for r in server.requests] == ["10.0.0.8", "10.0.0.7"]


# ── Through real sockets: a second lookup would have gone elsewhere ──


class _Answers(http.server.BaseHTTPRequestHandler):
    hosts: list

    def _reply(self, body: dict):
        self.hosts.append(self.headers["Host"])
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply({"data": [{"id": MODEL}]})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._reply({"choices": [{"message": {"content": "YES"}}]})

    def log_message(self, *_args):
        pass


@pytest.fixture
def own_server():
    """A real AI server stand-in on 127.0.0.1, and the Host headers it saw."""
    handler = type("Handler", (_Answers,), {"hosts": []})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1], handler.hosts
    httpd.shutdown()
    httpd.server_close()


def test_a_dns_answer_that_changed_after_the_check_is_never_asked(
    own_server, monkeypatch
):
    """The check finds 127.0.0.1. Asked again, the system resolver would give
    another answer: here it fails, where an attacker's would point outside.
    The prompt reaches the checked server all the same, and the resolver is
    never asked."""
    port, hosts = own_server
    monkeypatch.setattr(
        config, "resolve_host", lambda host: [ipaddress.ip_address("127.0.0.1")]
    )
    looked_up = []
    getaddrinfo = socket.getaddrinfo

    def changed(host, *args, **kwargs):
        if host == "ai.internal":
            looked_up.append(host)
            raise socket.gaierror(socket.EAI_NONAME, "the answer changed")
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", changed)
    address = f"http://ai.internal:{port}/v1"

    assert _own(address).classify_relevance(CONTACT) is True
    assert own_server_models(address, None) == [MODEL]
    assert looked_up == []
    assert hosts == [f"ai.internal:{port}", f"ai.internal:{port}"]
