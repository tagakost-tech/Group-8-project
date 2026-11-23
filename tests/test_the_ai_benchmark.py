"""main.py ai-benchmark: a fixed exam that says whether a model is good enough.

Nothing told the person choosing a model whether it answers the agent's
questions well enough to switch one on; the only evidence was an ad-hoc probe
of 17 cases. The benchmark asks the product's own questions about cases with
known right answers, several times, and scores them by what a wrong answer
costs here: a client removed, a language chosen wrong, a lead missed.

These tests drive it with stand-in answerers and a stand-in server only, so
they pass with no model server running: an oracle passes, an answerer that
always says YES fails, a random one is inconsistent, a silent server stops
the run and is never scored as wrong, the company's own cases need an
agreement on record, and the case files are refused when they are wrong.
"""

import io
import json
import random
import re
import sys
from fnmatch import fnmatch
from pathlib import Path

import httpx
import pytest

import main
from modules import ai_benchmark, ai_generator
from modules.ai_benchmark import (
    CaseFileError,
    load_case_file,
    load_cases,
    quick_subset,
    run_exam,
    score_run,
    share,
)
from modules.ai_generator import AIGenerator

ROOT = Path(__file__).resolve().parent.parent

# Enough clear cases in every group for a verdict (MIN_CLEAR_CASES is 10).
FULL_SET = {
    "relevance": {
        "brand": 6,
        "retailer": 4,
        "competitor": 2,
        "off_target": 5,
        "non_european": 5,
    },
    "country": {"italian": 4, "foreign": 4, "no_evidence": 10},
    "reply": {
        "interested": 10,
        "declined": 4,
        "removal": 2,
        "auto_reply": 2,
        "neutral": 2,
    },
    "queries": {"no_hint": 1, "hint": 9},
}


def _case(question, category, n, difficulty="clear"):
    cid = f"{question[:3]}-{category.replace('_', '')}-{n:02d}"
    case = {
        "id": cid,
        "language": "en",
        "category": category,
        "difficulty": difficulty,
        "why": f"Invented case {cid}.",
    }
    if question in ("relevance", "country"):
        case.update(
            organization=f"Invented Company {cid}",
            domain=f"{cid}.example",
            page_text=f"Invented page of {cid}.",
        )
    if question == "reply":
        case.update(subject=f"Re: {cid}", body=f"Invented reply {cid}.")
    if question == "queries":
        case["focus_hint"] = "" if category == "no_hint" else f"hint {cid}"
        case["expected"] = 4
        return case
    must = ai_benchmark.CATEGORIES[question][category]
    case["expected"] = must or "DE"
    return case


