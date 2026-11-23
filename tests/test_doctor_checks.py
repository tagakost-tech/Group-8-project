"""The preflight report, as data.

`doctor` used to be one function that probed and printed in the same breath,
so nothing could assert what it concluded - and nothing did. It is the one
command an operator runs before handing the agent a mailbox, so a check that
silently stops checking is exactly the failure nobody would see.

run_doctor_checks returns the verdicts; cmd_doctor prints them and exits 1
when a BLOCKING check fails, so a deploy script can refuse to continue.
"""

import pytest


@pytest.fixture
def settings(settings_factory, tmp_path):
    return settings_factory(db_path=str(tmp_path / "doctor.db"))


def _by_label(checks, prefix):
    hits = [c for c in checks if c.label.startswith(prefix)]
    assert hits, f"no check labelled {prefix!r} in {[c.label for c in checks]}"
    return hits[0]


def test_the_checks_come_back_as_data(settings):
    from main import run_doctor_checks

    checks = run_doctor_checks(settings, network=False)
    assert _by_label(checks, "Database accessible").ok is True
    assert _by_label(checks, "Email template present").ok is True


def test_network_checks_are_skipped_not_failed_offline(settings):
    """Not assessed is not the same as failed. A WARN for every offline run
    trains the operator to ignore WARNs."""
    from main import run_doctor_checks

    checks = run_doctor_checks(settings, network=False)
    assert _by_label(checks, "DNS resolution").ok is None


def _opt_out(settings_factory, tmp_path, **kw):
    from main import run_doctor_checks

    s = settings_factory(db_path=str(tmp_path / "o.db"), **kw)
    return _by_label(run_doctor_checks(s, network=False), "Opt-out route")


def test_an_opt_out_nobody_reads_is_flagged(settings_factory, tmp_path):
    """Local BASE_URL means the unsubscribe link is a mailto. With IMAP off,
    those emails land in a mailbox nothing processes - the right to object
    is exercised and never honoured (Art. 21(3))."""
    check = _opt_out(
        settings_factory,
        tmp_path,
        imap_enabled=False,
        base_url="http://127.0.0.1:5000",
    )
    assert check.ok is False
    assert "IMAP" in check.detail


def test_either_route_working_is_enough(settings_factory, tmp_path):
    assert _opt_out(settings_factory, tmp_path, imap_enabled=True).ok is True
    assert (
        _opt_out(
            settings_factory,
            tmp_path,
            imap_enabled=False,
            base_url="https://outreach.example.com",
        ).ok
        is True
    )


def test_a_blocking_failure_exits_nonzero(settings, monkeypatch, capsys):
    import main

    monkeypatch.setattr(
        main,
        "run_doctor_checks",
        lambda *a, **k: [main.DoctorCheck("Legal thing", False, "missing", True)],
    )
    with pytest.raises(SystemExit) as exc:
        main.cmd_doctor(type("A", (), {"smtp": False})(), settings)
    assert exc.value.code == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_a_warning_alone_does_not_exit(settings, monkeypatch, capsys):
    """Advisory checks stay advisory: exiting on them would make the command
    useless in a script until every optional feature is configured."""
    import main

    monkeypatch.setattr(
        main,
        "run_doctor_checks",
        lambda *a, **k: [main.DoctorCheck("Optional thing", False, "off")],
    )
    main.cmd_doctor(type("A", (), {"smtp": False})(), settings)
    assert "[WARN]" in capsys.readouterr().out


# ── The config gaps doctor did not look at ─────────────────────────────────


def _check(settings_factory, tmp_path, label, **kw):
    from main import run_doctor_checks

    s = settings_factory(db_path=str(tmp_path / "c.db"), **kw)
    return _by_label(run_doctor_checks(s, network=False), label)


def test_hot_lead_alerts_without_a_recipient_are_flagged(settings_factory, tmp_path):
    """An interested reply that notifies nobody is the costliest silent gap."""
    assert _check(settings_factory, tmp_path, "Hot-lead alerts").ok is False
    ok = _check(
        settings_factory, tmp_path, "Hot-lead alerts", lead_alert_email="ops@co.it"
    )
    assert ok.ok is True


