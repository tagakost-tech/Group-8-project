"""A guard that four send paths apply and the fifth does not is not a guard.

This codebase's signature failure is a check added to one path and missed on
its sibling — the per-domain cap missing from retries, the bounce cohort join
applied to one function and not its twin, the active-client check needed in
three places. Two more instances, both on paths a human triggers or a database
hiccup reaches:

  * send_single_email (the dashboard's Direct Send) had the suppression,
    bounce, blacklist and warm-up guards but neither per-domain guard, so an
    operator working down a company's team page could send five in a row to
    one domain, or keep firing at a domain already known to be bouncing.

  * _process_sequences called advance_enrollment outside any try, unlike every
    other post-send write. A locked database left the step un-advanced, and
    get_due_followups computes "due" from current_step — so the same follow-up
    came back as due and the prospect received it twice.
"""

import sqlite3

import pytest

from modules.clients import ClientRegister


def _agent(tmp_path, settings_factory, **kw):
    from agent import OutreachAgent

    a = OutreachAgent(settings_factory(db_path=str(tmp_path / "g.db"), **kw))
    a._running = True
    a.relevance_filter = None
    a.lead_scorer = None
    a.email_verifier = None
    # A loaded list naming one unrelated company: it screens no contact
    # here. An empty or missing list stops live sends
    # (test_an_empty_client_list_refuses_live_sends.py).
    a.client_register = ClientRegister(["Zeta Unrelated Holding S.p.A."])
    return a


# ── Direct Send must honour the per-domain guards ──────────────────────────


def test_direct_send_refuses_once_the_domain_cap_is_reached(tmp_path, settings_factory):
    a = _agent(tmp_path, settings_factory, max_per_domain_per_day=2)
    a.domain_throttle.record_domain_send("acme.de")
    a.domain_throttle.record_domain_send("acme.de")

    ok, message = a.send_single_email("someone@acme.de", organization="Acme")

    assert ok is False, "Direct Send blew past the per-domain daily cap"
    # Assert on the CAP's own wording. "acme.de in message" passed even with
    # the guard removed, because the send then failed for an unrelated reason
    # and echoed the address back in that error - a test that could not fail.
    assert "limit" in message.lower() and "acme.de" in message, message


class _OkSMTP:
    """An SMTP session that accepts everything, so no test opens a real one."""

    def noop(self):
        return (250, b"ok")

    def send_message(self, msg):
        pass

    def close(self):
        pass


def test_direct_send_is_allowed_below_the_cap(tmp_path, settings_factory):
    """Control: the guard must refuse at the limit, not always."""
    a = _agent(tmp_path, settings_factory, max_per_domain_per_day=2)
    a.email_sender._new_connection = lambda: _OkSMTP()
    a.domain_throttle.record_domain_send("acme.de")

    _ok, message = a.send_single_email("someone@acme.de", organization="Acme")

    # Only the CAP's refusal is asserted against - the send may still fail for
    # an unrelated reason, and that is fine.
    assert "limit" not in message.lower(), message


def test_direct_send_refuses_a_backed_off_domain(tmp_path, settings_factory):
    """A manual retry to a hard-bouncing domain costs exactly the same
    reputation as an automated one."""
    a = _agent(
        tmp_path,
        settings_factory,
        domain_bounce_backoff_enabled=True,
        domain_bounce_min_sends=3,
        max_domain_bounce_rate=0.3,
    )
    sid = a.db.log_search("q", source="test")
    for i in range(4):
        cid = a.db.add_contact(sid, f"p{i}@bad.de")
        a.db.log_email(cid, f"p{i}@bad.de", "s", delivery_status="sent")
    for i in range(3):
        a.db.add_to_suppression(f"p{i}@bad.de", "bounce")
    a._refresh_domain_backoff()

    ok, message = a.send_single_email("new@bad.de", organization="Bad")

    assert ok is False, "Direct Send ignored the per-domain bounce backoff"
    assert "bounc" in message.lower()


# ── A failed bookkeeping write must not re-send the step ───────────────────


def test_a_failed_step_advance_does_not_take_down_the_loop(
    tmp_path, settings_factory, monkeypatch
):
    """The mail is already gone. Every sibling post-send write is contained;
    this one propagated, so the exception escaped _process_sequences entirely
    and the run loop caught it — with the step still un-advanced."""
    a = _agent(
        tmp_path,
        settings_factory,
        sequences_enabled=True,
        send_window_enabled=False,
        blacklist_check_enabled=False,
    )
    if not a.sequences:
        pytest.skip("sequences are not enabled in this build")

    attempts = []

    def boom(enrollment_id, step=None):
        attempts.append(enrollment_id)
        raise sqlite3.OperationalError("database is locked")

    # A stored contact: the send check reads each one, and a made-up id is
    # cancelled as "contact not found" before the advance is ever reached.
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "p@brand.de", organization="Brand")
    a.db.update_contact_status(cid, "emailed")

    monkeypatch.setattr(a.sequences, "advance_enrollment", boom)
    monkeypatch.setattr(a, "_interruptible_sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(a, "_process_contact", lambda *_a, **_k: True)
    monkeypatch.setattr(
        a.sequences,
        "get_due_followups",
        lambda *_a, **_k: [
            {
                "enrollment_id": 1,
                "contact_id": cid,
                "subject": "s",
                "template_name": "followup_1",
            }
        ],
    )

    # Must not raise. Before the fix this propagated out of the method.
    a._process_sequences(dry_run=False)
    assert attempts == [1], "the advance was never reached, so nothing was tested"