def _write(folder, question, cases, description="Invented cases for a test."):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{question}.json"
    path.write_text(
        json.dumps({"question": question, "description": description, "cases": cases}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def full_set(tmp_path, monkeypatch):
    """A shipped case folder with enough clear cases for a verdict, and one
    hard case per question."""
    folder = tmp_path / "shipped"
    for question, categories in FULL_SET.items():
        cases = [
            _case(question, category, n)
            for category, count in categories.items()
            for n in range(count)
        ]
        first = next(iter(categories))
        cases.append(_case(question, first, 99, "hard"))
        _write(folder, question, cases)
    monkeypatch.setattr(ai_benchmark, "CASES_DIR", folder)
    return load_cases()


class StandIn:
    """Answers the four questions as the product's generator would return
    them, from answer(question, key), where key identifies the case."""

    provider = "openai_compatible"
    MODEL_NAME = "stand-in"
    base_url = "http://127.0.0.1:11434/v1"
    reasoning_effort = ""
    api_key = "test"
    is_configured = True
    TIMEOUT_SECONDS = 30.0
    STALL_LIMIT = 3
    STALL_PAUSE_SECONDS = 600.0
    stalls_in_a_row = 0
    paused = False

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def classify_relevance(self, contact):
        self.asked.append(contact)
        return self.answer("relevance", contact["organization"])

    def classify_company_country(self, contact):
        self.asked.append(contact)
        return self.answer("country", contact["organization"])

    def classify_reply_intent(self, subject, body):
        self.asked.append(subject)
        return self.answer("reply", subject)

    def suggest_discovery_queries(self, count=5, focus_hint=""):
        self.asked.append(focus_hint)
        return self.answer("queries", focus_hint)


def _oracle(cases):
    """Always right, by the shipped expected answer."""
    expected = {}
    for question, question_cases in cases.items():
        for c in question_cases:
            key = {
                "relevance": "organization",
                "country": "organization",
                "reply": "subject",
                "queries": "focus_hint",
            }[question]
            expected[(question, c[key])] = c["expected"]

    def answer(question, key):
        want = expected[(question, key)]
        if question == "country":
            return "" if want == "UNKNOWN" else want
        if question == "queries":
            return [f"query {i}" for i in range(want)]
        return want == "YES"

    return answer


def _always_yes(question, key):
    if question == "country":
        return "IT"
    if question == "queries":
        return [f"query {i}" for i in range(5)]
    return True


def _verdicts(scores):
    return {q: s.verdict for q, s in scores.items()}


def _status(scores, question, key):
    return next(m for m in scores[question].measures if m.key == key).status


def test_an_oracle_scores_full_marks_and_passes(full_set, settings_factory):
    run = run_exam(StandIn(_oracle(full_set)), full_set, 3, say=lambda _: None)
    scores = score_run(run, full_set, settings_factory())
    assert _verdicts(scores) == {
        **dict.fromkeys(full_set, "PASS"),
        "queries": ai_benchmark.FORMAT_ONLY,
    }
    for s in scores.values():
        for m in s.measures:
            if m.mark is not None:
                assert m.status == "ok", (s.question, m.key)
        assert s.worst == []


def test_an_answerer_that_always_says_yes_fails_where_it_costs(
    full_set, settings_factory
):
    run = run_exam(StandIn(_always_yes), full_set, 3, say=lambda _: None)
    scores = score_run(run, full_set, settings_factory())
    assert scores["relevance"].verdict == "FAIL"
    assert _status(scores, "relevance", "caught") == "FAILS"
    assert _status(scores, "relevance", "wrongly_removed") == "ok"
    assert scores["reply"].verdict == "FAIL"
    assert _status(scores, "reply", "false_alerts") == "FAILS"
    assert _status(scores, "reply", "interest_missed") == "ok"
    caught = next(m for m in scores["relevance"].measures if m.key == "caught")
    assert (caught.hits, caught.total) == (0, 30)


def test_a_random_answerer_fails_consistency(full_set, settings_factory):
    rng = random.Random(7)

    def coin(question, key):
        if question == "queries":
            return [f"query {i}" for i in range(5)]
        if question == "country":
            return rng.choice(["IT", "DE", ""])
        return rng.choice([True, False])

    run = run_exam(StandIn(coin), full_set, 5, say=lambda _: None)
    scores = score_run(run, full_set, settings_factory())
    for question in ("relevance", "country", "reply"):
        assert _status(scores, question, "consistency") == "FAILS"
        assert scores[question].verdict == "FAIL"


def test_every_case_answered_two_ways_is_named(full_set, settings_factory):
    """The worst-case list stops at five; the report names every case whose
    answer changed between askings, clear or hard, which is where the prompt
    leaves a model unsure."""
    asked = []

    def flip(question, key):
        asked.append(key)
        return len(asked) % 2 == 0

    cases = {"relevance": full_set["relevance"]}
    run = run_exam(StandIn(flip), cases, 2, say=lambda _: None)
    scores = score_run(run, cases, settings_factory())
    ids = [c["id"] for c in full_set["relevance"]]
    assert len(ids) % 2 == 1, "an odd count makes every case flip"
    assert scores["relevance"].unstable == ids
    text = ai_benchmark.report([run], [scores], cases, 2, None)
    assert f"- Answered differently across askings ({len(ids)}): {', '.join(ids)}" in text


def test_a_measure_gives_its_plausible_range_over_the_cases():
    """3 wrong of 75 askings of 25 cases is as uncertain as 1 of 25: the
    askings of one case are not independent."""
    measure = ai_benchmark.Measure("x", "X", 3, 75, 25)
    assert measure.result == "3 of 75, 4%; plausible 1% to 20%"
    assert ai_benchmark.plausible_range(3, 75, 75) == (1, 11)
    assert ai_benchmark.plausible_range(0, 60, 60) == (0, 6)
    assert ai_benchmark.plausible_range(0, 0, 0) is None


def test_a_marked_measure_says_how_few_wrong_cases_fail_it():
    """With about 23 clear clients one more wrong case flipped a verdict, and
    the report did not say so. Each marked measure now says how many clear
    cases stand behind it and the fewest wrong ones that fail its mark."""
    at_most = ai_benchmark.Mark(0.05, True, "r")
    measure = ai_benchmark.Measure("x", "X", 0, 156, 52, at_most)
    assert measure.strength == (
        "52 clear cases; 3 wrong cases fail it (8 wrong of its 156 askings)"
    )
    tenth = ai_benchmark.Mark(0.10, True, "r")
    assert ai_benchmark.Measure("x", "X", 0, 20, 20, tenth).wrong_to_fail(20) == 3
    at_least = ai_benchmark.Mark(0.60, False, "r")
    assert ai_benchmark.Measure("y", "Y", 57, 57, 57, at_least).strength == (
        "57 clear cases; 23 wrong cases fail it"
    )
    assert ai_benchmark.Measure("z", "Z", 10, 10, 10, at_most).strength == (
        "10 clear cases; 1 wrong case fails it"
    )
    anything = ai_benchmark.Mark(1.0, True, "r")
    assert ai_benchmark.Measure("w", "W", 0, 5, 5, anything).strength == (
        "5 clear cases; no number of wrong cases fails it"
    )
    assert ai_benchmark.Measure("v", "V", 0, 5, 5).strength == "-"
    assert ai_benchmark.Measure("u", "U", 0, 0, 0, at_most).strength == "-"


def test_the_report_shows_the_cases_behind_each_mark(full_set, settings_factory):
    run = run_exam(StandIn(_oracle(full_set)), full_set, 3, say=lambda _: None)
    scores = score_run(run, full_set, settings_factory())
    text = ai_benchmark.report([run], [scores], full_set, 3, None)
    assert "| Status | Cases behind it | Why this mark |" in text
    assert (
        "| at most 5% | ok | 10 clear cases; 1 wrong case fails it "
        "(2 wrong of its 30 askings) | a real client" in text
    )
    assert "| Exact country right | 54 of 54, 100%; plausible 82% to 100% | - | - | - |" in text


def test_the_report_gives_the_share_right_by_language(full_set, settings_factory):
    """One share over all languages hides a model that fails the Polish or
    Greek companies; a line per question splits the clear askings by language."""
    cases = {"reply": [dict(c) for c in full_set["reply"]]}
    for c in cases["reply"][:4]:
        c["language"] = "pl"

    def wrong_in_polish(question, key):
        case = next(c for c in cases["reply"] if c["subject"] == key)
        right_answer = case["expected"] == "YES"
        return right_answer if case["language"] != "pl" else not right_answer

    run = run_exam(StandIn(wrong_in_polish), cases, 2, say=lambda _: None)
    scores = score_run(run, cases, settings_factory())
    assert scores["reply"].by_language == [("en", 32, 32), ("pl", 0, 8)]
    text = ai_benchmark.report([run], [scores], cases, 2, None)
    assert (
        "- Right by language (clear cases, each asking): en 32 of 32, 100%; pl 0 of 8, 0%"
        in text
    )


def test_the_report_names_the_temperatures_and_the_cases_asked(
    full_set, monkeypatch, settings_factory
):
    def build(settings, **kw):
        return StandIn(_oracle(full_set))

    fingerprint = ai_benchmark.case_fingerprint(full_set)
    status, text = _run(["--repeat", "1"], settings_factory(), monkeypatch, build)
    assert status == 0
    assert "- Temperature: 0 for relevance, country and reply; 0.7 for discovery" in text
    assert f"- Cases fingerprint: {fingerprint} " in text
    _, quick = _run(["--repeat", "1", "--quick"], settings_factory(), monkeypatch, build)
    assert "- Cases fingerprint: " in quick
    assert fingerprint not in quick


def test_no_answer_keeps_a_company_and_raises_no_alert(full_set, settings_factory):
    """As in the agent: None removes nobody and alerts nobody."""
    case = full_set["relevance"][0]
    assert ai_benchmark.right("relevance", case, None) is True
    interested = next(c for c in full_set["reply"] if c["expected"] == "YES")
    assert ai_benchmark.right("reply", interested, None) is False


def test_no_answer_to_an_interested_reply_is_interest_missed(
    full_set, settings_factory
):
    """No answer raises no alert in the agent, so the lead is missed."""
    run = run_exam(
        StandIn(lambda q, k: None if q == "reply" else _always_yes(q, k)),
        {"relevance": full_set["relevance"], "reply": full_set["reply"]},
        1,
        say=lambda _: None,
    )
    scores = score_run(run, {"reply": full_set["reply"]}, settings_factory())
    missed = next(m for m in scores["reply"].measures if m.key == "interest_missed")
    assert (missed.hits, missed.total) == (10, 10)
    assert missed.status == "FAILS"


def test_queries_fail_when_the_filters_drop_most_proposals(full_set, settings_factory):
    run = run_exam(
        StandIn(lambda q, k: ["query 1", "query 2"]),
        {"queries": full_set["queries"]},
        1,
        say=lambda _: None,
    )
    scores = score_run(run, {"queries": full_set["queries"]}, settings_factory())
    kept = next(m for m in scores["queries"].measures if m.key == "kept")
    assert (kept.hits, kept.total) == (20, 50)
    assert scores["queries"].verdict == "FAIL"


def test_the_queries_measure_counts_proposals_not_askings(full_set, settings_factory):
    """The queries measure counts five proposals an asking, and the report
    called them askings: '13 wrong of its 60 askings' for 12 askings."""
    cases = {"queries": full_set["queries"]}
    run = run_exam(StandIn(lambda q, k: ["banana"] * 5), cases, 1, say=lambda _: None)
    kept = next(m for m in score_run(run, cases, settings_factory())["queries"].measures)
    assert kept.strength == "10 clear cases; 3 wrong cases fail it (11 wrong of its 50 proposals)"


def test_queries_that_meet_the_mark_read_as_format_only(full_set, settings_factory):
    """Five lines of nonsense survive the supplier-word filter, so the
    queries question read PASS beside the classifiers for an answerer that
    said 'banana' five times; nothing checks that a proposal is a useful
    search."""
    cases = {"queries": full_set["queries"]}
    run = run_exam(StandIn(lambda q, k: ["banana"] * 5), cases, 1, say=lambda _: None)
    scores = score_run(run, cases, settings_factory())
    assert scores["queries"].verdict == "FORMAT OK"
    text = ai_benchmark.report([run], [scores], cases, 1, None)
    assert "| queries | FORMAT OK |" in text
    assert "PASS" not in text


def test_the_language_decision_follows_italian_countries(full_set, settings_factory):
    """An Italian company named San Marino is still an Italian email when
    ITALIAN_COUNTRIES lists SM: the product's own rule decides."""
    italian = [
        c
        for c in full_set["country"]
        if c["expected"] == "IT" and c["difficulty"] == "clear"
    ]
    cases = {"country": italian}
    run = run_exam(StandIn(lambda q, k: "SM"), cases, 1, say=lambda _: None)
    for countries, right in (("IT", 0), ("IT,SM", len(italian))):
        scores = score_run(run, cases, settings_factory(ITALIAN_COUNTRIES=countries))
        m = next(m for m in scores["country"].measures if m.key == "language_right")
        assert m.hits == right


def test_too_few_clear_cases_never_pass(tmp_path, monkeypatch, settings_factory):
    folder = tmp_path / "small"
    cases = [_case("reply", "interested", n) for n in range(3)]
    cases += [_case("reply", "declined", n) for n in range(3)]
    _write(folder, "reply", cases)
    monkeypatch.setattr(ai_benchmark, "CASES_DIR", folder)
    loaded = load_cases(["reply"])
    run = run_exam(StandIn(_oracle(loaded)), loaded, 3, say=lambda _: None)
    scores = score_run(run, loaded, settings_factory())
    assert scores["reply"].verdict == "not enough cases"


def test_asked_once_consistency_is_not_measured(full_set, settings_factory):
    """Asked once, the verdict read 'not enough cases', which DEPLOY.md
    defines as fewer than ten clear cases, with more than ten behind every
    measure: the reader looked for cases when --repeat 2 was the fix."""
    run = run_exam(StandIn(_oracle(full_set)), full_set, 1, say=lambda _: None)
    scores = score_run(run, full_set, settings_factory())
    assert _status(scores, "relevance", "consistency") == "not measured"
    assert scores["relevance"].verdict == "asked once: use --repeat 2"
    quick = {q: quick_subset(c) for q, c in full_set.items()}
    run = run_exam(StandIn(_oracle(quick)), quick, 1, say=lambda _: None)
    assert score_run(run, quick, settings_factory())["relevance"].verdict == "not enough cases"


def test_hard_cases_are_reported_apart(full_set, settings_factory):
    run = run_exam(StandIn(_always_yes), full_set, 2, say=lambda _: None)
    scores = score_run(run, full_set, settings_factory())
    # The hard relevance case is a brand: YES is right on both askings.
    assert scores["relevance"].hard == "2 of 2, 100% right"
    clear_total = next(
        m for m in scores["relevance"].measures if m.key == "answered"
    ).total
    assert clear_total == 2 * sum(FULL_SET["relevance"].values())


# ─── A silent server ───────────────────────────────────────────────────────


@pytest.fixture
def server(monkeypatch):
    """A stand-in for the own server's transport."""
    real_client = httpx.Client
    state = type("Server", (), {})()
    state.reply = None
    state.bodies = []

    def handle(request):
        if request.url.path.endswith("/chat/completions"):
            state.bodies.append(json.loads(request.content))
        return state.reply(request)

    class Client(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(ai_generator.time, "sleep", lambda s: None)
    return state


def _own(**kw):
    return AIGenerator(
        provider="openai_compatible",
        base_url="http://127.0.0.1:11434/v1",
        llm_model="qwen3.5:0.8b",
        personal_data_allowed=True,
        **kw,
    )


def _timed_out(request):
    raise httpx.ReadTimeout("timed out", request=request)


def test_a_stalling_server_stops_the_run_and_is_not_scored(
    full_set, server, settings_factory
):
    server.reply = _timed_out
    ai = _own()
    run = run_exam(ai, full_set, 3, say=lambda _: None)
    assert len(server.bodies) == ai.STALL_LIMIT
    assert "pauses AI questions for 10 minutes" in run.stopped
    scores = score_run(run, full_set, settings_factory())
    assert set(_verdicts(scores).values()) == {"INCOMPLETE"}
    removed = next(m for m in scores["relevance"].measures if m.key == "wrongly_removed")
    assert removed.total == 0, "a silent server's askings are not scored"
    assert scores["relevance"].silent == ai.STALL_LIMIT


def test_one_stall_between_answers_is_not_scored_as_wrong(
    full_set, server, settings_factory
):
    calls = []

    def flaky(request):
        calls.append(request)
        if len(calls) == 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "NO"}}]})

    server.reply = flaky
    cases = {"relevance": [c for c in full_set["relevance"] if c["expected"] == "YES"]}
    run = run_exam(_own(), cases, 1, say=lambda _: None)
    assert not run.stopped
    assert [a.silent for a in run.askings["relevance"]][:3] == [False, True, False]
    scores = score_run(run, cases, settings_factory())
    removed = next(m for m in scores["relevance"].measures if m.key == "wrongly_removed")
    clear = [c for c in cases["relevance"] if c["difficulty"] == "clear"]
    assert removed.total == len(clear) - 1


