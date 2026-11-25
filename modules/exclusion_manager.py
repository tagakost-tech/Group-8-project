"""Exclusion list management and screening.

Manages the spreadsheet exclusion lists named by CLIENT_LIST_PATH and
COMPETITOR_LIST_PATH (blank by default, which leaves the competitor screen off),
mtime stamping (_list_stamp), list caching (_list_stamps, _list_failed),
list reloading (refresh_lists_if_changed, load_client_register,
load_competitor_register), and screening (active_client, known_competitor,
contact_is_closed).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from config import PROJECT_ROOT
from modules.database import CLOSED_CONTACT_STATUSES, DISCARDED_CONTACT_STATUSES

if TYPE_CHECKING:
    from agent import OutreachAgent

logger = logging.getLogger(__name__)


class ExclusionManager:
    """Manages customer and competitor exclusion registers and screening."""

    def __init__(self, agent: OutreachAgent):
        self.agent = agent
        if not hasattr(self.agent, "_list_stamps") or self.agent._list_stamps is None:
            self.agent._list_stamps = {}
        if not hasattr(self.agent, "_list_failed") or self.agent._list_failed is None:
            self.agent._list_failed = {}

    @staticmethod
    def _list_stamp(path: Any) -> tuple[int, int] | None:
        """What identifies one saved version of a list file, or None."""
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _load_register(self, kind: str, raw_path: str) -> bool:
        """Read one spreadsheet into a ClientRegister. True when it loaded.

        Records the file's stamp either way, so an unchanged file is never
        re-read and a broken one is not retried until it changes. A failure
        keeps the previous register: screening with a slightly stale list
        beats screening with none.
        """
        label = "Client" if kind == "client" else "Competitor"
        path = PROJECT_ROOT / raw_path
        stamp = self.agent._list_stamp(path)
        try:
            from modules.clients import ClientRegister, load_from_xlsx

            if not path.is_file():
                # Loud, because a silently absent list means the agent emails
                # the very companies it exists to protect, and nothing says so.
                logger.error(
                    "%s list %s is missing - those companies are NOT being "
                    "screened out.",
                    label,
                    path,
                )
                return False
            register = ClientRegister(
                load_from_xlsx(path, key=self.agent.settings.lists_key)
            )
        except Exception as e:  # noqa: BLE001 - a screen must not stop the agent
            # openpyxl's InvalidFileException subclasses bare Exception, so a
            # narrower tuple once let an unreadable client list crash startup.
            logger.error(
                "Could not load the %s list (%s) - keeping the previous one.",
                kind,
                e,
            )
            self.agent._list_failed[kind] = stamp
            return False

        setattr(self.agent, f"{kind}_register", register)
        self.agent._list_stamps[kind] = stamp
        self.agent._list_failed.pop(kind, None)
        logger.info("%s list loaded: %d companies", label, len(register))
        if register.unmatchable:
            # A row that normalises to nothing is in the file but not in the
            # filter. Naming it is the difference between a gap someone can
            # fix and a company that quietly gets emailed.
            logger.warning(
                "%d %s row(s) cannot be matched against any company name and "
                "will NOT be screened out: %s",
                len(register.unmatchable),
                kind,
                ", ".join(repr(n) for n in register.unmatchable[:10]),
            )
        return True

    def load_client_register(self) -> bool:
        """(Re)load the active-client list from CLIENT_LIST_PATH.

        Called at startup, by refresh_lists_if_changed, and when the
        dashboard marks a client.
        """
        if not self.agent.settings.client_filter_enabled:
            return False
        raw = (self.agent.settings.client_list_path or "").strip()
        if not raw:
            # Blank is what .env.example ships. PROJECT_ROOT / "" is the
            # project folder, which "exists", so it used to reach openpyxl as
            # a directory and crash startup.
            logger.error(
                "CLIENT_FILTER_ENABLED is on but CLIENT_LIST_PATH is blank - "
                "existing customers will NOT be screened out."
            )
            return False
        return self.agent._load_register("client", raw)

    def load_competitor_register(self) -> bool:
        """(Re)load the client's named-competitor list from COMPETITOR_LIST_PATH.

        Blank path = feature off, so this costs nothing until the client supplies
        the names.
        """
        raw = (self.agent.settings.competitor_list_path or "").strip()
        if not (self.agent.settings.competitor_filter_enabled and raw):
            return False
        return self.agent._load_register("competitor", raw)

    def refresh_lists_if_changed(self) -> None:
        """Re-read the client or competitor list when the client has edited it.

        Both were read once, at startup, so a company added as a client that
        morning could still be cold-emailed all day by a campaign started
        before the edit. One stat() per list per round of work; never raises.
        """
        for kind, enabled, raw, loader in (
            (
                "client",
                self.agent.settings.client_filter_enabled,
                self.agent.settings.client_list_path,
                self.agent.load_client_register,
            ),
            (
                "competitor",
                self.agent.settings.competitor_filter_enabled,
                self.agent.settings.competitor_list_path,
                self.agent.load_competitor_register,
            ),
        ):
            raw = (raw or "").strip()
            if not (enabled and raw):
                continue
            stamp = self.agent._list_stamp(PROJECT_ROOT / raw)
            if stamp is None or stamp in (
                self.agent._list_stamps.get(kind),
                self.agent._list_failed.get(kind),
            ):
                continue
            if loader():
                register = getattr(self.agent, f"{kind}_register")
                try:
                    self.agent.db.log_activity(
                        "LIST_RELOADED",
                        f"{kind.capitalize()} list re-read after it was edited: "
                        f"{len(register)} companies",
                        status="info",
                    )
                except sqlite3.Error as e:
                    logger.warning("Could not log the %s list reload: %s", kind, e)

    def contact_is_closed(self, contact: dict) -> str | None:
        """Why no automated email may go to this contact, or None."""
        if self.agent.db.is_suppressed(contact.get("email") or ""):
            return "suppressed"
        if self.agent.db.company_blocked(contact.get("email") or ""):
            return "company blocked"
        status = contact.get("status")
        if status in CLOSED_CONTACT_STATUSES or status in DISCARDED_CONTACT_STATUSES:
            return status
        return None

    _contact_is_closed = contact_is_closed

    def known_competitor(self, organization: str | None) -> str | None:
        """The competitor this organisation is, or None. Never raises.

        Complements the keyword gate in modules/relevance.py, which infers
        rivalry from how a company describes itself. That inference missed
        "CDMO" - the industry's own term for what the client is - until a live
        discovery run found a company using it, and it can only ever catch
        firms that describe themselves at all.

        Fails OPEN, like the client register: a broken screen must not halt the
        campaign. The keyword gate still stands behind it.
        """
        if not self.agent.competitor_register or not organization:
            return None
        try:
            return self.agent.competitor_register.match(organization)
        except Exception as e:  # noqa: BLE001 - matching must never break a run
            logger.error("Competitor matching failed for %r: %s", organization, e)
            return None

    _known_competitor = known_competitor

    def active_client(self, organization: str | None) -> str | None:
        """The client this organisation is, or None. Never raises.

        A failure here must not stop a send - but it must be loud, because
        silently degrading to "no clients known" is exactly the state in which
        the agent cold-emails existing customers.
        """
        if not self.agent.client_register or not organization:
            return None
        try:
            return self.agent.client_register.match(organization)
        except Exception as e:  # noqa: BLE001 - matching must never break a run
            logger.error("Client matching failed for %r: %s", organization, e)
            return None

    _active_client = active_client
