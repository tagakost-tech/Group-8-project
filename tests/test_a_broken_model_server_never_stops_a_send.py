"""A model server that is down, slow or wrong never stops a send or says yes.

A model on the company's own server is one more machine that can be off,
still loading, missing the model, or answering with something that is not
the OpenAI shape. Every public question the send loop and the search loop
ask must then end in its own answer for "no AI" - [] , "" or None - and never
in an exception that escapes into the loop, nor in a yes.

Before, a reply whose content was text-shaped but not text (a number, a
list) came back from _complete as it was, and every caller's .strip(),
.split() or regex raised past the retry wrapper. _complete now refuses it,
in one place, where the wrapper already turns a malformed answer into "".

Some servers open a reasoning model's content (Qwen 3, DeepSeek-R1) with a
<think> ... </think> block; the verdict is read from the first word, so one
leading block is dropped, and a block that never closes is no answer.
Ollama 0.40.16 puts that thinking in message.reasoning instead, which is
never read.

A server that hangs cost every call its whole timeout, and a contact makes
up to four calls (relevance, country, opening line, body): measured with a
stand-in that never answers and a 5 s timeout, four contacts held the loop
83 s; 100 contacts at 30 s would hold it, the inbox unread, for over three
hours. After three calls in a row end in a timeout or a connection failure,
the own-server route now asks nothing for ten minutes and every caller gets
its fallback at once (the same four contacts: 15.7 s). Any answer resets
the count; an HTTP error or malformed answer is back at once and does not
count. doctor's question is not paused.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from modules import ai_generator
from modules.ai_generator import AIGenerator

CONTACT = {
    "organization": "Nordic Wellness",
    "qualification_rationale": "Sells own-brand vitamins across Scandinavia.",
    "email": "buyer@nordicwellness.se",
}

# Each public question the loops ask, and its answer when there is no AI.
CALLERS = {
    "generate_companies": (lambda ai: ai.generate_companies("supplement brands"), []),
    "generate_opening_line": (lambda ai: ai.generate_opening_line(CONTACT), ""),
    "generate_email_body": (
        lambda ai: ai.generate_email_body(CONTACT, "Anna", "SUPPLECO"),
        "",
    ),
    "classify_relevance": (lambda ai: ai.classify_relevance(CONTACT), None),
    "classify_company_country": (
        lambda ai: ai.classify_company_country(CONTACT),
        None,
    ),
    "classify_reply_intent": (
        lambda ai: ai.classify_reply_intent("Re: hello", "Tell me more"),
        None,
    ),
    "suggest_discovery_queries": (lambda ai: ai.suggest_discovery_queries(), []),
}


def _refused(request):
    raise httpx.ConnectError("[WinError 10061] connection refused", request=request)


def _timed_out(request):
    raise httpx.ReadTimeout("timed out", request=request)


FAILURES = {
    "connection refused": _refused,
    "timeout": _timed_out,
    # As Ollama 0.40.16 answered on 12 November 2026.
    "404 model not found": lambda r: httpx.Response(
        404,
        json={
            "error": {
                "message": "model 'qwen2.5:14b' not found",
                "type": "not_found_error",
                "param": None,
                "code": None,
            }
        },
    ),
    "307 for a doubled slash": lambda r: httpx.Response(
        307, headers={"location": "/v1/chat/completions"}
    ),
    "500": lambda r: httpx.Response(500, text="Internal Server Error"),
    "200 with an HTML page": lambda r: httpx.Response(
        200, text="<html><body>YES</body></html>", headers={"content-type": "text/html"}
    ),
    "200 with an error object": lambda r: httpx.Response(
        200, json={"error": "YES, the model is loading"}
    ),
    "200 with empty choices": lambda r: httpx.Response(200, json={"choices": []}),
    "200 with null content": lambda r: httpx.Response(
        200, json={"choices": [{"message": {"content": None}}]}
    ),
    "200 with a number for content": lambda r: httpx.Response(
        200, json={"choices": [{"message": {"content": 1}}]}
    ),
    "200 with a list for content": lambda r: httpx.Response(
        200, json={"choices": [{"message": {"content": ["YES"]}}]}
    ),
    "200 with a JSON list": lambda r: httpx.Response(200, json=["YES"]),
}


@pytest.fixture
def server(monkeypatch):
    real_client = httpx.Client
    state = type("Server", (), {})()
    state.reply = None

    # A subclass, not a function, so isinstance checks on httpx.Client hold.
    class Client(real_client):
        def __init__(self, **kwargs):
            super().__init__(
                transport=httpx.MockTransport(lambda r: state.reply(r)), **kwargs
            )

    def no_sleep(seconds):
        raise AssertionError(f"a failed call waited {seconds}s to be retried")

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(ai_generator.time, "sleep", no_sleep)
    return state


def _own():
    return AIGenerator(
        provider="openai_compatible",
        base_url="http://127.0.0.1:11434/v1",
        llm_model="qwen2.5:14b",
        personal_data_allowed=True,
    )


@pytest.mark.parametrize("failure", list(FAILURES))
@pytest.mark.parametrize("caller", list(CALLERS))
def test_every_caller_falls_back_on_every_failure(server, caller, failure):
    ask, fallback = CALLERS[caller]
    server.reply = FAILURES[failure]
    assert ask(_own()) == fallback


def _content(text):
    """Ollama 0.40.16's answer envelope, as received, around text."""
    return lambda r: httpx.Response(
        200,
        json={
            "id": "chatcmpl-628",
            "object": "chat.completion",
            "created": 1794512295,
            "model": "qwen2.5:14b",
            "system_fingerprint": "fp_ollama",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 2, "total_tokens": 52},
        },
    )


