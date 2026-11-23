"""Send on a filtered contact says the filters run again, not that it will go.

Send only sets the contact back to 'pending'. Nothing records an override, so
the next pass runs the filters again and the same verdict discards the contact
again: low_score, pending, low_score, with nothing sent, while the page said
"Queued for the next send". The real override is a later fix. Until then the
reply says what will happen, from the status the contact has when Send is
pressed, because the row on the operator's screen can be out of date.

The browser half is tests/ui/test_send_and_spam_test_promise_nothing_false.py.
"""

import pytest

import main
from conftest import _dry_run_logged

FILTERS_RUN_AGAIN = "the filters run again before anything is sent"


@pytest.fixture
def app(tmp_path, settings_factory):
    s = settings_factory(db_path=str(tmp_path / "d.db"))
    client = main.create_dashboard_app(s).test_client()
    db = main.Database(s.get_abs_db_path())
    return client, db


def _contact(db, status):
    cid = db.add_contact(db.log_search("q"), "buyer@brand.de", organization="Brand")
    db.update_contact_status(cid, status)
    return cid


def _act(client, cid, action):
    return client.post(f"/api/contacts/{cid}/action", json={"action": action})


def _send_now(client, cid):
    return _act(client, cid, "send_now")


def _agent(settings_factory, tmp_path, min_score):
    from agent import OutreachAgent

    agent = OutreachAgent(
        settings_factory(
            db_path=str(tmp_path / "agent.db"),
            verify_emails=False,  # no real DNS/MX lookups in tests
            dry_run=True,
            send_window_enabled=False,  # tests must not depend on the wall clock
            min_lead_score_to_email=min_score,
        )
    )
    agent._running = True
    return agent


def _filtered_out_on_score(agent):
    cid = agent.db.add_contact(
        agent.db.log_search("q"), "buyer@brand.de", organization="Brand GmbH"
    )
    agent._process_pending_contacts(dry_run=True)
    assert agent.db.get_contact_by_id(cid)["status"] == "low_score"
    return cid


@pytest.mark.parametrize(
    "status", ["low_score", "irrelevant", "ai_irrelevant", "unverified"]
)
def test_send_on_a_filtered_contact_says_the_filters_run_again(app, status):
    client, db = app
    cid = _contact(db, status)

    resp = _send_now(client, cid)

    assert resp.status_code == 200
    assert FILTERS_RUN_AGAIN in resp.get_json().get("message", ""), resp.get_json()
    assert db.get_contact_by_id(cid)["status"] == "pending"


@pytest.mark.parametrize("status", ["failed", "pending"])
def test_send_on_a_contact_no_filter_discarded_leaves_the_wording_to_the_page(
    app, status
):
    """The control: no verdict stands against these, so there is none to warn of."""
    client, db = app
    resp = _send_now(client, _contact(db, status))
    assert resp.status_code == 200
    assert "message" not in resp.get_json()


def test_the_next_pass_does_what_the_reply_said(tmp_path, settings_factory):
    """Low score, Send, pending, then low score again, with nothing sent. When the
    real override arrives this fails, and the reply must change with it."""
    agent = _agent(settings_factory, tmp_path, min_score=90)
    cid = _filtered_out_on_score(agent)
    verdict = agent.db.get_contact_by_id(cid)["discard_reason"]
    client = main.create_dashboard_app(agent.settings).test_client()

    reply = _send_now(client, cid).get_json()
    assert FILTERS_RUN_AGAIN in reply.get("message", ""), reply
    assert agent.db.get_contact_by_id(cid)["status"] == "pending"

    agent._process_pending_contacts(dry_run=True)

    after = agent.db.get_contact_by_id(cid)
    assert after["status"] == "low_score"
    assert after["discard_reason"] == verdict
    assert not _dry_run_logged(agent.db, cid)


def test_a_contact_that_now_passes_the_filters_goes_on_to_the_send(
    tmp_path, settings_factory
):
    """Why Send stays on these rows: with the threshold lowered it is the way to
    put a contact through the filters again, and this time it gets past them."""
    cid = _filtered_out_on_score(_agent(settings_factory, tmp_path, min_score=90))
    agent = _agent(settings_factory, tmp_path, min_score=0)
    client = main.create_dashboard_app(agent.settings).test_client()

    assert _send_now(client, cid).status_code == 200
    agent._process_pending_contacts(dry_run=True)

    assert _dry_run_logged(agent.db, cid), "the contact never reached the send"


@pytest.mark.parametrize("presses", [2, 3])
@pytest.mark.parametrize(
    "status", ["low_score", "irrelevant", "ai_irrelevant", "unverified"]
)
def test_every_press_before_the_next_pass_says_the_filters_run_again(
    app, status, presses
):
    """The first press already set the contact to pending, so a later press saw
    a plain pending contact and the page toasted "Queued for the next send",
    though the next pass discards it again just the same."""
    client, db = app
    cid = _contact(db, status)

    for _ in range(presses):
        reply = _send_now(client, cid).get_json()
        assert FILTERS_RUN_AGAIN in reply.get("message", ""), reply

    assert db.get_contact_by_id(cid)["status"] == "pending"


@pytest.mark.parametrize("presses", [2, 3])
@pytest.mark.parametrize("status", ["failed", "pending"])
def test_repeated_presses_on_a_contact_never_filtered_carry_no_warning(
    app, status, presses
):
    client, db = app
    cid = _contact(db, status)
    for _ in range(presses):
        resp = _send_now(client, cid)
        assert resp.status_code == 200
        assert "message" not in resp.get_json(), resp.get_json()


def test_a_contact_pending_for_another_reason_carries_no_warning(app):
    client, db = app
    cid = _contact(db, "blocked")
    assert _act(client, cid, "unblock").status_code == 200
    assert db.get_contact_by_id(cid)["status"] == "pending"

    resp = _send_now(client, cid)

    assert resp.status_code == 200
    assert "message" not in resp.get_json(), resp.get_json()


def test_after_a_pass_lets_it_through_a_press_is_judged_on_its_new_status(
    tmp_path, settings_factory
):
    """Low score, Send, a pass that now lets it through to a send that fails:
    the next press re-queues a failed contact, not a filtered one."""
    cid = _filtered_out_on_score(_agent(settings_factory, tmp_path, min_score=90))
    agent = _agent(settings_factory, tmp_path, min_score=0)
    client = main.create_dashboard_app(agent.settings).test_client()
    assert FILTERS_RUN_AGAIN in _send_now(client, cid).get_json().get("message", "")

    agent._process_pending_contacts(dry_run=True)
    assert _dry_run_logged(agent.db, cid), "the contact never reached the send"
    # What the sender writes when that send is refused (email_sender.py).
    agent.db.update_contact_status(cid, "failed", discard_reason="Send failed: 550")

    resp = _send_now(client, cid)
    assert resp.status_code == 200
    assert "message" not in resp.get_json(), resp.get_json()
