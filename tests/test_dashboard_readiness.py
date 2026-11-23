"""The Overview says whether a live first email can go out, and if not why.

The sidebar could read "Agent running" in green while every live send was
refused - no sender title, no Company Profile PDF, no client list, a country
lock - because each refusal was one activity row that scrolled away.
/api/readiness answers from the doctor's own checks, read-only, and returns
only the checks that decide a live first email, never a credential.
"""

import base64
import gc
import sqlite3

import pytest

import main
from conftest import anonymous

ALLOWED = {
    "Sender title",
    "PDF attachment present",
    "Italian email template present",
    "Client list file",
    "Competitor list file",
    "Inbox read",
    "Country lock",
    "Legal identity (email footer)",
    "Opt-out route",
    "Legal footer in templates",
    "SMTP credentials set",
    "Journal replayed",
}


def _app(tmp_path, settings_factory, **kw):
    kw.setdefault("db_path", str(tmp_path / "ready.db"))
    kw.setdefault("blacklist_check_enabled", False)
    return main.create_dashboard_app(settings_factory(**kw))


def _ready(tmp_path, settings_factory, **kw):
    resp = _app(tmp_path, settings_factory, **kw).test_client().get("/api/readiness")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _row(body, label):
    rows = [c for c in body["checks"] if c["label"] == label]
    assert len(rows) == 1, [c["label"] for c in body["checks"]]
    return rows[0]


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def test_it_returns_the_allowed_checks_and_no_credential(tmp_path, settings_factory):
    body = _ready(
        tmp_path,
        settings_factory,
        smtp_username="giulia@suppleco.example",
        smtp_password="smtp-secret-123",
        sender_signature="Giulia's private signature",
        sender_2_email="marco@suppleco.example",
        sender_2_password="second-secret-456",
    )
    assert {c["label"] for c in body["checks"]} == ALLOWED
    for c in body["checks"]:
        assert set(c) == {"label", "ok", "detail", "blocking"}
        assert c["ok"] in (True, False, None)
    for key in _keys(body):
        assert "password" not in key.lower() and "signature" not in key.lower(), key
    text = str(body)
    assert "smtp-secret-123" not in text and "second-secret-456" not in text
    assert "private signature" not in text
    assert body["mailboxes"] == [
        {"address": "giulia@suppleco.example", "title": "Business Development"},
        {"address": "marco@suppleco.example", "title": "Business Development"},
    ]
    assert body["can_send_live"] is True, body
    assert body["dry_run"] is False
    assert body["italian_followups"] == {"followup_1_it": True, "followup_2_it": True}
    assert body["competitor"] == {"state": "off", "count": None}


def test_a_missing_pdf_refuses_live_first_emails(tmp_path, settings_factory):
    body = _ready(
        tmp_path,
        settings_factory,
        attach_profile=True,
        pdf_path=str(tmp_path / "no-such-folder" / "profile.pdf"),
    )
    row = _row(body, "PDF attachment present")
    assert row["ok"] is False and row["blocking"] is True
    assert "profile.pdf" in row["detail"]
    assert body["can_send_live"] is False


def test_a_blank_country_list_refuses_live_sends(tmp_path, settings_factory):
    body = _ready(tmp_path, settings_factory, ENABLED_COUNTRIES="")
    row = _row(body, "Country lock")
    assert row["ok"] is False and row["blocking"] is True
    assert "ENABLED_COUNTRIES" in row["detail"]
    assert body["can_send_live"] is False


def test_a_mailbox_without_a_title_is_named(tmp_path, settings_factory):
    body = _ready(
        tmp_path,
        settings_factory,
        smtp_username="giulia@suppleco.example",
        sender_title="",
    )
    assert _row(body, "Sender title")["ok"] is False
    assert body["mailboxes"] == [{"address": "giulia@suppleco.example", "title": ""}]
    assert body["can_send_live"] is False


def test_dry_run_is_reported(tmp_path, settings_factory):
    assert _ready(tmp_path, settings_factory, dry_run=True)["dry_run"] is True


def test_the_competitor_list_is_counted_with_the_client_loader(
    tmp_path, settings_factory
):
    from openpyxl import Workbook

    path = tmp_path / "competitors.xlsx"
    wb = Workbook()
    wb.active.append(["Rival Integratori S.r.l."])
    wb.active.append(["Concorrente Nutra S.p.A."])
    wb.save(path)
    body = _ready(tmp_path, settings_factory, competitor_list_path=str(path))
    assert body["competitor"] == {"state": "ok", "count": 2}

    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"this is not a workbook")
    body = _ready(tmp_path, settings_factory, competitor_list_path=str(broken))
    assert body["competitor"] == {"state": "unreadable", "count": None}


