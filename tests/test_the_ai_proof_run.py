"""main.py ai-proof: the whole agent working with the configured AI, sending nothing.

The benchmark asks the AI's questions one at a time; nothing showed the
company's IT the agent itself working with their model. The proof run sends
a few invented companies through the agent's own qualification and dry-run
send, and a few invented replies through its own judgement of a reply.

These tests drive it with a stand-in AI server on 127.0.0.1 (and, for the
pause, a stand-in transport), so they pass with no model server running: an
answerer that is always right gets every outcome right; a step that reaches
for SMTP or DNS fails the run even when its error is swallowed; the real
database is never opened; and nothing is left behind.
"""

import functools
import io
import json
import re
import smtplib
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

import main
from modules import ai_generator, ai_proof
from modules.ai_benchmark import load_cases
from modules.compliance import scrub_for_ai
from modules.database import Database
from modules.reply_intent import strip_quoted

MODEL = "stand-in:1b"


@functools.cache
def _shipped():
    return load_cases(("relevance", "country", "reply"))


def _right_answer(prompt: str) -> str:
    """What an answerer that is always right says, from the shipped cases: a
    relevance case's own answer and YES for a country case (it says only
    which country); a country case's own country and UNKNOWN for a
    relevance case; a reply case's own answer."""
    cases = _shipped()
    domain = re.search(r"^Domain: (\S+)", prompt, re.MULTILINE)
    if "You qualify B2B leads" in prompt:
        relevance = {c["domain"]: c["expected"] for c in cases["relevance"]}
        return relevance.get(domain.group(1), "YES")
    if "In which country is this company based?" in prompt:
        country = {c["domain"]: c["expected"] for c in cases["country"]}
        return country.get(domain.group(1), "UNKNOWN")
    for case in cases["reply"]:
        own = scrub_for_ai(strip_quoted(case["body"])).strip()[:30]
        if f"Reply subject: {case['subject'] or '(none)'}\n" in prompt and own in prompt:
            return case["expected"]
    raise AssertionError(f"a prompt no case answers: {prompt[:200]}")


class StandInServer:
    """An OpenAI-compatible server on 127.0.0.1: it lists its model and
    answers each chat completion with answer(prompt)."""

    def __init__(self, answer=_right_answer, models=(MODEL,)):
        self.answer = answer
        self.models = list(models)
        self.prompts: list[str] = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, data):
                body = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._json({"object": "list", "data": [{"id": m} for m in stand_in.models]})

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                prompt = request["messages"][0]["content"]
                stand_in.prompts.append(prompt)
                answer = stand_in.answer(prompt)
                self._json({"choices": [{"message": {"content": answer}}]})

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stand_in():
    server = StandInServer()
    yield server
    server.close()


def _settings(settings_factory, url, model=MODEL, **kw):
    """An installation as shipped would be: a sending mailbox (so the
    blocklist check has a domain to look up), the country lock's default
    for addresses that show no country, and the own server."""
    kw.setdefault("smtp_username", "outreach@sender.example")
    kw.setdefault("UNKNOWN_COUNTRY_ALLOWED", False)
    return settings_factory(
        llm_provider="openai_compatible",
        LLM_BASE_URL=url,
        LLM_MODEL=model,
        LLM_TIMEOUT_SECONDS=10,
        **kw,
    )


def _run(settings, *argv, work=None):
    """The command as main.py runs it; its exit status and what it said."""
    if work is not None:
        argv = (*argv, "--work-dir", str(work))
    args = main.build_parser().parse_args(["ai-proof", *argv])
    said = []
    status = ai_proof.run_command(args, settings, say=said.append)
    return status, "\n".join(said)


def _lines(text, heading):
    block = text.split(heading, 1)[1].split("\n\n", 1)[0]
    return [line for line in block.splitlines() if line.startswith("  ")]


# ─── The outcome table ─────────────────────────────────────────────────────


