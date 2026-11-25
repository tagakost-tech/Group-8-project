"""The AI benchmark: a fixed exam of the agent's own AI questions.

Nothing told the person choosing a model whether it answers the agent's
questions well enough to switch one on: the only evidence was an ad-hoc probe
of 17 cases. This asks the product's own public questions, through a
generator built from the settings as the agent builds it, about cases with
known right answers shipped in benchmark/cases, several times each, and
scores the answers by what a wrong one costs in this product, against a pass
mark per question (PASS_MARKS).

It writes nothing to the contacts database and sends no email. The shipped
cases are invented, so they are asked although no processing agreement is on
record; the company's own cases (--cases) may be real and are asked only when
AI_DPA_REFERENCE is set, as every other AI question is.
"""

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from modules import clock, reply_intent
from modules.ai_generator import (
    AIGenerator,
    model_listed,
    own_server_models,
    own_server_options,
)
from modules.company_country import stored_evidence
from modules.compliance import italian_countries
from modules.imap_monitor import ImapMonitor
from modules.relevance import RelevanceFilter

CASES_DIR = Path(__file__).resolve().parent.parent / "benchmark" / "cases"
QUESTIONS = ("relevance", "country", "reply", "queries")
DEFAULT_REPEAT = 3
MAX_REPEAT = 10
# The discovery loop asks for five queries a batch.
QUERY_COUNT = 5
# --quick asks the first this many clear cases of each category, in file order.
QUICK_CASES_PER_CATEGORY = 2
# A measure resting on fewer clear cases than this reads "not enough cases":
# with five cases one odd answer moves a share by 20 points.
MIN_CLEAR_CASES = 10
# A model whose first askings all get no answer is unreachable, refusing, or
# not answering in the form asked; scoring it would read as wrong answers.
FIRST_ASKINGS_WITHOUT_ANSWER = 3
# The queries question is scored only on how many proposals survive the
# product's supplier-word filter, so five lines of nonsense meet its mark;
# its verdict says that rather than PASS beside the classifiers'.
FORMAT_ONLY = "FORMAT OK"
# Asked once, a case's consistency cannot be measured; "not enough cases"
# would send the reader looking for cases when --repeat 2 is the fix.
ASKED_ONCE = "asked once: use --repeat 2"
# A share measured on a few dozen cases is uncertain: 2 wrong of 40 fits a
# true rate anywhere from about 1% to 17%. Each measure says so, as a 95%
# Wilson interval.
WILSON_Z = 1.96

