""" "Stop" is an ordinary English word before it is an opt-out.

NEGATIVE_PATTERNS is checked first and returns immediately, so a match there
means the positive signals in the same message are never read. A bare
\\bstop\\b is correctly word-bounded and still wrong: "can we stop by for a
quick call next week?" was classified NOT_INTERESTED, which suppresses the
hot-lead alert and ends the sequence.

A rejection that is really an acceptance is the most expensive mistake this
module can make, because nothing downstream ever revisits it - and the reply
that would have told a human otherwise has already been filed as a no.
"""

import pytest

from modules.reply_intent import classify, is_optout


@pytest.mark.parametrize(
    "body",
    [
        "Happy to discuss - can we stop by for a quick call next week?",
        "Sounds interesting, I will stop by your booth at the fair.",
        "Great, we can stop over at your facility in March.",
    ],
)
def test_an_interested_reply_containing_stop_is_not_a_rejection(body):
    assert classify(body=body) != "not_interested", (
        "a hot lead was filed as a rejection because it contained the word "
        "'stop' - and negative patterns short-circuit, so the positive signal "
        "in the same sentence was never read"
    )


@pytest.mark.parametrize(
    "body",
    [
        "Please stop sending me these emails.",
        "Stop emailing me.",
        "stop contacting us please",
        "Please stop mailing this address.",
    ],
)
def test_a_real_opt_out_is_still_caught(body):
    """Control: the fix must not work by never matching. A missed opt-out is a
    GDPR problem, not just an annoyance."""
    assert classify(body=body) == "not_interested"
    assert is_optout(body) is True


def test_an_explicit_rejection_is_unaffected():
    """These never depended on the bare 'stop' pattern."""
    assert classify(body="Not interested, thanks.") == "not_interested"
    assert classify(body="Please remove me from your list.") == "not_interested"
    assert classify(body="No grazie, non siamo interessati.") == "not_interested"


def test_a_plain_interested_reply_still_reads_as_interested():
    assert classify(body="We are interested, please send a quote.") == "interested"