def test_an_always_right_answerer_gets_every_outcome_right(
    stand_in, settings_factory, tmp_path
):
    status, text = _run(
        _settings(settings_factory, stand_in.url), work=tmp_path / "work"
    )
    assert status == 0, text
    companies = _lines(text, "Companies, through the agent's own qualification")
    replies = _lines(text, "Replies, through the agent's own judgement of a reply")
    assert len(companies) == ai_proof.DEFAULT_COMPANIES
    assert len(replies) == ai_proof.DEFAULT_REPLIES
    assert not [line for line in companies + replies if "DISAGREES" in line]
    agree = re.search(r"Agree with the cases' right answers: (\d+) of (\d+)", text)
    assert agree and agree.group(1) == agree.group(2), text
    # Through the agent's own steps, each kind of outcome is there.
    assert "removed by the AI: not a target" in text
    assert "would be emailed in Italian" in text
    assert "would be emailed in English" in text
    assert "a salesperson would be alerted" in text
    # All three of the agent's AI questions reached the server, its own prompts.
    assert any("You qualify B2B leads" in p for p in stand_in.prompts)
    assert any("In which country is this company based?" in p for p in stand_in.prompts)
    assert any("Reply subject:" in p for p in stand_in.prompts)
    assert "nothing but the AI server" in text
    assert "none was attempted" in text


def test_a_wrong_answerer_is_shown_disagreeing(settings_factory, tmp_path):
    server = StandInServer(answer=lambda prompt: "NO")
    try:
        status, text = _run(
            _settings(settings_factory, server.url), "--companies", "3", "--replies", "2",
            work=tmp_path / "work",
        )
    finally:
        server.close()
    assert status == 0
    companies = _lines(text, "Companies, through the agent's own qualification")
    assert "removed by the AI: not a target" in companies[0]
    assert "right answer: keep, DISAGREES" in companies[0]
    # A country case says only which language a kept company's email is in.
    assert "removed by the AI: not a target" in companies[1]
    assert "right answer: IT, in Italian if kept, not judged" in companies[1]
    assert re.search(r"Agree with the cases' right answers: (\d+) of \1\b", text) is None


def test_an_answerer_that_names_italy_for_everyone_is_shown_wrong(
    settings_factory, tmp_path
):
    def italy(prompt):
        return "IT" if "In which country" in prompt else "YES"

    server = StandInServer(answer=italy)
    try:
        status, text = _run(
            _settings(settings_factory, server.url), "--companies", "4", "--replies", "0",
            work=tmp_path / "work",
        )
    finally:
        server.close()
    assert status == 0
    italian, foreign = _lines(text, "Companies, through")[1::2]
    assert "would be emailed in Italian" in italian and ", agrees" in italian
    assert "would be emailed in Italian" in foreign
    assert "in English if kept, DISAGREES" in foreign


def test_a_reply_the_inbox_files_away_is_never_judged(
    stand_in, settings_factory, tmp_path, monkeypatch
):
    """The run passed each reply straight to the agent's judgement, skipping
    the inbox that comes first: an out-of-office reply whose subject says so,
    which the agent never shows the AI, read as 'a salesperson would be
    alerted' and counted against the model."""
    from modules import ai_benchmark

    folder = tmp_path / "cases"
    folder.mkdir()
    for question in ("relevance", "country", "reply"):
        data = json.loads((ai_benchmark.CASES_DIR / f"{question}.json").read_text("utf-8"))
        if question == "reply":
            away = next(c for c in data["cases"] if c["category"] == "auto_reply")
            away["subject"] = "Automatic reply: Supplement production"
        (folder / f"{question}.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(ai_benchmark, "CASES_DIR", folder)
    status, text = _run(
        _settings(settings_factory, stand_in.url), "--companies", "0", "--replies", "5",
        work=tmp_path / "work",
    )
    assert status == 0, text
    (line,) = [r for r in _lines(text, "Replies, through") if away["id"] in r]
    assert line == (
        f"  {away['id']} | filed by the inbox as an auto-reply, the AI is not asked | "
        "no alert | right answer: no alert, agrees"
    )
    assert not [p for p in stand_in.prompts if "Automatic reply" in p]
    assert "1 filed away by the inbox before any judgement" in text
    assert "Agree with the cases' right answers: 5 of 5" in text


def test_the_settings_and_the_options_reach_the_run(stand_in, settings_factory, tmp_path):
    settings = _settings(settings_factory, stand_in.url)
    status, text = _run(settings, "--companies", "2", "--replies", "1", work=tmp_path / "w")
    assert status == 0
    assert len(_lines(text, "Companies, through")) == 2
    assert len(_lines(text, "Replies, through")) == 1
    for _name, _value, line in ai_proof.RUN_ONLY:
        if line:
            assert line in text
    assert "AI_DPA_REFERENCE is blank: the AI is asked anyway" in text
    # Changed for the run only: the settings given are as they were.
    assert settings.ai_relevance_enabled is False
    assert settings.ai_reply_classification_enabled is False
    assert settings.dry_run is False
    assert settings.verify_mx is True


def test_the_cases_are_clear_mixed_and_always_the_same():
    cases = load_cases(("relevance", "country", "reply"))
    first = ai_proof.pick(cases, ai_proof.COMPANY_GROUPS, 8)
    assert first == ai_proof.pick(cases, ai_proof.COMPANY_GROUPS, 8)
    assert [(q, c["category"]) for q, c in first] == list(ai_proof.COMPANY_GROUPS)
    assert {c["difficulty"] for _, c in ai_proof.pick(cases, ai_proof.COMPANY_GROUPS, 50)} == {
        "clear"
    }
    replies = ai_proof.pick(cases, ai_proof.REPLY_GROUPS, 5)
    assert len({c["category"] for _, c in replies}) == 5


def test_no_email_waits_for_the_pace_or_the_hour(
    stand_in, settings_factory, tmp_path, monkeypatch
):
    """Nothing is sent, so neither the pause between emails nor the send
    window may hold the run: at 30 an hour each dry-run email would wait two
    minutes, and on a Sunday night no company would even be qualified."""
    from datetime import datetime

    from modules import clock
    from modules.loop_orchestration import LoopOrchestrator

    sunday_night = datetime(2026, 11, 15, 3, 0, tzinfo=clock.now().tzinfo)
    monkeypatch.setattr(clock, "now", lambda: sunday_night)
    slept = []
    monkeypatch.setattr(
        LoopOrchestrator, "interruptible_sleep", lambda self, seconds: slept.append(seconds)
    )
    status, text = _run(
        _settings(
            settings_factory, stand_in.url,
            delay_between_emails=30.0, emails_per_hour=30, send_jitter_seconds=5.0,
        ),
        "--companies", "2", "--replies", "0",
        work=tmp_path / "work",
    )
    assert status == 0, text
    assert text.count("would be emailed in") == 2
    assert slept and set(slept) == {0.0}


@pytest.mark.parametrize("argv", [["--companies", "51"], ["--replies", "-1"], ["--companies", "x"]])
def test_the_options_refuse_what_they_cannot_do(argv, capsys):
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["ai-proof", *argv])


