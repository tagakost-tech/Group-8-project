"""
Reply-intent classification for the B2B Outreach Agent.

Reads a prospect's reply and decides whether it signals genuine interest
("yes, let's talk", "send me a quote") so a human can be alerted and take over.

Deterministic and free - no API needed. Multilingual (EN/IT/DE/FR/ES) because
prospects are European. The AI layer may refine the verdict when available, but
this module always produces an answer on its own.
"""

import logging
import re

logger = logging.getLogger(__name__)

INTERESTED = "interested"
NOT_INTERESTED = "not_interested"
UNCLEAR = "unclear"

# Explicit rejections - checked FIRST so "not interested" never reads as
# "interested" just because it contains the word.
NEGATIVE_PATTERNS = [
    r"\bnot interested\b",
    r"\bno[, ]+thank",
    r"\bno thanks\b",
    r"\bnot at (this|the) (time|moment)\b",
    r"\bnot relevant\b",
    r"\bnot looking\b",
    r"\bwe (already|currently) (have|work with)\b",
    r"\bno longer\b",
    r"\bunsubscribe\b",
    r"\bremove me\b",
    # "stop" needs an object. Bare \bstop\b is correctly word-bounded and still
    # wrong, because the word is ordinary English: "can we stop by for a quick
    # call next week?" was classified NOT_INTERESTED, and since the negative
    # patterns are checked first and return immediately, the positive signal in
    # the same sentence was never read. A hot lead, dropped silently, which is
    # the failure this module's docstring calls the worst one.
    #
    # OPTOUT_PATTERNS below already qualifies it this way; a genuine "stop" on
    # its own is an opt-out, and is_optout() handles that case.
    r"\bstop (sending|emailing|contacting|mailing)\b",
    r"\bdo not contact\b",
    r"\bdon't contact\b",
    # Italian / German / French / Spanish
    r"\bnon (siamo )?interessat",
    r"\bno grazie\b",
    r"\bnon ci interessa\b",
    r"\bkein interesse\b",
    r"\bnicht interessiert\b",
    r"\bpas int[ée]ress",
    r"\bno (nos )?interesa\b",
]

# Signals of genuine buying interest.
POSITIVE_PATTERNS = [
    r"\b(we|i)('| a)?m? (are |am )?interested\b",
    r"\binterested in\b",
    r"\bsounds (good|great|interesting)\b",
    r"\bhappy to (talk|chat|discuss)\b",
    r"\b(let'?s|we can|we could) (talk|chat|discuss|schedule|set ?up|arrange)\b",
    r"\btell me more\b",
    r"\bmore (information|info|details)\b",
    r"\bsend (me|us) (more|the|a|your)\b",
    r"\b(would|we'?d) like to\b",
    r"\bcan you (send|provide|share|quote)\b",
    r"\b(price|pricing|price list|quote|quotation|offer)\b",
    r"\b(minimum order|moq|lead time|sample|samples|catalogue|catalog)\b",
    r"\b(book|schedule|arrange) a (call|meeting|chat)\b",
    r"\bavailable (for|to) (a )?(call|meeting|chat)\b",
    r"\bwhen (are|can) (you|we)\b",
    r"\bcall me\b",
    r"\blooks interesting\b",
    r"\b(yes|sure|certainly)\b.{0,40}\b(interested|talk|call|meeting|discuss)\b",
    # Italian
    r"\bsiamo interessat",
    r"\bmi interessa\b",
    r"\bci interessa\b",
    r"\bvorremmo\b",
    r"\bpreventivo\b",
    r"\bcampion[ei]\b",
    r"\blistino\b",
    r"\bmaggiori informazioni\b",
    r"\bvolentieri\b",
    # German / French / Spanish
    r"\bwir sind interessiert\b",
    r"\bangebot\b",
    r"\bmuster\b",
    r"\bnous sommes int[ée]ress",
    r"\bdevis\b",
    r"\b[ée]chantillon",
    r"\bestamos interesad",
    r"\bpresupuesto\b",
    r"\bmuestra\b",
]

_NEG_RE = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_PATTERNS]
_POS_RE = [re.compile(p, re.IGNORECASE) for p in POSITIVE_PATTERNS]

