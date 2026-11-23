"""An AI provider sees as little personal data as the task needs.

Every prompt went out as built: the recipient's role in the opening line,
our sender's name in the body, up to 1,200 characters of a reply with its
quoted history and signature, and site text with whatever phone numbers and
tracking links it held. Reply classification ran whenever any AI key was
set, and nothing stopped any of it before a processing agreement existed.

Now (step G10, decision L2.x; the developer's proposals):
- every prompt is scrubbed of addresses, phone numbers and URL queries
  before it leaves, whatever the call site;
- the opening line no longer sends the role, the body no longer sends the
  sender's name;
- reply classification has its own switch, off by default, and sends at
  most 600 characters of the reply's own text, without quote or signature;
- no call that carries a recipient's data is made without a processing
  agreement on record (AI_DPA_REFERENCE); choosing search words still works;
- a lead alert keeps the reply without the quoted history.
"""

import pytest

from modules.ai_generator import AIGenerator
from modules.compliance import ai_in_use, scrub_for_ai
from modules.reply_intent import cut_signature


def _ai(allowed=True):
    ai = AIGenerator.__new__(AIGenerator)
    ai.is_configured = True
    ai.personal_data_allowed = allowed
    ai.provider = "openai_compatible"
    ai.prompts = []

    def complete(prompt, temperature=None):
        ai.prompts.append(prompt)
        return "YES"

    ai._complete = complete
    return ai


@pytest.mark.parametrize(
    "text,gone,kept",
    [
        ("write to anna.rossi@brandco.de today", "anna.rossi@brandco.de", "today"),
        ("call +39 06 1234 5678 now", "1234 5678", "now"),
        ("tel. 0049-30-123456789", "123456789", "tel."),
        (
            "see https://brand.de/p?utm_source=x&id=7 here",
            "utm_source",
            "https://brand.de/p",
        ),
    ],
)
def test_a_prompt_is_scrubbed(text, gone, kept):
    out = scrub_for_ai(text)
    assert gone not in out and kept in out


def test_numbers_that_are_not_phones_stay():
    text = "Founded 2019, 450 staff, ISO 22000, since 2026-10-27, EUR 10.000"
    assert scrub_for_ai(text) == text


def test_every_call_is_scrubbed_before_it_leaves():
    ai = _ai()
    ai._complete_with_retry("Write to anna@brand.de or +39 06 1234 5678", "test")
    assert "anna@brand.de" not in ai.prompts[0]
    assert "1234 5678" not in ai.prompts[0]


def test_the_opening_line_does_not_send_the_role():
    ai = _ai()
    ai.generate_opening_line(
        {
            "organization": "Brandco",
            "role": "Head of Purchasing",
            "qualification_rationale": "Makes vitamins",
        }
    )
    assert "Head of Purchasing" not in ai.prompts[0] and "Brandco" in ai.prompts[0]


def test_the_body_does_not_send_the_senders_name():
    ai = _ai()
    ai.generate_email_body({"organization": "Brandco"}, "Marco Ferri", "SUPPLECO")
    assert "Marco Ferri" not in ai.prompts[0] and "SUPPLECO" in ai.prompts[0]


def test_a_reply_goes_without_its_quote_or_signature_and_short():
    body = (
        "Thanks - could you tell me your minimum order? "
        + "We sell in Germany. " * 60
        + "\nBest regards\nAnna Rossi\nHead of Purchasing\n"
        + "\nOn Mon, SUPPLECO <sales@suppleco.it> wrote:\n> our offer"
    )
    ai = _ai()
    ai.classify_reply_intent("Re: Offer", body)
    sent = ai.prompts[0]
    assert "Anna Rossi" not in sent and "our offer" not in sent
    reply = sent.split("Reply text: ", 1)[1].split("\n\nAnswer", 1)[0]
    assert len(reply) <= 600


def test_a_signature_is_cut_at_the_sign_off():
    assert cut_signature("Yes, send samples.\nCordiali saluti\nMario Bianchi") == (
        "Yes, send samples."
    )
    assert cut_signature("Yes please.\n--\nAnna") == "Yes please."
    assert cut_signature("Regards to the team, we are interested.") == (
        "Regards to the team, we are interested."
    )


def test_no_recipient_data_goes_out_without_an_agreement():
    ai = _ai(allowed=False)
    contact = {"organization": "Brandco", "email": "anna@brand.de"}
    assert ai.generate_opening_line(contact) == ""
    assert ai.generate_email_body(contact, "Marco", "SUPPLECO") == ""
    assert ai.classify_relevance(contact) is None
    assert ai.classify_reply_intent("Re", "Yes") is None
    assert ai.prompts == []
    ai.suggest_discovery_queries(count=2)
    assert len(ai.prompts) == 1, "choosing search words sees nobody's data"


def test_the_agent_allows_recipient_data_only_with_an_agreement(
    settings_factory, tmp_path
):
    from agent import OutreachAgent

    def allowed(**kw):
        agent = OutreachAgent(
            settings_factory(
                db_path=str(tmp_path / "a.db"),
                llm_provider="openai_compatible", LLM_BASE_URL="http://127.0.0.1:11434/v1", LLM_MODEL="test-model",
                **kw,
            )
        )
        return agent.ai_gen.personal_data_allowed

    assert allowed() is False
    assert allowed(AI_DPA_REFERENCE="Own server, no outside processor, 2026-11-16") is True


def test_reply_classification_is_off_by_default(settings_factory, tmp_path):
    from agent import OutreachAgent

    def asked(**kw):
        agent = OutreachAgent(settings_factory(db_path=str(tmp_path / "r.db"), **kw))
        agent.ai_gen = _ai()
        agent._reply_is_interested(
            "Re: Partnership", "Hmm, maybe. Who are you exactly?"
        )
        return bool(agent.ai_gen.prompts)

    assert asked() is False
    assert asked(ai_reply_classification_enabled=True) is True


def test_reply_classification_counts_as_an_ai_seeing_data(settings_factory):
    assert ai_in_use(settings_factory(ai_reply_classification_enabled=True))
    # A configured AI alone sends nobody's data any more.
    assert not ai_in_use(settings_factory(llm_provider="openai_compatible", LLM_BASE_URL="http://127.0.0.1:11434/v1", LLM_MODEL="test-model"))


def test_a_lead_alert_keeps_the_reply_without_the_quote(
    settings_factory, tmp_path, monkeypatch
):
    from agent import OutreachAgent

    agent = OutreachAgent(
        settings_factory(db_path=str(tmp_path / "l.db"), lead_alert_email="sales@co.it")
    )
    agent.db.add_contact(agent.db.log_search("q"), "anna@brand.de")
    monkeypatch.setattr(agent, "_send_lead_alert", lambda entry, recipients: True)
    agent._handle_interested_replies(
        [
            {
                "email": "anna@brand.de",
                "subject": "Re: Offer",
                "body": "Yes, send the price list.\n\nOn Mon, SUPPLECO wrote:\n> secret quote",
            }
        ]
    )
    excerpt = agent.db.conn.execute("SELECT reply_excerpt FROM lead_alerts").fetchone()[
        0
    ]
    assert "price list" in excerpt and "secret quote" not in excerpt