@pytest.mark.parametrize(
    "content,verdict",
    [
        ("<think>\nIt sells vitamins.\n</think>\n\nYES", True),
        ("<think>Is it a news site? It is.</think> NO", False),
        ("  <think>a</think>YES", True),
        ("YES", True),
    ],
)
def test_one_leading_think_block_is_dropped_before_the_verdict(
    server, content, verdict
):
    server.reply = _content(content)
    assert _own().classify_relevance(CONTACT) is verdict


@pytest.mark.parametrize(
    "content",
    ["<think>YES, it sells vitamins, but I am still", "<think>"],
)
def test_a_think_block_that_never_closes_is_no_answer(server, content):
    server.reply = _content(content)
    ai = _own()
    assert ai.classify_relevance(CONTACT) is None
    assert ai.generate_opening_line(CONTACT) == ""
    # Company discovery has no copy check after it: the reasoning would have
    # become a company name to search for.
    assert ai.generate_companies("supplement brands") == []


def test_only_one_think_block_is_dropped(server):
    server.reply = _content("<think>a</think><think>YES</think> NO")
    assert ai_generator._without_think_block("<think>a</think><think>b</think> c") == (
        "<think>b</think> c"
    )
    assert _own().classify_relevance(CONTACT) is None


@pytest.mark.parametrize(
    "content",
    [
        "<think>a</think><think>b</think>Your vitamin range caught our eye.",
        "Your vitamin range <think>is it?</think> caught our eye.",
        "Your vitamin range caught our eye. </THINK>",
    ],
)
def test_think_markup_left_in_the_text_is_never_sent(server, content):
    """Only one leading block is dropped; a second one, or one after text,
    passed every check and would have been written into the email."""
    server.reply = _content(content)
    ai = _own()
    assert ai.generate_opening_line(CONTACT) == ""
    assert ai.generate_email_body(CONTACT, "Anna", "SUPPLECO") == ""


def test_the_verdict_and_the_opening_line_come_after_the_think_block(server):
    """Before the strip, a reasoning model's '<think>' text was the opening
    line a recipient got, and nothing pinned that it no longer is."""
    server.reply = _content("<think>The answer is YES.</think>NO")
    assert _own().classify_relevance(CONTACT) is False
    server.reply = _content("<think>plan</think> Your vitamin range caught our eye.")
    assert _own().generate_opening_line(CONTACT) == (
        "Your vitamin range caught our eye."
    )


@pytest.mark.parametrize("caller", list(CALLERS))
def test_each_caller_falls_back_on_content_that_is_not_text(server, caller):
    ask, fallback = CALLERS[caller]
    server.reply = _content(5)
    assert ask(_own()) == fallback


class _Trickle(BaseHTTPRequestHandler):
    """Answers 200 at once, then one byte of body every 0.3 s for 6 s."""

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
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


def _call_threads():
    return {t for t in threading.enumerate() if t.name == "ai-server-call"}