# Each question's categories, and the answer each category must expect
# (None: the case states it). The category says what kind of case it is, so
# --blind leaves it out.
CATEGORIES = {
    "relevance": {
        "brand": "YES",
        "retailer": "YES",
        "competitor": "NO",
        "off_target": "NO",
        "non_european": "NO",
    },
    "country": {"italian": "IT", "foreign": None, "no_evidence": "UNKNOWN"},
    "reply": {
        "interested": "YES",
        "declined": "NO",
        "removal": "NO",
        "auto_reply": "NO",
        "neutral": "NO",
    },
    "queries": {"no_hint": None, "hint": None, "supplier_hint": None},
}
CLIENT_CATEGORIES = ("brand", "retailer")
NON_CLIENT_CATEGORIES = ("off_target", "non_european")
INPUTS = {
    "relevance": ("organization", "domain", "page_text"),
    "country": ("organization", "domain", "page_text"),
    "reply": ("subject", "body"),
    "queries": ("focus_hint",),
}
COMMON_FIELDS = ("id", "language", "category", "difficulty", "why", "expected")
# What the agent's inbox files a message as when it is no reply a person
# wrote: none of these reaches the judgement of a reply.
INBOX_WORDS = {
    "bounce": "a bounce",
    "soft_bounce": "a delivery failure to retry",
    "dsn_info": "a delivery report",
    "read_receipt": "a read receipt",
    "auto_reply": "an auto-reply",
    "unsubscribe": "an opt-out",
    "request_erasure": "a request for erasure",
    "request_access": "a request for access",
}
DIFFICULTIES = ("clear", "hard")

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
_LANGUAGE = re.compile(r"[a-z]{2}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_DOMAIN = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")
_ADDRESS_DOMAIN = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_URL_HOST = re.compile(r"(?:https?://|\bwww\.)([^\s/:?#]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Mark:
    """A pass mark: the share a measure may reach (at_most) or must reach."""

    limit: float
    at_most: bool
    reason: str

    def met(self, share: float) -> bool:
        return share <= self.limit if self.at_most else share >= self.limit

    def words(self) -> str:
        return f"{'at most' if self.at_most else 'at least'} {self.limit:.0%}"


# The proposed pass marks, for the client to agree; docs/DEPLOY.md section 11
# says how to change them. "*" is every question.
PASS_MARKS = {
    ("relevance", "wrongly_removed"): Mark(
        0.05,
        True,
        "a real client the AI removes is never emailed, and nobody sees it",
    ),
    ("relevance", "caught"): Mark(
        0.60,
        False,
        "under this the question sends company data out for little filtering",
    ),
    ("country", "language_right"): Mark(
        0.95,
        False,
        "the recipient reads the language chosen; an Italian email to someone "
        "who cannot read it is the worse mistake",
    ),
    ("country", "named_without_evidence"): Mark(
        0.10,
        True,
        "a guessed country is stored on the contact and never asked again",
    ),
    ("reply", "interest_missed"): Mark(
        0.10, True, "a missed interested reply is a lead nobody is alerted to"
    ),
    ("reply", "false_alerts"): Mark(
        0.20, True, "each false alert costs a person a read; many and alerts are ignored"
    ),
    ("queries", "kept"): Mark(
        0.80, False, "a proposal dropped as a supplier search is a search not made"
    ),
    ("*", "answered"): Mark(
        0.98,
        False,
        "an unanswered question takes the path without AI, so the switch does nothing",
    ),
    ("*", "consistency"): Mark(
        0.90,
        False,
        "a verdict that changes when the same case is asked again cannot be trusted",
    ),
}


def mark_for(question: str, key: str) -> Mark | None:
    return PASS_MARKS.get((question, key)) or PASS_MARKS.get(("*", key))


class CaseFileError(ValueError):
    """A case file the benchmark refuses, with the file and the case named."""


# ─── Cases ─────────────────────────────────────────────────────────────────


def _domains_in(text: str) -> list[str]:
    """Every address's and link's domain written in text."""
    return [
        m.group(1).lower().rstrip(".")
        for pattern in (_ADDRESS_DOMAIN, _URL_HOST)
        for m in pattern.finditer(text or "")
    ]


def _case_problem(question: str, case, invented: bool = True) -> str | None:
    """What makes case unusable for question, or None.

    invented: a shipped case, asked with no processing agreement on record,
    so every domain in it must end in .example. The company's own cases
    may be real, and are asked only with AI_DPA_REFERENCE set."""
    if not isinstance(case, dict):
        return "is not an object"
    wanted = (*COMMON_FIELDS, *INPUTS[question])
    missing = [f for f in wanted if f not in case]
    if missing:
        return f"is missing {', '.join(missing)}"
    extra = sorted(set(case) - set(wanted))
    if extra:
        return f"has fields this question does not read: {', '.join(extra)}"
    for name in ("id", "language", "category", "difficulty", "why", *INPUTS[question]):
        if not isinstance(case[name], str):
            return f"{name} is not text"
    if not _ID.fullmatch(case["id"]):
        return "id must be lower-case letters, digits, - and _ (48 at most)"
    if not _LANGUAGE.fullmatch(case["language"]):
        return "language must be a two-letter code in lower case (it, en, de)"
    if case["difficulty"] not in DIFFICULTIES:
        return "difficulty must be clear or hard"
    why = case["why"].strip()
    if not why or "\n" in why or len(why) > 200:
        return "why must be one line of at most 200 characters"
    categories = CATEGORIES[question]
    if case["category"] not in categories:
        return f"category must be one of {', '.join(categories)}"
    problem = _expected_problem(question, case)
    if problem:
        return problem
    if question in ("relevance", "country"):
        domain = case["domain"].strip().lower()
        if not _DOMAIN.fullmatch(domain):
            return f"domain {case['domain']!r} is not a domain name"
        if invented and not domain.endswith(".example"):
            return f"domain {case['domain']!r} does not end in .example"
        if not case["organization"].strip():
            return "organization is blank"
    if question == "reply" and not case["body"].strip():
        return "body is blank"
    if question == "queries" and (case["category"] == "no_hint") != (
        not case["focus_hint"].strip()
    ):
        return "focus_hint must be blank exactly when the category is no_hint"
    for name in INPUTS[question] if invented else ():
        for domain in _domains_in(case[name]):
            if not domain.endswith(".example"):
                return f"{name} names {domain}, which does not end in .example"
    return None


def _expected_problem(question: str, case: dict) -> str | None:
    """Why case's expected answer is one question cannot give, or None."""
    expected = case["expected"]
    if question == "queries":
        if not isinstance(expected, int) or isinstance(expected, bool):
            return "expected must be a whole number"
        if not 1 <= expected <= QUERY_COUNT:
            return f"expected must be from 1 to {QUERY_COUNT} usable queries"
        return None
    if not isinstance(expected, str):
        return "expected is not text"
    must = CATEGORIES[question][case["category"]]
    if must is not None:
        if expected != must:
            return f"expected must be {must} for category {case['category']}"
        return None
    # country, category foreign: a code the product stores as it is.
    if not _COUNTRY.fullmatch(expected) or expected in ("XX", "IT"):
        return "expected must be a two-letter country code in capitals, not IT"
    if expected == "UK":
        return "expected must be GB, as the product stores UK"
    return None


def load_case_file(path: Path, question: str, invented: bool = True) -> list[dict]:
    """The cases of one file, or CaseFileError; invented as _case_problem."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CaseFileError(f"{path.name}: cannot be read as JSON ({e})") from e
    if not isinstance(data, dict) or data.get("question") != question:
        raise CaseFileError(f"{path.name}: \"question\" must be {question!r}")
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        raise CaseFileError(f"{path.name}: \"description\" is missing")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CaseFileError(f"{path.name}: \"cases\" must be a list of cases")
    for number, case in enumerate(cases, 1):
        problem = _case_problem(question, case, invented)
        if problem:
            name = case.get("id") if isinstance(case, dict) else None
            raise CaseFileError(f"{path.name}, case {name or f'#{number}'}: {problem}")
    return cases


def load_cases(
    questions=QUESTIONS, own_folder: Path | None = None, shipped: Path | None = None
) -> dict[str, list[dict]]:
    """Each question's cases: the shipped file's (CASES_DIR), then the
    company's own (own_folder/<question>.json, where present), each marked
    with its source. An id used twice across both is refused, and so is an
    own folder holding no file for the questions asked: a mistyped --cases
    was skipped without a word, and the report covered the shipped cases
    only."""
    shipped = shipped or CASES_DIR
    if own_folder is not None:
        folder = Path(own_folder)
        names = [f"{question}.json" for question in questions]
        if not folder.is_dir():
            raise CaseFileError(f"--cases {folder}: the folder does not exist")
        if not any((folder / name).is_file() for name in names):
            raise CaseFileError(f"--cases {folder} holds none of {', '.join(names)}")
    loaded = {}
    for question in questions:
        cases = [
            {**c, "source": "shipped"}
            for c in load_case_file(shipped / f"{question}.json", question)
        ]
        own = Path(own_folder) / f"{question}.json" if own_folder else None
        if own is not None and own.exists():
            cases += [
                {**c, "source": "own"}
                for c in load_case_file(own, question, invented=False)
            ]
        seen = set()
        for case in cases:
            if case["id"] in seen:
                raise CaseFileError(
                    f"{question}.json, case {case['id']}: the id is used twice"
                )
            seen.add(case["id"])
        loaded[question] = cases
    return loaded


def quick_subset(cases: list[dict]) -> list[dict]:
    """The first QUICK_CASES_PER_CATEGORY clear cases of each category, in
    file order: the same subset every time. The shipped cases and the
    company's own are taken apart, since the shipped ones come first and
    filled every category, so --quick --cases asked none of the own."""
    taken = {}
    chosen = []
    for case in cases:
        if case["difficulty"] != "clear":
            continue
        group = (case.get("source"), case["category"])
        if taken.get(group, 0) < QUICK_CASES_PER_CATEGORY:
            taken[group] = taken.get(group, 0) + 1
            chosen.append(case)
    return chosen


def blind(question: str, cases: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """The cases as a label checker sees them, and the key from each case's
    number back to its id.

    Only a neutral number, the language and the inputs are shown. Every id
    names its category (rep-interested-01) and the category fixes the
    answer, so the id is replaced; the difficulty is left out as well. The
    order is shuffled by a hash of the ids, since the files group cases by
    kind: the same files give the same numbers every time."""
    order = sorted(cases, key=lambda c: hashlib.sha256(c["id"].encode("utf-8")).hexdigest())
    shown, key = [], {}
    for number, case in enumerate(order, 1):
        label = f"{question}-{number:03d}"
        key[label] = case["id"]
        shown.append(
            {"case": label, "language": case["language"]}
            | {name: case[name] for name in INPUTS[question]}
        )
    return shown, key


def _contact(case: dict) -> dict:
    """The contact the agent would hold for a relevance or country case."""
    return {
        "organization": case["organization"],
        "email": f"office@{case['domain']}",
        "qualification_rationale": case["page_text"],
        "source_channel": "own_website",
        "source_url": f"https://{case['domain']}/",
    }


def _as_found(case: dict) -> dict:
    """The case as the agent would find the company: a shipped domain is a
    real-looking one with .example added so that it can never be anyone's,
    and the agent's gates read the real one. vidacerrado.com.br.example
    passed the gate as shipped while vidacerrado.com.br is removed as
    non-European before the AI is asked."""
    return {**case, "domain": case["domain"].removesuffix(".example")}


def inbox_files_as(subject: str, body: str) -> str | None:
    """What the agent's inbox files a message as, in its own steps and
    order (ImapMonitor), when it is not a reply a person wrote; None for one
    that is, the only kind the agent's judgement of a reply ever sees. A case
    holds no sender and no headers, so a delivery failure is known by its
    subject, as the inbox knows one when it has no message to read."""
    inbox = ImapMonitor(None, "", 0, "", "")
    kind = inbox._classify_email("", subject, body)
    if kind in ("reply", "bounce"):
        message = Message()
        message["Subject"] = subject
        if inbox._is_read_receipt(message, subject):
            kind = "read_receipt"
        elif kind == "reply" and inbox._is_auto_reply(message, subject, body):
            kind = "auto_reply"
    return None if kind == "reply" else INBOX_WORDS.get(kind, kind)


def decided_without_ai(question: str, case: dict, settings) -> str | None:
    """Why the product would settle this case before the AI is asked, or None.

    Such a case never reaches the AI in the agent, so it says little about
    a model; --check counts them."""
    if question in ("relevance", "country"):
        case = _as_found(case)
    if question == "reply":
        filed = inbox_files_as(case["subject"], case["body"])
        if filed:
            return f"filed by the inbox as {filed}"
        if reply_intent.is_optout(case["subject"], case["body"]):
            return "an opt-out by its wording"
        verdict = reply_intent.classify(case["subject"], case["body"])
        if verdict != reply_intent.UNCLEAR:
            return f"{verdict.replace('_', ' ')} by its wording"
    if question == "relevance":
        gate = RelevanceFilter(
            verticals=settings.get_relevance_verticals(),
            require_vertical=settings.relevance_require_vertical,
        )
        assessment = gate.assess(_contact(case))
        if not assessment["relevant"]:
            return f"removed by the relevance gate ({assessment['reason']})"
    if question == "country":
        country, _ = stored_evidence(_contact(case))
        if country:
            return f"{country} from its own page"
    return None


# ─── Asking ────────────────────────────────────────────────────────────────


@dataclass
class Asking:
    """One question asked once: the answer as the product read it (YES/NO,
    a country code or UNKNOWN, the number of queries kept, or None for no
    answer), how long it took, and whether the server was silent (timed out
    or unreachable), which is never scored as a wrong answer."""

    case_id: str
    answer: object
    seconds: float
    silent: bool = False
    late: bool = False


@dataclass
class ModelRun:
    provider: str
    model: str
    server: str
    reasoning: str
    timeout: str
    askings: dict = field(default_factory=dict)
    finished: list = field(default_factory=list)
    stopped: str = ""


def build_generator(settings, *, invented_only: bool, llm_model: str | None = None):
    """The generator the agent builds (agent.py), from the same settings.

    invented_only: every case to be asked is a shipped, invented one, so
    the exam runs although no processing agreement is on record. That is
    this generator's only difference, and it is not the agent's."""
    options = own_server_options(settings)
    if llm_model:
        options["llm_model"] = llm_model
    return AIGenerator(
        **options,
        personal_data_allowed=invented_only
        or bool((settings.ai_dpa_reference or "").strip()),
    )


def _ask(ai, question: str, case: dict):
    """The product's own question and its answer, read as the product reads
    it; the reply goes without its quote, as the agent sends it."""
    if question == "relevance":
        return {True: "YES", False: "NO"}.get(ai.classify_relevance(_contact(case)))
    if question == "country":
        answer = ai.classify_company_country(_contact(case))
        return None if answer is None else (answer or "UNKNOWN")
    if question == "reply":
        verdict = ai.classify_reply_intent(
            case["subject"], reply_intent.strip_quoted(case["body"])
        )
        return {True: "YES", False: "NO"}.get(verdict)
    return len(ai.suggest_discovery_queries(count=QUERY_COUNT, focus_hint=case["focus_hint"]))


def _usable(answer) -> bool:
    return bool(answer) if isinstance(answer, int) else answer is not None


def _server(ai) -> str:
    parts = urlsplit(ai.base_url or "")
    return parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl()


def new_run(ai) -> ModelRun:
    return ModelRun(
        provider=ai.provider,
        model=ai.MODEL_NAME,
        server=_server(ai),
        reasoning=ai.reasoning_effort or "not sent",
        timeout=f"{ai.TIMEOUT_SECONDS:g} s",
    )


def run_exam(ai, cases: dict, repeat: int, say=print) -> ModelRun:
    """Ask every case repeat times, round after round, and stop at the
    first sign that the server is silent rather than wrong."""
    run = new_run(ai)
    asked = 0
    for question, question_cases in cases.items():
        run.askings[question] = []
        say(f"  {run.model}: {question}, {len(question_cases)} cases x {repeat}")
        for _ in range(repeat):
            for case in question_cases:
                before = ai.stalls_in_a_row
                started = time.monotonic()
                answer = _ask(ai, question, case)
                took = time.monotonic() - started
                late = not _usable(answer) and took >= ai.TIMEOUT_SECONDS * 0.99
                silent = late or ai.stalls_in_a_row > before
                run.askings[question].append(
                    Asking(case["id"], answer, round(took, 3), silent, late)
                )
                asked += 1
                if ai.paused:
                    run.stopped = (
                        f"the server gave no answer {ai.stalls_in_a_row} times in "
                        f"a row, and the agent pauses AI questions for "
                        f"{ai.STALL_PAUSE_SECONDS / 60:g} minutes after "
                        f"{ai.STALL_LIMIT}: this model was asked nothing more"
                    )
                    return run
                if asked == FIRST_ASKINGS_WITHOUT_ANSWER and not any(
                    _usable(a.answer) for q in run.askings.values() for a in q
                ):
                    run.stopped = (
                        f"the first {asked} questions got no answer at all: the "
                        "AI is unreachable, refusing, or not answering in the form "
                        "asked; nothing was scored"
                    )
                    return run
        run.finished.append(question)
    return run


def server_problem(ai, model: str) -> str | None:
    """Why the own server cannot be examined on model, or None: one list
    request, as doctor makes, so an unreachable server is never scored."""
    try:
        models = own_server_models(ai.base_url, ai.api_key)
    except (httpx.HTTPError, ValueError, TypeError) as e:
        return f"the server at {_server(ai)} did not answer its list of models ({e})"
    if not model_listed(model, models):
        return f"the server does not list {model} (it lists: {', '.join(models[:5]) or 'none'})"
    return None


# ─── Scoring ───────────────────────────────────────────────────────────────


@dataclass
class Measure:
    key: str
    label: str
    hits: int
    total: int
    cases: int
    mark: Mark | None = None
    note: str = ""
    # What total counts: one per asking, or for the queries five proposals
    # an asking.
    unit: str = "askings"

    @property
    def enough(self) -> bool:
        return self.total > 0 and self.cases >= MIN_CLEAR_CASES

    @property
    def result(self) -> str:
        """The count and share, or why there is none."""
        if not self.total and self.note:
            return f"not measured: {self.note}"
        text = share(self.hits, self.total)
        bounds = plausible_range(self.hits, self.total, self.cases)
        if bounds:
            text += f"; plausible {bounds[0]}% to {bounds[1]}%"
        return text + (f" ({self.note})" if self.note else "")

    @property
    def status(self) -> str:
        if self.mark is None:
            return ""
        if not self.total and self.note:
            return "not measured"
        if not self.enough:
            return "not enough cases"
        return "ok" if self.mark.met(self.hits / self.total) else "FAILS"

    def wrong_to_fail(self, count: int) -> int | None:
        """The fewest wrong ones of count, cases or askings, that fail the
        mark when the rest are right, or None when no number does. A hit is a
        wrong one under an "at most" mark and a right one under "at least"."""
        for wrong in range(1, count + 1):
            hits = wrong if self.mark.at_most else count - wrong
            if not self.mark.met(hits / count):
                return wrong
        return None

    @property
    def strength(self) -> str:
        """How many clear cases stand behind a marked measure and how few
        wrong ones would fail it: a PASS on 20 cases that two more wrong
        cases would turn into a FAIL is weaker than it reads."""
        if self.mark is None or not self.total:
            return "-"
        text = f"{self.cases} clear case{'s' if self.cases != 1 else ''}"
        cases = self.wrong_to_fail(self.cases)
        if cases is None:
            return text + "; no number of wrong cases fails it"
        text += f"; {cases} wrong " + ("case fails it" if cases == 1 else "cases fail it")
        if self.total != self.cases:
            askings = self.wrong_to_fail(self.total)
            text += f" ({askings} wrong of its {self.total} {self.unit})"
        return text


def share(hits: int, total: int) -> str:
    """"3 of 75, 4%": a count always goes with its percentage."""
    if not total:
        return "none asked"
    return f"{hits} of {total}, {round(100 * hits / total)}%"


def plausible_range(hits: int, total: int, cases: int) -> tuple[int, int] | None:
    """Where the true share probably lies, in whole percent: a 95% Wilson
    interval around hits of total, counted over the cases rather than the
    askings. The askings of one case are not independent, so counting each
    of them would claim more certainty than the exam has."""
    if not total or not cases:
        return None
    p = hits / total
    z2 = WILSON_Z**2
    denominator = 1 + z2 / cases
    centre = (p + z2 / (2 * cases)) / denominator
    margin = WILSON_Z * math.sqrt(p * (1 - p) / cases + z2 / (4 * cases**2)) / denominator
    return round(100 * max(0.0, centre - margin)), round(100 * min(1.0, centre + margin))


def case_fingerprint(cases: dict) -> str:
    """A short hash of the cases asked: two reports with the same
    fingerprint examined the same cases, whatever the files' line endings."""
    text = json.dumps(cases, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def right(question: str, case: dict, answer) -> bool:
    """Whether the product would act rightly on answer: no answer keeps a
    company and raises no alert, as it does in the agent."""
    expected = case["expected"]
    if question == "relevance":
        return (answer == "NO") == (expected == "NO")
    if question == "reply":
        return (answer == "YES") == (expected == "YES")
    if question == "country":
        return answer == expected
    return isinstance(answer, int) and answer >= expected


@dataclass
class QuestionScore:
    question: str
    verdict: str
    measures: list
    median_seconds: float | None
    slowest_seconds: float | None
    late: int
    silent: int
    askings: int
    hard: str
    worst: list
    unstable: list = field(default_factory=list)
    by_language: list = field(default_factory=list)


def _measure(question, key, label, askings, by_id, hit, note=""):
    return Measure(
        key,
        label,
        sum(1 for a in askings if hit(a, by_id[a.case_id])),
        len(askings),
        len({a.case_id for a in askings}),
        mark_for(question, key),
        note,
    )


def score_question(
    question: str, cases: list[dict], askings: list[Asking], italian: set, finished: bool
) -> QuestionScore:
    by_id = {c["id"]: c for c in cases}
    clear = [a for a in askings if by_id[a.case_id]["difficulty"] == "clear"]
    scored = [a for a in clear if not a.silent]

    def of(categories):
        return [a for a in scored if by_id[a.case_id]["category"] in categories]

    measures = []
    if question == "relevance":
        measures += [
            _measure(
                question,
                "wrongly_removed",
                "Real clients wrongly removed",
                of(CLIENT_CATEGORIES),
                by_id,
                lambda a, c: a.answer == "NO",
            ),
            _measure(
                question,
                "caught",
                "Non-clients caught",
                of(NON_CLIENT_CATEGORIES),
                by_id,
                lambda a, c: a.answer == "NO",
            ),
            _measure(
                question,
                "competitors",
                "Contract manufacturers (competitors) removed",
                of(("competitor",)),
                by_id,
                lambda a, c: a.answer == "NO",
            ),
        ]
    elif question == "country":
        measures += [
            _measure(
                question,
                "language_right",
                "Language decision right",
                scored,
                by_id,
                lambda a, c: (a.answer in italian) == (c["expected"] in italian),
            ),
            _measure(
                question,
                "named_without_evidence",
                "Named a country where nothing shows one",
                of(("no_evidence",)),
                by_id,
                lambda a, c: a.answer not in (None, "UNKNOWN"),
            ),
            _measure(
                question,
                "exact",
                "Exact country right",
                scored,
                by_id,
                lambda a, c: a.answer == c["expected"],
            ),
        ]
    elif question == "reply":
        measures += [
            _measure(
                question,
                "interest_missed",
                "Interest missed",
                of(("interested",)),
                by_id,
                lambda a, c: a.answer != "YES",
            ),
            _measure(
                question,
                "false_alerts",
                "False alerts",
                [a for a in scored if by_id[a.case_id]["expected"] == "NO"],
                by_id,
                lambda a, c: a.answer == "YES",
            ),
        ]
    else:
        kept = Measure(
            "kept",
            f"Proposals kept by the product's filters (of {QUERY_COUNT} an asking)",
            sum(a.answer or 0 for a in scored),
            QUERY_COUNT * len(scored),
            len({a.case_id for a in scored}),
            mark_for(question, "kept"),
            unit="proposals",
        )
        measures.append(kept)
    measures.append(
        _measure(
            question,
            "answered",
            "Answered (a usable answer)",
            clear,
            by_id,
            lambda a, c: _usable(a.answer),
        )
    )
    if question != "queries":
        answers = {}
        for a in scored:
            answers.setdefault(a.case_id, []).append(a.answer)
        repeated = {cid: v for cid, v in answers.items() if len(v) > 1}
        measures.append(
            Measure(
                "consistency",
                "Consistency (same answer on every asking)",
                sum(1 for v in repeated.values() if len(set(v)) == 1),
                len(repeated),
                len(repeated),
                mark_for(question, "consistency"),
                "" if repeated else "each case asked once; use --repeat 2 or more",
            )
        )

    marked = [m for m in measures if m.mark is not None]
    if not finished:
        verdict = "INCOMPLETE"
    elif any(m.status == "FAILS" for m in marked):
        verdict = "FAIL"
    elif any(m.status == "not enough cases" for m in marked):
        verdict = "not enough cases"
    elif any(m.status == "not measured" for m in marked):
        verdict = ASKED_ONCE
    elif question == "queries":
        verdict = FORMAT_ONLY
    else:
        verdict = "PASS"

    seconds = [a.seconds for a in askings]
    hard = [
        a for a in askings if by_id[a.case_id]["difficulty"] == "hard" and not a.silent
    ]
    hard_right = sum(1 for a in hard if right(question, by_id[a.case_id], a.answer))
    return QuestionScore(
        question=question,
        verdict=verdict,
        measures=measures,
        median_seconds=statistics.median(seconds) if seconds else None,
        slowest_seconds=max(seconds) if seconds else None,
        late=sum(1 for a in askings if a.late),
        silent=sum(1 for a in askings if a.silent),
        askings=len(askings),
        hard=share(hard_right, len(hard)) + " right" if hard else "none asked",
        worst=_worst(question, by_id, askings),
        unstable=[] if question == "queries" else _unstable(cases, askings),
        by_language=_by_language(question, by_id, scored),
    )


def _by_language(question: str, by_id: dict, askings: list[Asking]) -> list:
    """(language, right, asked) over the clear askings, most asked first: a
    model that does well in English and badly in Polish answers the agent's
    Polish companies badly, which one share over all languages hides."""
    counts = {}
    for a in askings:
        case = by_id[a.case_id]
        tally = counts.setdefault(case["language"], [0, 0])
        tally[0] += right(question, case, a.answer)
        tally[1] += 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1][1], item[0]))
    return [(language, hits, total) for language, (hits, total) in ranked]