# ─── The guard ─────────────────────────────────────────────────────────────


def test_a_swallowed_smtp_attempt_still_fails_the_run(
    stand_in, settings_factory, tmp_path, monkeypatch
):
    """A dry-run send that opened an SMTP session, and degraded gracefully
    when refused, would read as a clean run without the guard's record."""
    from modules.email_sender import EmailSender

    dispatch = EmailSender.dispatch_to_contact
    tried = []

    def dispatch_after_smtp(self, *args, **kwargs):
        for host in ("smtp.example.test", "127.0.0.1"):
            try:
                smtplib.SMTP(host, 2525, timeout=2)
            except OSError as e:
                tried.append(str(e))
        return dispatch(self, *args, **kwargs)

    monkeypatch.setattr(EmailSender, "dispatch_to_contact", dispatch_after_smtp)
    status, text = _run(
        _settings(settings_factory, stand_in.url), "--companies", "2", "--replies", "0",
        work=tmp_path / "work",
    )
    assert status == 1
    assert tried and all("allows no connection but the AI server's" in t for t in tried)
    assert "[FAILED] The run reached for the network beyond the AI server" in text
    assert "look up 'smtp.example.test'" in text
    assert "connect ('127.0.0.1', 2525)" in text
    assert "none was attempted" not in text


def test_a_dns_lookup_fails_the_run_loudly(stand_in, settings_factory, tmp_path, monkeypatch):
    from modules.lead_scoring import LeadScorer

    score = LeadScorer.score_contact

    def score_after_mx(self, contact):
        socket.getaddrinfo("mx.example.test", 25)
        return score(self, contact)

    monkeypatch.setattr(LeadScorer, "score_contact", score_after_mx)
    status, text = _run(
        _settings(settings_factory, stand_in.url), "--companies", "2", "--replies", "1",
        work=tmp_path / "work",
    )
    assert status == 1
    assert "look up 'mx.example.test'" in text
    assert not (tmp_path / "work").exists()