def test_a_server_that_trickles_its_answer_is_cut_off_at_the_timeout():
    """httpx's timeout is per read, and every byte resets it: the 1 s timeout
    held this call for the whole 6 s the server kept writing. The caller now
    stops waiting on time whatever happens; the call's own thread must stop
    reading too, or each question to such a server leaves a thread and a
    connection open for as long as the server keeps writing."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ai = AIGenerator(
            provider="openai_compatible",
            base_url=f"http://127.0.0.1:{httpd.server_port}/v1",
            llm_model="qwen2.5:14b",
            personal_data_allowed=True,
            timeout=1.0,
        )
        before = _call_threads()
        started = time.monotonic()
        assert ai.classify_relevance(CONTACT) is None
        assert time.monotonic() - started < 3
        for thread in _call_threads() - before:
            thread.join(1.5)
            assert not thread.is_alive()
    finally:
        httpd.shutdown()
        httpd.server_close()


class _TrickledHeaders(BaseHTTPRequestHandler):
    """Sends its status line at once, then one header line every 0.3 s for
    6 s, then a valid answer."""

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = b'{"choices": [{"message": {"content": "YES"}}]}'
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


def test_a_server_that_trickles_its_headers_is_cut_off_at_the_timeout():
    """The deadline was checked only between body chunks, and each header
    line reset httpx's per-read timeout: with a 5 s timeout, a stand-in
    sending one header line a second held the question 20 s."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _TrickledHeaders)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        ai = AIGenerator(
            provider="openai_compatible",
            base_url=f"http://127.0.0.1:{httpd.server_port}/v1",
            llm_model="qwen2.5:14b",
            personal_data_allowed=True,
            timeout=1.0,
        )
        started = time.monotonic()
        with pytest.raises(httpx.TimeoutException):
            ai.ask_once("Reply with the single word YES and nothing else.")
        assert time.monotonic() - started < 3
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── A server that hangs does not hold the pass ─────────────────────────────


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _counting(server, *replies):
    """Answer with replies in turn, the last one for ever; count the calls."""
    calls = []

    def reply(request):
        calls.append(request)
        return replies[min(len(calls), len(replies)) - 1](request)

    server.reply = reply
    return calls


def _own_with_clock():
    ai = _own()
    ai.clock = _Clock()
    return ai


def _ok(request):
    return _content("YES")(request)


_not_found = FAILURES["404 model not found"]


def test_the_pause_is_three_stalls_then_ten_minutes():
    assert (AIGenerator.STALL_LIMIT, AIGenerator.STALL_PAUSE_SECONDS) == (3, 600.0)


@pytest.mark.parametrize("stall", [_timed_out, _refused], ids=["timeout", "refused"])
@pytest.mark.parametrize("caller", list(CALLERS))
def test_after_three_stalls_every_caller_falls_back_without_asking(
    server, caller, stall
):
    calls = _counting(server, stall)
    ai = _own_with_clock()
    for _ in range(3):
        ai.classify_relevance(CONTACT)
    ask, fallback = CALLERS[caller]
    assert ask(ai) == fallback
    assert len(calls) == 3


def test_the_pause_starts_with_one_warning(server, caplog):
    _counting(server, _timed_out)
    ai = _own_with_clock()
    with caplog.at_level("INFO", logger="modules.ai_generator"):
        for _ in range(10):
            ai.classify_relevance(CONTACT)
    warned = [
        r for r in caplog.records if "no answer 3 times in a row" in r.getMessage()
    ]
    assert len(warned) == 1 and warned[0].levelname == "WARNING"
    assert "127.0.0.1:11434" in warned[0].getMessage()
    assert "next 10 minutes" in warned[0].getMessage()


@pytest.mark.parametrize("stall", [_timed_out, _refused], ids=["timeout", "refused"])
def test_a_silent_server_is_one_line_without_a_traceback(server, caplog, stall):
    """A timeout was logged with logger.exception, so a slow model printed
    three full tracebacks on the benchmark's console, which read as a crash
    although the timeout was handled as designed."""
    _counting(server, stall)
    with caplog.at_level("INFO", logger="modules.ai_generator"):
        assert _own_with_clock().classify_relevance(CONTACT) is None
    (record,) = [r for r in caplog.records if "AI relevance classification" in r.getMessage()]
    assert record.levelname == "WARNING"
    assert record.exc_info is None
    assert "got no answer from the AI server" in record.getMessage()


def test_an_unexpected_failure_keeps_its_traceback(server, caplog):
    server.reply = lambda r: httpx.Response(500, text="Internal Server Error")
    with caplog.at_level("INFO", logger="modules.ai_generator"):
        _own().classify_relevance(CONTACT)
    failed = [r for r in caplog.records if r.getMessage() == "AI relevance classification failed"]
    assert failed and failed[0].exc_info is not None


def test_deploy_quotes_the_pause_lines_as_the_log_prints_them(server, caplog):
    """DEPLOY.md quoted the two pause lines with the server's IP address,
    but the log masks every IP address as [ip], so a search of the log for
    the quoted line found nothing."""
    from pathlib import Path

    from modules.compliance import mask_personal

    _counting(server, _timed_out, _timed_out, _timed_out, _ok)
    ai = AIGenerator(
        provider="openai_compatible",
        base_url="http://192.168.1.20:11434/v1",
        llm_model="qwen2.5:14b",
        personal_data_allowed=True,
    )
    ai.clock = _Clock()
    with caplog.at_level("INFO", logger="modules.ai_generator"):
        for _ in range(3):
            ai.classify_relevance(CONTACT)
        ai.clock.now += 600
        ai.classify_relevance(CONTACT)
    logged = [
        mask_personal(r.getMessage())
        for r in caplog.records
        if "pause" in r.getMessage() or "in a row" in r.getMessage()
    ]
    deploy = (Path(__file__).resolve().parent.parent / "docs" / "DEPLOY.md").read_text("utf-8")
    assert len(logged) == 2
    for line in logged:
        assert "[ip]:11434" in line
        assert f"\n{line}\n" in deploy, line


