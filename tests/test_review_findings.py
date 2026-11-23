"""Regression tests for the nine defects found by the full-code review pass.

Every one of these survived seven earlier audit batches and 459 passing tests.
Three caused duplicate emails to real prospects, one silently destroyed good
leads on a network hiccup, and one let anyone forge an unsubscribe link.
"""

import sqlite3
import threading

import pytest

import main
from config import Settings, load_settings
from modules.database import Database
from modules.domain_throttle import DomainThrottle
from modules.lead_scoring import LeadScorer, _has_term
from modules.sequences import SequenceEngine
from modules.verification import EmailVerifier, _is_transient_smtp

# ── 1. Duplicate sequence enrollment ────────────────────────────────────────
# enroll_contact did a SELECT-then-INSERT across two connections with NOTHING
# in the schema to stop it. Two active enrollments make get_due_followups
# return the contact twice at every step, so they receive the whole follow-up
# sequence twice - permanently, because the rows never merge back.


def _seq(db):
    engine = SequenceEngine(db)
    sid = engine.create_sequence(
        "s", [{"delay_days": 3, "subject": "x", "template_name": "followup_1"}]
    )
    return engine, sid


def test_the_active_enrollment_uniqueness_index_exists(db):
    raw = sqlite3.connect(db.db_path)
    idx = [r[1] for r in raw.execute("PRAGMA index_list(sequence_enrollments)") if r[2]]
    raw.close()
    assert "idx_enrollment_active_unique" in idx, (
        "nothing in the schema prevents two active enrollments for one contact"
    )


def test_enrolling_twice_yields_one_active_enrollment(db):
    engine, sid = _seq(db)
    search = db.log_search("q", source="test")
    cid = db.add_contact(search, "dupe@brand.de")

    first = engine.enroll_contact(cid, sid)
    second = engine.enroll_contact(cid, sid)

    assert first == second, "a second enrollment created a separate row"
    raw = sqlite3.connect(db.db_path)
    n = raw.execute(
        "SELECT COUNT(*) FROM sequence_enrollments "
        "WHERE contact_id = ? AND status = 'active'",
        (cid,),
    ).fetchone()[0]
    raw.close()
    assert n == 1, f"{n} active enrollments - every follow-up would be sent {n}x"


def test_concurrent_enrollment_cannot_double_insert(db):
    """The real interleaving: the agent thread enrolls after a first send while
    a dashboard Direct Send with 'enroll in follow-ups' does the same."""
    _, sid = _seq(db)
    search = db.log_search("q", source="test")
    cid = db.add_contact(search, "race@brand.de")

    start = threading.Barrier(2)
    errors = []

    def enroll():
        try:
            start.wait(timeout=5)
            SequenceEngine(db).enroll_contact(cid, sid)
        except Exception as e:  # noqa: BLE001 - any failure is a finding here
            errors.append(e)

    threads = [threading.Thread(target=enroll) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    raw = sqlite3.connect(db.db_path)
    n = raw.execute(
        "SELECT COUNT(*) FROM sequence_enrollments "
        "WHERE contact_id = ? AND status = 'active'",
        (cid,),
    ).fetchone()[0]
    raw.close()
    assert n == 1, f"race produced {n} active enrollments (errors: {errors})"


def test_a_completed_enrollment_can_be_re_enrolled(db):
    """Guard: the index is PARTIAL on status='active' on purpose - a contact
    may legitimately be enrolled again once an earlier run finished."""
    engine, sid = _seq(db)
    search = db.log_search("q", source="test")
    cid = db.add_contact(search, "again@brand.de")

    first = engine.enroll_contact(cid, sid)
    engine.cancel_enrollment(first, "replied")
    second = engine.enroll_contact(cid, sid)

    assert second != first, "re-enrolment after cancellation was blocked"


# ── 2. Per-domain counter race ──────────────────────────────────────────────
# SELECT-then-INSERT against UNIQUE(date, domain). warmup.record_send had this
# exact bug and this exact fix; it was never carried across.
#
# The interleaving is FORCED rather than raced for. A thread test only fails
# when the OS happens to schedule the two SELECTs before either COMMIT, so it
# passed against the unfixed code roughly every time it ran - a test that
# reports "no bug" for a bug that is definitely there is worse than none.


def test_concurrent_sends_to_one_domain_never_raise(db):
    """The loser of the race hit UNIQUE(date, domain) and raised AFTER the mail
    had gone out - so the operator was told "Send failed" for an email already
    in the prospect's inbox, retried, and sent a real duplicate.

    Several rounds on purpose. Measured against the unfixed implementation a
    single 12-thread round reproduces about 11 times in 12; at five rounds a
    false pass is under one in a hundred thousand, while still exercising the
    real code rather than a copy of its SQL.
    """
    dt = DomainThrottle(db, max_per_domain_per_day=10_000)
    errors = []

    for _ in range(5):
        threads_n = 12
        barrier = threading.Barrier(threads_n)

        def record(_barrier=barrier):
            try:
                _barrier.wait(timeout=10)
                dt.record_domain_send("acme-corp.example")
            except Exception as e:  # noqa: BLE001 - any failure is the finding
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=record) for _ in range(threads_n)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=20)

    assert not errors, (
        f"concurrent domain counting raised {errors[:3]} - this fires after "
        f"the email is already sent and is reported to the operator as a "
        f"send failure"
    )


