"""A reply must be readable whatever charset the sender's mail system used.

get_payload(decode=True) reverses the Content-Transfer-Encoding and stops
there — applying the MIME charset is the caller's job, by design. The caller
did `payload.decode(errors="replace")`, which assumes UTF-8 for every message
on earth, so every non-UTF-8 accented byte became U+FFFD.

The prospects here are Italian, German, French and Spanish companies, and
plenty of corporate mail systems still send ISO-8859-1 or windows-1252. The
consequence was measured, not theorised: a French prospect writing "Merci de
me désabonner de votre liste" arrived as "d�sabonner", is_optout()
returned False, and the agent would have carried on emailing someone who had
asked it to stop. That is GDPR Art. 21, and it fails silently — the classifier
reports "no opt-out found", which is indistinguishable from a healthy inbox.
"""

import email

import pytest

from modules import reply_intent
from modules.imap_monitor import ImapMonitor


@pytest.fixture
def monitor():
    return ImapMonitor(
        db=None, imap_host="h", imap_port=993, username="u", password="p"
    )


def _message(charset, text, ctype="text/plain"):
    return email.message_from_bytes(
        b"From: prospect@client.fr\r\nTo: me@corp.com\r\n"
        b"Subject: Re: proposition\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: " + ctype.encode() + b"; charset=" + charset.encode() + b"\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n" + text.encode(charset)
    )


# ── The opt-out must survive the encoding ──────────────────────────────────


@pytest.mark.parametrize("charset", ["iso-8859-1", "windows-1252", "utf-8"])
def test_a_french_opt_out_is_caught_in_any_charset(monitor, charset):
    """The failure that prompted this: 'désabonner' needs its accent, and
    there is no unaccented alternative in the opt-out pattern list."""
    msg = _message(charset, "Merci de me désabonner de votre liste.")
    body = monitor._get_body(msg)

    assert "�" not in body, f"the body was corrupted decoding {charset}"
    assert reply_intent.is_optout(reply_intent.strip_quoted(body)) is True, (
        "a prospect asked to be removed and the agent did not hear it"
    )


@pytest.mark.parametrize("charset", ["iso-8859-1", "windows-1252", "utf-8"])
def test_an_interested_french_reply_is_still_escalated(monitor, charset):
    """The same corruption downgraded a hot lead to 'unclear', so nobody was
    ever told the prospect said yes."""
    msg = _message(charset, "Nous sommes intéressés, envoyez un échantillon.")
    body = monitor._get_body(msg)

    assert "�" not in body
    assert reply_intent.classify(body=reply_intent.strip_quoted(body)) == "interested"


@pytest.mark.parametrize(
    "charset,text",
    [
        ("iso-8859-1", "Non siamo interessati, grazie."),
        ("windows-1252", "Bitte löschen Sie meine Adresse."),
        ("iso-8859-15", "No nos interesa, gracias."),
        ("utf-8", "Grazie, però non fa per noi."),
    ],
)
def test_other_european_replies_decode_intact(monitor, charset, text):
    """Italian, German and Spanish prospects are the other three-quarters of
    the target market."""
    body = monitor._get_body(_message(charset, text))
    assert "�" not in body
    assert body.strip() == text


# ── Multipart and HTML take the same path ──────────────────────────────────


def test_a_multipart_part_uses_its_own_charset(monitor):
    """Each PART declares its own charset; the multipart branch was decoding
    every one of them as UTF-8 too."""
    raw = (
        b"From: p@client.fr\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain; charset=iso-8859-1\r\n\r\n"
        + "Merci de me désabonner.".encode("iso-8859-1")
        + b"\r\n--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        # utf-8 by default here, deliberately unlike the latin-1 part above:
        # the two parts of one message declare different charsets.
        + "<p>Merci de me désabonner.</p>".encode()
        + b"\r\n--B--\r\n"
    )
    body = monitor._get_body(email.message_from_bytes(raw))

    assert "�" not in body
    assert reply_intent.is_optout(body) is True


def test_an_html_only_reply_decodes_by_its_charset(monitor):
    msg = _message(
        "iso-8859-1",
        "<html><body><p>Merci de me désabonner.</p></body></html>",
        ctype="text/html",
    )
    body = monitor._get_body(msg)

    assert "�" not in body
    assert reply_intent.is_optout(body) is True


# ── Degrade, never crash ───────────────────────────────────────────────────


def test_an_unknown_charset_falls_back_instead_of_raising(monitor):
    """A misdeclared or invented charset name must not take down the polling
    loop - an unattended agent that dies on one odd message stops seeing every
    opt-out after it."""
    raw = (
        b"From: p@client.fr\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=x-not-a-real-charset\r\n\r\n"
        + "Merci de me désabonner.".encode("iso-8859-1")
    )
    body = monitor._get_body(email.message_from_bytes(raw))
    assert "desabonner" in body.lower() or "désabonner" in body.lower()


def test_an_encoded_subject_survives_its_charset(monitor):
    """The SUBJECT feeds the same classifier, and _decode_header_value had the
    identical errors="replace" flaw: its fallback chain could never advance
    past its first entry, because a lossy decode never fails."""
    header = "=?iso-8859-1?Q?D=E9sabonnement_imm=E9diat?="
    assert monitor._decode_header_value(header) == "Désabonnement immédiat"


def test_a_subject_in_an_unknown_charset_falls_back(monitor):
    """An invented charset name must reach a codec that can read the bytes,
    rather than stopping at utf-8 and substituting U+FFFD."""
    header = "=?x-not-a-charset?Q?D=E9sabonnement?="
    assert "�" not in monitor._decode_header_value(header)


def test_a_mislabelled_body_still_yields_its_ascii_words(monitor):
    """Declared UTF-8, actually latin-1 - common from misconfigured servers.
    The accented character is lost, but the surrounding words must survive so
    a multi-word opt-out phrase can still match."""
    raw = (
        b"From: p@client.fr\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        + "Please remove me from your list, merci déjà.".encode("iso-8859-1")
    )
    body = monitor._get_body(email.message_from_bytes(raw))
    assert reply_intent.is_optout(body) is True