def test_the_guard_lets_only_the_ai_server_through():
    guard = ai_proof.NetworkGuard()
    guard.allow("http://127.0.0.1:11434/v1")
    with guard:
        with pytest.raises(ConnectionRefusedError):
            socket.gethostbyname("example.test")
        with socket.socket() as sock, pytest.raises(ConnectionRefusedError):
            sock.connect(("127.0.0.1", 25))
        with (
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
            pytest.raises(ConnectionRefusedError),
        ):
            udp.sendto(b"a DNS question", ("192.0.2.1", 53))
    assert guard.attempts == [
        "look up 'example.test'",
        "connect ('127.0.0.1', 25)",
        "send to ('192.0.2.1', 53)",
    ]
    # Left as it was found.
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_a_server_given_by_name_is_reached_through_the_guard(
    stand_in, settings_factory, tmp_path, monkeypatch
):
    """An AI call connects to the address its name was checked as, by number
    (ai_generator._local_stream). Opening that connection looks the number up,
    which asks no name server; the guard refused it, and every proof run with
    a name in LLM_BASE_URL (localhost, the AI machine's name) failed."""
    real = socket.getaddrinfo

    def office_dns(host, *args, **kwargs):
        return real("127.0.0.1" if host == "ai-server.test" else host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", office_dns)
    url = stand_in.url.replace("127.0.0.1", "ai-server.test")

    status, text = _run(
        _settings(settings_factory, url), "--companies", "2", "--replies", "1",
        work=tmp_path / "work",
    )

    assert status == 0, text
    assert stand_in.prompts, "no question reached the AI server"
    assert "look up" not in text


# ─── The database and the folder ───────────────────────────────────────────


def test_the_real_database_is_never_opened(stand_in, settings_factory, tmp_path):
    real = tmp_path / "real" / "agent.db"
    real.parent.mkdir()
    Database(str(real)).log_activity("TEST", "an existing database")
    before = real.read_bytes()
    missing = tmp_path / "nowhere" / "agent.db"

    for db_path in (real, missing):
        status, text = _run(
            _settings(settings_factory, stand_in.url, db_path=str(db_path)),
            "--companies", "2", "--replies", "1",
            work=tmp_path / f"work-{db_path.parent.name}",
        )
        assert status == 0, text
    assert real.read_bytes() == before
    assert sorted(p.name for p in real.parent.iterdir()) == ["agent.db"]
    assert not missing.parent.exists()


def test_an_existing_work_folder_is_refused_and_left_alone(
    stand_in, settings_factory, tmp_path
):
    work = tmp_path / "work"
    work.mkdir()
    (work / "proof.db").write_bytes(b"someone else's")
    status, text = _run(_settings(settings_factory, stand_in.url), work=work)
    assert status == 1
    assert f"[ERROR] {work} already exists. The proof run works only in a new folder" in text
    assert (work / "proof.db").read_bytes() == b"someone else's"
    assert stand_in.prompts == []


def test_a_work_folder_under_a_missing_folder_is_refused_in_a_sentence(
    stand_in, settings_factory, tmp_path
):
    """--work-dir under a folder that does not exist gave a raw
    FileNotFoundError traceback."""
    work = tmp_path / "no" / "such" / "nested"
    status, text = _run(_settings(settings_factory, stand_in.url), work=work)
    assert status == 1
    assert f"[ERROR] {work} cannot be created" in text
    assert f"{work.parent}, must exist" in text
    assert stand_in.prompts == []
    assert not (tmp_path / "no").exists()


def test_nothing_is_left_behind(stand_in, settings_factory, tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(ai_proof.tempfile, "tempdir", str(temp))
    out = tmp_path / "out"
    status, text = _run(
        _settings(settings_factory, stand_in.url), "--companies", "2", "--replies", "1",
        "--out", str(out),
    )
    assert status == 0, text
    assert list(temp.iterdir()) == []
    assert sorted(p.name for p in out.iterdir()) == ["agent.log", "proof.txt"]
    assert "Companies, through" in (out / "proof.txt").read_text(encoding="utf-8")
    assert "Processing" in (out / "agent.log").read_text(encoding="utf-8")


def test_an_out_that_cannot_be_a_folder_is_refused_before_the_run(
    stand_in, settings_factory, tmp_path
):
    """--out naming an existing file ran the whole run, then crashed with a
    traceback that lost the printed result and left proof.db behind."""
    taken = tmp_path / "afile.txt"
    taken.write_text("someone's file", encoding="utf-8")
    status, text = _run(
        _settings(settings_factory, stand_in.url), "--companies", "1", "--replies", "0",
        "--out", str(taken), work=tmp_path / "work",
    )
    assert status == 1
    assert f"[ERROR] --out {taken} cannot be made a folder" in text
    assert stand_in.prompts == []
    assert not (tmp_path / "work").exists()
    assert taken.read_text(encoding="utf-8") == "someone's file"


def test_a_log_that_cannot_be_copied_still_leaves_nothing_behind(
    stand_in, settings_factory, tmp_path, monkeypatch
):
    def refuse(*args):
        raise PermissionError("the folder refuses writes")

    monkeypatch.setattr(ai_proof.shutil, "copyfile", refuse)
    status, text = _run(
        _settings(settings_factory, stand_in.url), "--companies", "1", "--replies", "0",
        "--out", str(tmp_path / "out"), work=tmp_path / "work",
    )
    assert status == 1
    assert "Companies, through" in text
    assert "[ERROR] Not everything could be written to" in text
    assert "the folder refuses writes" in text
    assert not (tmp_path / "work").exists()


# ─── Stopping cleanly ──────────────────────────────────────────────────────


def test_an_unconfigured_ai_stops_the_run(settings_factory, tmp_path):
    """With LLM_PROVIDER never set the AI is off, and the line says what the
    own server needs."""
    status, text = _run(settings_factory(), work=tmp_path / "work")
    assert status == 1
    assert "[ERROR] The AI is off: LLM_PROVIDER is not set" in text
    assert "LLM_PROVIDER=openai_compatible, LLM_BASE_URL" in text
    assert "Nothing was asked." in text
    assert not (tmp_path / "work").exists()


def test_a_model_the_server_does_not_list_stops_the_run(settings_factory, tmp_path):
    server = StandInServer(models=("another:7b",))
    try:
        status, text = _run(_settings(settings_factory, server.url), work=tmp_path / "w")
    finally:
        server.close()
    assert status == 1
    assert f"[STOPPED] the server does not list {MODEL}" in text
    assert server.prompts == []


def test_an_unreachable_server_stops_the_run(settings_factory, tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    status, text = _run(
        _settings(settings_factory, f"http://127.0.0.1:{port}/v1"), work=tmp_path / "w"
    )
    assert status == 1
    assert "did not answer its list of models" in text
    assert "Nothing was asked." in text


def test_the_agents_pause_stops_the_run(settings_factory, tmp_path, monkeypatch):
    real_client = httpx.Client
    chats = []

    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": MODEL}]})
        chats.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    class Client(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(ai_generator.time, "sleep", lambda s: None)
    status, text = _run(
        _settings(settings_factory, "http://127.0.0.1:11434/v1"), work=tmp_path / "w"
    )
    assert status == 1
    assert len(chats) == ai_generator.AIGenerator.STALL_LIMIT
    assert "[STOPPED] the AI server gave no answer 3 times in a row" in text
    assert "not reached: the run stopped" in text
    assert "not judged: the run stopped" in text
    # A company the send loop never reached passed no rule: its lead score
    # of 0 is the column's default.
    unreached = [line for line in _lines(text, "Companies, through") if "AI: not asked" in line]
    assert unreached and all("| rules: not reached |" in line for line in unreached)
    assert "passed (lead score 0)" not in text
    assert "server silent" in text
    assert not (tmp_path / "w").exists()


def test_a_console_that_cannot_print_a_letter_does_not_crash(
    settings_factory, tmp_path, monkeypatch
):
    model = "modèle-ż:1b"
    server = StandInServer(models=(model,))
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp437", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    try:
        args = main.build_parser().parse_args(
            ["ai-proof", "--companies", "1", "--replies", "0", "--work-dir", str(tmp_path / "w")]
        )
        status = ai_proof.run_command(
            args, _settings(settings_factory, server.url, model=model)
        )
    finally:
        server.close()
    assert status == 0
    console.flush()
    assert b"mod\x8ale-?:1b" in console.buffer.getvalue()


def test_the_command_is_wired_into_main_and_logs_to_no_file(
    settings_factory, monkeypatch
):
    """Every other command logs to logs/<command>.log; the proof run writes
    nothing outside its own folder, so main.py gives it the console only."""
    args = main.build_parser().parse_args(["ai-proof"])
    assert (args.companies, args.replies) == (12, 8)
    assert args.work_dir is None and args.out is None

    seen, ran = {}, []
    monkeypatch.setattr(main, "load_settings", lambda: settings_factory())
    monkeypatch.setattr(main, "setup_logging", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(ai_proof, "run_command", lambda args, settings: ran.append(args) or 0)
    monkeypatch.setattr("sys.argv", ["main.py", "ai-proof", "--replies", "3"])
    main.main()
    assert seen["name"] == "ai-proof" and seen["to_file"] is False
    assert [a.replies for a in ran] == [3]