def test_after_ten_minutes_one_line_and_the_server_is_asked_again(server, caplog):
    calls = _counting(server, _timed_out, _timed_out, _timed_out, _ok)
    ai = _own_with_clock()
    for _ in range(3):
        ai.classify_relevance(CONTACT)
    ai.clock.now += 599.9
    assert ai.classify_relevance(CONTACT) is None and len(calls) == 3
    ai.clock.now += 0.1
    with caplog.at_level("INFO", logger="modules.ai_generator"):
        assert ai.classify_relevance(CONTACT) is True
        ai.classify_relevance(CONTACT)
    assert len(calls) == 5
    over = [r for r in caplog.records if "pause" in r.getMessage()]
    assert len(over) == 1 and "is over" in over[0].getMessage()


def test_a_stall_after_the_pause_pauses_again_at_once(server):
    calls = _counting(server, _timed_out)
    ai = _own_with_clock()
    for _ in range(3):
        ai.classify_relevance(CONTACT)
    ai.clock.now += 600
    ai.classify_relevance(CONTACT)
    ai.classify_relevance(CONTACT)
    assert len(calls) == 4


def test_the_warning_after_a_failed_trial_counts_on(server, caplog):
    """DEPLOY.md said the pause starts again "with the same warning", which
    it quoted with 3; the log said 4, so a search for the quoted line missed
    every later pause. The count is the true one, and DEPLOY says it rises."""
    _counting(server, _timed_out)
    ai = _own_with_clock()
    with caplog.at_level("WARNING", logger="modules.ai_generator"):
        for _ in range(3):
            ai.classify_relevance(CONTACT)
        ai.clock.now += 600
        ai.classify_relevance(CONTACT)
    warned = [r.getMessage() for r in caplog.records if "in a row" in r.getMessage()]
    assert [m.split(" gave no answer ")[1][:17] for m in warned] == [
        "3 times in a row:",
        "4 times in a row:",
    ]


def test_an_answer_after_the_pause_needs_three_stalls_again(server):
    calls = _counting(
        server, _timed_out, _timed_out, _timed_out, _ok, _timed_out, _timed_out
    )
    ai = _own_with_clock()
    for _ in range(3):
        ai.classify_relevance(CONTACT)
    ai.clock.now += 600
    for _ in range(3):
        ai.classify_relevance(CONTACT)
    assert len(calls) == 6


def test_an_answer_resets_the_count(server):
    calls = _counting(server, _timed_out, _timed_out, _ok, _timed_out, _timed_out)
    ai = _own_with_clock()
    for _ in range(6):
        ai.classify_relevance(CONTACT)
    assert len(calls) == 6


@pytest.mark.parametrize(
    "quick",
    [
        _not_found,
        lambda r: httpx.Response(500, text="Internal Server Error"),
        lambda r: httpx.Response(200, json={"choices": []}),
        lambda r: httpx.Response(200, text="<html>router</html>"),
    ],
    ids=["404", "500", "empty choices", "html"],
)
def test_an_error_answer_neither_counts_nor_keeps_the_count(server, quick):
    """It comes back at once and costs no time."""
    calls = _counting(server, quick)
    ai = _own_with_clock()
    for _ in range(5):
        ai.classify_relevance(CONTACT)
    assert len(calls) == 5
    calls = _counting(server, _timed_out, _timed_out, quick, _timed_out, _timed_out)
    ai = _own_with_clock()
    for _ in range(6):
        ai.classify_relevance(CONTACT)
    assert len(calls) == 6


def test_doctors_question_is_not_paused_and_does_not_feed_the_pause(server):
    calls = _counting(server, _timed_out)
    ai = _own_with_clock()
    for _ in range(3):
        with pytest.raises(httpx.TimeoutException):
            ai.ask_once("Reply with the single word YES and nothing else.")
    ai.classify_relevance(CONTACT)
    assert len(calls) == 4
    for _ in range(2):
        ai.classify_relevance(CONTACT)
    with pytest.raises(httpx.TimeoutException):
        ai.ask_once("Reply with the single word YES and nothing else.")
    assert len(calls) == 7


def test_the_opening_line_after_a_think_block_is_the_answer(server):
    server.reply = _content(
        "<think>They sell vitamins in Scandinavia.</think>\n"
        "Your own-brand vitamin range across Scandinavia caught our eye."
    )
    assert _own().generate_opening_line(CONTACT) == (
        "Your own-brand vitamin range across Scandinavia caught our eye."
    )