def _unstable(cases: list[dict], askings: list[Asking]) -> list[str]:
    """Every case, clear or hard, given more than one answer across its
    askings, in file order: where the question's prompt leaves a model
    unsure. The worst-case list shows only five."""
    answers = {}
    for a in askings:
        if not a.silent:
            answers.setdefault(a.case_id, set()).add(a.answer)
    return [c["id"] for c in cases if len(answers.get(c["id"], ())) > 1]


def _worst(question: str, by_id: dict, askings: list[Asking], count: int = 5) -> list:
    """The cases answered wrong most often, then least consistently."""
    per_case = {}
    for a in askings:
        if not a.silent:
            per_case.setdefault(a.case_id, []).append(a.answer)
    ranked = []
    for case_id, answers in per_case.items():
        case = by_id[case_id]
        wrong = sum(1 for x in answers if not right(question, case, x))
        if wrong or len(set(answers)) > 1:
            ranked.append((wrong, len(set(answers)), case_id, answers))
    ranked.sort(key=lambda r: (-r[0], -r[1], r[2]))
    return [
        {
            "id": case_id,
            "difficulty": by_id[case_id]["difficulty"],
            "expected": by_id[case_id]["expected"],
            "answers": answers,
            "wrong": wrong,
        }
        for wrong, _, case_id, answers in ranked[:count]
    ]


