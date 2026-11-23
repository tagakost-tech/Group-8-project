"""
Signed-token unsubscribe system.
Generates and verifies HMAC-signed tokens for secure one-click unsubscribe.
"""

import hashlib
import hmac
import logging
import re
import sqlite3

from modules import compliance, optout_spool
from modules.company_block import block_company
from modules.database import Database

logger = logging.getLogger(__name__)

# The token unsubscribe links carry since step G20.
OPAQUE_TOKEN = re.compile(r"u[0-9a-f]{32}")

# What the second half of a token is an HMAC of, before the first half: it keeps
# the tag apart from any HMAC of an address.
TOKEN_TAG_LABEL = "unsubscribe-token-tag:"


class UnsubscribeHandler:
    """Handles secure unsubscribe operations using HMAC-signed tokens."""

    CONFIRMATION_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Unsubscribed</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                         Roboto, 'Helvetica Neue', Arial, sans-serif;
            background-color: #ffffff;
            color: #333333;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            text-align: center;
            max-width: 480px;
            padding: 40px;
        }
        .icon {
            font-size: 48px;
            margin-bottom: 24px;
            color: #4CAF50;
        }
        h1 {
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 16px;
            color: #1a1a1a;
        }
        p {
            font-size: 16px;
            line-height: 1.5;
            color: #666666;
            margin-bottom: 12px;
        }
        .note {
            font-size: 14px;
            color: #999999;
            margin-top: 24px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="icon">&#10003;</div>
        <h1>You have been unsubscribed</h1>
        <p>We will not email you again.</p>
        <p>To make sure of that, we keep your address on a
            do-not-contact list and use it for nothing else.</p>
        <p class="note">
            To have your other details erased as well, or if this was a
            mistake, reply to any email we sent you.
        </p>
    </div>
</body>
</html>"""

    # The same page for a contact written to in Italian (compliance.
    # language_for), statement for statement. The pages below are for a link
    # whose address is not known, so they stay English.
    CONFIRMATION_HTML_IT = (
        CONFIRMATION_HTML.split('    <div class="container">')[0]
        .replace('<html lang="en">', '<html lang="it">')
        .replace(
            "<title>Unsubscribed</title>", "<title>Cancellazione completata</title>"
        )
        + """    <div class="container">
        <div class="icon">&#10003;</div>
        <h1>Cancellazione completata</h1>
        <p>Non riceverà più nostre email.</p>
        <p>Per esserne certi, conserviamo il suo indirizzo in un elenco di
            esclusione e non lo usiamo per altro.</p>
        <p class="note">
            Per far cancellare anche gli altri suoi dati, o se si tratta di un
            errore, risponda a una qualsiasi delle nostre email.
        </p>
    </div>
</body>
</html>"""
    )

    # An opaque link that matches nobody: for an address erased or removed by
    # retention since, sent since a backup we were restored from, or not one of
    # ours. The click is kept, and applied if we come to email whoever it belongs
    # to (modules/optout_spool.py), and the page promises that and no more: we
    # cannot tell whose it is.
    NO_MATCH = "This link matches no one on our mailing list; your request is kept."
    NO_MATCH_HTML = (
        CONFIRMATION_HTML.split('    <div class="container">')[0]
        + """    <div class="container">
        <h1>Request kept</h1>
        <p>This link matches no one on our mailing list at the moment. We have
            kept your request for a year, and will apply it if we come to email
            the address it belongs to.</p>
        <p class="note">
            If another email from us reaches you, use the
            unsubscribe link in it, or reply to it, and we will stop.
        </p>
    </div>
</body>
</html>"""
    )

    # The same link once the clicks kept are at their limit, or a token no key of
    # ours made. Nothing is recorded, so the page promises nothing.
    NOT_KEPT = "This link matches no one on our mailing list, so nothing was changed."
    NOT_KEPT_HTML = (
        CONFIRMATION_HTML.split('    <div class="container">')[0]
        + """    <div class="container">
        <h1>Nothing to unsubscribe</h1>
        <p>This link matches no one on our mailing list,
            so nothing was changed.</p>
        <p class="note">
            If another email from us reaches you, use the
            unsubscribe link in it, or reply to it, and we will stop.
        </p>
    </div>
</body>
</html>"""
    )

    def __init__(self, db: Database, secret_key: str):
        self.db = db
        self.secret_key = secret_key

    def _hmac_hex(self, message: str) -> str:
        return hmac.new(
            self.secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _tag(self, head: str) -> str:
        return self._hmac_hex(TOKEN_TAG_LABEL + head)[:16]

    def token_for(self, email_address: str) -> str:
        """An opaque token for an address: "u", 16 hex digits of its HMAC, and
        a 16-digit tag of those under the same key.

        The address cannot be read back out of it (step G20). The form it
        replaced, base64(address) + "." + signature, could be decoded by
        anyone who saw the link: a link scanner, a forwarded email, a proxy
        or tunnel log. The tag lets a click the database has no record of
        show it is one of ours (is_our_token). Pure: it records nothing.
        """
        address = (email_address or "").strip().lower()
        head = self._hmac_hex(address)[:16]
        return "u" + head + self._tag(head)

    def is_our_token(self, token: str) -> bool:
        """Whether this key could have made the token. One it could not was
        never sent by us under the current key, and a click kept for it could
        never be applied: the send check looks up token_for(recipient)."""
        if not OPAQUE_TOKEN.fullmatch(token or ""):
            return False
        head, tag = token[1:17], token[17:]
        return hmac.compare_digest(tag, self._tag(head))

    def generate_token(self, email_address: str) -> str:
        """The token for a link we are about to send, recorded so a click on
        it is one lookup. A database error propagates: an email goes out only
        with a link that can be resolved."""
        token = self.token_for(email_address)
        self.db.register_unsubscribe_token(token, email_address)
        return token

    def record_links_again(self) -> int:
        """After a restore, which takes the record of our links back to its
        backup: record the link of every contact held, so that a click on one
        sent since is one lookup again. Under the same UNSUBSCRIBE_SECRET it is
        the same token. Only contacts: no link goes out to an address without
        one, and retention, which follows contacts, would never take such a
        record away. Returns how many were missing."""
        return self.db.register_unsubscribe_tokens(
            (self.token_for(address), address)
            for address in self.db.contact_addresses()
        )

    def verify_token(self, token: str):
        """The address a token belongs to, or None.

        An opaque token is honoured when it is in the record of the links we
        made, which only generate_token and record_links_again write, whatever
        key made it: a link sent before UNSUBSCRIBE_SECRET changed keeps
        working. A database error propagates, so a busy database is never
        mistaken for a bad link.

        Nothing else is a token. The old form, base64(address) + "." +
        signature, is refused: no email carrying one was ever sent, and
        whoever holds UNSUBSCRIBE_SECRET could make one for any address at
        all, not only for those the agent emailed.
        """
        if OPAQUE_TOKEN.fullmatch(token or ""):
            return self.db.address_for_unsubscribe_token(token)
        logger.warning("An unsubscribe token that is not in the opaque form: refused")
        return None

    def confirmation_page(self, token: str, settings) -> str:
        """The confirmation in the language the contact is written to in, its
        first email's when it had one. English when the address cannot be
        read, the database being busy included: the opt-out itself is already
        recorded or held."""
        try:
            address = self.verify_token(token)
            italian = bool(address) and (
                compliance.language_for(settings, self.db, email=address)[0] == "it"
            )
        except sqlite3.Error as e:
            logger.warning("Could not read the address for the page language: %s", e)
            italian = False
        if italian:
            return self.CONFIRMATION_HTML_IT
        return self.CONFIRMATION_HTML

    def generate_unsubscribe_url(
        self, email_address: str, base_url: str, record: bool = True
    ) -> str:
        """
        Generate a full unsubscribe URL.

        Args:
            email_address: The email address to generate the URL for.
            base_url: The base URL of the application (e.g., 'https://example.com').
            record: False for a preview: the same link, not recorded, since
                no email carrying it goes out.

        Returns:
            Full unsubscribe URL.
        """
        if record:
            token = self.generate_token(email_address)
        else:
            token = self.token_for(email_address)
        # Strip trailing slash from base_url
        base = base_url.rstrip("/")
        url = f"{base}/unsubscribe/{token}"
        return url

    def process_unsubscribe(self, token: str, one_click: bool = False) -> tuple:
        """
        Process an unsubscribe request from a token.

        Verifies the token, adds the email to the suppression list,
        and cancels any active sequence enrollments.

        Args:
            token: The signed unsubscribe token.

        Returns:
            Tuple of (success: bool, message: str).
        """
        how = "Opted out via the unsubscribe link"
        try:
            email_address = self.verify_token(token)
        except sqlite3.Error as e:
            # Too busy to tell whose link this is. The token itself waits on
            # disk: the send check refuses any recipient it belongs to, and
            # the replay resolves and applies it.
            logger.warning("Opt-out held until the database is free: %s", e)
            optout_spool.spool_optout(
                self.db.db_path, None, how, company=one_click, token=token
            )
            return (True, "Your request is recorded.")

        if email_address is None:
            if OPAQUE_TOKEN.fullmatch(token or ""):
                if not self.is_our_token(token):
                    # Made up, or made under another key: no send could ever
                    # apply it, so keeping it would only use up the cap.
                    logger.warning("An unsubscribe link no key of ours made: not kept")
                    return (True, self.NOT_KEPT)
                logger.warning("An unsubscribe link matched no address held")
                # Mailbox providers never re-send a one-click, and a restore
                # can have lost the record of a real link: the send check
                # applies this to its recipient, should we come to email them.
                if optout_spool.set_aside_click(
                    self.db.db_path, token, how, company=one_click
                ):
                    return (True, self.NO_MATCH)
                logger.warning("Too many clicks set aside: this one was refused")
                return (True, self.NOT_KEPT)
            return (False, "Invalid or expired unsubscribe link.")

        # Shared with the opt-out-by-email path (modules/imap_monitor.py): it
        # normalises the address - a raw INSERT here once stored the token's
        # casing, which matched nothing on lookup and let an unsubscribed
        # person keep receiving mail (GDPR Art. 21) - marks the contact
        # 'unsubscribed', ends its follow-ups, and turns an operator's block
        # into an opt-out that Unblock cannot remove.
        try:
            outcome = self.db.record_opt_out(email_address, how)
        except sqlite3.Error as e:
            # A locked database must not lose the opt-out: the one-click
            # request is never re-sent. It waits on disk and is applied
            # before the next email goes out (modules/optout_spool.py).
            logger.warning("Opt-out held until the database is free: %s", e)
            optout_spool.hold_optout(self.db, email_address, how, company=one_click)
            return (True, "Your request is recorded.")
        cancelled_count = outcome["cancelled"]
        # The one-click button is the recipient's own deliberate act, so it
        # stops the whole company. A plain click on the footer link may be a
        # mail filter opening links by itself, so it stops only the address.
        if one_click:
            try:
                block_company(self.db, email_address, how)
            except sqlite3.Error as e:
                # The opt-out stands; the company block waits on disk and is
                # applied with the next replay, like a held opt-out.
                logger.warning("Company block held until the database is free: %s", e)
                optout_spool.hold_optout(self.db, email_address, how, company=True)

        if outcome["new"]:
            logger.info("Added '%s' to suppression list", email_address)
            # The one-click link is the primary opt-out route whenever BASE_URL
            # is public, yet it was the one suppression path that left nothing
            # on the dashboard - opt-outs arriving by email were recorded and
            # these were not.
            try:
                self.db.log_activity(
                    "SUPPRESSED_OPTOUT",
                    f"{email_address} suppressed - unsubscribe link used",
                    status="warning",
                )
            except sqlite3.Error as e:
                # The opt-out is already committed above, so this only costs
                # the dashboard entry - the opt-out itself stands.
                logger.warning("Could not log the unsubscribe: %s", e)
        else:
            logger.info("'%s' already on suppression list", email_address)

        msg = f"Successfully unsubscribed '{email_address}'."
        if cancelled_count > 0:
            msg += f" Cancelled {cancelled_count} active sequence enrollment(s)."

        logger.info("%s", msg)
        return (True, msg)