def test_an_unreachable_server_is_named_and_nothing_is_asked(server):
    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    server.reply = refused
    problem = ai_benchmark.server_problem(_own(), "qwen3.5:0.8b")
    assert "did not answer its list of models" in problem
    assert server.bodies == []


def test_a_model_the_server_does_not_list_is_named(server):
    server.reply = lambda r: httpx.Response(
        200, json={"object": "list", "data": [{"id": "qwen3.5:0.8b"}]}
    )
    assert ai_benchmark.server_problem(_own(), "qwen3.5:0.8b") is None
    assert "does not list llama3:8b" in ai_benchmark.server_problem(_own(), "llama3:8b")


def test_the_command_reports_an_unreachable_server_without_asking(
    full_set, server, monkeypatch, settings_factory
):
    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    server.reply = refused
    settings = settings_factory(
        llm_provider="openai_compatible",
        LLM_BASE_URL="http://127.0.0.1:11434/v1",
        LLM_MODEL="qwen3.5:0.8b",
    )
    status, text = _run(["--repeat", "1"], settings, monkeypatch)
    assert status == 1, "a script must tell a server that was down from a model examined"
    assert "**Run incomplete:** the server at http://127.0.0.1:11434/v1 did not" in text
    assert "| relevance | INCOMPLETE |" in text
    assert server.bodies == []