def score_run(run: ModelRun, cases: dict, settings) -> dict:
    italian = italian_countries(settings)
    return {
        q: score_question(q, cases[q], run.askings.get(q, []), italian, q in run.finished)
        for q in cases
    }


# ─── Report ────────────────────────────────────────────────────────────────


def _answer_words(answer) -> str:
    if answer is None:
        return "no answer"
    return f"{answer} kept" if isinstance(answer, int) else str(answer)


def _seconds(value) -> str:
    return "-" if value is None else f"{value:.1f}"


def report(runs: list, scores: list, cases: dict, repeat: int, own: Path | None) -> str:
    """The report a non-developer reads, as Markdown."""
    counts = ", ".join(f"{q} {len(c)}" for q, c in cases.items())
    first = runs[0]
    lines = [
        "# AI benchmark",
        "",
        f"- Date: {clock.now().strftime('%Y-%m-%d %H:%M')}",
        f"- Provider: {first.provider}",
        f"- Model: {', '.join(r.model for r in runs)}",
        f"- Server: {first.server}",
        f"- Reasoning setting: {first.reasoning}",
        f"- Timeout per question: {first.timeout}",
        (
            f"- Temperature: {AIGenerator.CLASSIFIER_TEMPERATURE:g} for relevance, "
            f"country and reply; {AIGenerator.WRITING_TEMPERATURE:g} for discovery "
            "queries, as the agent asks them"
        ),
        f"- Each case asked: {repeat} time{'s' if repeat > 1 else ''}",
        f"- Cases: {counts}. The shipped cases are invented companies, people "
        "and replies."
        + (f" The company's own cases from {own} are included." if own else ""),
        (
            f"- Cases fingerprint: {case_fingerprint(cases)} (the same cases give "
            "the same fingerprint, so two reports can be compared)"
        ),
        (
            "- Plausible: where the true share probably lies (95%), counting each "
            "case once, since the askings of one case are not independent. A "
            "verdict reads the share itself."
        ),
        (
            "- The pass marks are a proposal for the client to agree "
            "(docs/DEPLOY.md section 11). A result holds for this model with this "
            "reasoning setting and this timeout only: run it again after any change."
        ),
        "",
    ]
    for run, score in zip(runs, scores, strict=True):
        lines += _model_section(run, score, len(runs) > 1)
    if len(runs) > 1:
        lines += _comparison(runs, scores)
    return "\n".join(lines) + "\n"


