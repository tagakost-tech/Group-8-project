"""The AI proof run: the whole agent, with the configured AI, on invented cases.

The benchmark (ai_benchmark.py) asks the AI's questions one at a time, so it
could not answer what the company's IT asks first: does the agent itself work
with our model, end to end? This run builds the agent as main.py does, stores
a few of the benchmark's invented companies as if the search had found them,
and sends them through the agent's own qualification and dry-run send
(process_pending_contacts, the method the send loop runs), then puts a few
invented replies through the inbox's filing (what it files away as an
auto-reply or a bounce is never judged) and the agent's own judgement of a
reply (reply_is_interested). The three AI questions are switched on for this run
only, and it prints what became of each case for someone who is not a
developer.

It sends nothing. It works in a database of its own, in a new folder that is
removed afterwards, and never reads DB_PATH. The agent's own network steps
on this path (the MX and blocklist lookups) are switched off for the run, and
said so; the web search, the inbox and the lead alert's email are outside
it. While the run lasts every outgoing connection but the AI server's is
refused and recorded, and one attempt fails the run, so a step that reaches
for the network is seen rather than trusted away.

It shows the plumbing and the model's answers on a handful of cases; whether
a model is good enough is the benchmark's to judge.
"""

import argparse
import logging
import shutil
import socket
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from agent import OutreachAgent
from config import Settings
from modules import ai_benchmark, reply_intent
from modules.company_country import collected_country, page_country
from modules.compliance import italian_countries, notice_language

DEFAULT_COMPANIES = 12
DEFAULT_REPLIES = 8
MAX_CASES = 50
WORK_PREFIX = "ai-proof-"
SEARCH_QUERY = "ai-proof: invented benchmark cases"
INVENTED_ONLY = "invented benchmark cases only (ai-proof run)"

# The kinds of case taken in turn, so that a small batch holds clients,
# competitors, companies outside the target, Italian and foreign companies,
# and every kind of reply.
COMPANY_GROUPS = (
    ("relevance", "brand"),
    ("country", "italian"),
    ("relevance", "competitor"),
    ("country", "foreign"),
    ("relevance", "retailer"),
    ("country", "no_evidence"),
    ("relevance", "off_target"),
    ("relevance", "non_european"),
)
REPLY_GROUPS = tuple(("reply", category) for category in ai_benchmark.CATEGORIES["reply"])

# What this run changes for itself, and the line that says so; a None line
# is said by the one before it. Each turns off a step that would reach a
# machine other than the AI server, or one that would hold a dry run up.
RUN_ONLY = (
    ("ai_relevance_enabled", True, "the AI relevance question is on"),
    ("company_country_ai", True, "the AI company-country question is on"),
    ("ai_reply_classification_enabled", True, "the AI reply question is on"),
    ("dry_run", True, "dry run: each email is composed and logged, none is sent"),
    (
        "verify_mx",
        False,
        "no MX lookup of the addresses: it asks DNS, and the addresses are invented",
    ),
    (
        "blacklist_check_enabled",
        False,
        "no blocklist check of the sending domain: it asks public DNS",
    ),
    ("send_window_enabled", False, "no send window: the run works at any hour"),
    ("delay_between_emails", 0.0, "no pause between emails: none is sent"),
    ("emails_per_hour", 0, None),
    ("send_jitter_seconds", 0.0, None),
    (
        "unknown_country_allowed",
        True,
        (
            "addresses whose domain shows no country count as allowed, so the "
            "country question is asked: every invented address ends in .example"
        ),
    ),
)