def test_the_report_never_shows_a_password_in_the_address():
    ai = AIGenerator(
        provider="openai_compatible",
        base_url="https://bench:s3cret@models.internal.example/v1",
        llm_model="qwen3.5:0.8b",
    )
    run = ai_benchmark.new_run(ai)
    assert run.server == "https://models.internal.example/v1"


def test_a_run_that_stopped_exits_1(full_set, monkeypatch, settings_factory):
    """ai-benchmark exited 0 when nothing could be asked, so a scheduled
    check could not tell 'examined' from 'never asked'; ai-proof exits 1."""
    silent = StandIn(lambda q, k: [] if q == "queries" else None)
    status, text = _run(["--repeat", "1"], settings_factory(), monkeypatch, lambda *a, **k: silent)
    assert status == 1
    assert "**Run incomplete:**" in text
    status, _ = _run(
        ["--repeat", "1"], settings_factory(), monkeypatch, lambda *a, **k: StandIn(_always_yes)
    )
    assert status == 0, "a model that fails is examined, not stopped"


def test_three_first_askings_without_answer_stop_the_run(full_set, settings_factory):
    ai = StandIn(lambda q, k: [] if q == "queries" else None)
    run = run_exam(ai, full_set, 3, say=lambda _: None)
    assert len(ai.asked) == ai_benchmark.FIRST_ASKINGS_WITHOUT_ANSWER
    assert "got no answer at all" in run.stopped