def _model_section(run: ModelRun, score: dict, titled: bool) -> list:
    lines = [f"## {run.model}", ""] if titled else []
    if run.stopped:
        lines += [f"**Run incomplete:** {run.stopped}.", ""]
    lines += [
        "| Question | Verdict | Answered | Consistency | Median s | Slowest s |",
        "|---|---|---|---|---|---|",
    ]
    for q, s in score.items():
        found = {m.key: m for m in s.measures}
        consistency = found.get("consistency")
        same = "not measured"
        if consistency is not None and consistency.total:
            same = share(consistency.hits, consistency.total)
        lines.append(
            f"| {q} | {s.verdict} | {found['answered'].result} | {same} "
            f"| {_seconds(s.median_seconds)} | {_seconds(s.slowest_seconds)} |"
        )
    lines.append("")
    for q, s in score.items():
        lines += [
            f"### {q}: {s.verdict}",
            "",
            "| Measure | Result | Pass mark | Status | Cases behind it | Why this mark |",
            "|---|---|---|---|---|---|",
        ]
        for m in s.measures:
            lines.append(
                f"| {m.label} | {m.result} | {m.mark.words() if m.mark else '-'} "
                f"| {m.status or '-'} | {m.strength} "
                f"| {m.mark.reason if m.mark else 'reported only'} |"
            )
        lines += [
            "",
            (
                f"- Seconds per asking: median {_seconds(s.median_seconds)}, slowest "
                f"{_seconds(s.slowest_seconds)}; answers that came too late: "
                f"{share(s.late, s.askings)}; askings the server was silent on (not "
                f"scored): {share(s.silent, s.askings)}"
            ),
            f"- Hard cases (not counted toward the verdict): {s.hard}",
        ]
        if s.by_language:
            lines.append(
                "- Right by language (clear cases, each asking): "
                + "; ".join(f"{lang} {share(hits, total)}" for lang, hits, total in s.by_language)
            )
        if s.unstable:
            lines.append(
                f"- Answered differently across askings ({len(s.unstable)}): "
                + ", ".join(s.unstable)
            )
        if s.worst:
            lines.append("- Worst cases:")
            for w in s.worst:
                answers = ", ".join(_answer_words(x) for x in w["answers"])
                hard = " (hard)" if w["difficulty"] == "hard" else ""
                lines.append(
                    f"  - {w['id']}{hard}: expected {w['expected']}; answered {answers}"
                )
        lines.append("")
    return lines