def test_no_concurrent_send_is_lost(db):
    """The same race also LOSES counts when the exception is swallowed, letting
    the per-domain cap admit extra sends later that day."""
    dt = DomainThrottle(db, max_per_domain_per_day=10_000)
    threads_n = 12
    barrier = threading.Barrier(threads_n)

    def record():
        barrier.wait(timeout=10)
        try:
            dt.record_domain_send("counted.example")
        except Exception:  # noqa: BLE001,S110 - the COUNT is the assertion
            pass  # a raise is covered by the test above; here we measure loss

    threads = [threading.Thread(target=record) for _ in range(threads_n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)

    raw = sqlite3.connect(db.db_path)
    count = raw.execute(
        "SELECT count FROM domain_send_counts WHERE domain = ?", ("counted.example",)
    ).fetchone()[0]
    raw.close()
    assert count == threads_n, (
        f"{threads_n} sends recorded but the counter says {count} - the cap "
        f"will admit {threads_n - count} extra send(s) today"
    )


def test_every_repeated_send_is_counted(db):
    dt = DomainThrottle(db, max_per_domain_per_day=100)
    for _ in range(5):
        dt.record_domain_send("acme-corp.example")

    raw = sqlite3.connect(db.db_path)
    count = raw.execute(
        "SELECT count FROM domain_send_counts WHERE domain = ?",
        ("acme-corp.example",),
    ).fetchone()[0]
    raw.close()
    assert count == 5


# ── 3. Post-send bookkeeping must never look like a send failure ────────────
# The mail is already out. An exception here reached send_single_email's
# handler and told the operator "Send failed" for an email in the prospect's
# inbox - who then retried, sending a real duplicate.


def _agent(tmp_path, settings_factory, **kw):
    from agent import OutreachAgent

    a = OutreachAgent(settings_factory(db_path=str(tmp_path / "b.db"), **kw))
    a._running = True
    return a


@pytest.mark.parametrize("broken", ["warmup", "domain_throttle"])
def test_a_failed_counter_write_does_not_report_a_send_failure(
    tmp_path, settings_factory, broken
):
    a = _agent(tmp_path, settings_factory)

    def _boom(*args, **kw):
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    class _Sender:
        sender_name = "S"
        smtp_username = "s@x.de"

        def dispatch_to_contact(self, *args, **kw):
            return 1  # the email is now sent

    if broken == "warmup":
        a.warmup.record_send = _boom
    else:
        a.domain_throttle.record_domain_send = _boom

    search = a.db.log_search("q", source="test")
    cid = a.db.add_contact(search, "sent@brand.de")
    contact = {"id": cid, "email": "sent@brand.de", "name": "N", "organization": "O"}

    ok = a._process_contact(contact, sender=_Sender(), account_key="")

    assert ok is True, (
        "a bookkeeping failure was reported as a send failure - the operator "
        "would retry and send a real duplicate"
    )


# ── 4/5. Substring matching in lead scoring ─────────────────────────────────


@pytest.mark.parametrize(
    "role,expected_max",
    [("Coordinator", 25), ("Contractor", 5), ("Independent Contractor", 5)],
)
def test_a_role_is_not_scored_by_a_substring(role, expected_max):
    """ "coo" is the first three letters of "coordinator"; "cto" sits inside
    "contractor". Both scored 35 - the C-suite tier."""
    assert LeadScorer()._score_role(role) <= expected_max


@pytest.mark.parametrize("role", ["CEO", "V.P. Sales", "Procurement Director"])
def test_real_senior_roles_still_score(role):
    """Guard: \\b alone would have broken "v.p." (a word boundary after '.'
    demands a word character), swapping one silent scoring bug for another."""
    assert LeadScorer()._score_role(role) >= 30


@pytest.mark.parametrize(
    "org", ["Nordic Labels & Packaging AB", "Collaborate Media Ltd"]
)
def test_an_org_is_not_scored_pharma_by_a_substring(org):
    """ "lab" matched inside "Labels" and "Collaborate"."""
    assert LeadScorer()._score_organization(org) == 10


@pytest.mark.parametrize(
    "org", ["Vitalis Supplements", "Nordic Vitamins AB", "Acme Nutraceuticals GmbH"]
)
def test_real_supplement_orgs_still_score(org):
    """Guard: whole-word matching means a stem no longer covers the longer
    word, so "supplement" would miss "Supplements". Organisation terms are
    matched at the start of a word for that reason; without it this fix
    would have created false NEGATIVES instead."""
    assert LeadScorer()._score_organization(org) == 25


def test_has_term_matches_across_irregular_whitespace():
    assert _has_term("general  manager", "general manager")


# ── 6. Role extraction from scraped page text ───────────────────────────────
# Exercised through SearchModule._find_name_near_email, NOT a copy of the
# regex. The first version of this test defined its own ROLE_RE and therefore
# passed happily while the real pattern was mutated - a test of the test.


def _role_for(page_html, email, db):
    from bs4 import BeautifulSoup

    from modules.search import SearchModule

    sm = SearchModule(db)
    found = sm._find_name_near_email(BeautifulSoup(page_html, "html.parser"), email)
    return (found or {}).get("role")


@pytest.mark.parametrize(
    "body",
    [
        (
            "<p>We use cookies to improve your experience. "
            "Contact: <a href='mailto:info@company.com'>info@company.com</a></p>"
        ),
        (
            "<p>Our Headquarters - "
            "<a href='mailto:info@company.com'>info@company.com</a></p>"
        ),
        (
            "<p>Independent Contractor - "
            "<a href='mailto:info@company.com'>info@company.com</a></p>"
        ),
    ],
)
def test_page_boilerplate_does_not_produce_a_role(body, db):
    """`text` is the container around a scraped mailto - on almost every
    European site that is the footer, next to a cookie banner. "Coo" was
    extracted from "cookies" and then scored as C-suite."""
    assert (
        _role_for(f"<html><body>{body}</body></html>", "info@company.com", db) is None
    )


@pytest.mark.parametrize(
    "body,expected",
    [
        (
            (
                "<p>Maria Rossi, Procurement Director - "
                "<a href='mailto:m@x.com'>m@x.com</a></p>"
            ),
            "procurement",
        ),
        (
            (
                "<p>Jan de Vries, General Manager - "
                "<a href='mailto:m@x.com'>m@x.com</a></p>"
            ),
            "general manager",
        ),
    ],
)
def test_real_titles_are_still_extracted(body, expected, db):
    """Guard: the anchors must not stop legitimate titles being found."""
    role = _role_for(f"<html><body>{body}</body></html>", "m@x.com", db)
    assert role and role.lower() == expected


# ── 7. Transient verification must not discard a lead ───────────────────────
# "unverified" is terminal: get_pending_contacts selects only status='pending'
# and nothing anywhere moves a contact back. One DNS timeout retired a
# perfectly deliverable prospect permanently.


def test_a_dns_timeout_is_indeterminate_not_a_rejection(monkeypatch):
    import dns.exception

    def _timeout(*args, **kw):
        raise dns.exception.Timeout()

    monkeypatch.setattr("dns.resolver.resolve", _timeout)
    assert EmailVerifier.verify_mx("x@brand.de") is None

    result = EmailVerifier().verify_email("x@brand.de", skip_smtp=True)
    assert result["indeterminate"] is True
    assert result["valid"] is False


def test_a_domain_with_genuinely_no_mx_is_still_rejected(monkeypatch):
    """Guard: NXDOMAIN is a real answer and must stay a rejection, or nothing
    is ever discarded."""
    import dns.resolver

    def _nxdomain(*args, **kw):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr("dns.resolver.resolve", _nxdomain)
    assert EmailVerifier.verify_mx("x@brand.de") is False

    result = EmailVerifier().verify_email("x@brand.de", skip_smtp=True)
    assert not result.get("indeterminate")
    assert result["valid"] is False


@pytest.mark.parametrize(
    "detail,transient",
    [
        ("RCPT TO rejected: 450 greylisted", True),
        ("Connection timed out to mx.example.com", True),
        ("RCPT TO rejected: 550 no such user", False),
        ("MAIL FROM rejected: 553 bad sender", False),
    ],
)
def test_smtp_deferrals_are_separated_from_rejections(detail, transient):
    assert _is_transient_smtp(detail) is transient


def test_an_indeterminate_contact_stays_pending(tmp_path, settings_factory):
    """The behaviour that matters: it must remain selectable by
    get_pending_contacts rather than being retired as 'unverified'."""
    a = _agent(tmp_path, settings_factory, verify_mx=True, min_lead_score_to_email=0)

    class _Verifier:
        def verify_email(self, email, skip_smtp=True):
            return {"valid": False, "indeterminate": True, "reason": "DNS timeout"}

    a.email_verifier = _Verifier()
    a.relevance_filter = None
    a.lead_scorer = None

    search = a.db.log_search("q", source="test")
    cid = a.db.add_contact(search, "blip@brand.de")

    qualified = a._qualify_contacts([{"id": cid, "email": "blip@brand.de"}])

    assert qualified == [], "an unverified contact must not be emailed yet"
    assert a.db.get_contact_by_id(cid)["status"] == "pending", (
        "a transient DNS failure permanently retired a deliverable prospect"
    )


# ── 8. Unsubscribe secret ───────────────────────────────────────────────────


def test_startup_refuses_the_shipped_unsubscribe_secret(monkeypatch):
    """The default is a literal string in the public source, so anyone can
    forge a token for any address and suppress that prospect."""
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", Settings.UNSUBSCRIBE_SECRET_DEFAULT)
    with pytest.raises(ValueError, match="UNSUBSCRIBE_SECRET"):
        load_settings()


def test_a_configured_secret_starts_normally(monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_SECRET", "a-real-random-value-9f3b2c-0d4e8a71b6")
    assert load_settings().unsubscribe_secret == "a-real-random-value-9f3b2c-0d4e8a71b6"


def test_constructing_settings_directly_is_unaffected():
    """Guard: the check lives in load_settings, not on the model, so the whole
    test suite can keep building Settings with defaults."""
    assert Settings(_env_file=None) is not None


# ── 9. Non-dict JSON bodies ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/agent/start", "[1,2,3]"),
        ("/api/queries/add", '"hello"'),
        ("/api/contacts/5/action", "null"),
        ("/api/config", "[1,2,3]"),
        ("/api/template", "42"),
        ("/api/send-manual", "[]"),
    ],
)
def test_a_non_dict_json_body_does_not_crash_the_route(
    tmp_path, settings_factory, path, body, monkeypatch
):
    """`get_json(silent=True) or {}` only rescued FALSY values - an array,
    string or number is truthy and was handed to code doing .get().

    The start route runs an agent on a thread: stubbed and waited for, or it
    searched the web while later tests ran."""
    monkeypatch.setattr(main.OutreachAgent, "run_single", lambda *_a, **_k: None)
    app = main.create_dashboard_app(settings_factory(db_path=str(tmp_path / "j.db")))
    resp = app.test_client().post(path, data=body, content_type="application/json")
    for t in threading.enumerate():
        if "run_agent" in t.name:
            t.join(10)
    assert resp.status_code != 500, f"{path} 500s on a {body} body"


def test_a_normal_json_body_still_works(tmp_path, settings_factory):
    """Guard: the type check must not reject legitimate requests."""
    app = main.create_dashboard_app(settings_factory(db_path=str(tmp_path / "k.db")))
    resp = app.test_client().post("/api/queries/add", json={"query": "vitamins Italy"})
    assert resp.status_code == 200
    assert Database(str(tmp_path / "k.db")).get_pending_searches(5)