# ─── Through the product's own questions ───────────────────────────────────


def test_the_exam_asks_the_products_own_questions(server, settings_factory):
    """The shipped cases, asked through a real generator: the request is
    the product's (its prompt, temperature 0 for a classifier), and the
    reply goes without its quote, as the agent sends it."""
    cases = load_cases()
    by_org = {c["organization"]: c for c in cases["relevance"]}

    def answer(request):
        prompt = json.loads(request.content)["messages"][0]["content"]
        org = next(o for o in by_org if f"Company: {o}" in prompt)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": by_org[org]["expected"]}}]},
        )

    server.reply = answer
    run = run_exam(_own(), {"relevance": cases["relevance"]}, 2, say=lambda _: None)
    assert [a.answer for a in run.askings["relevance"]] == [
        c["expected"] for c in cases["relevance"]
    ] * 2
    assert {b["temperature"] for b in server.bodies} == {0.0}
    assert "You qualify B2B leads for SUPPLECO" in server.bodies[0]["messages"][0]["content"]

    server.bodies.clear()
    server.reply = lambda r: httpx.Response(
        200, json={"choices": [{"message": {"content": "NO"}}]}
    )
    quoted = next(c for c in cases["reply"] if "wrote:" in c["body"])
    run_exam(_own(), {"reply": [quoted]}, 1, say=lambda _: None)
    assert "Dear team" not in server.bodies[0]["messages"][0]["content"]


def test_the_generator_is_the_agents_but_for_the_agreement(settings_factory, tmp_path):
    from agent import OutreachAgent

    settings = settings_factory(
        db_path=str(tmp_path / "agent.db"),
        llm_provider="openai_compatible",
        LLM_BASE_URL="http://127.0.0.1:11434/v1",
        LLM_MODEL="qwen3.5:0.8b",
        LLM_TIMEOUT_SECONDS=45,
    )
    agents = OutreachAgent(settings).ai_gen
    ours = ai_benchmark.build_generator(settings, invented_only=False)
    for name in (
        "provider",
        "MODEL_NAME",
        "base_url",
        "TIMEOUT_SECONDS",
        "reasoning_effort",
        "api_key",
        "personal_data_allowed",
    ):
        assert getattr(ours, name) == getattr(agents, name), name
    assert agents.personal_data_allowed is False
    assert ai_benchmark.build_generator(settings, invented_only=True).personal_data_allowed
    other = ai_benchmark.build_generator(settings, invented_only=True, llm_model="llama3:8b")
    assert other.MODEL_NAME == "llama3:8b"


# ─── The command ───────────────────────────────────────────────────────────


def _args(*argv):
    return main.build_parser().parse_args(["ai-benchmark", *argv])


def _run(argv, settings, monkeypatch, generator=None):
    out = []
    if generator is not None:
        monkeypatch.setattr(ai_benchmark, "build_generator", generator)
        # A stand-in has no server to list its models.
        monkeypatch.setattr(ai_benchmark, "server_problem", lambda ai, model: None)
    status = ai_benchmark.run_command(_args(*argv), settings, say=out.append)
    return status, "\n".join(out)


def test_own_cases_without_an_agreement_are_refused(
    full_set, tmp_path, monkeypatch, settings_factory
):
    own = tmp_path / "own"
    _write(own, "reply", [_case("reply", "interested", 50)])
    built = []
    status, text = _run(
        ["--cases", str(own)],
        settings_factory(),
        monkeypatch,
        lambda *a, **k: built.append(k) or StandIn(_always_yes),
    )
    assert status == 1
    assert "AI_DPA_REFERENCE" in text
    assert built == [], "nothing is asked"


def test_own_cases_with_an_agreement_are_asked_as_the_agent_asks(
    full_set, tmp_path, monkeypatch, settings_factory
):
    own = tmp_path / "own"
    _write(own, "reply", [_case("reply", "interested", 50)])
    built = []

    def build(settings, **kw):
        built.append(kw)
        return StandIn(_always_yes)

    status, text = _run(
        ["--cases", str(own), "--questions", "reply", "--repeat", "1"],
        settings_factory(AI_DPA_REFERENCE="own server, no outside processor"),
        monkeypatch,
        build,
    )
    assert status == 0
    assert built == [{"invented_only": False, "llm_model": None}]
    assert "own cases" in text


def test_own_cases_may_be_real_companies(
    full_set, tmp_path, monkeypatch, settings_factory
):
    """DEPLOY.md says the company's own cases may be real, and are asked
    with AI_DPA_REFERENCE set; the validator refused every own case whose
    domain did not end in .example, so the client's IT could not measure a
    model on its own companies."""
    own = tmp_path / "own"
    real = {
        **_case("relevance", "brand", 60),
        "id": "own-brand-01",
        "domain": "integratori-rossi.it",
        "page_text": "Integratori a marchio proprio; scrivete a info@integratori-rossi.it.",
    }
    _write(own, "relevance", [real])
    status, text = _run(
        ["--check", "--questions", "relevance", "--cases", str(own)],
        settings_factory(AI_DPA_REFERENCE="own server, no outside processor"),
        monkeypatch,
    )
    assert status == 0, text
    assert "1 the company's own" in text
    with pytest.raises(CaseFileError, match="does not end in .example"):
        load_case_file(own / "relevance.json", "relevance")
    _write(own, "relevance", [{**real, "domain": "not a domain"}])
    with pytest.raises(CaseFileError, match="is not a domain name"):
        load_cases(["relevance"], own)