def _comparison(runs: list, scores: list) -> list:
    questions = list(scores[0])
    lines = [
        "## Comparison",
        "",
        "| Model | " + " | ".join(questions) + " | Median s | Run |",
        "|---|" + "---|" * len(questions) + "---|---|",
    ]
    for run, score in zip(runs, scores, strict=True):
        seconds = [a.seconds for q in run.askings.values() for a in q]
        lines.append(
            f"| {run.model} | "
            + " | ".join(score[q].verdict for q in questions)
            + f" | {_seconds(statistics.median(seconds) if seconds else None)} | "
            + ("incomplete" if run.stopped else "complete")
            + " |"
        )
    return lines + [""]


def results_json(runs: list, scores: list, repeat: int) -> dict:
    """Every asking and every measure, for whoever compares runs later."""
    return {
        "date": clock.now().isoformat(),
        "repeat": repeat,
        "cases_invented": "the shipped cases are invented",
        "models": [
            {
                "provider": run.provider,
                "model": run.model,
                "server": run.server,
                "reasoning": run.reasoning,
                "timeout": run.timeout,
                "stopped": run.stopped,
                "questions": {
                    q: {
                        "verdict": s.verdict,
                        "measures": [
                            {
                                "key": m.key,
                                "label": m.label,
                                "hits": m.hits,
                                "total": m.total,
                                "cases": m.cases,
                                "mark": m.mark.words() if m.mark else None,
                                "status": m.status,
                                "plausible": plausible_range(m.hits, m.total, m.cases),
                            }
                            for m in s.measures
                        ],
                        "worst": s.worst,
                        "unstable": s.unstable,
                        "askings": [a.__dict__ for a in run.askings.get(q, [])],
                    }
                    for q, s in score.items()
                },
            }
            for run, score in zip(runs, scores, strict=True)
        ],
    }


