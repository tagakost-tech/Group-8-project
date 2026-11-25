"""
Email dispatch module for B2B outreach.
Handles SMTP connection, HTML email assembly with PDF attachments,
personalization, and delivery verification.

V2: Added tracking_id support for open tracking analytics.
"""

import html
import logging
import mimetypes
import re
import smtplib
import sqlite3
import ssl
import threading
from collections.abc import Callable
from datetime import timedelta
from email.errors import MessageError
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from modules.database import Database
from modules.microsoft_signin import (
    MicrosoftSignIn,
    SignInRefused,
    explain_mailbox_refusal,
    smtp_authenticate,
)

logger = logging.getLogger(__name__)

# A template placeholder: {{name}}, {{legal_footer}}, ... and the full stop
# that may follow it.
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}(\.?)")

# The start of an RFC 2047 encoded word, a control character (CR, LF and tab
# among them) or a Unicode line separator.
_NOT_FOR_A_HEADER = re.compile(r"=\?|[\x00-\x1f\x7f-\x9f\u2028\u2029]")

# A line break in a header written with CR LF line ends, other than the one
# that ends it and a fold's, whose next line starts with a space or a tab.
_STRAY_LINE_BREAK = re.compile(r"\r\n(?![ \t])|\r(?!\n)|(?<!\r)\n")

# What stands in for a contact's organisation when none is known, in the
# email's language. The Italian reads after "con", "per" and "presso" alike.
UNKNOWN_ORGANIZATION = {"en": "your company", "it": "la Sua azienda"}


def name_for_header(value: str | None, default: str) -> str:
    """A name or organisation fit to stand in a header such as the Subject,
    or `default`, the caller's word for having none.

    A scraped organisation is whatever a page calls itself. The email package
    decodes an encoded word ("=?utf-8?b?...?=") wherever a header holds one,
    even where it touches the text beside it, so an organisation holding one
    puts in the header whatever it decodes to; see create_email. A control
    character has no place in a header, and a raw line break makes the library
    refuse the whole email. Either way the name is left out, so the email
    still goes.
    """
    text = (value or "").strip()
    if not text or _NOT_FOR_A_HEADER.search(text):
        return default
    return text


# Every blocking SMTP step - opening the connection, each command, the
# message itself - gives up after this many seconds.
SMTP_TIMEOUT = 30

# The most blocking SMTP exchanges one _deliver makes: a failed health check
# on the pooled connection (1), a reconnect (9: connect, greeting, EHLO,
# STARTTLS, the TLS handshake, EHLO and three for AUTH LOGIN), a failed
# pre-flight NOOP (1), the one further reconnect it allows (9), and the
# message (5: MAIL, RCPT, DATA, the content, the server's answer).
SMTP_EXCHANGES_PER_SEND = 25

# How long a send can stay 'preparing' while the process making it is alive:
# its own exchanges after waiting out one other send on the same sender's
# lock (Direct Send shares the running campaign's), plus a minute for the
# database writes around it (busy_timeout is 5 s each) - 26 minutes. The
# retry queue's settings add nothing: every retry is a row of its own. A row
# older than this was left by a process that is gone.
PREPARING_STALE_AFTER = timedelta(
    seconds=2 * SMTP_EXCHANGES_PER_SEND * SMTP_TIMEOUT + 60
)


class ClaimLost:
    """What dispatch_to_contact returns when another send holds the contact.
    Falsy, as every send that did not go out is; told apart with ``is``, because
    this one is a refusal: no failure, no retry spent, nothing written."""

    def __bool__(self) -> bool:
        return False


CLAIM_LOST = ClaimLost()