def test_a_cases_folder_with_nothing_to_read_is_refused(
    full_set, tmp_path, monkeypatch, settings_factory
):
    """A mistyped --cases was skipped without a word: 'Case files are
    valid.' with the shipped counts only."""
    agreed = settings_factory(AI_DPA_REFERENCE="own server, no outside processor")
    missing = tmp_path / "no-such-folder"
    status, text = _run(["--check", "--cases", str(missing)], agreed, monkeypatch)
    assert status == 1
    assert f"[ERROR] --cases {missing}: the folder does not exist" in text
    other = tmp_path / "other"
    _write(other, "reply", [_case("reply", "interested", 50)])
    status, text = _run(
        ["--check", "--questions", "relevance", "--cases", str(other)], agreed, monkeypatch
    )
    assert status == 1
    assert "holds none of relevance.json" in text


def test_quick_asks_the_companys_own_cases_too(
    full_set, tmp_path, monkeypatch, settings_factory
):
    """--quick took the first clear cases of each category, and the shipped
    ones, coming first, filled every category: no own case was asked."""
    own = tmp_path / "own"
    mine = {**_case("relevance", "brand", 60), "id": "own-brand-01"}
    _write(own, "relevance", [mine])
    stand_in = StandIn(_always_yes)
    status, text = _run(
        ["--quick", "--repeat", "1", "--questions", "relevance", "--cases", str(own)],
        settings_factory(AI_DPA_REFERENCE="own server, no outside processor"),
        monkeypatch,
        lambda *a, **k: stand_in,
    )
    assert status == 0, text
    assert mine["organization"] in [c["organization"] for c in stand_in.asked]
    assert "The company's own cases from" in text


def test_the_shipped_cases_run_without_an_agreement(
    full_set, monkeypatch, settings_factory
):
    built = []

    def build(settings, **kw):
        built.append(kw)
        return StandIn(_oracle(full_set))

    status, text = _run(["--repeat", "2"], settings_factory(), monkeypatch, build)
    assert status == 0
    assert built == [{"invented_only": True, "llm_model": None}]
    assert "The shipped cases are invented" in text
    assert "| relevance | PASS |" in text


def test_blind_gives_nothing_away(monkeypatch, settings_factory, tmp_path):
    """--blind left out the answer and the category but kept the ids, and
    each id names its category (rep-interested-01), which fixes the answer;
    the difficulty showed too. A label checker saw the answer. Now a case
    shows a neutral number, its language and its inputs, in an order that
    does not follow the file, and --out writes the key apart."""
    status, text = _run(["--blind", "reply"], settings_factory(), monkeypatch)
    assert status == 0
    shown = json.loads(text)
    shipped = load_cases(["reply"])["reply"]
    assert len(shown) == len(shipped)
    for case in shown:
        assert set(case) == {"case", "language", "subject", "body"}
        assert re.fullmatch(r"reply-\d{3}", case["case"])
    for word in ("interested", "declined", "removal", "autoreply", "neutral", "clear", "hard"):
        assert f'"{word}' not in text and f"-{word}-" not in text
    for case in shipped:
        assert case["why"] not in text
    assert [c["subject"] for c in shown] != [c["subject"] for c in shipped]

    out = tmp_path / "blind"
    status, said = _run(["--blind", "reply", "--out", str(out)], settings_factory(), monkeypatch)
    assert status == 0 and "keep it from the reader" in said
    written = json.loads((out / "blind-reply.json").read_text(encoding="utf-8"))
    assert written == shown
    key = json.loads((out / "key-reply.json").read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in shipped}
    for case in written:
        assert by_id[key[case["case"]]]["body"] == case["body"]


def test_check_validates_and_counts_what_the_product_settles(
    monkeypatch, settings_factory, tmp_path
):
    folder = tmp_path / "cases"
    _write(
        folder,
        "reply",
        [
            {**_case("reply", "interested", 1), "body": "We are interested, send prices."},
            _case("reply", "interested", 2),
        ],
    )
    monkeypatch.setattr(ai_benchmark, "CASES_DIR", folder)
    status, text = _run(["--check", "--questions", "reply"], settings_factory(), monkeypatch)
    assert status == 0
    assert "1 of 2, 50% are settled by the product before the AI is asked" in text
    assert "rep-interested-01: interested by its wording" in text


def test_the_gate_reads_a_case_domain_as_the_agent_would_find_it(settings_factory):
    """A case's domain is a real-looking one with .example added. The agent
    removes vidacerrado.com.br as non-European before the AI is asked, but
    the gate passed vidacerrado.com.br.example, so 13 such easy cases counted
    toward "Non-clients caught" although the agent never asks the AI about
    them."""
    case = {
        **_case("relevance", "non_european", 1),
        "domain": "vidacerrado.com.br.example",
        "page_text": "Food supplements with guarana and acai, shipped all over Brazil.",
    }
    assert ai_benchmark.decided_without_ai("relevance", case, settings_factory()) == (
        "removed by the relevance gate (non-European domain (.br))"
    )
    assert ai_benchmark.decided_without_ai(
        "relevance", {**case, "domain": "vidacerrado.example"}, settings_factory()
    ) is None


def test_a_reply_the_inbox_files_away_is_settled_before_the_ai(settings_factory):
    """Before a reply is judged, the agent's inbox drops an auto-reply whose
    subject says so and files a delivery failure as a bounce. --check said
    none of the 115 reply cases was settled, yet 7 of them never reach the
    AI in the agent and still moved the false-alert verdict."""
    settings = settings_factory()
    case = {**_case("reply", "auto_reply", 1), "subject": "Re: Supplement production"}
    for subject, filed in (
        ("Automatic reply: Supplement production", "an auto-reply"),
        ("Abwesenheitsnotiz: Supplement production", "an auto-reply"),
        ("Delivery Status Notification (Failure)", "a bounce"),
        ("Undeliverable: Supplement production", "a bounce"),
        ("Read: Supplement production", "a read receipt"),
    ):
        assert ai_benchmark.decided_without_ai(
            "reply", {**case, "subject": subject}, settings
        ) == f"filed by the inbox as {filed}", subject
    assert ai_benchmark.decided_without_ai("reply", case, settings) is None