# ─── The command ───────────────────────────────────────────────────────────


def check_report(cases: dict, settings) -> str:
    """--check: what the case files hold, and which cases the product
    settles before any AI is asked."""
    lines = ["Case files are valid."]
    for question, question_cases in cases.items():
        clear = sum(1 for c in question_cases if c["difficulty"] == "clear")
        by_category = {}
        for c in question_cases:
            by_category[c["category"]] = by_category.get(c["category"], 0) + 1
        own = sum(1 for c in question_cases if c["source"] == "own")
        lines.append(
            f"{question}: {len(question_cases)} cases ({clear} clear, "
            f"{len(question_cases) - clear} hard"
            + (f", {own} the company's own" if own else "")
            + "); "
            + ", ".join(f"{k} {v}" for k, v in by_category.items())
        )
        settled = [
            (c["id"], why)
            for c in question_cases
            if (why := decided_without_ai(question, c, settings))
        ]
        if settled:
            lines.append(
                f"  {share(len(settled), len(question_cases))} are settled by the "
                "product before the AI is asked, so in the agent they never reach it:"
            )
            lines += [f"    {cid}: {why}" for cid, why in settled]
        elif question == "reply":
            lines.append(
                "  none is filed away by the inbox or settled by the wording rules"
            )
    return "\n".join(lines)