def proof_settings(settings: Settings, db_path: Path) -> tuple[Settings, list[str]]:
    """The settings of this run, and the lines that say what changed: the
    operator's, with RUN_ONLY, the run's own database, and a note in
    AI_DPA_REFERENCE when it is blank, since every case asked is invented (the
    benchmark's exception). Rebuilt and validated as a dashboard change is
    (config.validate_override), and stored nowhere."""
    changes = {name: value for name, value, _ in RUN_ONLY}
    changes["db_path"] = str(db_path)
    said = [line for _, _, line in RUN_ONLY if line]
    if not (settings.ai_dpa_reference or "").strip():
        changes["ai_dpa_reference"] = INVENTED_ONLY
        said.append(
            "AI_DPA_REFERENCE is blank: the AI is asked anyway, as the benchmark "
            "is, since every case is invented"
        )
    data = settings.model_dump(by_alias=True)
    for name, value in changes.items():
        data[Settings.model_fields[name].alias or name] = value
    return Settings(_env_file=None, **data), said


def pick(cases: dict, groups, count: int) -> list[tuple[str, dict]]:
    """count clear cases, one of each group in turn, in file order: the same
    cases every time. A company whose domain is already taken is passed over,
    as the agent stores one contact per address."""
    queues = [
        [
            (question, c)
            for c in cases[question]
            if c["category"] == category and c["difficulty"] == "clear"
        ]
        for question, category in groups
    ]
    chosen, domains = [], set()
    while len(chosen) < count and any(queues):
        for queue in queues:
            while queue and len(chosen) < count:
                question, case = queue.pop(0)
                domain = case.get("domain")
                if domain in domains:
                    continue
                if domain:
                    domains.add(domain)
                chosen.append((question, case))
                break
    return chosen


# ─── The guard ─────────────────────────────────────────────────────────────


class NetworkGuard:
    """Refuses, and records, every outgoing connection and name lookup but
    the AI server's, while it is entered.

    Python's own sockets and lookups are guarded, and the two HTTP clients
    that open connections from native code (curl_cffi for page fetches, primp
    for searches), as the tests' guard does (conftest.py). A refusal raises
    ConnectionRefusedError where it happens; a caller that swallows it still
    leaves it in attempts, which fails the run."""

    def __init__(self):
        self.attempts: list[str] = []
        self._server: tuple[str, int] | None = None
        self._addresses: set[str] = set()
        self._saved: list = []

    def allow(self, url: str) -> None:
        """Let connections to the server at url through: its host and port,
        and the addresses its name was looked up as."""
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        self._server = ((parts.hostname or "").lower(), port)

    def _refuse(self, what: str):
        self.attempts.append(what)
        raise ConnectionRefusedError(
            f"the AI proof run allows no connection but the AI server's: {what}"
        )

    def _server_name(self, host) -> bool:
        name = host.decode() if isinstance(host, bytes) else str(host or "")
        return self._server is not None and name.lower() == self._server[0]

    def _server_address(self, host) -> bool:
        """An address the server's name was looked up as, given as itself: the
        AI call connects to it by number (ai_generator._local_stream), and
        looking a number up asks no name server."""
        name = host.decode() if isinstance(host, bytes) else str(host or "")
        return name.lower() in self._addresses

    def _to_server(self, address) -> bool:
        if self._server is None or not isinstance(address, tuple):
            return False
        host = str(address[0]).lower()
        return address[1] == self._server[1] and (
            host == self._server[0] or host in self._addresses
        )

    def _patch(self, owner, name: str, replacement) -> None:
        self._saved.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def __enter__(self):
        connect = socket.socket.connect
        connect_ex = socket.socket.connect_ex
        sendto = socket.socket.sendto
        getaddrinfo = socket.getaddrinfo

        def guarded_connect(sock, address):
            if self._to_server(address):
                return connect(sock, address)
            return self._refuse(f"connect {address!r}")

        def guarded_connect_ex(sock, address):
            if self._to_server(address):
                return connect_ex(sock, address)
            return self._refuse(f"connect {address!r}")

        def guarded_sendto(sock, data, *flags_and_address):
            if self._to_server(flags_and_address[-1]):
                return sendto(sock, data, *flags_and_address)
            return self._refuse(f"send to {flags_and_address[-1]!r}")

        def guarded_getaddrinfo(host, port, *args, **kwargs):
            if not (self._server_name(host) or self._server_address(host)):
                return self._refuse(f"look up {host!r}")
            found = getaddrinfo(host, port, *args, **kwargs)
            self._addresses.update(str(info[4][0]).lower() for info in found)
            return found

        def guarded_lookup(name):
            lookup = getattr(socket, name)

            def guarded(host, *args, **kwargs):
                if not self._server_name(host):
                    return self._refuse(f"look up {host!r}")
                return lookup(host, *args, **kwargs)

            return guarded

        self._patch(socket.socket, "connect", guarded_connect)
        self._patch(socket.socket, "connect_ex", guarded_connect_ex)
        self._patch(socket.socket, "sendto", guarded_sendto)
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            self._patch(socket, name, guarded_lookup(name))
        self._patch(socket, "getaddrinfo", guarded_getaddrinfo)

        def refused(what):
            return lambda *args, **kwargs: self._refuse(what)

        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            curl_requests = None
        if curl_requests is not None:
            for session in (curl_requests.Session, curl_requests.AsyncSession):
                self._patch(
                    session, "request", refused(f"curl_cffi {session.__name__}.request")
                )
        try:
            import primp
        except ImportError:
            primp = None
        if primp is not None:
            for verb in ("request", "get", "head", "options", "delete", "post", "put", "patch"):
                for client in (primp.Client, primp.AsyncClient):
                    if hasattr(client, verb):
                        self._patch(client, verb, refused(f"primp {client.__name__}.{verb}"))
                if hasattr(primp, verb):
                    self._patch(primp, verb, refused(f"primp.{verb}"))
        return self

    def __exit__(self, *exc):
        for owner, name, original in reversed(self._saved):
            setattr(owner, name, original)
        self._saved.clear()
        return False


