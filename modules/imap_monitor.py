"""
IMAP inbox monitor for bounce, reply, and unsubscribe detection.
Connects via IMAP4_SSL to scan and classify incoming emails.
"""

import email
import hashlib
import imaplib
import logging
import re
import sqlite3
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.errors import HeaderParseError, MessageError
from email.header import decode_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import ClassVar

from modules import clock, optout_spool
from modules.company_block import block_company
from modules.database import Database, is_address, undoes_a_failed_write
from modules.microsoft_signin import (
    MicrosoftSignIn,
    SignInRefused,
    explain_mailbox_refusal,
    imap_authenticate,
)

logger = logging.getLogger(__name__)

# A complete read counts for this many check intervals: one is the normal gap
# between reads, the rest room for a slow scan or a send already under way.
READ_COUNTS_FOR = 3


def watermark_key(username: str) -> str:
    """The agent_state key holding when the last complete scan of an inbox
    began. run_check writes it only once the whole window was acted on."""
    return f"imap_last_scan::{(username or '').strip().lower()}"


def inbox_read(
    db: Database, inboxes: list[str], check_interval_minutes: int
) -> tuple[bool, str]:
    """Whether every inbox in ``inboxes`` has had a complete read just now.

    With IMAP on, the inbox is what reads an opt-out or a data request sent
    back by email, and the bounces the bounce guard counts. Being switched on
    proves none of that: a mailbox whose login fails logs IMAP_ERROR on every
    check and reads nothing. The record is the scan watermark in the
    database, so an agent that reads no mail itself - the dashboard's Direct
    Send agent - sees the campaign agent's reads. doctor reports this and the
    send check enforces it, from this one rule.
    """
    limit = timedelta(seconds=READ_COUNTS_FOR * max(60, check_interval_minutes * 60))
    now = clock.now()
    unread = []
    names = list(dict.fromkeys((u or "").strip().lower() for u in inboxes))
    for name in names:
        mark = db.get_state(watermark_key(name))
        try:
            last = datetime.fromisoformat(mark) if mark else None
        except ValueError:
            last = None  # corrupt: no proof of a read
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=clock.get_timezone())
        if last is None or now - last > limit:
            when = (
                last.astimezone(clock.get_timezone()).strftime("%Y-%m-%d %H:%M")
                if last
                else "never"
            )
            unread.append(f"{name or '(no login)'} (last {when})")
    minutes = int(limit.total_seconds() // 60)
    if unread:
        return False, (
            f"no complete read of {', '.join(unread)} in the last {minutes} "
            "minutes - an opt-out emailed there would go unread until one succeeds"
        )
    return True, f"{len(names)} inbox(es) read completely in the last {minutes} minutes"


def unread_gap(
    db: Database, account: str, key: str, folder: str, window_start: datetime
) -> str | None:
    """The report of the mail between a folder's last complete scan (the
    agent_state key ``key``) and ``window_start``, the furthest a scan reaches:
    only a person reading that stretch can find an opt-out, a data request or
    a bounce in it. None when the window reaches the last scan.
    """
    mark = db.get_state(key)
    try:
        last = datetime.fromisoformat(mark) if mark else None
    except ValueError:
        last = None  # corrupt: inbox_read and _lookback_hours report it
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=clock.get_timezone())
    if last >= window_start:
        return None
    # IMAP's SINCE is date-only, in the server's timezone, so part of this
    # stretch may have been read; the window is all the agent can vouch for.
    return (
        f"Mail to {account} ({folder}) received {last:%Y-%m-%d %H:%M} to "
        f"{window_start:%Y-%m-%d %H:%M} may not have been read: it is beyond the "
        f"{ImapMonitor.MAX_LOOKBACK_HOURS // 24}-day look-back, and no read will "
        "reach it. Check that stretch of the mailbox by hand for opt-outs, data "
        "requests and bounces."
    )


def inbox_gaps(db: Database, inboxes: list[str]) -> tuple[bool, str]:
    """Whether mail beyond the look-back needs checking by hand.

    The read that resumes after a long outage records what it could not reach
    as an IMAP_GAP row (run_check). doctor repeats those rows for a month: a
    data request in the stretch is due a month after it arrived (GDPR Art.
    12(3)). Before that read, an inbox's watermark already shows the mail it
    will not reach, and doctor is what an operator runs before restarting a
    stopped agent. Only INBOX's watermark: it moves with every complete read,
    while a junk folder since gone would leave its own behind for good.
    """
    now = clock.now()
    since = clock.add_months(now, -1).strftime("%Y-%m-%d %H:%M:%S")
    found = [row["details"] for row in db.get_activity_since("IMAP_GAP", since)]
    window_start = now - timedelta(hours=ImapMonitor.MAX_LOOKBACK_HOURS)
    for name in dict.fromkeys((u or "").strip().lower() for u in inboxes):
        gap = unread_gap(db, name, watermark_key(name), "INBOX", window_start)
        if gap:
            found.append(gap)
    if found:
        return False, " ".join(found)
    return True, "no mail beyond the look-back in the last month"