def test_it_needs_the_dashboard_login_and_is_read_only(tmp_path, settings_factory):
    client = _app(
        tmp_path, settings_factory, DASHBOARD_PASSWORD="s3cret-passphrase"
    ).test_client()
    assert anonymous(client).get("/api/readiness").status_code == 401
    auth = {
        "Authorization": "Basic "
        + base64.b64encode(b"admin:s3cret-passphrase").decode()
    }
    assert client.get("/api/readiness", headers=auth).status_code == 200
    assert client.post("/api/readiness", headers=auth).status_code == 405


def _activity_rows(path):
    raw = sqlite3.connect(path)
    try:
        return raw.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
    finally:
        raw.close()


def test_asking_writes_no_activity(tmp_path, settings_factory):
    """send_blocker() and first_email_blocker() record each refusal as an
    activity row; the readiness check must not."""
    db_path = str(tmp_path / "ready.db")
    client = _app(
        tmp_path,
        settings_factory,
        db_path=db_path,
        ENABLED_COUNTRIES="",
        sender_title="",
        pdf_path=str(tmp_path / "missing.pdf"),
    ).test_client()
    before = _activity_rows(db_path)
    for _ in range(3):
        assert client.get("/api/readiness").status_code == 200
    assert _activity_rows(db_path) == before


def _open_connections():
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, sqlite3.Connection))


@pytest.mark.parametrize("calls", [50])
def test_asking_often_leaves_no_database_handle_open(tmp_path, settings_factory, calls):
    client = _app(tmp_path, settings_factory).test_client()
    client.get("/api/readiness")
    baseline = _open_connections()
    held = sqlite3.connect(":memory:")
    assert _open_connections() == baseline + 1, "the count cannot see a connection"
    held.close()
    del held
    for _ in range(calls):
        assert client.get("/api/readiness").status_code == 200
    assert _open_connections() <= baseline