class AgentLog:
    """The agent's log, while the run lasts, in a file in the work folder: the
    console keeps the run's own lines for the person reading them. The file
    is written as the console was (main.py's masking formatter, when main.py
    set the logging up)."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        root = logging.getLogger()
        self._handlers, self._level = root.handlers[:], root.level
        self._file = logging.FileHandler(self.path, encoding="utf-8")
        self._file.setFormatter(
            next(
                (h.formatter for h in self._handlers if h.formatter is not None),
                logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s"),
            )
        )
        root.handlers = [self._file]
        root.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        root.handlers = self._handlers
        root.setLevel(self._level)
        self._file.close()
        return False


# ─── What the agent asked ──────────────────────────────────────────────────


@dataclass
class Asked:
    """One AI question the agent asked: which, about whom (a contact's address
    or a reply's case id), the answer as the agent got it, the seconds, and
    whether the server was silent (a timeout or no connection)."""

    question: str
    key: str
    answer: object
    seconds: float
    silent: bool


class AnswerLog:
    """Every AI question the agent asks during the run, timed on its way
    through the agent's own generator; the agent's code is not changed.

    When the agent's pause after STALL_LIMIT silent answers starts, the agent
    is stopped as its Stop button stops it, and stopped says why."""

    QUESTIONS: ClassVar[dict[str, str]] = {
        "classify_relevance": "relevance",
        "classify_company_country": "country",
        "classify_reply_intent": "reply",
        "generate_opening_line": "writing",
        "generate_email_body": "writing",
    }

    def __init__(self, agent):
        self.agent = agent
        self.asked: list[Asked] = []
        self.reply = ""
        self.stopped = ""
        ai = agent.ai_gen
        for method, question in self.QUESTIONS.items():
            setattr(ai, method, self._timed(ai, getattr(ai, method), question))

    def _timed(self, ai, ask, question: str):
        def timed(*args, **kwargs):
            # During the pause nothing reaches the server: not a question.
            if ai.paused:
                return ask(*args, **kwargs)
            before = ai.stalls_in_a_row
            started = time.monotonic()
            answer = ask(*args, **kwargs)
            took = time.monotonic() - started
            contact = args[0] if args else kwargs.get("contact") or {}
            key = self.reply if question == "reply" else contact.get("email") or ""
            self.asked.append(
                Asked(question, key, answer, took, ai.stalls_in_a_row > before)
            )
            if ai.paused and not self.stopped:
                self.stopped = (
                    f"the AI server gave no answer {ai.stalls_in_a_row} times in a "
                    f"row, and the agent pauses its AI questions for "
                    f"{ai.STALL_PAUSE_SECONDS / 60:g} minutes after {ai.STALL_LIMIT}: "
                    "the run was stopped there"
                )
                self.agent.stop()
            return answer

        return timed

    def about(self, key: str) -> list[Asked]:
        return [a for a in self.asked if a.key == key]

    def classifiers(self) -> list[Asked]:
        return [a for a in self.asked if a.question != "writing"]


# ─── Words ─────────────────────────────────────────────────────────────────


def _answer_words(asked: Asked) -> str:
    if asked.question == "writing":
        return "an email text written" if asked.answer else "no email text"
    if asked.answer is None:
        return f"{asked.question}: no answer" + (" (server silent)" if asked.silent else "")
    if asked.question == "relevance":
        return "a target: YES" if asked.answer else "a target: NO"
    if asked.question == "country":
        return f"country {asked.answer}" if asked.answer else "country: cannot tell"
    return "interested: YES" if asked.answer else "interested: NO"


def _seconds(value: float) -> str:
    return f"{value:.1f} s"


def _verdict_words(right: bool | None) -> str:
    return {True: "agrees", False: "DISAGREES", None: "not judged"}[right]


# ─── The run ───────────────────────────────────────────────────────────────


@dataclass
class CompanyResult:
    case: dict
    question: str
    email: str
    rules: str
    outcome: str
    kept: bool | None
    language: str | None
    right: bool | None
    expected: str


def _store(agent, companies: list) -> list[tuple[str, dict, int | None, str]]:
    """Store each company as the search stores what it finds (search.py,
    step 4): an address on its own website, the page's text, and the country
    its own website or page shows."""
    search_id = agent.db.log_search(SEARCH_QUERY, source="ai-proof", status="completed")
    stored = []
    for question, case in companies:
        contact = ai_benchmark._contact(case)
        country, source = collected_country(
            contact["source_url"],
            contact["source_channel"],
            page_country(case["page_text"], None, case["organization"]),
        )
        contact_id = agent.db.add_contact(
            search_id=search_id,
            email=contact["email"],
            organization=contact["organization"],
            source_url=contact["source_url"],
            qualification_rationale=contact["qualification_rationale"],
            source_channel=contact["source_channel"],
            company_country=country,
            company_country_source=source,
        )
        stored.append((question, case, contact_id, contact["email"]))
    return stored


def _company_result(agent, stored, answers: AnswerLog, emailed: dict, italian: set):
    """What became of one stored company, read back from the run's database:
    its status and why, the country recorded, and the dry-run email logged."""
    question, case, contact_id, email = stored
    expected = _expected_words(question, case, italian)
    if contact_id is None:
        return CompanyResult(
            case=case,
            question=question,
            email=email,
            rules="not stored: the agent refused its address",
            outcome="not run",
            kept=None,
            language=None,
            right=None,
            expected=expected,
        )
    row = agent.db.get_contact_by_id(contact_id) or {}
    status = row.get("status")
    asked_relevance = any(a.question == "relevance" for a in answers.about(email))
    language = emailed.get(contact_id)
    if status != "pending" and status != "ai_irrelevant" and not asked_relevance:
        rules = f"removed: {row.get('discard_reason') or status}"
    elif not asked_relevance:
        # Still pending and never asked: the send loop never reached it, and
        # its stored lead score of 0 is the column's default, not a score.
        rules = "not reached"
    else:
        rules = f"passed (lead score {row.get('lead_score')})"
        source = row.get("company_country_source")
        if source in ("website", "page"):
            rules += f", country {row.get('company_country')} from its own {source}"
    if language:
        kept = True
        outcome = f"would be emailed in {'Italian' if language == 'it' else 'English'}"
    elif status == "ai_irrelevant":
        kept, outcome = False, "removed by the AI: not a target"
    elif status != "pending":
        kept, outcome = False, f"removed by the rules: {row.get('discard_reason') or status}"
    else:
        kept = None
        outcome = (
            "not reached: the run stopped" if answers.stopped else "left waiting, not emailed"
        )
    return CompanyResult(
        case=case,
        question=question,
        email=email,
        rules=rules,
        outcome=outcome,
        kept=kept,
        language=language,
        right=_company_right(question, case, kept, language, italian),
        expected=expected,
    )


def _expected_words(question: str, case: dict, italian: set) -> str:
    if question == "relevance":
        return "keep" if case["expected"] == "YES" else "remove"
    language = "Italian" if case["expected"] in italian else "English"
    return f"{case['expected']}, in {language} if kept"


def _company_right(question, case, kept, language, italian) -> bool | None:
    """Whether the outcome is the case's right answer. A relevance case says
    whether the company is kept; a country case only which language its email
    is in, so a country case removed, or not reached, is not judged."""
    if kept is None:
        return None
    if question == "relevance":
        return kept == (case["expected"] == "YES")
    if not kept:
        return None
    return (language == "it") == (case["expected"] in italian)


def _emailed_languages(agent) -> dict[int, str]:
    """The language of each dry-run email the run logged, by contact."""
    return {
        row["contact_id"]: notice_language(row.get("notice_version"))
        for row in agent.db.get_all_emails(limit=10 * MAX_CASES)
        if row.get("delivery_status") == "dry_run" and row.get("contact_id")
    }


def _run(work: Path, settings: Settings, companies, replies, tell) -> int:
    """Build the agent in work, run the cases through it under the guard, and
    tell what happened. Returns the exit status."""
    run_settings, said = proof_settings(settings, work / "proof.db")
    unconfigured = ai_benchmark.not_configured(settings)
    guard = NetworkGuard()
    with guard, AgentLog(work / "agent.log"):
        agent = OutreachAgent(run_settings)
        try:
            return _through_the_agent(
                agent, run_settings, said, companies, replies, guard, tell, unconfigured
            )
        finally:
            agent._running = False
            for _key, _name, sender in agent.senders:
                sender.close()
            agent.db.close_thread_conn()


def _through_the_agent(
    agent, settings, said, companies, replies, guard, tell, unconfigured
) -> int:
    """The run proper, once the agent is built: the AI checked as the
    benchmark checks it, the companies stored and sent through the send
    loop's own method, the replies judged, and the outcome told.
    unconfigured is the line to tell when the AI is not configured."""
    ai = agent.ai_gen
    if ai is None or not ai.is_configured:
        tell(f"{unconfigured} Nothing was asked.")
        return 1
    guard.allow(ai.base_url)
    run = ai_benchmark.new_run(ai)
    tell(
        f"  AI: {run.provider}, model {run.model} at {run.server} (reasoning "
        f"{run.reasoning}, timeout {run.timeout})"
    )
    tell(
        f"  Cases: {len(companies)} invented companies and {len(replies)} invented "
        "replies, clear cases from benchmark/cases"
    )
    tell("  For this run only (no stored setting is changed):")
    for line in said:
        tell(f"    - {line}")
    tell()
    problem = ai_benchmark.server_problem(ai, ai.MODEL_NAME)
    if problem:
        tell(f"[STOPPED] {problem}. Nothing was asked.")
        return 1

    stored = _store(agent, companies)
    answers = AnswerLog(agent)
    alerted: dict[str, bool] = {}
    filed: dict[str, str] = {}
    agent._running = True
    try:
        agent._process_pending_contacts(dry_run=True)
        for _question, case in replies:
            if not agent._running:
                break
            # The inbox's step comes first in the agent: what it files away
            # as an auto-reply or a bounce is never judged, and alerts nobody.
            kind = ai_benchmark.inbox_files_as(case["subject"], case["body"])
            if kind:
                filed[case["id"]] = kind
                continue
            answers.reply = case["id"]
            alerted[case["id"]] = agent._reply_is_interested(case["subject"], case["body"])
    except Exception:
        # The guard's refusal, as raised or as a library wrapped it, is told
        # below with what was attempted; anything else is a fault to show.
        if not guard.attempts:
            raise

    italian = italian_countries(settings)
    emailed = _emailed_languages(agent)
    results = [_company_result(agent, s, answers, emailed, italian) for s in stored]
    tell("Companies, through the agent's own qualification and dry-run send:")
    for r in results:
        asked = answers.about(r.email)
        ai_words = ", ".join(_answer_words(a) for a in asked) or "not asked"
        tell(
            f"  {r.case['id']} ({r.case['organization']}) | rules: {r.rules} | AI: "
            f"{ai_words} | {r.outcome} | {_seconds(sum(a.seconds for a in asked))} | "
            f"right answer: {r.expected}, {_verdict_words(r.right)}"
        )
    tell()
    tell("Replies, through the agent's own judgement of a reply:")
    reply_right = []
    for _question, case in replies:
        wording = reply_intent.classify(case["subject"], case["body"]).replace("_", " ")
        asked = answers.about(case["id"])
        expected = "alert" if case["expected"] == "YES" else "no alert"
        if case["id"] in filed:
            right = case["expected"] != "YES"
            reply_right.append(right)
            tell(
                f"  {case['id']} | filed by the inbox as {filed[case['id']]}, the AI is "
                f"not asked | no alert | right answer: {expected}, {_verdict_words(right)}"
            )
            continue
        if case["id"] not in alerted:
            reply_right.append(None)
            tell(f"  {case['id']} | not judged: the run stopped | right answer: {expected}")
            continue
        alert = alerted[case["id"]]
        right = alert == (case["expected"] == "YES")
        reply_right.append(right)
        tell(
            f"  {case['id']} | wording: {wording} | AI: "
            f"{', '.join(_answer_words(a) for a in asked) or 'not asked'} | "
            f"{'a salesperson would be alerted' if alert else 'no alert'} | "
            f"{_seconds(sum(a.seconds for a in asked))} | right answer: {expected}, "
            f"{_verdict_words(right)}"
        )
    tell()
    _totals(settings, results, alerted, filed, reply_right, answers, run, guard.attempts, tell)

    if guard.attempts:
        tell()
        tell(
            "[FAILED] The run reached for the network beyond the AI server, and was "
            "refused: " + "; ".join(guard.attempts)
        )
        return 1
    if answers.stopped:
        tell()
        tell(f"[STOPPED] {answers.stopped}.")
        return 1
    return 0


def _totals(settings, results, alerted, filed, reply_right, answers, run, attempts, tell) -> None:
    kept = [r for r in results if r.kept]
    removed = [r for r in results if r.kept is False]
    by_ai = sum(1 for r in removed if r.outcome.startswith("removed by the AI"))
    italian = sum(1 for r in kept if r.language == "it")
    questions = answers.classifiers()
    answered = [a for a in questions if a.answer is not None]
    seconds = [a.seconds for a in questions]
    judged = [r.right for r in results if r.right is not None] + [
        x for x in reply_right if x is not None
    ]
    not_judged = len(results) + len(reply_right) - len(judged)
    tell("Totals:")
    tell(
        f"  Companies: {len(results)}; would be emailed {len(kept)} ({italian} in "
        f"Italian, {len(kept) - italian} in English); removed {len(removed)} "
        f"({len(removed) - by_ai} by the rules, {by_ai} by the AI)"
    )
    alerts = sum(1 for a in alerted.values() if a)
    line = f"  Replies: {len(alerted)} judged; a salesperson would be alerted for {alerts}"
    if filed:
        line += f"; {len(filed)} filed away by the inbox before any judgement"
    if not settings.get_lead_alert_recipients():
        line += " (LEAD_ALERT_EMAIL is blank: in the agent nobody would receive them)"
    tell(line)
    tell(
        f"  AI questions: {len(questions)} asked, {len(answered)} answered"
        + (
            f"; median {_seconds(statistics.median(seconds))}, slowest "
            f"{_seconds(max(seconds))}"
            if seconds
            else ""
        )
    )
    writing = [a for a in answers.asked if a.question == "writing"]
    if writing:
        tell(
            f"  Email texts asked of the AI (AI_HUMANIZE_EMAILS or "
            f"AI_PERSONALIZATION_ENABLED is on): {len(writing)}"
        )
    tell(
        f"  Agree with the cases' right answers: {sum(judged)} of {len(judged)}"
        + (
            f" ({not_judged} not judged: a country case says only the language of "
            "a company kept, and a case the run did not reach says nothing)"
            if not_judged
            else ""
        )
    )
    if attempts:
        tell(
            "  No email was sent, but the run reached for the network beyond the AI "
            "server: see below."
        )
    else:
        tell(
            f"  No email was sent, and nothing but the AI server ({run.server}) was "
            "contacted: the run's guard would have refused any other connection, "
            "and none was attempted."
        )


def _remove(work: Path) -> str | None:
    """Delete the work folder; what was left behind, or None."""
    try:
        shutil.rmtree(work)
    except OSError as e:
        return f"{work} ({e})"
    return None


def run_command(args, settings: Settings, say=print) -> int:
    """main.py ai-proof. Returns the exit status: 0 when every case went
    through, 1 when the run was refused, stopped, or reached for the
    network beyond the AI server."""
    # As the benchmark's report: a console that cannot print a letter gets
    # a ? rather than a crash.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        cases = ai_benchmark.load_cases(("relevance", "country", "reply"))
    except ai_benchmark.CaseFileError as e:
        say(f"[ERROR] {e}")
        return 1
    companies = pick(cases, COMPANY_GROUPS, args.companies)
    replies = pick(cases, REPLY_GROUPS, args.replies)

    # Checked before the run: found unusable afterwards, it cost the printed
    # result a traceback and left the run's database behind.
    out = Path(args.out) if args.out is not None else None
    if out is not None:
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            say(
                f"[ERROR] --out {out} cannot be made a folder ({e}): name a folder, "
                "new or existing. Nothing was run."
            )
            return 1

    if args.work_dir is not None:
        work = Path(args.work_dir).absolute()
        if work.exists():
            say(
                f"[ERROR] {work} already exists. The proof run works only in a new "
                "folder it creates and removes afterwards, so it can never open a "
                "database that is already there: name a folder that does not exist."
            )
            return 1
        try:
            work.mkdir()
        except OSError as e:
            say(
                f"[ERROR] {work} cannot be created ({e}): the folder above it, "
                f"{work.parent}, must exist and be writable. Nothing was run."
            )
            return 1
    else:
        work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX))

    lines: list[str] = []

    def tell(text: str = "") -> None:
        lines.append(text)
        say(text)

    tell("AI proof run: the whole agent with the configured AI, on invented cases")
    tell(f"  Work folder, removed at the end: {work}")
    not_copied = None
    try:
        status = _run(work, settings, companies, replies, tell)
    finally:
        # The copy never stands between the run and the folder's removal.
        if out is not None and (work / "agent.log").exists():
            try:
                shutil.copyfile(work / "agent.log", out / "agent.log")
            except OSError as e:
                not_copied = str(e)
        left = _remove(work)
    if left:
        tell(f"[ERROR] The work folder could not be removed: {left}. Delete it by hand.")
        status = 1
    if out is not None:
        try:
            (out / "proof.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as e:
            not_copied = (not_copied + "; " if not_copied else "") + str(e)
        if not_copied:
            say(f"[ERROR] Not everything could be written to {out}: {not_copied}")
            return 1
        say(f"Written: {out / 'proof.txt'} and {out / 'agent.log'}")
    return status


def count_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 0 <= value <= MAX_CASES:
        raise argparse.ArgumentTypeError(f"a whole number from 0 to {MAX_CASES}")
    return value