def _reason(text: str, limit: int = 300) -> str:
    """A server's answer, cut to a length that reads as a reason, not a dump."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class EmailSender:
    """
    Professional email sender with SMTP integration.
    Sends HTML emails with PDF attachments and logs all activity.
    """

    signin: MicrosoftSignIn | None = None

    def __init__(
        self,
        db: Database,
        smtp_host: str,
        smtp_port: int,
        smtp_username: str,
        smtp_password: str,
        sender_name: str = "",
        company_name: str = "",
        sender_title: str = "",
        sender_signature: str = "",
        signin: MicrosoftSignIn | None = None,
    ):
        self.db = db
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        # Given, this mailbox signs in with a Microsoft token (XOAUTH2) and
        # has no password.
        self.signin = signin
        self.sender_name = sender_name
        self.company_name = company_name
        # What this mailbox's sender is responsible for, and its signature
        # (blank: built from the name, the title and the company).
        self.sender_title = sender_title
        self.sender_signature = sender_signature
        # Reused SMTP connection (one login per batch instead of per email),
        # guarded by a lock so the campaign agent and a manual Direct Send can
        # never use the single connection at the same time.
        self._smtp = None
        self._send_lock = threading.Lock()
        # Latched by _deliver on SMTPAuthenticationError. The agent checks it
        # between contacts and halts the batch: an auth failure is a permanent
        # config error, and retrying it per contact both hammers the provider
        # and burns the leads.
        self.auth_failed = False

    # -----------------------------------------------------------------
    # Email Creation
    # -----------------------------------------------------------------

    def create_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        plain_body: str,
        pdf_path: str | None = None,
        unsubscribe_url: str | None = None,
        in_reply_to: str | None = None,
    ) -> EmailMessage:
        """
        Create a professional EmailMessage with optional PDF attachment.
        Uses the modern EmailMessage API (not legacy MIMEMultipart).

        `in_reply_to` is the Message-ID of the first mail in this conversation.
        Given one, the message threads under it in the prospect's client
        instead of arriving as unrelated mail - which is how three follow-ups
        over a week come to read as a machine working through a list.

        Raises ValueError for a header that holds a line break, raw or once
        its encoded words are decoded.
        """
        # No address holds an encoded word or a control character. Whether
        # the library decodes one inside an address it calls invalid, and so
        # writes a line break, has changed between Python versions; refusing
        # it here keeps the answer the same on every one.
        if _NOT_FOR_A_HEADER.search(to_email or ""):
            raise ValueError(
                "The To address holds an encoded word or a control character, "
                "which can become a line break"
            )
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["To"] = to_email

        # An id of our own choosing, on the sending domain. M365 would add one
        # if we did not, but an id we never chose is one we cannot reference
        # later - which is why follow-ups could not thread. The domain must
        # match the sender; a mismatch is a signal filters look at.
        domain = (
            self.smtp_username.rsplit("@", 1)[-1]
            if "@" in (self.smtp_username or "")
            else None
        )
        msg["Message-ID"] = make_msgid(domain=domain)

        # Thread under the original. Both headers, deliberately: clients differ
        # in which they honour, and References is the one most thread on.
        # Guarded against blank/whitespace, because a contact first emailed
        # before this feature existed has no stored id, and a literal empty
        # In-Reply-To is malformed rather than merely useless.
        if in_reply_to and in_reply_to.strip():
            msg["In-Reply-To"] = in_reply_to.strip()
            msg["References"] = in_reply_to.strip()

        if self.sender_name:
            msg["From"] = f"{self.sender_name} <{self.smtp_username}>"
        else:
            msg["From"] = self.smtp_username

        # Reply-To header
        msg["Reply-To"] = self.smtp_username

        # List-Unsubscribe header for email-client integration. Always advertise
        # a mailto method (it works from any inbox and is picked up by the IMAP
        # monitor). Only claim RFC 8058 one-click when the primary target is a
        # real HTTPS endpoint - advertising one-click for a mailto or a
        # localhost URL is invalid and looks broken to mailbox providers.
        mailto = f"<mailto:{self.smtp_username}?subject=Unsubscribe>"
        unsub_parts = []
        if unsubscribe_url and unsubscribe_url not in ("#", ""):
            primary = f"<{unsubscribe_url}>"
            if primary != mailto:
                unsub_parts.append(primary)
        unsub_parts.append(mailto)
        msg["List-Unsubscribe"] = ", ".join(unsub_parts)
        if unsubscribe_url and unsubscribe_url.lower().startswith("https://"):
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        # Set plain text content first (fallback)
        msg.set_content(plain_body)

        # Add HTML alternative
        msg.add_alternative(html_body, subtype="html")

        # Attach PDF if provided and exists
        if pdf_path:
            pdf_file = Path(pdf_path)
            if pdf_file.exists() and pdf_file.is_file():
                ctype, _ = mimetypes.guess_type(str(pdf_file))
                if ctype is None:
                    ctype = "application/octet-stream"
                maintype, subtype = ctype.split("/", 1)

                with open(pdf_file, "rb") as f:
                    msg.add_attachment(
                        f.read(),
                        maintype=maintype,
                        subtype=subtype,
                        filename=pdf_file.name,
                    )
                logger.info(f"Attached PDF: {pdf_file.name}")
            else:
                logger.warning(f"PDF not found: {pdf_path}")

        # The last word on every header, whatever built it. A value parsed into
        # a header has its encoded words decoded, and Python 3.11.9 writes a CR
        # or LF one decodes to back out as a real line break (CVE-2024-6923,
        # fixed in 3.11.10): the header block gains a line the value supplied,
        # or a blank line ends it, and the rest goes out as body. The library
        # refuses a raw line break; this refuses a decoded one the same way,
        # for any character str.splitlines() breaks a line at. The folded form,
        # which is what gets written, is read as well: str() of an address
        # shows nothing of a part the parser calls invalid, and that part is
        # still written out, decoded. It is read as smtplib writes it, with CR
        # LF line ends, both for a server without SMTPUTF8 and for one with
        # it, where non-ASCII text is written raw and a decoded CR LF beside
        # it with it.
        written = [
            msg.policy.clone(linesep="\r\n", utf8=utf8) for utf8 in (False, True)
        ]
        for name, value in msg.items():
            text = str(value)
            try:
                folds = [policy.fold(name, value) for policy in written]
            except MessageError:
                # Newer Pythons refuse to fold such a header themselves
                # (HeaderWriteError): the same verdict, reached first.
                raise ValueError(
                    f"The {name} header holds a line break the library will not write"
                ) from None
            if "".join(text.splitlines()) != text or not all(
                fold.endswith("\r\n") and not _STRAY_LINE_BREAK.search(fold[:-2])
                for fold in folds
            ):
                raise ValueError(f"The {name} header holds a line break once parsed")

        return msg

    # -----------------------------------------------------------------
    # Email Sending
    # -----------------------------------------------------------------

    def _new_connection(self):
        """Open and authenticate a fresh SMTP connection."""
        # The token first: a refused sign-in opens no connection.
        token = self.signin.token() if self.signin is not None else None
        context = ssl.create_default_context()
        if self.smtp_port == 465:
            smtp = smtplib.SMTP_SSL(
                self.smtp_host, self.smtp_port, context=context, timeout=SMTP_TIMEOUT
            )
        else:
            smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=SMTP_TIMEOUT)
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if token is not None:
            smtp_authenticate(smtp, self.smtp_username, token)
        else:
            smtp.login(self.smtp_username, self.smtp_password)
        return smtp

    def _ensure_connection(self):
        """Return a live SMTP connection, reusing the pooled one when possible
        and transparently reconnecting if it has gone stale."""
        if self._smtp is not None:
            try:
                if self._smtp.noop()[0] == 250:
                    return self._smtp
            except (smtplib.SMTPException, OSError) as e:
                # A pooled connection that has been idled out by the server
                # fails its NOOP with SMTPServerDisconnected, or with a socket
                # /TLS error (both OSError). Either way the fix is the same -
                # drop it and reconnect - but the reason belongs in the log.
                logger.debug("Pooled SMTP connection failed its health check: %s", e)
            self._reset_connection()
        self._smtp = self._new_connection()
        return self._smtp

    def _reset_connection(self):
        """Drop the pooled connection (next send reopens it)."""
        if self._smtp is not None:
            try:
                self._smtp.close()
            except (smtplib.SMTPException, OSError) as e:
                # Closing a socket the peer already reset raises; the pooled
                # reference is dropped below regardless, so this is only ever
                # informational.
                logger.debug("Error closing the pooled SMTP connection: %s", e)
            self._smtp = None

    def close(self):
        """Cleanly close the pooled connection (call at the end of a batch)."""
        with self._send_lock:
            if self._smtp is not None:
                try:
                    self._smtp.quit()
                except (smtplib.SMTPException, OSError) as e:
                    # A server that hung up before we said QUIT is normal at the
                    # end of a batch. Fall back to a hard close so the socket is
                    # released either way.
                    logger.debug("SMTP QUIT failed, closing the socket: %s", e)
                    self._reset_connection()
                self._smtp = None

    @staticmethod
    def _rejection_is_permanent(recipients: dict) -> bool:
        """True when the server gave a 5.x.x verdict for every refused address.

        A 5xx recipient rejection ("550 5.1.1 user unknown") is final: the
        mailbox does not exist and never will. Retrying it three times only
        repeats the same question to a server that already answered, and a
        rising unknown-user rate is exactly what filtering services score
        senders on. A 4xx ("451 greylisted") is worth retrying, so a mixed or
        empty response is treated as transient.
        """
        codes = [v[0] for v in recipients.values() if isinstance(v, (tuple, list))]
        return bool(codes) and all(500 <= int(c) < 600 for c in codes)

    def _deliver(
        self, msg: EmailMessage, blocked: Callable[[], bool] | None = None
    ) -> tuple[bool, str, bool, bool]:
        """Send `msg`.

        Returns ``(success, message, permanently_rejected, indeterminate)``.

        ``indeterminate`` means the connection died while we were waiting for
        the server's verdict, so we genuinely do not know whether the message
        was delivered. Callers must NOT resend on it.

        ``blocked`` is asked once the connection is ready, the last moment
        nothing is on the wire yet; when it says yes, nothing is sent.
        """
        with self._send_lock:
            # Set the moment send_message starts. smtplib raises
            # SMTPServerDisconnected when the server closes the connection
            # while it is being opened (greeting, EHLO, STARTTLS, LOGIN) as
            # well as mid-message, and only the second may have delivered it.
            transmitting = False
            try:
                conn = self._ensure_connection()

                # Validate the pooled connection BEFORE any of the message is
                # transmitted. Previously a failed send was blind-retried after
                # a reconnect, but smtplib.getreply() converts *any* socket
                # error during the post-DATA wait - including the 30s timeout -
                # into SMTPServerDisconnected. A slow acknowledgement (AV scan
                # on the PDF, throttling, greylisting) therefore meant the
                # server had accepted the mail and we sent it a second time.
                # Probing here moves the reconnect to the only point where a
                # resend is provably safe: nothing has been sent yet.
                try:
                    if conn.noop()[0] != 250:
                        raise smtplib.SMTPServerDisconnected("NOOP not accepted")
                except (
                    smtplib.SMTPServerDisconnected,
                    smtplib.SMTPConnectError,
                    ConnectionError,
                    TimeoutError,
                    OSError,
                ):
                    self._reset_connection()
                    conn = self._ensure_connection()

                if blocked is not None and blocked():
                    return False, "Not sent: the address was blocked", False, False

                transmitting = True
                conn.send_message(msg)
                return True, "Message sent successfully", False, False

            except sqlite3.Error:
                # Only `blocked` reads the database here. A check that cannot
                # be read must stop the send loudly, like the other reads
                # before a send, not pass as an SMTP failure of this address.
                raise

            except smtplib.SMTPServerDisconnected as e:
                self._reset_connection()
                if not transmitting:
                    # Closed while the connection was being opened: nothing was
                    # sent. Recording this as "maybe delivered" marked the
                    # contact emailed, counted it against the warm-up and domain
                    # caps, enrolled the follow-ups and threaded them under a
                    # message that never left.
                    err = f"Connection closed before sending: {e}"
                    logger.error(err)
                    return False, err, False, False
                # Raised from send_message, i.e. after the pre-flight succeeded,
                # so the message was at least partly transmitted. The server may
                # already have queued it. Retrying delivers it twice; reporting a
                # plain failure makes the retry queue do exactly the same. Report
                # it as indeterminate so the caller stops instead.
                err = f"Connection lost mid-send, delivery unconfirmed: {e}"
                logger.error(err)
                return False, err, False, True

            except smtplib.SMTPAuthenticationError as e:
                # Latch a circuit breaker. Every contact opens a fresh connection
                # and a fresh LOGIN, so an expired App Password produced hundreds
                # of failed logins in minutes - enough for the provider to flag or
                # lock the account - while every contact was marked 'failed'.
                # get_pending_contacts() selects only status='pending', so those
                # leads were removed from the backlog PERMANENTLY and never
                # retried once the credentials were fixed. There were guards for
                # bounce rate and for blacklisting, and none for auth.
                self._reset_connection()
                self.auth_failed = True
                # The server's own text, not just ours: Microsoft 365 answers 535
                # for a wrong password and for SMTP AUTH being disabled for the
                # tenant alike, and only its message tells the two apart.
                server = e.smtp_error
                if isinstance(server, bytes):
                    server = server.decode("utf-8", "replace")
                if self.signin is not None:
                    err = (
                        "SMTP authentication failed - "
                        f"{explain_mailbox_refusal('SMTP', server)} "
                        f"(server said: {e.smtp_code} {server})"
                    )
                else:
                    err = (
                        "SMTP authentication failed - check credentials/App "
                        f"Password (server said: {e.smtp_code} {server})"
                    )
                logger.error("%s - halting this batch", err)
                return False, err, False, False

            except SignInRefused as e:
                # Microsoft refused the token, or the certificate cannot be
                # used: as broken as a wrong password, and the same breaker.
                self._reset_connection()
                self.auth_failed = True
                err = f"SMTP authentication failed - Microsoft sign-in refused: {e}"
                logger.error("%s - halting this batch", err)
                return False, err, False, False

            except smtplib.SMTPRecipientsRefused as e:
                # Per-recipient rejection; the connection itself is still fine.
                permanent = self._rejection_is_permanent(e.recipients or {})
                err = f"Recipient refused: {e}"
                logger.error(err)
                return False, err, permanent, False

            except smtplib.SMTPException as e:
                self._reset_connection()
                # NOT permanent, even on a 5xx. Only SMTPRecipientsRefused is a
                # verdict on the ADDRESS. A 5xx here comes from DATA or MAIL
                # FROM - content filtering, a refused sender, a reputation block
                # - which is a verdict on US. Suppressing the recipient for that
                # would punish a valid prospect, and because every prospect gets
                # the same template, one content-filter rejection would walk the
                # whole list suppressing everyone on it.
                err = f"SMTP error: {e}"
                logger.error(err)
                return False, err, False, False

            except OSError as e:
                # Transport-level failure: DNS, refused connection, TLS
                # handshake, socket timeout. smtplib.send() and getreply()
                # convert every socket error raised *during* a message into
                # SMTPServerDisconnected, which the handler above already owns,
                # so an OSError reaching here comes from opening the connection
                # - nothing was transmitted. Same verdict as before: not a
                # judgement on the address (permanent=False) and not
                # indeterminate, because there is no message in flight.
                self._reset_connection()
                err = f"Unexpected email error: {e}"
                logger.error(err)
                return False, err, False, False

            except Exception as e:
                # Deliberately broad, and last. _deliver's contract is to return
                # a verdict, never to raise: dispatch_to_contact, _process_contact
                # and _email_contacts have no handler between here and the batch
                # loop, so an escaping defect would kill the run outright and
                # leave a contact whose message may already be on the wire marked
                # 'pending' - i.e. re-sent on the next loop. Log the traceback so
                # the defect is fixable instead of being reduced to one line.
                self._reset_connection()
                err = f"Unexpected email error: {e}"
                logger.exception(err)
                return False, err, False, False

            except BaseException:
                # Every Exception is answered above, so this is the process
                # being stopped: Ctrl+C, a shutdown. Once the message is going
                # to the server it cannot know how that ended. Before that it
                # knows nothing went out, and says so: left 'preparing', the
                # person would be settled as emailed and never get the email.
                if transmitting:
                    raise
                self._contained(self.db.record_stopped_send, msg["Message-ID"])
                raise

    @staticmethod
    def _contained(write, *args, **kwargs) -> None:
        """One write about a send that did not go out. Whatever the database
        does, the caller still has to see a failed send, and the batch to go on;
        the next write of the same send gets its own chance."""
        try:
            write(*args, **kwargs)
        except sqlite3.Error as e:
            logger.error("Could not record a send that did not go out: %s", e)

    def send_email(self, msg: EmailMessage) -> tuple[bool, str]:
        """
        Send an email via SMTP over a reused, lock-protected connection.
        Returns (success: bool, message: str).
        """
        success, message, _permanent, _indeterminate = self._deliver(msg)
        return success, message

    # -----------------------------------------------------------------
    # Full Dispatch Pipeline (with DB logging)
    # -----------------------------------------------------------------

    def _blocked_meanwhile(
        self, recipient: str, contact_id: int | None, email_log_id: int | None = None
    ) -> bool:
        """Whether the address was blocked after process_contact checked it.

        That check runs before the email is personalised, which takes seconds
        with AI personalisation. An erasure landing in between had already
        cleaned every table, and the send that followed wrote the address and
        name back and flipped the erased contact to 'emailed'. So this is
        asked before anything about the person is written, and again by
        _deliver once the connection is ready. The contact is left as the
        block left it, and neither the logged attempt nor the activity row
        names the address.
        """
        if not self.db.is_blocked(recipient):
            return False
        logger.info("Not sending to %s: it was blocked while being prepared", recipient)
        reason = "Not sent: the address was blocked while the email was prepared"
        if email_log_id:
            self.db.update_email_status(email_log_id, "failed", error_message=reason)
        self.db.log_activity(
            "EMAIL_STOPPED", f"{reason} (contact {contact_id})", status="info"
        )
        return True

    def dispatch_to_contact(
        self,
        contact: dict,
        subject: str,
        html_body: str,
        plain_body: str,
        pdf_path: str | None = None,
        dry_run: bool = False,
        tracking_id: str | None = None,
        unsubscribe_url: str | None = None,
        ab_variant: str | None = None,
        in_reply_to: str | None = None,
        notice_version: str | None = None,
        first_email: bool = True,
        sequence_step_id: int | None = None,
    ) -> int | bool | ClaimLost:
        """
        Full dispatch pipeline for a single contact:
        1. Claim the send: log pre-dispatch intent
        2. Create email with attachment
        3. Send email
        4. Log post-dispatch result
        5. Update contact status

        ``first_email`` is a send that goes only once, the campaign's or its
        retry; ``sequence_step_id`` the follow-up step this send is.

        Returns True if email was sent successfully (or dry_run), and CLAIM_LOST
        if another send holds the contact: nothing was sent or written.
        """
        contact_id = contact.get("id")
        recipient = contact["email"]
        attachment_name = Path(pdf_path).name if pdf_path else None
        row = {
            "contact_id": contact_id,
            "recipient_email": recipient,
            "subject": subject,
            "body_preview": plain_body[:200] if plain_body else None,
            "has_attachment": bool(pdf_path),
            "attachment_filename": attachment_name,
            "tracking_id": tracking_id,
            "ab_variant": ab_variant,
            # With sender rotation live, a row that doesn't say which mailbox
            # sent it makes per-account problems (one address landing in spam,
            # one throttled) undiagnosable from the dashboard.
            "sender_account": self.smtp_username,
            "notice_version": notice_version,
            "sequence_step_id": sequence_step_id,
        }

        if self._blocked_meanwhile(recipient, contact_id):
            return False

        # 'preparing' is a live send under way, or one cut off after the server
        # may have accepted it, and counts as sent until it is settled
        # (Database.settle_interrupted_sends). A dry run sends nothing, so it
        # claims nothing, and its row below says so from the start: written
        # 'preparing', a simulation whose next write failed would be settled
        # as an email that may have gone.
        if not dry_run:
            email_log_id = self.db.claim_send(first_email=first_email, **row)
            if email_log_id is None:
                logger.info("Not sending to %s: another send holds it", recipient)
                return CLAIM_LOST

        # Pre-dispatch logging. Contained: the claim is written, so this line
        # going missing must not leave it behind for a send that never started.
        self._contained(
            self.db.log_activity,
            "EMAIL_PREPARING",
            f"Preparing email to {recipient} ({contact.get('organization', 'N/A')})",
            status="info",
        )

        if dry_run and not contact_id:
            # A Direct Send preview of an address the agent does not hold:
            # there is no contact to log the simulated send against, and the
            # preview must not create one (it would join the campaign backlog).
            self.db.log_activity(
                "EMAIL_DRY_RUN",
                f"[DRY RUN] Would have sent to {recipient}",
                status="info",
            )
            logger.info(f"[DRY RUN] Would send to: {recipient}")
            return True

        if dry_run:
            email_log_id = self.db.log_email(
                **row,
                delivery_status="dry_run",
                smtp_response="DRY RUN - email not actually sent",
            )
            # Dry run: log the simulated send, and change NOTHING about the
            # contact.
            #
            # This used to set contacts.status = 'dry_run', which quietly
            # destroyed the backlog: get_pending_contacts selects only
            # status='pending', nothing anywhere moves a contact back, and no
            # code reads 'dry_run' at all - it is a dead end. So a single
            # `main.py run --dry-run` against a live database silently made
            # every contact it touched permanently unsendable. A dry run is a
            # SIMULATION; its whole promise is that running it changes nothing
            # you would not want changed.
            #
            # The email_log row still records delivery_status='dry_run', which
            # is the right place for it - that IS a record of the simulation.
            # The Emails table lists it; no count of emails includes it.
            self.db.log_activity(
                "EMAIL_DRY_RUN",
                f"[DRY RUN] Would have sent to {recipient}",
                status="info",
            )
            logger.info(f"[DRY RUN] Would send to: {recipient}")
            return email_log_id

        # Create the email message
        try:
            msg = self.create_email(
                to_email=recipient,
                subject=subject,
                html_body=html_body,
                plain_body=plain_body,
                pdf_path=pdf_path,
                unsubscribe_url=unsubscribe_url,
                in_reply_to=in_reply_to,
            )
        except (OSError, ValueError, TypeError, LookupError, MessageError) as e:
            # Everything create_email can actually fail on: reading the PDF
            # attachment (OSError), a header the email policy refuses such as a
            # subject with an embedded newline (ValueError), a wrongly typed
            # subject/body from the caller (TypeError, which also covers
            # MultipartConversionError), an unknown charset (LookupError), and
            # the email package's own MessageError family. Nothing was sent, so
            # failing this one contact and continuing the batch is safe.
            err = f"Failed to create email for {recipient}: {e}"
            logger.exception(err)
            self.db.record_refused_send(email_log_id, "failed", error_message=err)
            self._contained(
                self.db.update_contact_status,
                contact_id,
                "failed",
                discard_reason=_reason(err),
            )
            self._contained(self.db.log_activity, "EMAIL_ERROR", err, status="error")
            return False

        # Record the id we just minted, so a later follow-up can thread to it.
        # Best-effort: losing it costs threading on the NEXT touch, which is
        # never a reason to abandon a send that is otherwise ready to go.
        try:
            if msg["Message-ID"]:
                self.db.set_email_message_id(email_log_id, msg["Message-ID"])
        except Exception as e:  # noqa: BLE001 - threading is not worth a send
            logger.warning("Could not store the Message-ID for %s: %s", recipient, e)

        # Send the email. The last check runs inside _deliver: waiting for a
        # send on another thread, or reconnecting, is time an erasure can land
        # in too.
        stopped = False

        def blocked() -> bool:
            nonlocal stopped
            stopped = self._blocked_meanwhile(recipient, contact_id, email_log_id)
            return stopped

        success, message, permanent, indeterminate = self._deliver(msg, blocked)
        if stopped:
            return False

        if success or indeterminate:
            # The bookkeeping below runs AFTER the message is on the wire. If it
            # raises - a locked DB past busy_timeout, a full disk - the exception
            # used to unwind through _process_contact and _email_contacts (neither
            # has a handler), killing the rest of the batch AND leaving the
            # contact 'pending', so the next loop emailed the same person again.
            # A delivered message must never be re-sent because we failed to
            # write a row about it.
            status = "sent" if success else "sent_unconfirmed"
            try:
                self.db.update_email_status(email_log_id, status, smtp_response=message)
                self.db.update_contact_status(contact_id, "emailed")
            except sqlite3.Error as e:
                logger.error(
                    "Email to %s was delivered but recording it failed (%s). "
                    "The contact is NOT being re-queued; reconcile manually.",
                    recipient,
                    e,
                )
            # Journalled whatever became of the two writes above: the message
            # is out, and a restore from a backup older than this send would
            # otherwise send it again.
            self.db.journal_send(
                recipient,
                message_id=msg["Message-ID"],
                step_id=sequence_step_id,
                mailbox=self.smtp_username,
                notice=notice_version,
                unconfirmed=indeterminate,
            )
            # The activity row is bookkeeping after delivery too, and was the
            # one write left outside the handler above: with the database
            # locked it raised after all, and the send it describes was
            # reported as failed.
            try:
                if indeterminate:
                    # We do not know whether the server accepted it. A duplicate
                    # cold email costs a spam complaint; a missed one costs one
                    # prospect. Stop here rather than let the retry queue resend.
                    self.db.log_activity(
                        "EMAIL_UNCONFIRMED",
                        f"Delivery unconfirmed for {recipient} - not resending: "
                        f"{message}",
                        status="warning",
                    )
                else:
                    self.db.log_activity(
                        "EMAIL_SENT",
                        f"Successfully sent to {recipient}",
                        status="success",
                    )
            except sqlite3.Error as e:
                logger.error("Could not log the send to %s: %s", recipient, e)
            if indeterminate:
                logger.warning(f"[UNCONFIRMED] {recipient}: {message}")
            else:
                logger.info(f"[OK] Sent email to: {recipient}")
            return email_log_id
        else:
            # The server refused it and the process knows: every write here is
            # contained, so a locked database cannot turn that into a row that
            # reads as possibly sent, or take the batch down. The caller still
            # sees a failed send, and queues its retry as it always does.
            self.db.record_refused_send(
                email_log_id,
                "rejected" if permanent else "failed",
                smtp_response=message,
                error_message=message,
            )
            if self.auth_failed:
                # Our credentials are broken, which says nothing about this
                # address. Leave the contact 'pending' so it is picked up again
                # once the password is fixed, instead of marking it 'failed' -
                # get_pending_contacts() filters on 'pending', so 'failed' meant
                # the lead was gone for good.
                self._contained(
                    self.db.log_activity,
                    "SMTP_AUTH_FAILED",
                    f"Auth failure while sending to {recipient} - contact left "
                    "pending, batch halted",
                    status="error",
                )
                logger.error("[AUTH] %s left pending: %s", recipient, message)
                return False
            if permanent:
                # The server said this address will never accept mail. Treat it
                # exactly like a hard bounce: suppress it so no retry, follow-up
                # or future search result can resurrect it.
                self._contained(
                    self.db.add_to_suppression, recipient, reason="smtp_rejected"
                )
                self._contained(
                    self.db.update_contact_status,
                    contact_id,
                    "bounced",
                    discard_reason=_reason(
                        f"Refused by the recipient's server: {message}"
                    ),
                )
                self._contained(
                    self.db.log_activity,
                    "EMAIL_REJECTED",
                    f"{recipient} permanently rejected by SMTP - suppressed: {message}",
                    status="error",
                )
                logger.error(f"[REJECTED] {recipient} suppressed: {message}")
                return False

            self._contained(
                self.db.update_contact_status,
                contact_id,
                "failed",
                discard_reason=_reason(f"Send failed: {message}"),
            )
            self._contained(
                self.db.log_activity,
                "EMAIL_FAILED",
                f"Failed to send to {recipient}: {message}",
                status="error",
            )
            logger.error(f"[FAIL] Failed to send to {recipient}: {message}")
            return False

    # -----------------------------------------------------------------
    # Email Template Rendering
    # -----------------------------------------------------------------

    @staticmethod
    def render_template(
        template_html: str,
        contact: dict,
        company_name: str,
        sender_name: str,
        values: dict | None = None,
        markup: dict | None = None,
        language: str = "en",
    ) -> tuple[str, str]:
        """
        Render the email template with contact-specific personalization.
        Returns (html_body, plain_body).

        Each value is text, HTML-escaped here, once: the contact's name, role
        and organisation, the sender's names, and the caller's `values` (the
        greeting, the opening line, the unsubscribe link).
        `markup` is HTML whose builder escaped its own values (the body, the
        legal footer, the tracking pixel), inserted as it is. All placeholders
        are filled in one pass, so nothing inserted is searched again: an
        organisation that reads "{{organization}}" stays that text.
        `language` is the email's: an unknown organisation reads "your
        company", or "la Sua azienda" in an Italian email.
        """
        name = contact.get("name")
        # Use a clean greeting for unknown names
        if not name or name.lower() in ("sir/madam", "n/a", "unknown"):
            name = None
        role = contact.get("role") or "your"
        organization = contact.get("organization") or UNKNOWN_ORGANIZATION.get(
            language, UNKNOWN_ORGANIZATION["en"]
        )

        merged = {
            "name": name or "",
            "role": role,
            "organization": organization,
            "company_name": company_name,
            "sender_name": sender_name,
            **(values or {}),
        }
        filled = {key: html.escape(value) for key, value in merged.items()}
        filled.update(markup or {})
        # A value that ends a sentence and ends in a full stop of its own
        # ("Rossi S.p.A.") takes the place of the template's, so the email
        # reads "S.p.A." and not "S.p.A..". Markup is inserted as it is.
        ends_in_a_stop = {
            key
            for key, value in merged.items()
            if value.endswith(".") and key not in (markup or {})
        }

        def fill(m: re.Match) -> str:
            key = m.group(1)
            if key not in filled:
                return m.group(0)
            if key in ends_in_a_stop:
                return filled[key]
            return filled[key] + m.group(2)

        html_body = _PLACEHOLDER.sub(fill, template_html)

        # Fix greeting for unknown names: "Hi ," -> "Hello,"
        html_body = html_body.replace("Hi ,", "Hello,")
        html_body = html_body.replace("Hi  ,", "Hello,")
        html_body = html_body.replace("Dear ,", "Hello,")

        # Generate plain text version by stripping HTML. Drop <head>/<title>/
        # <style>/<script> first so the document title ("Hello") and any CSS
        # never leak into the readable plain-text body.
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_body, "html.parser")
        for tag in soup(["head", "title", "style", "script"]):
            tag.decompose()
        # get_text() keeps a link's words and drops its href, so the opt-out
        # link became the bare words "unsubscribe here" in the text part.
        for a in soup.find_all("a", href=True):
            href = str(a.get("href") or "").strip()
            text = a.get_text(" ", strip=True)
            if href and not href.startswith("#") and href != text:
                a.replace_with(f"{text} ({href})" if text else href)
        target = soup.body or soup
        plain_body = target.get_text(separator="\n", strip=True)

        return html_body, plain_body
