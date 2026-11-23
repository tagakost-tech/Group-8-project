"""Backlog draining, qualification execution, cadence, and retry queue processing.

Manages backlog draining (process_pending_contacts), fast-path vs fresh qualification
partitioning, qualification execution (qualify_contacts), follow-up sequence
cadence draining (process_sequences), and retry queue draining (process_retries).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from modules.company_country import settle_company_country
from modules.compliance import language_for
from modules.email_sender import CLAIM_LOST

if TYPE_CHECKING:
    from agent import OutreachAgent

logger = logging.getLogger(__name__)

# The Italian subjects of the shipped follow-up steps, by template name, for
# recipients written to in Italian. A step of the operator's own keeps its
# stored subject.
FOLLOW_UP_SUBJECTS_IT = {
    "followup_1": "Faccio seguito alla mia email precedente — {org}",
    "followup_2": "Un ultimo messaggio per {org}",
}


class BacklogDrainer:
    """Manages backlog processing, qualification pipeline, follow-up sequences, and retry queue."""

    def __init__(self, agent: OutreachAgent):
        self.agent = agent

    def process_pending_contacts(self, dry_run: bool = False) -> None:
        """Email contacts already stored as 'pending' in the database.

        Contacts discovered on a previous run (or skipped earlier because of
        warmup/domain limits, or an interrupted run) sit in the DB with
        status 'pending' and would otherwise never be contacted, because
        re-running a search only returns brand-new (de-duplicated) contacts.
        This drains that backlog so real leads actually get emailed.
        """
        pending = self.agent.db.get_pending_contacts()
        if not pending:
            return

        logger.info(f"Processing {len(pending)} pending contact(s) from the backlog")
        self.agent.db.log_activity(
            "BACKLOG", f"Processing {len(pending)} pending contact(s)", status="info"
        )
        # Contacts already verified on a prior pass skip the EXPENSIVE steps
        # (AI relevance classification + MX lookup) - re-running those over the
        # whole backlog every loop stalls sends behind hundreds of API calls.
        # We still apply the cheap, deterministic relevance gate (geo /
        # placeholder / vertical - no network, no API) so a non-European or junk
        # contact that slipped into the backlog is never emailed.
        # With MX checks off nothing sets is_verified, so qualified_at stands
        # in for it - otherwise every waiting contact was re-qualified (and
        # re-billed by the AI classifier) on every loop. With MX checks on,
        # only a real verification counts.
        mx_on = bool(self.agent.email_verifier and self.agent.settings.verify_mx)

        def _done(c):
            return bool(c.get("is_verified") or (c.get("qualified_at") and not mx_on))

        verified = [c for c in pending if _done(c)]
        needs_qualify = [c for c in pending if not _done(c)]

        to_email = []
        for c in verified:
            # The verified fast path skips _qualify_contacts entirely, so it
            # needs its own client gate - the same reason it carries its own
            # copy of the relevance check.
            client = self.agent._active_client(c.get("organization"))
            if client:
                logger.info(
                    "Backlog contact is an active client: %s (%s)",
                    c.get("email"),
                    client,
                )
                self.agent._stats["client_skipped"] = (
                    self.agent._stats.get("client_skipped", 0) + 1
                )
                if c.get("id"):
                    try:
                        self.agent.db.update_contact_status(
                            c["id"],
                            "active_client",
                            discard_reason=f"Active client: {client}",
                        )
                    except sqlite3.Error as e:
                        logger.error("Could not record the client verdict: %s", e)
                continue
            if self.agent.relevance_filter:
                verdict = self.agent.relevance_filter.assess(c)
                if not verdict["relevant"]:
                    logger.info(
                        f"Backlog contact filtered out: {c.get('email')} "
                        f"({verdict['reason']})"
                    )
                    self.agent._stats["irrelevant_skipped"] += 1
                    if c.get("id"):
                        try:
                            self.agent.db.update_contact_status(
                                c["id"], "irrelevant", discard_reason=verdict["reason"]
                            )
                        except sqlite3.Error as e:
                            logger.error(
                                "Could not record the backlog irrelevance verdict "
                                "for %s: %s",
                                c.get("email"),
                                e,
                            )
                    continue
            to_email.append(c)

        # Qualification is the expensive part (AI classification, MX and
        # SMTP probes), and it only pays off if a send can follow. At
        # night, while paused, or once every mailbox has used today's
        # allowance, the verdict would be hours stale by the send - and a
        # contact erased by retention meanwhile was classified for
        # nothing. The contacts stay pending and are qualified later.
        if needs_qualify and not self.agent._send_possible_now():
            logger.info(
                "No send can follow now - leaving %d backlog contact(s) "
                "unqualified until one can.",
                len(needs_qualify),
            )
            needs_qualify = []
        if needs_qualify:
            to_email = to_email + self.agent._qualify_contacts(needs_qualify)
        self.agent._email_contacts(to_email, dry_run)

    _process_pending_contacts = process_pending_contacts

    def qualify_contacts(self, contacts: list[dict]) -> list[dict]:
        """Score and verify contacts, filter out low-quality leads."""
        qualified = []

        for contact in contacts:
            # Honor a stop request promptly - qualification can grind through
            # hundreds of contacts (AI + MX per contact), and without this check
            # a stop wouldn't take effect until the whole batch finished.
            if not self.agent._running:
                break

            contact_id = contact.get("id")

            # Active client: drop before spending a verification, an AI call
            # or a send allowance on someone who already buys from us. The
            # verdict is recorded on the contact so the Contacts tab shows
            # WHICH client it matched - name matching is judgement, and a
            # judgement nobody can inspect is one nobody can correct. Ahead of
            # the score as well: a client that scored low was stored as
            # 'low_score', which says the lead is weak, not that it buys.
            client = self.agent._active_client(contact.get("organization"))
            if client:
                logger.info("Skipping active client: %s (%s)", contact["email"], client)
                self.agent._stats["client_skipped"] = (
                    self.agent._stats.get("client_skipped", 0) + 1
                )
                if contact_id:
                    try:
                        self.agent.db.update_contact_status(
                            contact_id,
                            "active_client",
                            discard_reason=f"Active client: {client}",
                        )
                    except sqlite3.Error as e:
                        logger.error(
                            "Could not record the client verdict for %s: %s",
                            contact["email"],
                            e,
                        )
                continue

            # Named competitor: the twin of the client screen above, for the
            # same reason. Checked only at send time, a rival still cost an MX
            # lookup, an AI call and a consent row claiming a legitimate
            # interest in emailing them.
            competitor = self.agent._known_competitor(contact.get("organization"))
            if competitor:
                logger.info(
                    "Skipping known competitor: %s (%s)", contact["email"], competitor
                )
                self.agent._stats["irrelevant_skipped"] += 1
                if contact_id:
                    try:
                        self.agent.db.update_contact_status(
                            contact_id,
                            "irrelevant",
                            discard_reason=f"competitor: {competitor}",
                        )
                    except sqlite3.Error as e:
                        logger.error(
                            "Could not record the competitor verdict for %s: %s",
                            contact["email"],
                            e,
                        )
                continue

            # Lead scoring. The verdict is computed inside the try but ACTED ON
            # outside it - the same shape the verification block below uses, and
            # for the same reason: the discard `continue` used to sit after
            # update_contact_status INSIDE the try, so a locked DB skipped the
            # `continue` entirely and a below-threshold lead fell through to the
            # send list with low_score_skipped already incremented. The counter
            # said skipped; the mailbox said sent.
            score_result = None
            if self.agent.lead_scorer:
                try:
                    score_result = self.agent.lead_scorer.score_contact(contact)
                    contact["lead_score"] = score_result["score"]
                    contact["lead_tier"] = score_result["tier"]
                    self.agent._stats["scored"] += 1

                    if contact_id:
                        self.agent.db.update_contact_score(
                            contact_id, score_result["score"], score_result["tier"]
                        )
                        self.agent.db.log_activity(
                            "LEAD_SCORED",
                            f"{contact['email']}: score={score_result['score']}, "
                            f"tier={score_result['tier']}",
                            status="info",
                        )
                except sqlite3.Error as e:
                    # Scoring itself is pure arithmetic over the contact dict;
                    # the only thing in here that can fail at runtime is the
                    # bookkeeping written back to the DB.
                    logger.warning(
                        f"Could not record the lead score for {contact['email']}: {e}"
                    )

                # Filter out low scores. Bookkeeping failure must not be able to
                # resurrect a rejected lead, so it is contained separately.
                if (
                    score_result is not None
                    and score_result["score"]
                    < self.agent.settings.min_lead_score_to_email
                ):
                    logger.info(
                        f"Skipping low-score lead: {contact['email']} "
                        f"(score={score_result['score']}, min={self.agent.settings.min_lead_score_to_email})"
                    )
                    self.agent._stats["low_score_skipped"] += 1
                    if contact_id:
                        try:
                            self.agent.db.update_contact_status(
                                contact_id,
                                "low_score",
                                discard_reason=f"Score {score_result['score']} below threshold {self.agent.settings.min_lead_score_to_email}",
                            )
                        except sqlite3.Error as e:
                            logger.error(
                                "Could not record the low-score verdict for %s: %s",
                                contact["email"],
                                e,
                            )
                    continue

            # Relevance filter (Phase 1): drop off-target / non-European leads
            # before spending a network verification on them.
            if self.agent.relevance_filter:
                assessment = self.agent.relevance_filter.assess(contact)
                if not assessment["relevant"]:
                    logger.info(
                        f"Filtering off-target lead: {contact['email']} "
                        f"({assessment['reason']})"
                    )
                    self.agent._stats["irrelevant_skipped"] += 1
                    if contact_id:
                        # Contained so a locked DB downgrades to "discarded but
                        # not recorded" instead of aborting the whole
                        # qualification pass on one bad write.
                        try:
                            self.agent.db.update_contact_status(
                                contact_id,
                                "irrelevant",
                                discard_reason=assessment["reason"],
                            )
                        except sqlite3.Error as e:
                            logger.error(
                                "Could not record the irrelevance verdict for %s: %s",
                                contact["email"],
                                e,
                            )
                    continue

            # AI relevance classification (Phase 2): nuanced target/no-target
            # judgement on the survivors of the cheap heuristic gate. Fails open
            # (only rejects on an explicit False) so AI hiccups never drop leads.
            if (
                self.agent.settings.ai_relevance_enabled
                and self.agent.ai_gen
                and getattr(self.agent.ai_gen, "is_configured", False)
            ):
                # Same shape as the scoring and verification blocks: the verdict
                # is obtained inside the try and acted on outside it, so a failed
                # bookkeeping write cannot skip the `continue` and let a contact
                # the classifier rejected fall through to the send list.
                verdict = None
                try:
                    verdict = self.agent.ai_gen.classify_relevance(contact)
                except sqlite3.Error as e:
                    # classify_relevance maps its own API/parse failures to None
                    # (which is not acted on), so a DB write is all that is left
                    # to fail here.
                    logger.warning(
                        f"Could not record the AI relevance verdict for "
                        f"{contact['email']}: {e}"
                    )

                if verdict is False:
                    logger.info(f"AI classified off-target: {contact['email']}")
                    self.agent._stats["irrelevant_skipped"] += 1
                    if contact_id:
                        try:
                            self.agent.db.update_contact_status(
                                contact_id,
                                "ai_irrelevant",
                                discard_reason="AI relevance classifier: not a target",
                            )
                        except sqlite3.Error as e:
                            logger.error(
                                "Could not record the AI-irrelevant verdict for %s: %s",
                                contact["email"],
                                e,
                            )
                    continue

            # Email verification (skip if already verified on a previous pass -
            # avoids re-doing DNS/MX lookups for the whole backlog every loop).
            if (
                self.agent.email_verifier
                and self.agent.settings.verify_mx
                and not contact.get("is_verified")
            ):
                # The verdict is computed inside the try, but ACTED ON outside it.
                # Previously the discard `continue` sat after the bookkeeping
                # write: if update_contact_verification raised (locked DB), the
                # verdict was never evaluated and a contact with no MX record
                # fell through and got emailed - a guaranteed hard bounce, logged
                # only as "Verification failed", which reads as "we skipped it".
                verify_result = None
                try:
                    verify_result = self.agent.email_verifier.verify_email(
                        contact["email"],
                        skip_smtp=not self.agent.settings.verify_smtp_probe,
                    )
                    self.agent._stats["verified"] += 1
                except (OSError, ValueError) as e:
                    # The verifier turns DNS/SMTP trouble into a verdict itself;
                    # what can still escape is a socket-level failure
                    # (smtplib.SMTPException is an OSError) or an unusable
                    # address (EmailNotValidError is a ValueError).
                    #
                    # This `continue` is the whole point of the branch. Without
                    # it, verify_result stayed None, every guard below is
                    # written `if verify_result is not None and ...`, so all
                    # three were skipped and the contact fell through to
                    # qualified.append() at the bottom of the loop - emailed
                    # this pass, unverified, on the strength of a verification
                    # that had just crashed. Leave it pending and try again:
                    # no verdict is not a pass.
                    logger.warning(f"Verification failed for {contact['email']}: {e}")
                    continue

                # Indeterminate is NOT a rejection. A DNS timeout or a greylist
                # deferral tells us nothing about the address, and "unverified"
                # is terminal: get_pending_contacts only ever selects
                # status='pending', and nothing anywhere moves a contact back.
                # So one resolver blip used to retire a perfectly deliverable
                # prospect permanently. Leave it pending and try again later.
                if verify_result is not None and verify_result.get("indeterminate"):
                    logger.info(
                        "Verification inconclusive for %s (%s) - leaving queued "
                        "for another attempt.",
                        contact["email"],
                        verify_result.get("reason", "unknown"),
                    )
                    continue

                if verify_result is not None and not verify_result.get("valid"):
                    logger.info(
                        f"Discarding unverified email: {contact['email']} "
                        f"({verify_result.get('reason', 'unknown')})"
                    )
                    # Bookkeeping must not be able to resurrect a rejected
                    # address, so its failure is contained separately.
                    if contact_id:
                        try:
                            self.agent.db.update_contact_verification(
                                contact_id,
                                is_verified=False,
                                details=verify_result.get("reason", ""),
                            )
                            self.agent.db.update_contact_status(
                                contact_id,
                                "unverified",
                                discard_reason=verify_result.get("reason", ""),
                            )
                        except sqlite3.Error as e:
                            logger.error(
                                "Could not record the unverified verdict for %s: %s",
                                contact["email"],
                                e,
                            )
                    continue

                if verify_result is not None and contact_id:
                    try:
                        self.agent.db.update_contact_verification(
                            contact_id,
                            is_verified=True,
                            details=verify_result.get("reason", ""),
                        )
                    except sqlite3.Error as e:
                        logger.warning(
                            "Could not record verification for %s: %s",
                            contact["email"],
                            e,
                        )

            # The basis is on the contact since it was stored, and is
            # recorded again only when a live send relies on it.
            if contact_id:
                # Its company's country, before its first email is composed:
                # an Italian company on a .com address is written to in
                # Italian. Once per contact, and only for one that will be
                # emailed; the AI part never raises.
                try:
                    settle_company_country(
                        self.agent.settings, self.agent.db, contact, self.agent.ai_gen
                    )
                except sqlite3.Error as e:
                    logger.warning(
                        "Could not record the company country for %s: %s",
                        contact["email"],
                        e,
                    )
                # So a contact still waiting for a send slot takes the fast
                # path next loop instead of being re-scored, re-classified by
                # the AI (billed again) and re-logged.
                try:
                    self.agent.db.mark_contact_qualified(contact_id)
                except sqlite3.Error as e:
                    logger.warning(
                        "Could not record qualification for %s: %s",
                        contact["email"],
                        e,
                    )

            qualified.append(contact)

        logger.info(
            f"Qualified {len(qualified)}/{len(contacts)} contacts "
            f"(scored: {self.agent._stats['scored']}, verified: {self.agent._stats['verified']}, "
            f"low_score_skipped: {self.agent._stats['low_score_skipped']})"
        )
        return qualified

    _qualify_contacts = qualify_contacts

    def load_followup_template(self, template_name: str) -> str:
        """Load a follow-up template by name, falling back to the main
        template if the file is missing."""
        if template_name:
            tpl = self.agent.file_handler.get_email_template(f"{template_name}.html")
            if tpl:
                return tpl
        self.agent.refresh_templates_if_changed()
        return self.agent._email_template

    _load_followup_template = load_followup_template

    def _italian_followup(self, template_name: str | None) -> str | None:
        """The Italian version of a follow-up step (``<name>_it.html``), or
        None. A missing one, or one of only white space, is said once in the
        log, and the English email goes whole: never English text under an
        Italian subject and footer, and never a blank text the footer guard
        refuses."""
        if not template_name:
            return None
        file_name = f"{template_name}_it.html"
        if (self.agent.file_handler.templates / file_name).exists():
            tpl = self.agent.file_handler.get_email_template(file_name)
            if tpl and tpl.strip():
                return tpl
        key = f"followup_it:{file_name}"
        if not self.agent._guard_logged.get(key):
            logger.warning(
                "templates/%s not found - Italian recipients get the English "
                "follow-up %s instead.",
                file_name,
                template_name,
            )
            self.agent._guard_logged[key] = True
        return None

    def process_sequences(self, dry_run: bool = False) -> None:
        """Send any due follow-up steps, then advance or cancel enrollments."""
        if not self.agent.sequences:
            return
        self.agent._refresh_domain_backoff()
        try:
            due = self.agent.sequences.get_due_followups()
        except (sqlite3.Error, ValueError, TypeError, OverflowError) as e:
            # Reads the enrollment tables (sqlite3.Error) and parses the stored
            # timestamps back into datetimes, so a legacy or malformed
            # enrolled_at / delay_days row surfaces as ValueError / TypeError,
            # or OverflowError when the delay runs off the calendar.
            logger.error(f"Failed to fetch due follow-ups: {e}")
            return
        if not due:
            return

        logger.info(f"Processing {len(due)} due follow-up(s)")
        try:
            for item in due:
                verdict = self.agent._send_verdict(
                    item.get("contact") or {"id": item["contact_id"]},
                    purpose="followup",
                    dry_run=dry_run,
                )
                if verdict.scope == "batch":
                    if verdict.kind == "window":
                        logger.info("Outside the send window - deferring follow-ups.")
                    elif verdict.kind == "warmup":
                        logger.info(
                            "All accounts at their daily cap - deferring "
                            "remaining follow-ups"
                        )
                    break
                # A contact that may never get this email (gone, closed,
                # screened, in a country the lock refuses) leaves the
                # sequence - otherwise the same refusal repeats on every
                # pass, forever.
                if verdict.scope == "drop":
                    self.agent.sequences.cancel_enrollment(
                        item["enrollment_id"],
                        "contact not found"
                        if verdict.kind == "missing"
                        else f"stopped: {verdict.reason}",
                    )
                    continue
                if verdict.scope != "cleared":
                    continue  # deferred: re-evaluated next cycle
                contact = verdict.contact
                step = item.get("current_step")
                step_id = item.get("step_id")

                # The due list is as old as the pass, and every send before
                # this one kept its delay: another process may have moved this
                # enrollment on, or ended it, since it was read.
                if step is not None and not self.agent.sequences.at_step(
                    item["enrollment_id"], step
                ):
                    logger.info(
                        "Skipping enrollment %s - it moved on since this pass read it",
                        item["enrollment_id"],
                    )
                    continue
                # The row says this step may have gone out, and the enrollment
                # was never moved on: a process stopped, or the database locked,
                # between the two writes. Move it on now rather than repeat it.
                if step_id is not None and self.agent.db.step_possibly_sent(
                    contact["id"], step_id
                ):
                    if not dry_run:
                        self._move_on(item, step, contact)
                    continue

                name = item.get("template_name")
                template = self.agent._load_followup_template(name)
                subject = item.get("subject") or ""
                # The language of the first email, as recorded with it, so a
                # contact reads one language throughout. With no Italian first
                # email on disk, that went in English, and so do these.
                language = language_for(self.agent.settings, self.agent.db, contact)[0]
                if language == "it" and not self.agent._email_template_it:
                    language = "en"
                if language == "it":
                    italian = self._italian_followup(name)
                    if italian:
                        template = italian
                        subject = FOLLOW_UP_SUBJECTS_IT.get(name, subject)
                    else:
                        language = "en"
                subject = subject.replace(
                    "{company_name}", self.agent.settings.company_name
                )
                if not subject:
                    subject = self.agent.settings.get_email_subject()

                stamp = self.agent._last_dispatch
                sent = self.agent._process_contact(
                    contact,
                    dry_run,
                    template_override=template,
                    subject_override=subject,
                    is_followup=True,
                    attach_pdf=False,  # first email carries the profile; follow-ups stay light
                    sender=verdict.sender,
                    account_key=verdict.account_key,
                    language=language,
                    sequence_step_id=step_id,
                )
                if sent and not dry_run:
                    self._move_on(item, step, contact)
                elif not dry_run:
                    # The send can close the contact as it goes - a server
                    # rejecting the address marks it bounced. When it has,
                    # end the enrolment - otherwise the same refusal repeats
                    # on every pass, forever.
                    fresh = self.agent.db.get_contact_by_id(contact["id"])
                    closed = self.agent._contact_is_closed(fresh) if fresh else None
                    if closed:
                        self.agent.sequences.cancel_enrollment(
                            item["enrollment_id"], f"stopped at send time: {closed}"
                        )
                # Only an attempt the server saw keeps the send delay.
                if sent or self.agent._last_dispatch != stamp:
                    self.agent._interruptible_sleep(self.agent._send_delay())
        finally:
            for _k, _n, es in self.agent.senders:
                es.close()

    _process_sequences = process_sequences

    def _move_on(self, item: dict, step: int | None, contact: dict) -> None:
        """Move an enrollment on from the step that went out."""
        try:
            self.agent.sequences.advance_enrollment(item["enrollment_id"], step)
        except sqlite3.Error as e:
            # The mail is already gone. Every other post-send write is
            # contained this way (see _process_contact); this one lives out
            # here and was missed, so a locked database propagated out of the
            # loop with the step un-advanced.
            logger.error(
                "Follow-up to %s was sent but the sequence step could not be "
                "advanced (%s). Its row says it went, so the next pass moves "
                "enrollment %s on instead of sending it again.",
                contact.get("email", "?"),
                e,
                item["enrollment_id"],
            )

    def process_retries(self) -> None:
        """Process any due retries from the retry queue."""
        if not self.agent.retry_queue:
            return
        # Before claiming anything: while sending is refused there is no work
        # to take, and the dashboard should not announce "N retries due" on
        # every loop. (The per-retry check below is what keeps an attempt
        # from being spent when the login dies mid-batch.)
        if self.agent._send_blocker(False) or self.agent._first_email_blocker(False):
            return

        self.agent._refresh_domain_backoff()
        # Recover rows a previous crash left claimed. get_due_retries() now marks
        # rows in_flight before returning them, so an interrupted run would strand
        # them; without this they would never be retried again.
        self.agent.retry_queue.reset_stale_in_flight()

        due_retries = self.agent.retry_queue.get_due_retries()
        if not due_retries:
            return

        logger.info(f"Processing {len(due_retries)} due retries...")
        self.agent.db.log_activity(
            "RETRY_PROCESSING", f"{len(due_retries)} retries due"
        )

        # Every row get_due_retries() claimed is in_flight. Rows this pass does
        # not resolve go straight back to pending at the end - otherwise they
        # sat claimed until the 30-minute stale reset.
        resolved: set[int] = set()
        try:
            for retry in due_retries:
                # The per-retry check matters as much as the one before the
                # claim: the login can die on the first retry of the batch, and
                # every retry after it was then refused at the chokepoint and
                # recorded as a failed attempt.
                verdict = self.agent._send_verdict(
                    {"id": retry["contact_id"]}, purpose="retry", dry_run=False
                )
                if verdict.scope == "batch":
                    if verdict.kind == "window":
                        logger.info("Outside the send window - deferring retries.")
                    elif verdict.kind == "warmup":
                        logger.info(
                            "All accounts at their daily cap - deferring retries."
                        )
                    break
                if verdict.kind == "missing":
                    self.agent.retry_queue.mark_retry_failed(
                        retry["id"], "Contact not found"
                    )
                    resolved.add(retry["id"])
                    continue
                if verdict.kind == "already_emailed":
                    self.agent.retry_queue.cancel_retry(retry["id"], "already emailed")
                    resolved.add(retry["id"])
                    continue
                # Closed or screened: the retry would re-send the first email to
                # someone who must not get it. Cancelled, not spent as a failed
                # attempt.
                if verdict.scope == "drop":
                    self.agent.retry_queue.cancel_retry(
                        retry["id"], f"contact {verdict.reason}"
                    )
                    resolved.add(retry["id"])
                    continue
                if verdict.kind == "in_doubt":
                    # Its last send may have reached them and has not settled:
                    # left queued and unspent. Settling that send closes it.
                    continue
                # Deferred (domain cap, backoff, or no titled mailbox with
                # allowance left): left queued rather than failed, so the
                # attempt is postponed, not spent.
                if verdict.kind == "title":
                    logger.info(
                        "Leaving retry for %s queued - no mailbox with a "
                        "SENDER_TITLE can send it today",
                        verdict.contact["email"],
                    )
                    continue
                if verdict.kind == "domain_cap":
                    logger.info(
                        f"Domain limit reached for {verdict.reason} - leaving retry queued"
                    )
                    continue
                if verdict.kind == "backoff":
                    logger.info(
                        "Leaving retry for %s queued - its domain is backed off",
                        verdict.contact["email"],
                    )
                    continue
                if verdict.kind == "review":
                    # Its stage is set to manual: a person sends it, not the queue.
                    continue
                contact = verdict.contact

                logger.info(
                    f"Retrying: {contact['email']} (attempt {retry['attempt'] + 1})"
                )

                # Re-process the contact and check return value
                stamp = self.agent._last_dispatch
                send_success = self.agent._process_contact(
                    contact,
                    dry_run=False,
                    sender=verdict.sender,
                    account_key=verdict.account_key,
                )

                if send_success is CLAIM_LOST:
                    # Another send got there first. As the hold above: left
                    # queued and unspent, and its settling closes it.
                    continue
                if send_success:
                    self.agent.retry_queue.mark_retry_success(retry["id"])
                elif (
                    after := self.agent._send_verdict(
                        {"id": retry["contact_id"]}, purpose="retry", dry_run=False
                    )
                ).kind == "closed":
                    # The send applied a held opt-out and refused: cancelled,
                    # like a retry found closed above. Only "closed": a screen
                    # is the check above's to catch, before the send.
                    self.agent.retry_queue.cancel_retry(
                        retry["id"], f"contact {after.reason}"
                    )
                elif self.agent._opt_out_held(contact["email"]):
                    # Refused while an opt-out waits on disk: left queued,
                    # unspent, until it is applied and the retry cancelled.
                    continue
                else:
                    self.agent.retry_queue.mark_retry_failed(
                        retry["id"], "Retry send failed"
                    )
                resolved.add(retry["id"])

                # Only an attempt the server saw keeps the send delay.
                if send_success or self.agent._last_dispatch != stamp:
                    self.agent._interruptible_sleep(self.agent._send_delay())
        finally:
            unresolved = [r["id"] for r in due_retries if r["id"] not in resolved]
            if unresolved:
                try:
                    self.agent.retry_queue.release(unresolved)
                except sqlite3.Error as e:
                    # The stale reset still recovers them, just later.
                    logger.error("Could not release %d retries: %s", len(unresolved), e)
            for _k, _n, es in self.agent.senders:
                es.close()

    _process_retries = process_retries