def test_a_client_list_that_is_not_there_is_flagged(settings_factory, tmp_path):
    from openpyxl import Workbook

    missing = _check(
        settings_factory,
        tmp_path,
        "Client list file",
        client_filter_enabled=True,
        client_list_path=str(tmp_path / "nope.xlsx"),
    )
    assert missing.ok is False
    # Said as missing, not as a file the loader then failed to open.
    assert "not found" in missing.detail
    assert "cannot be read" not in missing.detail
    # One client: an empty workbook screens no one and now fails the check
    # too (test_an_empty_client_list_refuses_live_sends.py).
    xlsx = tmp_path / "clients.xlsx"
    wb = Workbook()
    wb.active.append(["Farmacia Rossi S.r.l."])
    wb.save(xlsx)
    assert (
        _check(
            settings_factory,
            tmp_path,
            "Client list file",
            client_filter_enabled=True,
            client_list_path=str(xlsx),
        ).ok
        is True
    )


def test_a_competitor_list_is_optional_until_named(settings_factory, tmp_path):
    assert _check(settings_factory, tmp_path, "Competitor list file").ok is None
    named = _check(
        settings_factory,
        tmp_path,
        "Competitor list file",
        competitor_list_path=str(tmp_path / "rivals.xlsx"),
    )
    assert named.ok is False


def test_the_pdf_is_only_required_when_it_is_attached(settings_factory, tmp_path):
    check = _check(
        settings_factory, tmp_path, "PDF attachment present", attach_profile=False
    )
    assert check.ok is None


def test_a_linked_profile_needs_a_link(settings_factory, tmp_path):
    def link(**kw):
        return _check(settings_factory, tmp_path, "Profile link", **kw).ok

    assert link(attach_profile=False, profile_url="") is False
    assert link(attach_profile=False, profile_url="https://co.it/profile.pdf") is True
    assert link(attach_profile=True) is None


def test_attaching_the_profile_to_a_cold_email_is_flagged(settings_factory, tmp_path):
    """A 7 MB attachment on a first contact is a spam signal."""
    label = "Profile sent as a link, not attached"
    assert _check(settings_factory, tmp_path, label, attach_profile=True).ok is False
    assert _check(settings_factory, tmp_path, label, attach_profile=False).ok is True


def test_a_fast_warmup_is_flagged(settings_factory, tmp_path):
    fast = _check(
        settings_factory,
        tmp_path,
        "Warm-up ramp",
        warmup_start_daily=20,
        warmup_increment=15,
    )
    assert fast.ok is False
    assert _check(settings_factory, tmp_path, "Warm-up ramp").ok is True


def test_imap_needs_credentials_when_on(settings_factory, tmp_path):
    def imap(**kw):
        return _check(settings_factory, tmp_path, "IMAP credentials", **kw).ok

    assert imap(imap_enabled=False) is None
    assert imap(imap_enabled=True, smtp_username="", smtp_password="") is False
    assert (
        imap(imap_enabled=True, smtp_username="me@co.it", smtp_password="secret")
        is True
    )


def test_a_dashboard_without_a_good_password_is_flagged(settings_factory, tmp_path):
    """/api/template, /api/send-manual and /api/config are open without it,
    on this machine as well as from outside it."""

    def dash(**kw):
        c = _check(settings_factory, tmp_path, "Dashboard password", **kw)
        return c.ok, c.blocking

    public, local = "https://out.example.com", "http://127.0.0.1:5000"
    good = "a-good-dashboard-password"
    assert dash(base_url=public, DASHBOARD_PASSWORD="") == (False, True)
    assert dash(base_url=public, DASHBOARD_PASSWORD="pw") == (False, True)
    assert dash(base_url=public, DASHBOARD_PASSWORD=good) == (True, True)
    assert dash(base_url=local, DASHBOARD_PASSWORD="") == (False, True)
    assert dash(base_url=local, DASHBOARD_PASSWORD=good) == (True, True)


def test_an_unconfigured_optional_ai_is_not_a_warning(settings_factory, tmp_path):
    """Optional means optional: a WARN here flipped "All checks passed" for a
    setup with nothing wrong."""
    assert _check(settings_factory, tmp_path, "AI generator").ok is None


def test_the_template_sender_name_is_a_placeholder(settings_factory, tmp_path):
    """ "The name of the sender" is instruction text, not a name - and it was
    what a live .env still said, with doctor reporting [OK]. It would have
    signed every email."""
    check = _check(
        settings_factory,
        tmp_path,
        "Sender identity",
        sender_name="The name of the sender",
        company_name="SUPPLECO",
    )
    assert check.ok is False
    real = _check(
        settings_factory,
        tmp_path,
        "Sender identity",
        sender_name="Giulia Bianchi",
        company_name="SUPPLECO",
    )
    assert real.ok is True