def test_a_refused_file_stops_the_command(tmp_path, monkeypatch, settings_factory):
    folder = tmp_path / "cases"
    _write(folder, "reply", [_case("reply", "interested", 1)] * 2)
    monkeypatch.setattr(ai_benchmark, "CASES_DIR", folder)
    status, text = _run(["--check", "--questions", "reply"], settings_factory(), monkeypatch)
    assert status == 1
    assert "rep-interested-01" in text and "used twice" in text


def test_models_runs_each_model_and_compares_them(
    full_set, monkeypatch, settings_factory
):
    built = []

    def build(settings, **kw):
        built.append(kw["llm_model"])
        ai = StandIn(_oracle(full_set) if kw["llm_model"] == "good:1b" else _always_yes)
        ai.MODEL_NAME = kw["llm_model"]
        return ai

    settings = settings_factory(
        llm_provider="openai_compatible",
        LLM_BASE_URL="http://127.0.0.1:11434/v1",
        LLM_MODEL="qwen3.5:0.8b",
    )
    status, text = _run(["--models", "good:1b,yes:1b"], settings, monkeypatch, build)
    assert status == 0
    assert built == ["good:1b", "yes:1b"]
    assert "## Comparison" in text
    assert "| good:1b | PASS | PASS | PASS | FORMAT OK |" in text
    assert "| yes:1b | FAIL |" in text


def test_an_unset_provider_is_named_as_unset(monkeypatch, settings_factory):
    """With LLM_PROVIDER never set the AI is off, and the line says what the
    own server needs."""
    status, text = _run(["--repeat", "1"], settings_factory(), monkeypatch)
    assert status == 1
    assert text.startswith("[ERROR] The AI is off: LLM_PROVIDER is not set")
    assert "LLM_PROVIDER=openai_compatible, LLM_BASE_URL" in text
    assert "and LLM_MODEL" in text


def test_models_is_for_the_own_server_only(monkeypatch, settings_factory):
    status, text = _run(["--models", "a:1b"], settings_factory(), monkeypatch)
    assert status == 1
    assert text.startswith("[ERROR] --models compares models on the company's own server")


def test_out_writes_the_report_and_the_results(
    full_set, monkeypatch, settings_factory, tmp_path
):
    out = tmp_path / "out"
    status, text = _run(
        ["--out", str(out), "--repeat", "2"],
        settings_factory(),
        monkeypatch,
        lambda *a, **k: StandIn(_always_yes),
    )
    assert status == 0
    assert (out / "report.md").read_text(encoding="utf-8") in text
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    (model,) = results["models"]
    assert model["questions"]["relevance"]["verdict"] == "FAIL"
    assert len(model["questions"]["relevance"]["askings"]) == 2 * len(
        full_set["relevance"]
    )


def test_a_console_that_cannot_print_a_letter_does_not_crash(
    full_set, monkeypatch, settings_factory
):
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp437", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)

    def build(settings, **kw):
        ai = StandIn(_oracle(full_set))
        ai.MODEL_NAME = "modèle-ż"
        return ai

    monkeypatch.setattr(ai_benchmark, "build_generator", build)
    monkeypatch.setattr(ai_benchmark, "server_problem", lambda ai, model: None)
    assert ai_benchmark.run_command(_args("--repeat", "1"), settings_factory()) == 0
    console.flush()
    assert b"mod\x8ale-?" in console.buffer.getvalue()


def test_the_report_gives_counts_with_percentages():
    assert share(3, 75) == "3 of 75, 4%"
    assert share(0, 0) == "none asked"


@pytest.mark.parametrize(
    "argv",
    [
        ["--repeat", "0"],
        ["--repeat", "11"],
        ["--questions", "relevance,spelling"],
        ["--blind", "spelling"],
        ["--models", ","],
    ],
)
def test_the_options_refuse_what_they_cannot_do(argv, capsys):
    with pytest.raises(SystemExit):
        _args(*argv)


def test_quick_is_a_fixed_small_subset(full_set):
    first = quick_subset(full_set["relevance"])
    assert [c["id"] for c in first] == [c["id"] for c in quick_subset(full_set["relevance"])]
    per_category = {}
    for c in first:
        per_category[c["category"]] = per_category.get(c["category"], 0) + 1
        assert c["difficulty"] == "clear"
    assert set(per_category.values()) == {ai_benchmark.QUICK_CASES_PER_CATEGORY}


# ─── The case files ────────────────────────────────────────────────────────


def _refusal(tmp_path, question, case):
    path = _write(tmp_path / "bad", question, [case])
    with pytest.raises(CaseFileError) as error:
        load_case_file(path, question)
    return str(error.value)