def test_a_failing_doctor_gives_a_plain_message(
    tmp_path, settings_factory, monkeypatch
):
    def broken(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    client = _app(tmp_path, settings_factory).test_client()
    monkeypatch.setattr(main, "run_doctor_checks", broken)
    resp = client.get("/api/readiness")
    assert resp.status_code == 503
    assert resp.is_json
    assert "Traceback" not in resp.get_data(as_text=True)
    assert "could not" in resp.get_json()["error"].lower()


def test_the_polled_route_runs_only_the_checks_the_cards_show(
    tmp_path, settings_factory, monkeypatch
):
    """The page asks once a minute. Running the whole doctor there re-read and
    regex-scanned every log file, built the AI client and linted the template,
    none of which either card shows: 3 to 4.5 s a request with 22 MB of logs."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text(
        "2026-11-12 10:00:00 INFO sent to buyer@brand.example\n" * 50,
        encoding="utf-8",
    )
    doctor = main.run_doctor_checks
    monkeypatch.setattr(
        main, "run_doctor_checks", lambda s, **kw: doctor(s, log_dir=logs, **kw)
    )
    work = []
    import modules.ai_generator
    import modules.compliance
    import modules.deliverability

    mask = modules.compliance.mask_personal

    def counted_mask(line):
        work.append("log scan")
        return mask(line)

    monkeypatch.setattr(modules.compliance, "mask_personal", counted_mask)

    class NoAI:
        def __init__(self, *a, **k):
            work.append("AI client")

    monkeypatch.setattr(modules.ai_generator, "AIGenerator", NoAI)
    lint = modules.deliverability.DeliverabilityLinter.lint
    monkeypatch.setattr(
        modules.deliverability.DeliverabilityLinter,
        "lint",
        staticmethod(lambda tpl: work.append("lint") or lint(tpl)),
    )
    client = _app(tmp_path, settings_factory).test_client()
    assert client.get("/api/readiness").status_code == 200
    assert work == []
    # The same checks still run where the operator asks for all of them.
    full = main.run_doctor_checks(
        settings_factory(db_path=str(tmp_path / "ready.db")), network=False
    )
    assert "log scan" in work and "AI client" in work
    assert {"Log files masked", "AI generator (optional)"} <= {c.label for c in full}


def _strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def test_the_page_gets_file_names_never_a_path_or_an_exception(
    tmp_path, settings_factory, monkeypatch
):
    """The doctor writes absolute paths and exception text for the terminal.
    On the page they put the install folder, and with it the Windows account
    folder, into every poll's JSON."""
    broken = tmp_path / "clienti.xlsx"
    broken.write_bytes(b"this is not a workbook")
    import modules.compliance

    def failing_route(_settings):
        raise OSError(f"cannot open {tmp_path / 'secret' / 'route.cfg'}")

    monkeypatch.setattr(modules.compliance, "opt_out_route", failing_route)
    body = _ready(
        tmp_path,
        settings_factory,
        attach_profile=True,
        pdf_path=str(tmp_path / "assets" / "profile.pdf"),
        client_list_path=str(broken),
        competitor_list_path=str(tmp_path / "rivals.xlsx"),
    )
    for text in _strings(body):
        for absolute in (str(tmp_path), str(main.PROJECT_ROOT), tmp_path.as_posix()):
            assert absolute not in text, text
        assert "zip" not in text.lower() and "secret" not in text, text
    assert _row(body, "PDF attachment present")["detail"].startswith("profile.pdf ")
    clients = _row(body, "Client list file")
    assert clients["ok"] is False
    assert clients["detail"] == (
        "clienti.xlsx cannot be read - customers are not screened"
    )
    assert _row(body, "Competitor list file")["detail"].startswith("rivals.xlsx ")
    assert _row(body, "Opt-out route")["detail"] == (
        "could not be checked - see the server log"
    )


def test_a_file_in_the_project_is_named_from_the_project(tmp_path, settings_factory):
    body = _ready(
        tmp_path, settings_factory, attach_profile=True, pdf_path="assets/none.pdf"
    )
    assert _row(body, "PDF attachment present")["detail"].startswith("assets/none.pdf ")
    # The terminal still gets the whole path.
    doctor = main.run_doctor_checks(
        settings_factory(attach_profile=True, pdf_path="assets/none.pdf"),
        network=False,
        only=("PDF attachment present",),
    )
    assert doctor[0].detail.startswith(str(main.PROJECT_ROOT))


def test_no_smtp_login_means_no_live_first_email(tmp_path, settings_factory):
    """The doctor only warns about blank SMTP credentials, so the card said a
    live first email could go out while every live send failed at login."""
    body = _ready(
        tmp_path, settings_factory, smtp_username="", smtp_password="", dry_run=False
    )
    row = _row(body, "SMTP credentials set")
    assert row["ok"] is False and row["blocking"] is True
    assert "fail at the SMTP login" in row["detail"]
    assert body["can_send_live"] is False

    body = _ready(
        tmp_path,
        settings_factory,
        smtp_username="giulia@suppleco.example",
        smtp_password="smtp-secret-123",
    )
    assert _row(body, "SMTP credentials set")["ok"] is True
    assert body["can_send_live"] is True


def _product(tmp_path, monkeypatch, **rewritten):
    """The readiness route, pointed at a product folder whose templates are
    the shipped ones with some rewritten."""
    import shutil

    from modules.file_handler import FileHandler

    root = tmp_path / "product"
    shutil.copytree(main.PROJECT_ROOT / "templates", root / "templates")
    for name, edit in rewritten.items():
        path = root / "templates" / name.replace("__", ".")
        path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(main, "FileHandler", lambda: FileHandler(str(root)))


def _without(placeholder):
    return lambda text: text.replace(placeholder, "")


# Blank, the SMTP login alone refuses every live first email.
LOGIN = {"smtp_username": "giulia@suppleco.example", "smtp_password": "smtp-secret-123"}


def test_one_titled_mailbox_among_several_still_sends(tmp_path, settings_factory):
    """send_verdict sends the first email through the mailbox that has a
    title, so the card said "refused" while the campaign's first emails went."""
    body = _ready(
        tmp_path,
        settings_factory,
        smtp_username="a@sender.example.test",
        smtp_password="x",
        sender_title="",
        sender_2_email="b@sender.example.test",
        sender_2_password="x",
        sender_2_title="Sales",
    )
    row = _row(body, "Sender title")
    assert row["ok"] is False and row["blocking"] is False
    assert "a@sender.example.test" in row["detail"]
    assert "through b@sender.example.test" in row["detail"]
    assert body["can_send_live"] is True
    assert body["held_back"] == []


def test_a_broken_follow_up_does_not_refuse_first_emails(
    tmp_path, settings_factory, monkeypatch
):
    _product(tmp_path, monkeypatch, followup_2_it__html=_without("{{unsubscribe_url}}"))
    body = _ready(tmp_path, settings_factory, **LOGIN)
    row = _row(body, "Legal footer in templates")
    assert row["ok"] is False and row["blocking"] is False
    assert row["detail"] == (
        "first emails are not refused for this: followup_2_it.html lacks "
        "{{unsubscribe_url}}"
    )
    assert body["can_send_live"] is True
    assert body["held_back"] == []


def test_a_language_whose_text_lacks_the_footer_is_held_back(
    tmp_path, settings_factory, monkeypatch
):
    """process_contact refuses the chosen text without {{legal_footer}}: the
    English first emails stop, the Italian ones go."""
    _product(tmp_path, monkeypatch, email_template__html=_without("{{legal_footer}}"))
    body = _ready(tmp_path, settings_factory, **LOGIN)
    row = _row(body, "Legal footer in templates")
    assert row["ok"] is False and row["blocking"] is False
    assert row["detail"].startswith("English first emails are refused: ")
    assert body["can_send_live"] is True
    assert body["held_back"] == ["English first emails are refused"]


def test_no_language_left_refuses_every_first_email(
    tmp_path, settings_factory, monkeypatch
):
    _product(
        tmp_path,
        monkeypatch,
        email_template__html=_without("{{legal_footer}}"),
        email_template_it__html=_without("{{legal_footer}}"),
    )
    body = _ready(tmp_path, settings_factory, **LOGIN)
    row = _row(body, "Legal footer in templates")
    assert row["ok"] is False and row["blocking"] is True
    assert body["can_send_live"] is False
    assert body["held_back"] == []


def test_no_title_anywhere_holds_back_only_the_texts_that_name_it(
    tmp_path, settings_factory, monkeypatch
):
    _product(
        tmp_path, monkeypatch, email_template_it__html=_without("{{sender_title}}")
    )
    body = _ready(tmp_path, settings_factory, sender_title="", **LOGIN)
    row = _row(body, "Sender title")
    assert row["ok"] is False and row["blocking"] is False
    assert body["can_send_live"] is True
    assert body["held_back"] == [
        "English first emails are held back until a mailbox has a SENDER_TITLE"
    ]
    # Without an Italian text, Italian recipients get the English one too.
    body = _ready(
        tmp_path, settings_factory, sender_title="", ITALIAN_COUNTRIES="", **LOGIN
    )
    assert _row(body, "Sender title")["blocking"] is True
    assert body["can_send_live"] is False


def test_an_ab_arm_that_names_the_title_holds_back_english_first_emails(
    tmp_path, settings_factory, monkeypatch
):
    """first_email_needs_title counts the A/B arms: one arm that names the
    title holds back every English first email while SENDER_TITLE is blank.
    The card looked only at the main and Italian texts and gave a clean bill."""
    original = (main.PROJECT_ROOT / "templates" / "email_template.html").read_text(
        encoding="utf-8"
    )
    _product(
        tmp_path,
        monkeypatch,
        email_template__html=_without("{{sender_title}}"),
        email_template_it__html=_without("{{sender_title}}"),
    )
    (main.PROJECT_ROOT / "templates" / "email_template_b.html").write_text(
        original, encoding="utf-8"
    )
    body = _ready(
        tmp_path, settings_factory, sender_title="", ab_testing_enabled=True, **LOGIN
    )
    row = _row(body, "Sender title")
    assert row["ok"] is False and row["blocking"] is False
    assert body["can_send_live"] is True
    assert body["held_back"] == [
        "English first emails are held back until a mailbox has a SENDER_TITLE"
    ]
    doctor = main.run_doctor_checks(
        settings_factory(sender_title="", ab_testing_enabled=True),
        network=False,
        only=("Sender title",),
    )
    assert doctor[0].ok is False
    # With A/B testing off the arm is never sent, and nothing names the title.
    body = _ready(tmp_path, settings_factory, sender_title="", **LOGIN)
    assert _row(body, "Sender title")["ok"] is None
    assert body["held_back"] == []


def test_an_ab_arm_without_the_footer_refuses_some_english_first_emails(
    tmp_path, settings_factory, monkeypatch
):
    original = (main.PROJECT_ROOT / "templates" / "email_template.html").read_text(
        encoding="utf-8"
    )
    _product(tmp_path, monkeypatch)
    (main.PROJECT_ROOT / "templates" / "email_template_b.html").write_text(
        original.replace("{{legal_footer}}", ""), encoding="utf-8"
    )
    body = _ready(tmp_path, settings_factory, ab_testing_enabled=True, **LOGIN)
    row = _row(body, "Legal footer in templates")
    assert row["blocking"] is False and body["can_send_live"] is True
    assert body["held_back"] == [
        "some English first emails are refused (email_template_b.html)"
    ]
