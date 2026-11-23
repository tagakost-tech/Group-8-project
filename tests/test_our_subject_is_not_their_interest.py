"""Our own subject line is not the prospect's interest.

classify() read a reply's subject for signs of interest, and a reply's
subject is mostly ours: "Re: <our subject>". With an EMAIL_SUBJECTS line
that says "offer", "quote" or "samples", every reply that did not decline
read as interested and sent sales a lead alert. Interest is now read from
what the person wrote; a decline still counts wherever it is written.
"""

from modules.reply_intent import INTERESTED, NOT_INTERESTED, UNCLEAR, classify

OUR_SUBJECT = "Re: Our private-label offer - samples and a quote"


def test_our_subject_is_not_their_interest():
    assert classify(OUR_SUBJECT, "Hmm, maybe. Who are you exactly?") == UNCLEAR


def test_their_own_words_still_count():
    assert classify(OUR_SUBJECT, "Please send the price list.") == INTERESTED


def test_a_decline_in_the_subject_still_counts():
    assert classify("Not interested", "") == NOT_INTERESTED


def test_the_agent_sends_no_lead_alert_for_our_own_subject(settings_factory, tmp_path):
    from agent import OutreachAgent

    agent = OutreachAgent(settings_factory(db_path=str(tmp_path / "a.db")))
    assert agent._reply_is_interested(OUR_SUBJECT, "Who are you exactly?") is False