@pytest.mark.parametrize(
    ("question", "change", "words"),
    [
        ("relevance", {"domain": "vitaforma.it"}, "does not end in .example"),
        (
            "relevance",
            {"page_text": "Write to anna@vitaforma.it for prices."},
            "vitaforma.it, which does not end in .example",
        ),
        (
            "reply",
            {"body": "See https://www.realbrand.com/offer"},
            "www.realbrand.com, which does not end in .example",
        ),
        ("relevance", {"expected": "MAYBE"}, "expected must be YES"),
        ("relevance", {"category": "brand", "expected": "NO"}, "expected must be YES"),
        ("country", {"category": "foreign", "expected": "Germany"}, "two-letter"),
        ("country", {"category": "foreign", "expected": "UK"}, "GB"),
        ("queries", {"expected": 6}, "from 1 to 5"),
        ("reply", {"difficulty": "medium"}, "clear or hard"),
        ("reply", {"category": "angry"}, "category must be one of"),
        ("reply", {"why": "two\nlines"}, "one line"),
        ("reply", {"tone": "warm"}, "does not read: tone"),
    ],
)
def test_the_loader_refuses_a_wrong_case_and_names_it(tmp_path, question, change, words):
    first = next(iter(ai_benchmark.CATEGORIES[question]))
    case = {**_case(question, first, 7), **change}
    text = _refusal(tmp_path, question, case)
    assert case["id"] in text
    assert words in text


def test_the_loader_refuses_a_missing_field(tmp_path):
    case = _case("reply", "interested", 3)
    del case["expected"]
    assert "rep-interested-03: is missing expected" in _refusal(tmp_path, "reply", case)


def test_the_loader_refuses_a_duplicate_id_across_shipped_and_own(tmp_path, monkeypatch):
    _write(tmp_path / "shipped", "reply", [_case("reply", "interested", 1)])
    _write(tmp_path / "own", "reply", [_case("reply", "declined", 1) | {"id": "rep-interested-01"}])
    monkeypatch.setattr(ai_benchmark, "CASES_DIR", tmp_path / "shipped")
    with pytest.raises(CaseFileError, match="rep-interested-01: the id is used twice"):
        load_cases(["reply"], tmp_path / "own")


def _description(question):
    path = ai_benchmark.CASES_DIR / f"{question}.json"
    return json.loads(path.read_text("utf-8"))["description"]


def test_the_shipped_case_files_validate():
    cases = load_cases()
    assert set(cases) == set(ai_benchmark.QUESTIONS)
    for question in cases:
        assert "invented" in _description(question)


# The full sets: the groups a measure rests on and the clear cases each
# holds at least. With about 23 clear clients one more wrong case flipped
# a verdict, so the classifier groups hold far more than the
# MIN_CLEAR_CASES a measure needs before it reads anything but "not enough".
FULL_SETS = {
    "relevance": [
        (ai_benchmark.CLIENT_CATEGORIES, 50),
        (ai_benchmark.NON_CLIENT_CATEGORIES, 50),
        (("competitor",), 20),
    ],
    "country": [(("italian",), 30), (("foreign",), 45), (("no_evidence",), 30)],
    "reply": [
        (("interested",), 45),
        (("declined", "removal", "auto_reply", "neutral"), 55),
    ],
    "queries": [(("no_hint", "hint"), ai_benchmark.MIN_CLEAR_CASES)],
}


@pytest.mark.parametrize("question", list(FULL_SETS))
def test_a_full_shipped_set_can_give_a_verdict(question):
    question_cases = load_cases([question])[question]
    clear = [c for c in question_cases if c["difficulty"] == "clear"]
    for group, minimum in FULL_SETS[question]:
        in_group = [c for c in clear if c["category"] in group]
        assert len(in_group) >= max(minimum, ai_benchmark.MIN_CLEAR_CASES), group
    assert "only six cases" not in _description(question)


@pytest.mark.parametrize("question", list(FULL_SETS))
def test_no_two_shipped_cases_send_the_same_question(question):
    """qry-nohint-01 and -02 both had a blank hint, so the model got the
    same prompt twice and it counted as two of the twelve clear cases."""
    seen = {}
    for case in load_cases([question])[question]:
        inputs = tuple(case[name] for name in ai_benchmark.INPUTS[question])
        assert inputs not in seen, (seen.get(inputs), case["id"])
        seen[inputs] = case["id"]


@pytest.mark.parametrize("question", list(FULL_SETS))
def test_no_full_shipped_case_is_settled_before_the_ai(question, settings_factory):
    """A case the product settles by its own rules never reaches the AI in
    the agent, so it would measure the model on a question it is never asked."""
    settings = settings_factory()
    settled = [
        c["id"]
        for c in load_cases([question])[question]
        if ai_benchmark.decided_without_ai(question, c, settings)
    ]
    assert settled == []


def _plain_domain(case):
    """Whether the domain, read without .example, ends in no country code."""
    tld = case["domain"].removesuffix(".example").rsplit(".", 1)[-1]
    return not (len(tld) == 2 and tld.isalpha())


def test_a_relevance_domain_does_not_give_the_answer_away():
    """A non-European case may carry no country code (the agent's gate
    removes .br or .in before the AI is asked), and 107 of the 119 European
    cases carried one, so a domain with no code was NO in 36 of 39 cases: a
    reader or the model could tell a non-European company from its domain
    without reading the page. About a third of the European cases now carry
    a plain domain and show where they are in their page text."""
    cases = load_cases(["relevance"])["relevance"]
    european = [c for c in cases if c["category"] != "non_european"]
    assert sum(_plain_domain(c) for c in european) * 3 >= len(european)
    plain = [c for c in cases if _plain_domain(c)]
    no_share = sum(c["expected"] == "NO" for c in cases) / len(cases)
    plain_no_share = sum(c["expected"] == "NO" for c in plain) / len(plain)
    assert plain_no_share <= no_share + 0.05
    assert sum(c["category"] == "non_european" for c in plain) * 2 < len(plain)


def test_the_docker_image_carries_the_case_files():
    ignored = [
        line.strip().rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for path in ai_benchmark.CASES_DIR.glob("*.json"):
        relative = path.relative_to(ROOT).as_posix()
        parts = relative.split("/")
        for pattern in ignored:
            assert not any(
                fnmatch("/".join(parts[: i + 1]), pattern) for i in range(len(parts))
            ), (relative, pattern)