def not_configured(settings) -> str:
    """The line for an AI that is not configured: the only AI is the model
    on the company's own server, and LLM_PROVIDER blank means none."""
    return (
        "[ERROR] The AI is off: LLM_PROVIDER is not set. For the model on the "
        "company's own server set LLM_PROVIDER=openai_compatible, LLM_BASE_URL "
        "and LLM_MODEL (doctor shows which)."
    )


def run_command(args, settings, say=print) -> int:
    """main.py ai-benchmark. Returns the exit status: 1 when the cases are
    refused, the AI cannot be asked, or any model's run stopped before its
    end (a server down, a model it does not list, the pause), so that a
    script can tell an examined model from one that was never asked; 0
    otherwise, whatever the verdicts."""
    # As doctor's output, through setup_logging: a console that cannot print
    # a letter gets a ? rather than a crash.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    questions = list(args.questions)
    if args.blind:
        questions = [args.blind]
    try:
        cases = load_cases(questions, args.cases)
    except CaseFileError as e:
        say(f"[ERROR] {e}")
        return 1
    if args.blind:
        shown, key = blind(args.blind, cases[args.blind])
        if not args.out:
            say(json.dumps(shown, indent=2, ensure_ascii=True))
            return 0
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        cases_file, key_file = out / f"blind-{args.blind}.json", out / f"key-{args.blind}.json"
        cases_file.write_text(json.dumps(shown, indent=2, ensure_ascii=False), encoding="utf-8")
        key_file.write_text(json.dumps(key, indent=2), encoding="utf-8")
        say(
            f"Written: {cases_file} for the reader, and {key_file}, which turns each "
            "number back into its case id: keep it from the reader."
        )
        return 0
    if args.check:
        say(check_report(cases, settings))
        return 0
    if args.quick:
        cases = {q: quick_subset(c) for q, c in cases.items()}

    own = any(c["source"] == "own" for qc in cases.values() for c in qc)
    if own and not (settings.ai_dpa_reference or "").strip():
        say(
            "[ERROR] The company's own cases (--cases) may describe real companies "
            "and people, so they are asked only as the agent asks: set "
            "AI_DPA_REFERENCE to where the AI provider's processing agreement is "
            "filed (for a model on the company's own server, a note such as 'own "
            "server, no outside processor'), or run without --cases."
        )
        return 1
    if args.models and settings.llm_provider != "openai_compatible":
        say(
            "[ERROR] --models compares models on the company's own server: set "
            "LLM_PROVIDER=openai_compatible, LLM_BASE_URL and LLM_MODEL first."
        )
        return 1

    models = list(args.models or [None])
    runs, scores = [], []
    for model in models:
        ai = build_generator(settings, invented_only=not own, llm_model=model)
        if not ai.is_configured:
            say(not_configured(settings))
            return 1
        problem = server_problem(ai, ai.MODEL_NAME)
        if problem:
            run = new_run(ai)
            run.stopped = problem
        else:
            run = run_exam(ai, cases, args.repeat, say)
        runs.append(run)
        scores.append(score_run(run, cases, settings))

    text = report(runs, scores, cases, args.repeat, args.cases if own else None)
    say(text)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(text, encoding="utf-8")
        (out / "results.json").write_text(
            json.dumps(results_json(runs, scores, args.repeat), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        say(f"Written: {out / 'report.md'} and {out / 'results.json'}")
    return 1 if any(run.stopped for run in runs) else 0


# ─── Arguments ─────────────────────────────────────────────────────────────


def questions_arg(text: str) -> list[str]:
    names = [n.strip() for n in text.split(",") if n.strip()]
    unknown = [n for n in names if n not in QUESTIONS]
    if unknown or not names:
        raise argparse.ArgumentTypeError(
            f"questions are {', '.join(QUESTIONS)}, separated by commas"
        )
    return list(dict.fromkeys(names))


def repeat_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_REPEAT:
        raise argparse.ArgumentTypeError(f"--repeat is a whole number from 1 to {MAX_REPEAT}")
    return value


def models_arg(text: str) -> list[str]:
    names = list(dict.fromkeys(n.strip() for n in text.split(",") if n.strip()))
    if not names:
        raise argparse.ArgumentTypeError("--models names one model or more, separated by commas")
    return names