# Quoted original message / signature markers - text below these is OUR email
# coming back in the reply, not what the prospect wrote.
_QUOTE_MARKERS = [
    r"^\s*>",
    r"-{2,}\s*original message",
    r"^from:\s",
    r"^da:\s",
    r"^von:\s",
    r"\bwrote:\s*$",
    r"\bha scritto:\s*$",
    r"\bschrieb:\s*$",
    r"unsubscribe here",
    r"if this isn'?t relevant",
    # The opt-out line of our Italian emails. Without it the Italian footer
    # below, which names "cancellami" and the right to erasure, read as the
    # recipient's own words.
    r"cancellarsi qui",
]
_QUOTE_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _QUOTE_MARKERS]


# A line that ends what the person wrote: a sign-off, or the "--" that opens
# a signature. What follows is their name, title and phone number.
_SIGN_OFF = re.compile(
    r"^\s*(?:--|best(?: regards)?|kind regards|regards|many thanks|thanks|"
    r"thank you|cordiali saluti|distinti saluti|saluti|grazie|"
    r"mit freundlichen gr(?:ü|ue)(?:ß|ss)en|viele gr(?:ü|ue)(?:ß|ss)e|"
    r"beste gr(?:ü|ue)(?:ß|ss)e|cordialement|bien cordialement|saludos|"
    r"un saludo|atentamente)\s*[,.!]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def cut_signature(text: str) -> str:
    """The text above the first sign-off line, without the signature."""
    m = _SIGN_OFF.search(text or "")
    return (text[: m.start()] if m else text or "").strip()


def strip_quoted(body: str) -> str:
    """Keep only the newly written part of a reply, dropping the quoted
    original below markers like '>' or 'On ... wrote:'.

    Handles both top- and bottom-posting. Cutting at the earliest marker
    anywhere in the body discarded the ENTIRE message when the prospect wrote
    BELOW the quote - standard in many European clients and in Gmail's trimmed
    view - because the marker then sits at offset 0. An explicit
    "Yes, very interested - please send pricing" was reduced to an empty string
    and classified 'unclear', so no hot-lead alert ever fired for it.
    """
    if not body:
        return ""
    text = body.replace("\r\n", "\n")

    cut = len(text)
    for rx in _QUOTE_RE:
        m = rx.search(text)
        if m and m.start() < cut:
            cut = m.start()

    above = text[:cut].strip()
    if above:
        return above

    # Nothing above the quote means the reply was bottom-posted. Recover the
    # final run of unquoted lines rather than returning "".
    def _is_quote(line: str) -> bool:
        return line.lstrip().startswith(">") or any(rx.match(line) for rx in _QUOTE_RE)

    trailing = []
    for line in reversed(text.split("\n")):
        if _is_quote(line):
            if trailing:
                break  # reached the quote block above the new text
            continue  # still inside the trailing quote, keep walking up
        trailing.append(line)
    return "\n".join(reversed(trailing)).strip()


# The subset of NEGATIVE_PATTERNS that is an explicit request to STOP being
# contacted (as opposed to merely "not interested right now"). These must lead
# to suppression - GDPR Art. 21 right to object, which Italy enforces strictly.
OPTOUT_PATTERNS = [
    r"\bunsubscribe\b",
    r"\bremove me\b",
    r"\bremove (us|my|our)\b",
    r"\btake me off\b",
    r"\bopt[- ]?out\b",
    r"\bdo not contact\b",
    r"\bdon'?t contact\b",
    # "mailing" kept in step with NEGATIVE_PATTERNS above. "Please stop mailing
    # this address" is as clear an opt-out as "stop emailing", and the two lists
    # disagreeing means a reply that ends the sequence without being recorded as
    # a withdrawal of consent.
    r"\bstop (sending|emailing|contacting|mailing)\b",
    r"\bno longer wish\b",
    r"\bcancellami\b",
    r"\bcancellate(ci|mi)\b",
    # NOT a bare \bcancellare\b: it is the everyday Italian verb "to cancel"
    # ("dobbiamo cancellare la riunione" = we must cancel the meeting). Matching
    # it permanently suppressed engaged prospects who were proposing to
    # reschedule. Only anchor it to what is being cancelled.
    (
        r"\bcancellare\s+(la\s+)?(mia\s+|nostra\s+)?"
        r"(iscrizione|email|e-mail|newsletter|registrazione)\b"
    ),
    r"\bcancellar(mi|ci)\b",
    r"\bnon contattarmi\b",
    r"\bnon contattateci\b",
    # The other common Italian ways (native-speaker review): a pronoun joined
    # to the verb, or "più", is what makes them a request about the sender.
    # "Rimuovere il collega dalla copia" and "cancellare l'appuntamento" carry
    # neither. A data request among them ("eliminatemi dal database") is
    # caught first by dsr_kind.
    r"\bdisiscri\w*",
    r"\brimuov(i|ete)(mi|ci)\b",
    r"\btogli(ete)?(mi|ci)\b",
    r"\belimina(te)?(mi|ci)\b",
    r"\bnon (mi |ci )?(scriv|contatt|invi)\w*\s+pi[uùú]\b(?!\s+di\b)",
    r"\bnon (voglio|vogliamo|desidero|desideriamo) pi[uùú] ricevere\b",
    (
        r"\bnon (voglio|vogliamo|desidero|desideriamo) ricevere\b.{0,30}"
        r"\b(e-?mail|mail|messaggi|comunicazioni|newsletter)\b"
    ),
    r"\babmelden\b",
    r"\bkeine (weiteren )?e?-?mails\b",
    r"\bd[ée]sabonner\b",
    r"\bne plus (me )?contacter\b",
    r"\bdar(me|nos) de baja\b",
    r"\bno (me )?contacten\b",
    # Wider German, French and Spanish phrasing (two patterns each was too few).
    r"\bstreichen sie (mich|uns)\b",
    r"\bnicht mehr (kontaktieren|anschreiben|anrufen)\b",
    r"\b(kontaktieren|anschreiben) sie (mich|uns) nicht mehr\b",
    r"\b(austragen|abbestellen)\b",
    r"\bne plus m'?envoyer\b",
    r"\bne m'?envoyez plus\b",
    r"\bretirez[- ]moi\b",
    r"\bno me (env[ií]en|escriban) m[aá]s\b",
    r"\bqu[ií]ten(me|nos)\b",
    r"\bdejen de (enviar|escribir)\b",
]
_OPTOUT_RE = [re.compile(p, re.IGNORECASE) for p in OPTOUT_PATTERNS]
# "Stop" as the whole reply, in any language: in a sentence it is an ordinary
# word ("lo stop della produzione", "stop by for a call").
_ONLY_STOP = re.compile(r"\s*stop\s*[.!]*\s*", re.IGNORECASE)

# ── Data-subject requests (GDPR Arts. 15 and 17) ────────────────────────────
# Checked before the opt-out phrases: "cancellatemi dal vostro database" is
# also an opt-out phrase, and filed as one it got no record and no one-month
# deadline (Art. 12(3)). A request needs a verb AND a personal-data object -
# "delete my data", "remove me from your database" - so "please delete the
# previous email" stays a reply. A bare mention of GDPR is not a request.
# A question about the source ("where did you get my email?") is an access
# request: Art. 15(1)(g) entitles them to the answer.
ERASURE_PATTERNS = [
    # English
    (
        r"\b(delete|erase|remove|purge|wipe)\s+(all\s+)?(of\s+)?(my|our)\s+"
        r"(personal\s+)?(data|details|information|contact\s+details|records?|"
        r"e-?mail(\s+address)?|address)\b"
    ),
    (
        r"\b(delete|erase|remove)\s+(me|us)\s+from\s+(your|the)\s+"
        r"(database|records|systems?|crm|files)\b"
    ),
    r"\bright\s+to\s+(be\s+forgotten|erasure)\b",
    # Italian
    (
        r"\b(cancell|elimin|rimuov|rimozion)\w*\b.{0,40}"
        r"\b(dati|database|archivi\w*|anagrafic\w*)\b"
    ),
    r"\bdiritto\s+all'?\s*oblio\b",
    # German
    (
        r"\b(lösch|entfern)\w*\b.{0,40}"
        r"\b(daten|datenbank|adresse|kontaktdaten|e-?mail-?adresse)\b"
    ),
    r"\brecht\s+auf\s+(vergessenwerden|löschung)\b",
    # French
    (
        r"\b(supprim|effac)\w*\b.{0,40}"
        r"\b(données|coordonnées|adresse|base\s+de\s+données|fichier)\b"
    ),
    r"\bdroit\s+à\s+l'?\s*(oubli|effacement)\b",
    # Spanish
    (
        r"\b(borr|elimin|supr)\w*\b.{0,40}"
        r"\b(datos|base\s+de\s+datos|direcci[oó]n|correo)\b"
    ),
    r"\bderecho\s+(al\s+olvido|de\s+supresi[oó]n)\b",
]

ACCESS_PATTERNS = [
    # English
    (
        r"\b(where|how)\s+did\s+you\s+(get|find|obtain)\s+(my|our)\s+"
        r"(e-?mail|address|details|data|contact)"
    ),
    r"\b(what|which)\s+(personal\s+)?data\s+(do\s+)?you\s+(hold|have|keep|store)\b",
    r"\bsubject\s+access\s+request\b",
    r"\b(access\s+to|copy\s+of)\s+(my|our)\s+(personal\s+)?data\b",
    # Italian
    r"\bchi\s+vi\s+ha\s+(dato|fornito|passato)\b",
    r"\bdove\s+avete\s+(preso|trovato|ottenuto)\b",
    r"\bquali\s+dati\b.{0,30}\b(avete|conservate|trattate)\b",
    # German
    r"\bwoher\s+haben\s+sie\b",
    r"\bwelche\s+daten\b.{0,30}\b(gespeichert|haben\s+sie)\b",
    r"\bauskunft\b.{0,30}\bdaten\b",
    # French
    r"\b(où|comment)\s+avez[- ]vous\s+(obtenu|trouvé|eu)\b",
    r"\bdroit\s+d'?\s*accès\b",
    r"\bquelles\s+données\b",
    # Spanish
    r"\bde\s+d[oó]nde\s+(sacaron|obtuvieron|han\s+sacado|han\s+obtenido)\b",
    r"\bderecho\s+de\s+acceso\b",
    r"\bqu[eé]\s+datos\b.{0,30}\b(tienen|guardan|tratan)\b",
]

_ERASURE_RE = [re.compile(p, re.IGNORECASE) for p in ERASURE_PATTERNS]
_ACCESS_RE = [re.compile(p, re.IGNORECASE) for p in ACCESS_PATTERNS]


def dsr_kind(subject: str = "", body: str = "") -> str | None:
    """ "erasure", "access" or None: is this reply a data-subject request?

    Pass the ALREADY-STRIPPED body (see strip_quoted). Erasure wins over
    access when both appear: it is the stronger request."""
    haystack = f"{subject or ''}\n{body or ''}"
    if any(rx.search(haystack) for rx in _ERASURE_RE):
        return "erasure"
    if any(rx.search(haystack) for rx in _ACCESS_RE):
        return "access"
    return None


def is_optout(subject: str = "", body: str = "") -> bool:
    """True when the sender explicitly asks to stop being contacted.

    Pass the ALREADY-STRIPPED body (see strip_quoted) so our own email footer
    ("unsubscribe here") quoted underneath can't trigger a false positive."""
    haystack = f"{subject or ''}\n{body or ''}"
    if _ONLY_STOP.fullmatch(cut_signature(body or "")):
        return True
    return any(rx.search(haystack) for rx in _OPTOUT_RE)


def is_decline(subject: str = "", body: str = "") -> bool:
    """True for a reply that declines and shows no sign of interest.

    A decline is treated as an objection (decision L1.16), which is
    permanent. classify() lets a negative phrase win outright; here any sign
    of interest left once the negative phrases are taken out ("we already
    work with someone, but send a quote") leaves the reply for a person.
    Interest is read from their own text only: the subject is mostly ours,
    and an operator's subject may say "offer" or "samples"."""
    fresh = strip_quoted(body or "")
    if not any(rx.search(f"{subject or ''}\n{fresh}") for rx in _NEG_RE):
        return False
    # "non siamo interessati" holds the positive "siamo interessat".
    rest = fresh
    for rx in _NEG_RE:
        rest = rx.sub(" ", rest)
    return not any(rx.search(rest) for rx in _POS_RE)


def classify(subject: str = "", body: str = "") -> str:
    """Return INTERESTED / NOT_INTERESTED / UNCLEAR for a prospect's reply.

    Only the newly written text is considered, so our own quoted email (which
    contains words like 'interested') can't trigger a false positive. Interest
    is read from that text alone: the subject is mostly ours ("Re: <our
    subject>"), and one saying "offer" or "quote" made every reply read as
    interested. A decline counts in the subject too."""
    fresh = strip_quoted(body or "")

    for rx in _NEG_RE:
        if rx.search(f"{subject or ''}\n{fresh}"):
            return NOT_INTERESTED
    for rx in _POS_RE:
        if rx.search(fresh):
            return INTERESTED
    return UNCLEAR