class ImapMonitor:
    """Monitors an IMAP inbox for bounces, unsubscribes, and replies."""

    signin: MicrosoftSignIn | None = None

    def __init__(
        self,
        db: Database,
        imap_host: str,
        imap_port: int,
        username: str,
        password: str,
        signin: MicrosoftSignIn | None = None,
    ):
        self.db = db
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.username = username
        self.password = password
        # Given, the inbox is opened with a Microsoft token (XOAUTH2) and has
        # no password.
        self.signin = signin
        # Consecutive failures reading the scan watermark; escalated to the
        # dashboard once it is clearly persistent rather than a locked-DB blip.
        self._watermark_errors = 0

    # Socket timeout for every IMAP operation. Without one the socket is fully
    # blocking, and _maybe_check_imap runs on the SAME thread as the send loop:
    # a half-open connection (NAT/firewall idle drop, provider stall mid-FETCH)
    # froze sending, bounce processing, opt-outs and hot-lead alerts with no log
    # line at all - and the IMAP_STALE alarm could not fire because it lives
    # inside the frozen call. SMTP already sets timeout=30; IMAP was missed.
    SOCKET_TIMEOUT = 30

    def _connect(self):
        """Establish an IMAP4_SSL connection and login."""
        try:
            # The token first: a refused sign-in opens no connection.
            token = self.signin.token() if self.signin is not None else None
            # Without an explicit context imaplib uses the standard library's
            # UNVERIFIED one: no certificate or host-name check, so anyone on
            # the path could read the password at LOGIN and every reply after.
            mail = imaplib.IMAP4_SSL(
                self.imap_host,
                self.imap_port,
                ssl_context=ssl.create_default_context(),
                timeout=self.SOCKET_TIMEOUT,
            )
            if token is not None:
                imap_authenticate(mail, self.username, token)
            else:
                mail.login(self.username, self.password)
            return mail
        except SignInRefused as e:
            logger.error("Authentication error: Microsoft sign-in refused: %s", e)
            return None
        except imaplib.IMAP4.error as e:
            if self.signin is not None:
                logger.error(
                    "Authentication error: %s (%s)",
                    explain_mailbox_refusal("IMAP", str(e)),
                    e,
                )
            else:
                logger.error("Authentication error: %s", e)
            return None
        except (OSError, UnicodeError) as e:
            # Everything socket- and TLS-related arrives as an OSError:
            # ssl.SSLError, socket.gaierror and the SOCKET_TIMEOUT above
            # (TimeoutError) all subclass it. UnicodeError is what imaplib
            # raises encoding a non-ASCII password into the LOGIN command.
            logger.error("Connection error: %s", e)
            return None

    def _decode_header_value(self, value):
        """Decode an email header value to a plain string.

        Never raises. A header this cannot decode used to poison the whole
        mailbox for a fortnight:

        - decode_header() raises HeaderParseError on a malformed encoded-word.
        - errors="replace" does NOT protect against an unknown charset *name*:
          b"...".decode("unknown-8bit") raises LookupError, and unknown-8bit is
          a registered charset MTAs emit specifically in DSN subject lines - so
          bounces were the likeliest carrier.
        - A charset name can pass that lookup and still refuse: the "undefined"
          codec raises a plain UnicodeError, a NUL in the name a plain ValueError.

        Any of these reached the generic per-message handler, the message
        was never expunged, so every later scan re-fetched and re-failed on it.
        The watermark never advanced, the lookback grew to the 14-day maximum,
        and IMAP_STALE claimed bounces and opt-outs were not being processed
        while they actually were. It only self-healed once the poison message
        aged out of the window - during which a genuine credential expiry was
        indistinguishable from the stuck message.
        """
        if value is None:
            return ""
        try:
            decoded_parts = decode_header(value)
        except (HeaderParseError, UnicodeError, ValueError, TypeError):
            logger.warning("Un-parseable header, using the raw value: %.80s", value)
            return str(value)

        result = []
        for part, charset in decoded_parts:
            if not isinstance(part, bytes):
                result.append(part)
                continue
            # Strictly, and latin-1 last - see _decode_payload. With
            # errors="replace" the first candidate always "succeeds", so an
            # unknown charset name silently became U+FFFD instead of falling
            # through to a codec that could actually read the bytes.
            for candidate in (charset, "utf-8", "latin-1"):
                if not candidate:
                    continue
                try:
                    result.append(part.decode(candidate))
                    break
                except (LookupError, ValueError):
                    continue  # unknown charset name, a refusal, or the wrong guess
            else:
                # latin-1 maps every byte, so this is close to unreachable.
                result.append(part.decode("utf-8", errors="replace"))
        return " ".join(result)

    def _header_text(self, msg, name: str) -> str:
        """A header's value as text, decoded as From and Subject are.

        For a value carrying raw 8-bit bytes (an accented file name in
        Content-Type, a localised Auto-Submitted) compat32's msg.get() returns
        an email.header.Header, not a str, and Header has no .lower(). Read
        raw, one such reply, bounce or read receipt was unreadable: never acted
        on, the read incomplete and the watermark held, and with it every live
        send, which waits for a complete read.
        """
        return self._decode_header_value(msg.get(name))

    @staticmethod
    def _decode_payload(part, payload: bytes) -> str:
        """Decode a body part using the charset it actually declares.

        get_payload(decode=True) reverses the Content-Transfer-Encoding
        (base64, quoted-printable) and deliberately stops there - it never
        applies the MIME charset, by design. A bare payload.decode() therefore
        assumes UTF-8 for every message on earth, and every non-UTF-8 byte
        becomes U+FFFD under errors="replace".

        That silently defeated the classifier for exactly the prospects this
        agent targets. A French company on a legacy mail system writing
        "Merci de me desabonner" with the accent sends ISO-8859-1; the body
        arrived as "d�sabonner", is_optout() returned False, and the
        agent would have gone on emailing someone who had asked it to stop -
        GDPR Art. 21. The same corruption downgraded an interested French
        reply to "unclear", so the hot lead was never escalated either.

        Same fallback chain as _decode_header_value above, for the same
        reason: an unknown or misdeclared charset must degrade to a readable
        guess rather than to nothing.

        Each candidate is tried STRICTLY. errors="replace" never raises, so a
        chain built on it stops at its first entry: an unknown charset name
        fell through to utf-8, utf-8 "succeeded" by substituting U+FFFD for
        every high byte, and latin-1 was never reached. The fallback only
        works if a wrong guess is allowed to fail. latin-1 is last because it
        maps every byte and would otherwise mask a genuine UTF-8 body.
        """
        for candidate in (part.get_content_charset(), "utf-8", "latin-1"):
            if not candidate:
                continue
            try:
                return payload.decode(candidate)
            except (LookupError, UnicodeDecodeError):
                continue  # unknown charset name, or the wrong guess
        # latin-1 maps every byte, so reaching here means the payload is not
        # bytes at all. Lossy is still better than losing the message.
        return payload.decode("utf-8", errors="replace")

    @staticmethod
    def _html_to_text(html: str) -> str:
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(html, "html.parser").get_text(" ")
        except (ImportError, ValueError, TypeError) as e:
            # bs4 not installed (ImportError), the parser feature missing
            # (FeatureNotFound subclasses ValueError) or a non-str payload.
            # The raw markup still yields keywords, so this degrades rather
            # than fails - but degrading silently hides a missing dependency.
            logger.debug("HTML-to-text fell back to the raw markup: %s", e)
            return html

    def _get_body(self, msg):
        """Extract a text body, preferring text/plain and falling back to the
        stripped text of an HTML part (so HTML-only mails still yield keywords)."""
        if msg.is_multipart():
            html_fallback = ""
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                ctype = part.get_content_type()
                try:
                    payload = part.get_payload(decode=True)
                except (MessageError, ValueError, TypeError) as e:
                    # A broken transfer-encoding: base64/quoted-printable
                    # defects surface as MessageError or as ValueError
                    # (binascii.Error). One unreadable part must not cost us
                    # the sibling part that carries the text.
                    logger.debug("Undecodable %s part, skipping it: %s", ctype, e)
                    payload = None
                if payload is None:
                    continue
                text = self._decode_payload(part, payload)
                if ctype == "text/plain":
                    return text
                if ctype == "text/html" and not html_fallback:
                    html_fallback = text
            return self._html_to_text(html_fallback) if html_fallback else ""
        try:
            payload = msg.get_payload(decode=True)
            text = self._decode_payload(msg, payload) if payload else ""
            if msg.get_content_type() == "text/html":
                return self._html_to_text(text)
            return text
        except (MessageError, ValueError, TypeError) as e:
            # Same decode failures as the multipart branch above. An empty
            # body only costs us body-based opt-out detection, so it is the
            # right fallback - but it has to be visible, since a prospect's
            # "please remove me" disappearing is a GDPR Art. 21 miss.
            logger.debug("Could not extract a body from the message: %s", e)
            return ""

    # Subjects that unambiguously mean a permanent failure. "delivery status
    # notification" is deliberately NOT here: Microsoft 365 and Gmail use that
    # exact subject for DELAY notices too ("(Delay)" - the message is still
    # queued and will probably arrive), so it is resolved from the DSN body.
    _FAILURE_SUBJECTS: ClassVar[list[str]] = [
        "undeliverable",
        "delivery failed",
        "delivery failure",
        "returned mail",
        "message not delivered",
        "mailbox not found",
        "mail delivery failed",
        "failure notice",
        "returned to sender",
        "unzustellbar",
        "non recapitabile",
        "mancata consegna",
    ]
    _AMBIGUOUS_DSN_SUBJECTS: ClassVar[list[str]] = [
        "delivery status notification",
        "delivery report",
    ]

    @staticmethod
    def _dsn_groups(msg):
        """Per-recipient (final_recipient, action, status) triples from an
        RFC 3464 report.

        Parsed as GROUPS rather than three independent searches: a DSN for an
        address that expands to a distribution list carries one block per
        member, so taking the first Action/Status/Final-Recipient found anywhere
        can pair a verdict with the wrong recipient - or read 'delivered' from
        member 1 and miss the hard failure of member 2 entirely."""
        if msg is None:
            return []
        blocks = []
        try:
            parts = (
                [
                    p
                    for p in msg.walk()
                    if p.get_content_type() == "message/delivery-status"
                ]
                if msg.is_multipart()
                else []
            )
            for part in parts:
                payload = part.get_payload()
                if isinstance(payload, list):
                    text = "\n\n".join(str(x) for x in payload)
                else:
                    text = str(payload or "")
                blocks.extend(re.split(r"\n\s*\n", text))
            if not blocks:  # no structured part - whole msg
                blocks = re.split(r"\n\s*\n", msg.as_string())
        except (MessageError, LookupError, TypeError, ValueError) as e:
            # as_string() is the fragile call: refolding the headers of a
            # malformed report raises MessageError/TypeError, and an unknown
            # charset NAME raises LookupError (the same class of failure
            # _decode_header_value exists to survive). Returning no groups is
            # the safe fallback - the caller then judges by the subject - but
            # a lost verdict is exactly how a hard bounce reads as a reply,
            # so it must not be silent.
            logger.warning("Could not parse the DSN report: %s", e)
            return []

        out = []
        for b in blocks:
            a = re.search(r"^Action:\s*([a-zA-Z]+)", b, re.IGNORECASE | re.MULTILINE)
            s = re.search(r"^Status:\s*(\d\.\d+\.\d+)", b, re.IGNORECASE | re.MULTILINE)
            # Angle brackets are optional: plenty of MTAs write
            # "Final-Recipient: rfc822; <dead@brand.de>". Without allowing
            # them the address came back empty, the verdict lost its
            # recipient, and _extract_bounced_recipient silently fell back to
            # scanning the whole message - the first-match-anywhere behaviour
            # this parser exists to replace.
            r = re.search(
                r"^(?:Final|Original)-Recipient:\s*(?:[^;\n]*;)?\s*"
                r"<?([^\s;<>]+@[^\s;<>]+)>?",
                b,
                re.IGNORECASE | re.MULTILINE,
            )
            if a or s:
                out.append(
                    (
                        (r.group(1).lower() if r else None),
                        (a.group(1).lower() if a else None),
                        (s.group(1) if s else None),
                    )
                )
        return out

    @classmethod
    def _dsn_verdict(cls, msg):
        """(recipient, action, status) for the WORST outcome in the report.

        A permanent failure anywhere in a multi-recipient DSN must win over a
        sibling block that merely says 'delivered'."""
        groups = cls._dsn_groups(msg)
        if not groups:
            return None, None, None

        def rank(g):
            _rcpt, action, status = g
            # Severity is judged from the status INDEPENDENTLY of the action:
            # not every MTA emits an 'Action:' line, and a 5.x.x block that
            # scored equal to 'delivered' would leave the winner decided by
            # block order - dropping the bounce, and (via _extract_bounced_
            # recipient) suppressing the delivered sibling instead.
            hard = (status or "").startswith("5")
            if action == "failed" and hard:
                return 4  # unambiguous hard failure
            if hard:
                return 3  # permanent status, no Action: line
            if action == "failed":
                return 2  # gave up, non-5.x.x (queue expiry)
            if (status or "").startswith("4"):
                return 1  # transient
            return 0  # delivered/delayed/relayed

        return max(groups, key=rank)

    def _classify_email(self, from_addr, subject, body, msg=None):
        """
        Classify an email.

        Returns one of:
          'bounce'      - permanent failure; the address should be suppressed
          'soft_bounce' - transient (4.x.x, mailbox full, greylisting); retry,
                          never suppress
          'dsn_info'    - a delay/delivered/relayed report; no action at all
          'request_erasure' / 'request_access' - a data-subject request
                          (checked before opt-outs, which some also match)
          'unsubscribe' - an explicit opt-out
          'reply'       - a genuine human reply
        """
        from_lower = from_addr.lower()
        subject_lower = subject.lower()

        # ── Bounce ────────────────────────────────────────────────────────
        # Detected STRUCTURALLY (who sent it / DSN subject), never by scanning
        # the body for substrings. Body scanning was actively dangerous: a real
        # prospect writing "our MOQ is 550 units" or "we had a failure with our
        # last supplier" was classified as a bounce and then permanently
        # suppressed - destroying the most valuable reply the agent can get.
        bounce_senders = [
            "mailer-daemon",
            "postmaster",
            "mail delivery",
            "mail delivery subsystem",
            "internet mail delivery",
        ]
        is_bounce_sender = any(s in from_lower for s in bounce_senders)
        has_failure_subject = any(k in subject_lower for k in self._FAILURE_SUBJECTS)
        has_dsn_subject = any(k in subject_lower for k in self._AMBIGUOUS_DSN_SUBJECTS)

        # A subject alone is not enough to treat mail from a HUMAN as a DSN:
        # an Italian prospect writing "Re: mancata consegna del campione" (about
        # a missed sample delivery) would otherwise be swallowed as a bounce.
        # Require a daemon sender or a real report structure.
        is_report = False
        if msg is not None:
            try:
                is_report = (
                    msg.get_content_type() or ""
                ).lower() == "multipart/report" or any(
                    p.get_content_type() == "message/delivery-status"
                    for p in msg.walk()
                )
            except (AttributeError, TypeError) as e:
                # msg comes from the caller and is only assumed to be an
                # email.message.Message here; anything else simply is not a
                # report, and the structural test is what keeps a human's
                # "mancata consegna" out of the bounce branch.
                logger.debug("Could not inspect the message structure: %s", e)
                is_report = False
        if (
            is_bounce_sender
            or is_report
            or ((has_failure_subject or has_dsn_subject) and msg is None)
        ):
            _rcpt, action, status = self._dsn_verdict(msg)

            # The DSN says what actually happened - trust it over the subject.
            if action in ("delayed", "delivered", "relayed", "expanded"):
                return "dsn_info"  # still queued / already delivered
            # 4.x.x is transient even when the MTA has given up retrying
            # (Action: failed + 4.4.7 = queue expiry). The address may be
            # perfectly valid, so retry it rather than suppressing a prospect.
            if status and status.startswith("4"):
                return "soft_bounce"
            if action == "failed" or (status and status.startswith("5")):
                return "bounce"  # permanent - safe to suppress

            # No machine-readable verdict - fall back to the subject wording.
            # Providers qualify the ambiguous phrase explicitly, e.g. M365 sends
            # "Delivery Status Notification (Failure)" vs "(Delay)".
            if "delay" in subject_lower:
                return "dsn_info"
            if (
                has_failure_subject
                or "failure" in subject_lower
                or "failed" in subject_lower
            ):
                return "bounce"
            # A bare "Delivery Status Notification" with no verdict at all could
            # equally be a delay; treat it as informational rather than risk
            # suppressing a prospect whose message may still be delivered.
            if has_dsn_subject:
                return "dsn_info"
            return "bounce" if is_bounce_sender else "reply"

        # ── Unsubscribe / opt-out ─────────────────────────────────────────
        # Checked in the BODY too (post strip_quoted so our own footer's
        # "unsubscribe here" can't self-trigger) and multilingual: a prospect
        # replying "please remove me from your list" is exercising the GDPR
        # right to object and must be suppressed, not filed as a chat.
        try:
            from modules import reply_intent

            fresh = reply_intent.strip_quoted(body or "")
            kind = reply_intent.dsr_kind(subject, fresh)
            if kind:
                return f"request_{kind}"
            if reply_intent.is_optout(subject, fresh):
                return "unsubscribe"
        except (ImportError, AttributeError, TypeError):
            # never let classification crash the inbox scan
            #
            # Logged with a traceback rather than swallowed: the fallback only
            # sees the SUBJECT, so a body-only "please remove me from your
            # list" is missed while this is broken - a right-to-object failure
            # that otherwise leaves no trace anywhere.
            logger.exception("Opt-out detection failed - using the subject only")
            if "unsubscribe" in subject_lower:
                return "unsubscribe"

        # Default: normal reply
        return "reply"

    @staticmethod
    def _sender_address(msg) -> str:
        """The address in the message's From header, lower-cased, or "" when
        the header holds none that is_address accepts.

        Read from the raw header with the standard library's address parser.
        The text between the first '<' and the first '>' of the decoded
        header filed an opt-out from 'anna@brand.it (Anna Rossi)' under that
        whole string and one from '"Rossi, Anna <Vendite>" <anna@brand.it>'
        under 'vendite', and lost one from '"Anna > Sales" <anna@brand.it>',
        whose slice was empty: each sender kept being emailed (GDPR Art. 21).
        The decoded header is the wrong input as well, since an encoded
        display name can decode to 'Anna <Sales>'. getaddresses reads what
        parseaddr takes for two addresses, an unquoted 'Rossi, Anna
        <anna@brand.it>'. str() because a header holding raw 8-bit bytes
        comes back as a Header object.

        Never raises, for the reason _decode_header_value gives: the parser
        recurses on comments and groups, and a message that raises here is
        skipped on every scan, which holds the watermark and every live send.
        """
        raw = str(msg.get("From", ""))
        try:
            parsed = [parseaddr(raw), *getaddresses([raw])]
        except RecursionError:
            logger.warning("Un-parseable From header: %.80s", raw)
            return ""
        for _name, address in parsed:
            address = address.strip().lower()
            if is_address(address):
                return address
        return ""

    def _extract_email_address(self, from_header):
        """Extract a bare email address from a From header."""
        if "<" in from_header and ">" in from_header:
            start = from_header.index("<") + 1
            end = from_header.index(">")
            return from_header[start:end].strip().lower()
        return from_header.strip().lower()

    def _extract_bounced_recipient(self, msg):
        """Pull the *failed recipient* out of a bounce/DSN, not the
        mailer-daemon sender. Uses RFC 3464 Final-Recipient/Original-Recipient
        (which only appear in DSNs), then falls back to the attached original
        message's To:."""
        # Prefer the recipient from the block that actually FAILED, so a
        # multi-recipient report can't pair the verdict with a sibling address.
        rcpt, _action, _status = self._dsn_verdict(msg)
        if rcpt:
            return self._extract_email_address(rcpt)

        addr_re = re.compile(
            r"(?:Final-Recipient|Original-Recipient):\s*(?:[^;\n]*;)?\s*"
            r"([^\s;<>]+@[^\s;<>]+)",
            re.IGNORECASE,
        )
        try:
            raw = msg.as_string()
        except (MessageError, LookupError, TypeError, ValueError) as e:
            # Same serialisation failures as _dsn_groups; the decoded body is
            # a weaker but usable source for the Final-Recipient line.
            logger.debug("Could not serialise the bounce, using its body: %s", e)
            raw = self._get_body(msg)
        m = addr_re.search(raw)
        if m:
            return self._extract_email_address(m.group(1))

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "message/rfc822":
                    sub = part.get_payload()
                    orig = sub[0] if isinstance(sub, list) and sub else sub
                    if hasattr(orig, "get"):
                        addr = self._extract_email_address(
                            self._decode_header_value(orig.get("To", ""))
                        )
                        if addr and "@" in addr:
                            return addr
        return None

    _MESSAGE_ID = re.compile(r"<([^<>\s]+)>")

    @classmethod
    def _original_message_ids(cls, msg) -> list[str]:
        """The Message-IDs a bounce report gives for the email it reports on:
        the attached original's, whole (message/rfc822) or as its headers only
        (text/rfc822-headers, as Exchange Online and Gmail often attach it),
        an X-Original-Message-ID in the report part (Gmail), and the report's
        own In-Reply-To and References, which both point at the original.

        Never raises: what cannot be read gives no id, and a report with none
        is matched by its address and date (process_bounces)."""
        found = []

        def take(value) -> None:
            text = str(value or "")
            found.extend(cls._MESSAGE_ID.findall(text) or text.split()[:1])

        try:
            take(msg.get("In-Reply-To"))
            take(msg.get("References"))
            for part in msg.walk() if msg.is_multipart() else []:
                kind = part.get_content_type()
                if kind == "message/rfc822":
                    inner = part.get_payload()
                    for original in inner if isinstance(inner, list) else [inner]:
                        if hasattr(original, "get"):
                            take(original.get("Message-ID"))
                elif kind == "text/rfc822-headers":
                    raw = part.get_payload(decode=True) or b""
                    headers = email.message_from_bytes(raw)
                    take(headers.get("Message-ID"))
                elif kind == "message/delivery-status":
                    blocks = part.get_payload()
                    for block in blocks if isinstance(blocks, list) else []:
                        if hasattr(block, "get"):
                            take(block.get("X-Original-Message-ID"))
        except (MessageError, LookupError, TypeError, ValueError, AttributeError) as e:
            logger.warning("Could not read every Message-ID of a bounce report: %s", e)
        return list(dict.fromkeys(m.strip("<>") for m in found if m.strip("<>")))

    # Markers of an automated / out-of-office response (multi-language, since
    # the recipients are European).
    _AUTO_SUBJECT_MARKERS = (
        "out of office",
        "out-of-office",
        "automatic reply",
        "auto-reply",
        "autoreply",
        "auto reply",
        "away from my",
        "on vacation",
        "on holiday",
        "absence",
        "fuori sede",
        "assente",
        "risposta automatica",
        "abwesenheit",
        "abwesend",
        "réponse automatique",
        "ausencia",
    )

    def _is_read_receipt(self, msg, subject) -> bool:
        """True for an RFC 3798 read receipt (MDN).

        These arrive FROM the prospect's own address with no Auto-Submitted
        header, so they used to be filed as a genuine reply - which marked the
        contact 'responded' and cancelled their whole follow-up sequence.
        Someone merely opening the email should not end the campaign."""
        if msg is not None:
            ctype = (msg.get_content_type() or "").lower()
            if (
                "disposition-notification"
                in self._header_text(msg, "Content-Type").lower()
            ):
                return True
            if ctype == "multipart/report":
                for part in msg.walk():
                    if part.get_content_type() == "message/disposition-notification":
                        return True
        subj = (subject or "").lower()
        return any(
            subj.startswith(p)
            for p in ("read:", "letto:", "gelesen:", "lu :", "lu:", "leído:", "leido:")
        )

    def _is_auto_reply(self, msg, subject, body):
        """True for out-of-office / vacation auto-responders, which must not be
        treated as genuine replies."""
        auto_submitted = self._header_text(msg, "Auto-Submitted").lower()
        if auto_submitted and auto_submitted != "no":
            return True
        if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
            return True
        if self._header_text(msg, "Precedence").lower() in ("auto_reply", "auto-reply"):
            return True
        subj = (subject or "").lower()
        return any(m in subj for m in self._AUTO_SUBJECT_MARKERS)

    @staticmethod
    def _received_at(fetch_header, date_header: str, window_start: datetime) -> str:
        """When a message reached the mailbox, which starts a request's month
        (Art. 12(3)): the server's INTERNALDATE, else the sender's Date header.
        A sender can write any date, so that one is kept within this scan's
        window; nothing is dated after the scan."""
        latest = clock.now()
        tz = clock.get_timezone()
        stamp = (
            imaplib.Internaldate2tuple(fetch_header)
            if isinstance(fetch_header, bytes)
            else None
        )
        if stamp:
            when = datetime.fromtimestamp(time.mktime(stamp), tz=timezone.utc)
        else:
            try:
                when = parsedate_to_datetime(date_header)
            except (TypeError, ValueError, IndexError):
                return latest.isoformat()
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when = max(when, window_start)
        return min(when, latest).astimezone(tz).isoformat()

    # A message bigger than this is read in part: its whole header and the
    # first PARTIAL_BODY_BYTES of its body. Opt-outs, requests and bounce
    # reports are short text at the start, before any attachment; a 40 MB
    # attachment read whole each scan, on the send loop's own thread, held
    # the loop and its memory for nothing.
    LARGE_MESSAGE_BYTES = 10 * 1024 * 1024
    PARTIAL_BODY_BYTES = 256 * 1024
    _SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)", re.IGNORECASE)

    @classmethod
    def _reported_size(cls, data) -> int | None:
        """The RFC822.SIZE in a FETCH answer, or None when it has none. Read
        from the server's own lines only, never from a message literal."""
        for item in data or ():
            line = item[0] if isinstance(item, tuple) else item
            if isinstance(line, bytes):
                found = cls._SIZE_RE.search(line)
                if found:
                    return int(found.group(1))
        return None

    def _fetch_message(self, mail, msg_id) -> tuple:
        """(status, the FETCH line with its INTERNALDATE, the message bytes).

        The size is asked first. Up to LARGE_MESSAGE_BYTES (or when the
        server reports none) the message is fetched whole, as before; above
        it, the header and the start of the body only, with BODY.PEEK so
        nothing is marked read. A server answer without the header raises,
        which the caller's per-message handler counts as unread."""
        status, data = mail.fetch(msg_id, "(RFC822.SIZE)")
        if status != "OK":
            return status, None, None
        size = self._reported_size(data)
        if size is None or size <= self.LARGE_MESSAGE_BYTES:
            status, data = mail.fetch(msg_id, "(INTERNALDATE RFC822)")
            if status != "OK":
                return status, None, None
            return status, data[0][0], data[0][1]
        logger.warning(
            "Message %s is %d bytes: reading its header and the first %d bytes "
            "of its body only",
            msg_id,
            size,
            self.PARTIAL_BODY_BYTES,
        )
        status, data = mail.fetch(
            msg_id,
            f"(INTERNALDATE BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.{self.PARTIAL_BODY_BYTES}>)",
        )
        if status != "OK":
            return status, None, None
        lines = b""
        header = text = None
        for item in data:
            if isinstance(item, tuple):
                lines += item[0] + b" "
                label = item[0].upper()
                if b"BODY[HEADER]" in label:
                    header = item[1]
                elif b"BODY[TEXT]" in label:
                    text = item[1]
            elif isinstance(item, bytes):
                lines += item
        if header is None:
            raise ValueError(f"the server sent no header for message {msg_id!r}")
        return status, lines, header + (text or b"")[: self.PARTIAL_BODY_BYTES]

    _AR_RESULT = re.compile(r"\s*([a-z0-9_-]+)\s*=\s*([a-z]+)", re.IGNORECASE)
    _AR_QUOTED = r'"(?:[^"\\]|\\.)*"'
    # A value is a token, a quoted string, or an address whose local part
    # may be quoted ("anna rossi"@brand.de).
    _AR_PROPERTY = re.compile(
        rf"([a-z0-9_-]+\.[a-z0-9_-]+)=((?:{_AR_QUOTED})?[^\s;\"]*)", re.IGNORECASE
    )
    _AR_WHOLLY_QUOTED = re.compile(_AR_QUOTED)
    _AR_ESCAPE = re.compile(r"\\(.)")
    # Where each method names the domain it checked, first choice first: SPF
    # checks the envelope sender, or the HELO name when the sender is empty
    # (a bounce report); DKIM the signing domain.
    _AR_CHECKED_DOMAIN: ClassVar[dict[str, tuple[str, ...]]] = {
        "spf": ("smtp.mailfrom", "smtp.helo"),
        "dkim": ("header.d", "header.i"),
    }

    @staticmethod
    def _ar_statements(text: str) -> list[str] | None:
        """The header's ';'-separated parts, comments removed, quoted strings
        kept whole; None when a comment or a quoted string never ends.

        A ';' or '(' inside quotes belongs to the value: the envelope
        sender's local part is the sender's to choose and may hold both
        ("x;dkim=pass header.d=brand.de;"@forger.example). Unlike RFC 5322,
        a quoted string inside a comment is kept whole too, since some
        servers copy that address into a comment unescaped, where its ')'
        would otherwise end the comment early."""
        parts: list[str] = []
        current: list[str] = []
        depth = 0
        quoted = escaped = False
        for ch in text:
            keep = not depth
            if escaped:
                escaped = False
            elif ch == "\\" and (quoted or depth):
                escaped = True
            elif quoted:
                quoted = ch != '"'
            elif ch == '"':
                quoted = True
            elif ch == "(":
                depth += 1
                keep = False
            elif ch == ")" and depth:
                depth -= 1
                keep = False
                if not depth:
                    current.append(" ")
            elif depth:
                keep = False
            elif ch == ";":
                parts.append("".join(current))
                current = []
                keep = False
            if keep:
                current.append(ch)
        if depth or quoted or escaped:
            return None
        parts.append("".join(current))
        return parts

    @classmethod
    def _checked_domain(cls, value: str) -> str:
        """The domain in a property value: a quoted value unquoted, then the
        part after the last @ (a quoted local part may hold an @ of its
        own)."""
        if cls._AR_WHOLLY_QUOTED.fullmatch(value):
            value = cls._AR_ESCAPE.sub(r"\1", value[1:-1])
        return value.rsplit("@", 1)[-1].strip("<> ")

    @staticmethod
    def _aligned(checked: str, from_domain: str) -> bool:
        """Whether a domain SPF or DKIM checked belongs to the From domain:
        the same name, or one inside the other (mail.brand.de signs for
        brand.de). A bare top-level name aligns with nothing."""
        checked = checked.rstrip(".").lower()
        if not checked or not from_domain or "." not in checked:
            return False
        return (
            checked == from_domain
            or from_domain.endswith("." + checked)
            or checked.endswith("." + from_domain)
        )

    def _failed_authentication(self, msg) -> str:
        """Why SUPPLECO's receiving server judged this message forged, or "".

        Read from the TOP-MOST Authentication-Results header only. The
        receiving server adds its header above everything the message
        arrived with, so the first one is its own; any below it may have
        been written by the sender, who can claim anything there. Matching
        the authserv-id to the mailbox's domain was not chosen: Microsoft
        365, which SUPPLECO uses, writes no authserv-id at all, so a sender's
        header naming SUPPLECO's domain lower down would then be the one
        believed. If the receiving server adds none, the top-most may be the
        sender's; a forger gains nothing by that, since a failing verdict
        only makes the agent do less with the forger's own message, and a
        passing one is what having no header means.

        Forged: dmarc=fail; spf=fail together with dkim=fail (and no DKIM
        signature that passed); compauth=fail (Microsoft 365's own verdict);
        or, when the From domain publishes no DMARC policy (dmarc=none, or
        no dmarc result), no SPF or DKIM pass for the From domain itself. A
        forger with a domain of their own passes SPF and DKIM for THAT
        domain, and without DMARC nothing else ties the pass to the From
        address. No header means "" (today's behaviour); a top-most header
        whose comment or quoted string never ends is not trusted."""
        headers = msg.get_all("Authentication-Results") or []
        if not headers:
            return ""
        statements = self._ar_statements(str(headers[0]))
        if statements is None:
            return "the Authentication-Results header could not be read"
        results: dict[str, set[str]] = {}
        aligned_pass = False
        from_domain = self._sender_address(msg).rpartition("@")[2]
        for part in statements:
            found = self._AR_RESULT.match(part)
            if found:
                method, result = found.group(1).lower(), found.group(2).lower()
                results.setdefault(method, set()).add(result)
                if result == "pass" and method in self._AR_CHECKED_DOMAIN:
                    properties = {
                        name.lower(): value
                        for name, value in self._AR_PROPERTY.findall(
                            part[found.end() :]
                        )
                    }
                    checked = next(
                        (
                            value
                            for name in self._AR_CHECKED_DOMAIN[method]
                            if (value := properties.get(name, "").strip("<> "))
                        ),
                        "",
                    )
                    aligned_pass = aligned_pass or self._aligned(
                        self._checked_domain(checked), from_domain
                    )
        if "fail" in results.get("dmarc", set()):
            return "dmarc=fail"
        dkim = results.get("dkim", set())
        if "fail" in results.get("spf", set()) and "fail" in dkim and "pass" not in dkim:
            return "spf=fail and dkim=fail"
        if "fail" in results.get("compauth", set()):
            return "compauth=fail"
        if results.get("dmarc", set()) <= {"none"} and not aligned_pass:
            return "no DMARC policy, and no SPF or DKIM pass for the From domain"
        return ""

    def _log_unauthenticated(self, entry: dict, what: str, done: str) -> None:
        """Record that a message failed its authentication and what was done
        with it, for a person to see."""
        details = (
            f"{what} from {entry.get('from') or entry.get('email')} failed "
            f"SUPPLECO's mail server's sender check ({entry['unauthenticated']}); "
            f"{done}"
        )
        details = details.encode("utf-8", "replace").decode("utf-8")
        logger.warning("%s", details)
        try:
            self.db.log_activity("MAIL_UNAUTHENTICATED", details, status="warning")
        except sqlite3.Error as e:
            logger.warning("Could not record MAIL_UNAUTHENTICATED: %s", e)

    def _log_unmatched(self, entry: dict, addr: str) -> None:
        """Record a bounce report about no email the agent sent, which
        suppressed no one, for a person to see."""
        named = "the Message-ID it names" if entry.get("sent_ids") else "its date"
        details = (
            f"A bounce report for {addr} from {entry.get('from') or 'no sender'} "
            f"matches no email the agent sent to that address (by {named}); "
            "the address was not marked bounced"
        )
        details = details.encode("utf-8", "replace").decode("utf-8")
        logger.warning("%s", details)
        try:
            self.db.log_activity("BOUNCE_UNMATCHED", details, status="warning")
        except sqlite3.Error as e:
            logger.warning("Could not record BOUNCE_UNMATCHED: %s", e)

    def _message_ref(self, msg, raw_email: bytes) -> str:
        """What tells this message apart on every re-read: its Message-ID,
        or without one a digest of the message, which the server serves
        unchanged each time it is fetched."""
        message_id = "".join(self._decode_header_value(msg.get("Message-ID")).split())
        return message_id or "sha256:" + hashlib.sha256(raw_email).hexdigest()

    # Never look back further than this, however long the outage was: an
    # unbounded SINCE on a busy mailbox is a slow full-folder scan. run_check
    # reports the mail that leaves unread (unread_gap).
    MAX_LOOKBACK_HOURS = 24 * 14

    # RFC 3501 requires these exact English abbreviations in a SEARCH date.
    # strftime("%b") would follow the process locale - on an Italian host
    # (which is where this runs) it emits "giu" instead of "Jun" and the
    # server answers BAD, silently ending inbox monitoring.
    _IMAP_MONTHS = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )

    @classmethod
    def _imap_date(cls, dt) -> str:
        """Format a date as IMAP's locale-independent 'DD-Mon-YYYY'."""
        return f"{dt.day:02d}-{cls._IMAP_MONTHS[dt.month - 1]}-{dt.year}"

    @property
    def _watermark_key(self) -> str:
        return watermark_key(self.username)

    @property
    def _junk_watermark_key(self) -> str:
        return watermark_key(self.username) + "::junk"

    # Exchange Online's "Junk Email" and the names other servers give the
    # folder, for a server whose LIST does not mark it (RFC 6154 \Junk).
    JUNK_NAMES: ClassVar[tuple[str, ...]] = (
        "Junk Email",
        "Junk E-mail",
        "Junk",
        "Junk-E-Mail",
        "Spam",
        "Posta indesiderata",
        "[Gmail]/Spam",
    )
    _LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')

    def _find_junk_folder(self, mail) -> str | None:
        """The folder this server files suspected spam in: the one LIST marks
        \\Junk, else one with a known junk name, as LIST spells it (quoted if
        it was). Exchange's default anti-spam action moves mail there, opt-outs,
        data requests and bounces included."""
        try:
            status, lines = mail.list()
        except (imaplib.IMAP4.error, OSError) as e:
            logger.warning("Could not list the folders of %s: %s", self.username, e)
            return None
        if status != "OK":
            return None
        named = {}
        for line in lines or []:
            match = self._LIST_LINE.match(line if isinstance(line, bytes) else b"")
            if not match:
                continue
            name = match.group("name").decode("utf-8", "replace").strip()
            if b"\\junk" in match.group("flags").lower():
                return name
            named[name.strip('"').lower()] = name
        for known in self.JUNK_NAMES:
            if known.lower() in named:
                return named[known.lower()]
        return None

    def _lookback_hours(self, since_hours: int, key: str | None = None) -> int:
        """How far back this scan must reach.

        The default 24h window assumes we ran yesterday. If the agent was
        stopped over a weekend, or IMAP was failing, everything older than a day
        would be skipped forever - and IMAP is the ONLY source of bounce and
        opt-out suppression, so those would never be honoured (GDPR Art. 21).
        Stretch the window to cover the gap since the last COMPLETE scan.
        Re-reading a few already-seen messages is harmless: suppression,
        'responded' marking and lead alerts are all idempotent.
        """
        try:
            mark = self.db.get_state(key or self._watermark_key)
        except Exception:
            # Deliberately broad: this is a must-not-crash boundary. IMAP is
            # the only source of bounce and opt-out suppression, so NOTHING
            # that goes wrong reading a scheduling hint may stop the scan
            # itself - a narrower set would turn an unexpected storage error
            # into no inbox monitoring at all.
            #
            # Falling back to the short window is the safe BEHAVIOUR, but doing
            # it silently is not: a broken watermark looks identical to a
            # first-ever run, so outage recovery could stay dead for weeks with
            # nothing to show for it. logger.exception keeps the traceback, so
            # a locked DB and a defect here are not the same log line.
            logger.exception(
                "IMAP watermark unreadable - falling back to a %dh window. "
                "Mail older than that will NOT be re-scanned.",
                since_hours,
            )
            self._watermark_errors += 1
            if self._watermark_errors == 3:
                try:
                    self.db.log_activity(
                        "WATERMARK_ERROR",
                        "Cannot read the IMAP scan watermark - outage recovery "
                        "is off and mail older than 24h will be missed.",
                        status="error",
                    )
                except sqlite3.Error as e:
                    # usually the same DB failure; never mask the original
                    logger.debug("Could not record WATERMARK_ERROR either: %s", e)
            return since_hours
        self._watermark_errors = 0
        if not mark:
            return since_hours
        try:
            last = datetime.fromisoformat(mark)
        except ValueError:
            # Corrupt, not absent - that is a bug, not a normal state.
            logger.error(
                "Corrupt IMAP watermark %r - using the %dh default", mark, since_hours
            )
            return since_hours
        if last.tzinfo is None:
            # Tolerate a watermark written before the clock module owned it.
            last = last.replace(tzinfo=clock.get_timezone())
        gap = (clock.now() - last).total_seconds() / 3600
        # +2h of slack for clock skew and IMAP's date-only SINCE granularity.
        return int(max(since_hours, min(gap + 2, self.MAX_LOOKBACK_HOURS)))

    def check_inbox(
        self, folder: str = "INBOX", since_hours: int = 24, watermark: str | None = None
    ) -> dict:
        """
        Connect via IMAP4_SSL, search for recent emails, and classify them.

        Args:
            folder: IMAP folder to check.
            since_hours: Minimum lookback (hours). The actual window widens to
                cover any gap since the last complete scan.
            watermark: the agent_state key of this folder's last complete scan;
                INBOX's by default. Scanning INBOX also finds the junk folder
                (result["junk_folder"]), which run_check reads next.

        Returns:
            Dict with keys: bounces, unsubscribes, replies (each a list of dicts),
            plus ``ok``/``error`` describing whether the scan actually ran.

        The health flags matter: an empty result from a BROKEN mailbox (expired
        app password, IMAP disabled by an admin, TLS failure) is otherwise
        byte-identical to a genuinely quiet inbox. That silence is dangerous -
        IMAP is the only source of bounce and unsubscribe suppression, so while
        it is down the bounce rate stays pinned near zero and the circuit
        breaker can never fire.
        """
        result = {
            "bounces": [],
            "unsubscribes": [],
            "requests": [],
            "replies": [],
            "ok": False,
            "error": None,
            "scan_started": None,
            "junk_folder": None,
            "folder": folder.strip('"'),
            "window_start": None,
        }

        # Taken BEFORE the scan so mail arriving mid-pass is picked up next
        # time instead of falling in a gap. run_check persists it once every
        # classified message has actually been acted on.
        result["scan_started"] = clock.now_iso()

        mail = self._connect()
        if mail is None:
            msg = "IMAP connection/authentication failed"
            logger.error("%s for %s", msg, self.username)
            result["error"] = msg
            return result

        try:
            status, _ = mail.select(folder, readonly=True)
            if status != "OK":
                logger.error("Could not select IMAP folder '%s'", folder)
                result["error"] = f"could not select folder {folder}"
                return result
            if folder == "INBOX":
                result["junk_folder"] = self._find_junk_folder(mail)

            lookback = self._lookback_hours(since_hours, watermark)
            if lookback > since_hours:
                logger.info(
                    "Widening IMAP window to %dh to cover the gap since the "
                    "last complete scan",
                    lookback,
                )
            since = clock.now() - timedelta(hours=lookback)
            since_date = self._imap_date(since)
            # SINCE matches whole days, so the window starts at this midnight.
            window_start = since.replace(hour=0, minute=0, second=0, microsecond=0)
            result["window_start"] = since

            status, message_ids = mail.search(None, f'(SINCE "{since_date}")')
            if status != "OK":
                logger.error("IMAP search failed (status=%s)", status)
                result["error"] = "search failed"
                return result
            if not message_ids[0]:
                logger.info("No messages found since %s", since_date)
                result["ok"] = True  # a genuinely quiet inbox
                mail.logout()
                return result

            ids = message_ids[0].split()
            logger.info("Found %d messages to process", len(ids))

            skipped = 0
            for msg_id in ids:
                try:
                    status, fetch_header, raw_email = self._fetch_message(mail, msg_id)
                    if status != "OK":
                        # NO means the server REFUSED to serve the message
                        # (RFC 3501 "can't fetch that data") - it still exists
                        # and may be an unread bounce, so this scan is not
                        # complete. (A message expunged mid-scan is different:
                        # imaplib returns ('OK', [None]) for those, which fails
                        # in _fetch_message and is counted the same way.)
                        logger.warning(
                            "IMAP fetch returned %s for message %s - skipping",
                            status,
                            msg_id,
                        )
                        skipped += 1
                        continue

                    msg = email.message_from_bytes(raw_email)

                    from_addr = self._decode_header_value(msg.get("From", ""))
                    subject = self._decode_header_value(msg.get("Subject", ""))
                    to_addr = self._decode_header_value(msg.get("To", ""))
                    date_str = self._decode_header_value(msg.get("Date", ""))
                    body = self._get_body(msg)

                    email_address = self._sender_address(msg)
                    classification = self._classify_email(from_addr, subject, body, msg)

                    # Delay/delivered reports and transient failures are NOT
                    # bounces: the message is still in flight or will be retried.
                    # Suppressing on these would blacklist a live prospect.
                    if classification == "dsn_info":
                        continue
                    if classification == "soft_bounce":
                        logger.info(
                            "Soft bounce (transient) from %s: %s", from_addr, subject
                        )
                        # Recorded, not suppressed: a domain that soft-bounces
                        # repeatedly is a deliverability signal an operator
                        # should see, and it explains why a contact is stalled.
                        try:
                            self.db.log_activity(
                                "SOFT_BOUNCE",
                                f"Transient failure for "
                                f"{self._extract_bounced_recipient(msg) or from_addr}"
                                f" ({subject}) - will be retried by the provider",
                                status="warning",
                            )
                        except sqlite3.Error as e:
                            # Only the activity WRITE is guarded here: a
                            # failure inside _extract_bounced_recipient is a
                            # defect, and must reach the per-message handler
                            # below rather than be filed as a missing log row.
                            logger.warning(
                                "Could not record the soft bounce from %s: %s",
                                from_addr,
                                e,
                            )
                        continue

                    # A read receipt (MDN) is not a reply either. _is_read_receipt
                    # existed for exactly this but was never called, so an Outlook
                    # receipt fell through the bounce branch - MDNs carry
                    # "Disposition:" not the "Action:"/"Status:" a DSN has - and
                    # was classified 'reply'. That marked the contact 'responded'
                    # and CANCELLED their whole follow-up sequence, so a prospect
                    # who merely opened the mail dropped out of the campaign and
                    # could page a human with a hot-lead alert.
                    if classification in ("reply", "bounce") and self._is_read_receipt(
                        msg, subject
                    ):
                        classification = "read_receipt"

                    # An out-of-office / auto-responder is not a real reply -
                    # don't let it mark the contact 'responded' and cancel the
                    # follow-up sequence.
                    if classification == "reply" and self._is_auto_reply(
                        msg, subject, body
                    ):
                        classification = "auto_reply"

                    # For a bounce, the real failed recipient is inside the DSN,
                    # NOT the From address. If we cannot extract it we must NOT
                    # fall back to the sender - suppressing an unparseable
                    # "bounce" can permanently blacklist a real prospect. Flag it
                    # for a human instead.
                    if classification == "bounce":
                        bounced = self._extract_bounced_recipient(msg)
                        if bounced:
                            email_address = bounced
                        else:
                            logger.error(
                                "Bounce-classified mail from %s (subject: %s) has no "
                                "extractable failed recipient - NOT suppressing",
                                from_addr,
                                subject,
                            )
                            try:
                                self.db.log_activity(
                                    "BOUNCE_UNPARSED",
                                    f"Could not extract failed recipient from "
                                    f"{from_addr} (subject: {subject}) - review manually",
                                    status="error",
                                )
                            except sqlite3.Error as e:
                                # The log row IS the review request - if it
                                # cannot be written, the file log has to say so.
                                logger.warning(
                                    "Could not record BOUNCE_UNPARSED for %s: %s",
                                    from_addr,
                                    e,
                                )
                            continue

                    # An opt-out, a request or a reply with no sender address
                    # cannot be filed under anyone, and processing it dropped
                    # it without a trace. Kept for a person instead, as an
                    # unparseable bounce is. An auto-reply or a read receipt
                    # asks for nothing.
                    if not email_address and classification not in (
                        "auto_reply",
                        "read_receipt",
                    ):
                        details = (
                            f"Could not read a sender address from "
                            f"{from_addr or '(no From)'} in {folder} "
                            f"(subject: {subject}, read as {classification})"
                            " - review manually"
                        )
                        # A utf-7 encoded word can decode to a lone surrogate.
                        # SQLite refuses one, and a raise here would skip this
                        # message on every scan.
                        details = details.encode("utf-8", "replace").decode("utf-8")
                        logger.error("%s", details)
                        try:
                            self.db.log_activity(
                                "SENDER_UNPARSED", details, status="error"
                            )
                        except sqlite3.Error as e:
                            # As for BOUNCE_UNPARSED: the row is the review
                            # request, so the file log has to carry its loss.
                            logger.warning("Could not record SENDER_UNPARSED: %s", e)
                        continue

                    entry = {
                        "from": from_addr,
                        "email": email_address,
                        "to": to_addr,
                        "subject": subject,
                        "date": date_str,
                        "classification": classification,
                        # Kept (truncated) so reply intent can be judged and the
                        # text quoted in a hot-lead alert.
                        "body": (body or "")[:4000],
                        "unauthenticated": self._failed_authentication(msg),
                    }

                    if classification == "bounce":
                        entry["sent_ids"] = self._original_message_ids(msg)
                        result["bounces"].append(entry)
                    elif classification == "unsubscribe":
                        result["unsubscribes"].append(entry)
                    elif classification.startswith("request_"):
                        entry["request_kind"] = classification.removeprefix("request_")
                        entry["source_ref"] = self._message_ref(msg, raw_email)
                        entry["received_at"] = self._received_at(
                            fetch_header, date_str, window_start
                        )
                        result["requests"].append(entry)
                    elif classification in ("auto_reply", "read_receipt"):
                        # Ignore: neither is a human reply. A read receipt in
                        # particular must not reach result["replies"], because
                        # process_replies marks the contact 'responded' and the
                        # agent then cancels their follow-up sequence - dropping
                        # a prospect from the campaign for merely opening the mail.
                        pass
                    else:
                        result["replies"].append(entry)

                except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError):
                    # The CONNECTION died, not the message. Every remaining
                    # fetch would fail the same way, so swallowing these would
                    # walk the rest of the mailbox logging warnings and then
                    # declare the scan complete - stamping a watermark over
                    # mail that was never read. Let it reach the outer handler,
                    # which marks the scan failed and holds the watermark.
                    raise
                except Exception:
                    # A genuinely per-message problem (unparseable MIME). Skip
                    # it, but the pass is no longer complete: this message may
                    # have been a bounce or an opt-out, so the watermark must
                    # not advance past it.
                    #
                    # Deliberately broad, and it must stay broad: this is the
                    # per-message boundary, where ONE bad message may not cost
                    # us the rest of the mailbox. It is also where the three
                    # causes meet - a mid-scan expunge (data[0][1] on
                    # ('OK', [None]) raises TypeError), a permanently poisoned
                    # message, and a real defect in the classification code
                    # above. They must behave identically (skip, hold the
                    # watermark) but they are not the same problem, so
                    # logger.exception records the traceback that tells them
                    # apart instead of one indistinguishable warning line.
                    skipped += 1
                    logger.exception("Error processing message %s", msg_id)
                    continue

            # "ok" must mean what run_check relies on it to mean: every message
            # in the window was read and acted on. If any were skipped the
            # watermark has to stay put so the next scan sees them again.
            result["skipped"] = skipped
            result["ok"] = skipped == 0
            if skipped:
                result["error"] = f"{skipped}/{len(ids)} messages could not be read"
                logger.error(
                    "Inbox scan incomplete: %d of %d messages unreadable. "
                    "Bounces or opt-outs may be among them; not advancing the "
                    "scan watermark.",
                    skipped,
                    len(ids),
                )
            mail.logout()

        except Exception as e:
            # Partial scan: whatever we collected is incomplete, so do NOT let
            # the caller treat it as a clean pass.
            logger.exception("Error during inbox check")
            result["error"] = str(e)[:200]
            try:
                mail.logout()
            except (imaplib.IMAP4.error, OSError) as e:
                # The connection being already dead is usually WHY we are in
                # this handler, so a failed logout is expected - but it must
                # never mask the scan error we are here to report.
                logger.debug("IMAP logout after a failed scan: %s", e)

        logger.info(
            "Results: %d bounces, %d unsubscribes, %d replies",
            len(result["bounces"]),
            len(result["unsubscribes"]),
            len(result["replies"]),
        )
        return result

    def process_bounces(self, bounces: list):
        """
        Add bounced email addresses to the suppression list.

        Args:
            bounces: List of bounce dicts from check_inbox.
        """
        added = 0

        own = (self.username or "").strip().lower()
        for bounce in bounces:
            addr = (bounce.get("email") or "").strip().lower()
            # entry['email'] is the DSN's Final-Recipient when we could parse it.
            # If all we have is the daemon/postmaster (recipient unknown), skip -
            # do NOT fall back to the DSN 'To', which is our OWN address.
            if not addr or "mailer-daemon" in addr or "postmaster" in addr:
                continue
            if addr == own:
                continue
            # Anyone can send a report that says a prospect's address is
            # dead. One the server found forged marks nobody bounced.
            if bounce.get("unauthenticated"):
                self._log_unauthenticated(
                    bounce,
                    f"A bounce report for {addr}",
                    "the address was not marked bounced",
                )
                continue
            # Nor does one about no email the agent sent to that address.
            if not self.db.bounce_matches_a_send(addr, bounce.get("sent_ids") or ()):
                self._log_unmatched(bounce, addr)
                continue

            # A bounce also replaces an operator's block, as an opt-out does:
            # the block is removable by Unblock, the server's verdict is not.
            new = not self.db.is_suppressed(addr)
            if new:
                self.db.add_to_suppression(addr, "bounce")
            else:
                new = self.db.replace_block(addr, "bounce_blocked")
            if new:
                added += 1
                # Every suppression needs a dated, human-readable trail: this is
                # the record that proves when and why an address stopped being
                # contacted (GDPR Art. 21 accountability).
                try:
                    self.db.log_activity(
                        "SUPPRESSED_BOUNCE",
                        f"{addr} suppressed after a hard bounce "
                        f"({bounce.get('subject') or 'no subject'})",
                        status="warning",
                    )
                except sqlite3.Error as e:
                    # The address IS suppressed either way; what is lost is the
                    # accountability record, so the file log has to carry it.
                    logger.warning("Suppressed %s but could not record it: %s", addr, e)

            # The SMTP-rejection path sets 'bounced' as well as suppressing;
            # this one only suppressed, so the dashboard kept showing a dead
            # address as 'emailed'. A reply or client status is the more
            # important fact and is left alone; an operator's block is not -
            # the address is dead whatever the operator decided.
            try:
                contact = self.db.get_contact_by_email(addr)
                if contact and contact["status"] in (
                    "emailed",
                    "pending",
                    "failed",
                    "blocked",
                ):
                    self.db.update_contact_status(
                        contact["id"], "bounced", discard_reason="hard bounce (DSN)"
                    )
            except sqlite3.Error as e:
                logger.warning("Could not mark %s as bounced: %s", addr, e)

        logger.info(
            "Processed %d bounces, added %d to suppression list", len(bounces), added
        )

    def process_unsubscribes(self, unsubs: list):
        """
        Add unsubscribe requests to the suppression list.

        Args:
            unsubs: List of unsubscribe dicts from check_inbox.
        """
        added = 0

        for unsub in unsubs:
            addr = (unsub.get("email") or "").strip().lower()
            if not addr:
                continue
            # Forged mail still stops its own From address - wrongly honouring
            # an opt-out harms no one - but never the company: anyone could
            # write "remove me" as any prospect and stop the firm.
            forged = bool(unsub.get("unauthenticated"))
            if forged:
                self._log_unauthenticated(
                    unsub,
                    "An opt-out",
                    f"only {addr} was stopped, not the company",
                )
            # One routine for both opt-out paths (this and the web link), so
            # the address is normalised in one place - writing raw here once
            # stored the sender's casing, which matched nothing on lookup - and
            # an opt-out by email marks the contact and ends its follow-ups
            # exactly as the link does. It also turns an operator's block into
            # an opt-out, which Unblock cannot remove.
            try:
                contact = self.db.get_contact_by_email(addr)
                emailed = contact is not None and self.db.has_been_emailed(
                    contact["id"]
                )
                new = self.db.record_opt_out(addr, "Opted out by email")["new"]
            except sqlite3.Error as e:
                # Held, and the rest of the batch carries on. The send check
                # refuses the address until the replay applies it, which also
                # blocks the company if we had emailed them.
                logger.warning(
                    "Opt-out by email held until the database is free: %s", e
                )
                optout_spool.hold_optout(
                    self.db,
                    addr,
                    "Opted out by email",
                    company=False if forged else "if_emailed",
                )
                continue
            if new:
                added += 1
                try:
                    self.db.log_activity(
                        "SUPPRESSED_OPTOUT",
                        f"{addr} suppressed - opt-out received by email",
                        status="warning",
                    )
                    optout_spool.flag_unless_one_address(self.db, addr, "Opt-out")
                except sqlite3.Error as e:
                    # As above: the opt-out is honoured, but the dated proof
                    # that it was honoured is what this row exists to be.
                    logger.warning("Suppressed %s but could not record it: %s", addr, e)
            # From someone we emailed: their company said no. From anyone else
            # (a forwarded reply, a colleague, an address we only stored) only
            # that address stops.
            if emailed and not forged:
                try:
                    block_company(self.db, addr, "Opted out by email")
                except sqlite3.Error as e:
                    # As for the one-click button: held, never lost.
                    logger.warning(
                        "Company block held until the database is free: %s", e
                    )
                    optout_spool.hold_optout(
                        self.db, addr, "Opted out by email", company=True
                    )
        logger.info(
            "Processed %d unsubscribes, added %d to suppression list",
            len(unsubs),
            added,
        )

    def process_requests(self, requests: list):
        """Data-subject requests found in replies: stop all email at once.

        The address is suppressed, and its company blocked if we had emailed
        them, exactly as for an objection. The request is recorded for a
        person to answer within a month (Art. 12(3)), once per message: every
        scan reads the last day of mail again. A request never reaches the
        hot-lead path or an AI provider.
        """
        for req in requests:
            addr = (req.get("email") or "").strip().lower()
            if not addr:
                continue
            kind = req.get("request_kind") or "unspecified"
            how = f"Data request ({kind}) received by email"
            ref = req.get("source_ref")
            # As for an opt-out: forged mail stops its own address and is
            # registered for a person, who checks who sent it before
            # answering; it never stops the company.
            forged = bool(req.get("unauthenticated"))
            if forged:
                self._log_unauthenticated(
                    req,
                    f"A data request ({kind})",
                    f"only {addr} was stopped, not the company; check who sent "
                    "it before answering",
                )
            try:
                contact = self.db.get_contact_by_email(addr)
                emailed = contact is not None and self.db.has_been_emailed(
                    contact["id"]
                )
                self.db.record_opt_out(addr, how)
                request_id = self.db.add_dsr(
                    addr, kind, "reply", ref, received_at=req.get("received_at")
                )
            except sqlite3.Error as e:
                # Held with its kind, so the replay registers it with its
                # deadline too: held as an opt-out only, the request was lost.
                # And with its message, so the replay and the next scan of the
                # same mail register it once between them.
                logger.warning("Data request held until the database is free: %s", e)
                optout_spool.spool_optout(
                    self.db.db_path,
                    addr,
                    how,
                    company=False if forged else "if_emailed",
                    request=kind,
                    source_ref=ref,
                    received_at=req.get("received_at"),
                )
                continue
            if request_id is not None:
                try:
                    # Under her token if she was erased already.
                    who = self.db.get_dsr(request_id)["email"]
                    self.db.log_activity(
                        "DSR_RECEIVED",
                        f"{kind} request from {who}: answer within one month",
                        status="warning",
                    )
                    optout_spool.flag_unless_one_address(self.db, addr, "Data request")
                except sqlite3.Error as e:
                    logger.warning("Could not log the request from %s: %s", addr, e)
            if emailed and not forged:
                try:
                    block_company(self.db, addr, how)
                except sqlite3.Error as e:
                    logger.warning(
                        "Company block held until the database is free: %s", e
                    )
                    optout_spool.hold_optout(self.db, addr, how, company=True)

    def _record_declines(self, replies: list) -> None:
        """Record each clean decline from someone we emailed as an objection.

        Decision L1.16 (the developer's default): the address goes on the
        do-not-contact list and the contact is erased at the next retention
        run. Never a reply showing interest, and not from a stranger - except
        while the database is busy, when the decline is held unchecked:
        suppressing someone who said no is the safe error. Done before
        process_replies writes: record_opt_out uses its own connection, which
        would wait on that loop's open transaction. The contact is then
        'unsubscribed', which process_replies leaves alone.
        """
        from modules import reply_intent

        for reply in replies:
            addr = (reply.get("email") or "").strip().lower()
            if not addr or not reply_intent.is_decline(
                reply.get("subject", ""), reply.get("body", "")
            ):
                continue
            how = "Declined by reply"
            try:
                contact = self.db.get_contact_by_email(addr)
                if contact is None or not self.db.has_been_emailed(contact["id"]):
                    continue
                new = self.db.record_opt_out(addr, how)["new"]
            except sqlite3.Error as e:
                logger.warning("Decline held until the database is free: %s", e)
                optout_spool.hold_optout(self.db, addr, how)
                continue
            if new:
                try:
                    self.db.log_activity(
                        "SUPPRESSED_DECLINE",
                        f"{addr} declined by reply: recorded as an objection",
                        status="success",
                    )
                except sqlite3.Error as e:
                    logger.warning("Could not log the decline from %s: %s", addr, e)

    @undoes_a_failed_write
    def process_replies(self, replies: list):
        """
        Update contact status to 'responded' for reply senders.

        A write that fails undoes the marks before it, and the inbox check
        that called this fails with it: the watermark stays, so the next
        check reads the same replies again.

        Args:
            replies: List of reply dicts from check_inbox.
        """
        updated = 0
        replied: list[str] = []
        self._record_declines(replies)

        for reply in replies:
            addr = (reply.get("email") or "").strip().lower()
            if not addr:
                continue

            # A client, an opt-out and an operator's block are each the more
            # important fact and are left alone, as process_bounces does for a
            # client. Setting 'responded' over them reverted a freshly marked
            # client on the very next scan, since each scan re-reads the last
            # day of mail. The reason replaces whatever stale verdict the
            # contact carried from before the reply.
            result = self.db.conn.execute(
                """UPDATE contacts
                   SET status = 'responded', discard_reason = 'Replied by email'
                   WHERE LOWER(email) = ? AND status NOT IN
                       ('responded', 'active_client', 'unsubscribed', 'blocked',
                        'erased')""",
                (addr,),
            )
            if result.rowcount > 0:
                updated += 1
                replied.append(addr)
            # The engaged retention period runs from the last exchange. A
            # re-read of the same reply moves it by at most the day re-read.
            self.db.conn.execute(
                "UPDATE contacts SET last_reply_at = ? WHERE LOWER(email) = ?",
                (clock.db_now(), addr),
            )

        self.db.conn.commit()
        # After the commit: log_activity writes on its own connection, which
        # would wait on the lock this one held. Once per contact - a re-read
        # reply changes nothing, so it logs nothing.
        for addr in replied:
            try:
                self.db.log_activity(
                    "REPLY_RECEIVED", f"{addr} replied", status="success"
                )
            except sqlite3.Error as e:
                logger.warning("Could not log the reply from %s: %s", addr, e)
        logger.info(
            "Processed %d replies, updated %d contacts to 'responded'",
            len(replies),
            updated,
        )

    def run_check(self) -> dict:
        """
        Full pipeline: check inbox and process all classified emails.

        Returns:
            Summary dict with counts for bounces, unsubscribes, and replies.
        """
        logger.info("Starting full inbox check...")

        inbox = self.check_inbox()
        junk_folder = inbox.get("junk_folder")
        scans = [(inbox, self._watermark_key)]
        if junk_folder:
            # What the server filed as spam is read like the inbox: Exchange's
            # default anti-spam action moves opt-outs and bounces there too.
            scans.append(
                (
                    self.check_inbox(
                        folder=junk_folder, watermark=self._junk_watermark_key
                    ),
                    self._junk_watermark_key,
                )
            )
        elif inbox.get("ok"):
            logger.info("No junk folder for %s: reading INBOX only", self.username)

        results = {
            key: [entry for scan, _ in scans for entry in scan.get(key, [])]
            for key in ("bounces", "unsubscribes", "requests", "replies")
        }
        self.process_bounces(results["bounces"])
        self.process_unsubscribes(results["unsubscribes"])
        self.process_requests(results["requests"])
        self.process_replies(results["replies"])

        # The account's read is complete only when every folder it has was
        # read: the send check holds live mail on INBOX's watermark, and an
        # opt-out filed as junk must not go unread while mail keeps going out.
        complete = all(scan.get("ok") for scan, _ in scans)
        # Advance a watermark only now: every suppression and 'responded'
        # mark from this window is on disk, so the next run may safely start
        # here. A failed scan leaves it alone, and the following run widens its
        # window to cover the gap.
        for scan, key in scans:
            if not scan.get("ok") or not scan.get("scan_started"):
                continue
            if key == self._watermark_key and not complete:
                continue
            try:
                # The gap first: once the watermark moves past it, nothing
                # else remembers this mail. Reported by the read that completes,
                # when its dates are final.
                gap = unread_gap(
                    self.db, self.username, key, scan["folder"], scan["window_start"]
                )
                if gap:
                    logger.error("%s", gap)
                    self.db.log_activity("IMAP_GAP", gap, status="error")
                self.db.set_state(key, scan["scan_started"])
            except sqlite3.Error as e:
                # Not persisting is safe (the next run just re-reads a window
                # it has already processed - all of it is idempotent), so a
                # storage failure must not fail the run it is reporting on.
                logger.warning("Could not persist IMAP watermark: %s", e)

        summary = {
            "bounces": len(results["bounces"]),
            "unsubscribes": len(results["unsubscribes"]),
            "requests": len(results["requests"]),
            "replies": len(results["replies"]),
            "total_processed": (
                len(results["bounces"])
                + len(results["unsubscribes"])
                + len(results["replies"])
            ),
            # Full reply details (incl. body) so the caller can judge intent
            # and alert a human on a hot lead.
            "reply_entries": results["replies"],
            # Whether the scan actually completed - a failed check must never be
            # mistaken for "nothing new" (see check_inbox).
            "ok": complete,
            "error": next(
                (scan["error"] for scan, _ in scans if scan.get("error")), None
            ),
            "account": self.username,
        }

        logger.info(
            "Check complete: %d bounces, %d unsubs, %d replies (%d total)",
            summary["bounces"],
            summary["unsubscribes"],
            summary["replies"],
            summary["total_processed"],
        )
        return summary
