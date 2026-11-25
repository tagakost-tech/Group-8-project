"""Leads from a CSV file: a trade-fair list, a directory export, a referral.

Columns are matched by name in English or Italian, and a semicolon file from
Italian Excel reads the same as a comma one. Every row goes through the same
door as a lead the agent found (Database.add_contact), so the do-not-contact
list, erased addresses and blocked domains stop it there too, and the client
and competitor lists screen it when the agent next runs. A dry run reports
what would happen without writing anything."""

from __future__ import annotations

import csv
import io

from modules.compliance import SOURCE_CHANNELS
from modules.database import domain_token, email_domain, erasure_token, is_address

MAX_ROWS = 5000
MAX_BYTES = 2 * 1024 * 1024
# The channels a person can vouch for when uploading a list; the two that
# mean "the agent read it on the company's site" are the agent's to claim.
IMPORT_CHANNELS = tuple(
    c for c in SOURCE_CHANNELS if c not in ("own_website", "contact_page")
)

_ALIASES = {
    "email": ("email", "e-mail", "email address", "mail", "indirizzo email", "posta elettronica"),
    "organization": ("company", "organization", "organisation", "company name", "azienda", "ragione sociale", "societa", "società"),
    "name": ("name", "contact", "contact name", "full name", "nome", "referente"),
    "role": ("role", "title", "job title", "position", "ruolo"),
    "website": ("website", "url", "web", "domain", "sito", "sito web"),
    "country": ("country", "country code", "paese", "nazione"),
    "sector": ("sector", "industry", "settore"),
}


def _columns(header: list[str]) -> dict[str, int]:
    found = {}
    for i, raw in enumerate(header):
        key = (raw or "").strip().lower().lstrip("﻿")
        for field, names in _ALIASES.items():
            if key in names and field not in found:
                found[field] = i
    return found


def _rows(text: str) -> tuple[list[str], list[list[str]]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any((c or "").strip() for c in r)]
    return (rows[0], rows[1:]) if rows else ([], [])


def _known(db, email: str) -> str | None:
    """Why add_contact would refuse this address, or None."""
    q = db.conn.execute
    if q("SELECT 1 FROM contacts WHERE email = ?", (email,)).fetchone():
        return "already in the leads"
    if q(
        "SELECT 1 FROM suppression_list WHERE email IN (?, ?)",
        (email, erasure_token(email)),
    ).fetchone():
        return "on the do-not-contact list"
    if q("SELECT 1 FROM contacts WHERE email = ?", (erasure_token(email),)).fetchone():
        return "erased earlier"
    domain = email_domain(email)
    if q(
        "SELECT 1 FROM suppressed_domains WHERE domain IN (?, ?)",
        (domain, domain_token(domain)),
    ).fetchone():
        return "company asked not to be contacted"
    return None


def run(db, text: str, *, channel: str, filename: str = "", dry_run: bool = True) -> dict:
    """Check, and unless dry_run import, a CSV of leads.

    ValueError for a file that cannot be imported at all."""
    if channel not in IMPORT_CHANNELS:
        raise ValueError("Say where the list came from.")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("The file is over 2 MB.")
    header, rows = _rows(text)
    cols = _columns(header)
    if "email" not in cols:
        raise ValueError("No email column: name one 'email'.")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"The file has {len(rows):,} rows; the most is {MAX_ROWS:,}.")

    def cell(row: list[str], field: str, limit: int = 200) -> str | None:
        i = cols.get(field)
        if i is None or i >= len(row):
            return None
        return (row[i] or "").strip()[:limit] or None

    skipped = {
        "not an email address": 0,
        "twice in the file": 0,
        "already in the leads": 0,
        "on the do-not-contact list": 0,
        "erased earlier": 0,
        "company asked not to be contacted": 0,
    }
    problems: list[str] = []
    seen: set[str] = set()
    ready: list[dict] = []
    for n, row in enumerate(rows, start=2):
        email = (cell(row, "email") or "").lower()
        if not is_address(email):
            skipped["not an email address"] += 1
            if len(problems) < 5:
                problems.append(f"Row {n}: '{email[:60]}' is not one email address.")
            continue
        if email in seen:
            skipped["twice in the file"] += 1
            continue
        seen.add(email)
        why = _known(db, email)
        if why:
            skipped[why] += 1
            continue
        country = (cell(row, "country", 2) or "").upper()
        sector = cell(row, "sector")
        ready.append(
            {
                "email": email,
                "organization": cell(row, "organization"),
                "name": cell(row, "name"),
                "role": cell(row, "role"),
                "source_url": cell(row, "website", 300),
                "company_country": country if len(country) == 2 and country.isalpha() else None,
                "qualification_rationale": f"Imported list{f' ({filename[:80]})' if filename else ''}"
                + (f"; sector: {sector}" if sector else ""),
            }
        )

    result = {
        "rows": len(rows),
        "would_add": len(ready),
        "skipped": skipped,
        "problems": problems,
        "columns": sorted(cols),
    }
    if dry_run or not ready:
        result["added"] = 0
        return result

    search_id = db.log_search(
        f"CSV import: {filename[:80] or 'unnamed file'}",
        source="csv_import",
        status="completed",
        notes=f"{len(ready)} rows, source: {SOURCE_CHANNELS[channel][0]}",
    )
    added = 0
    for lead in ready:
        country = lead.pop("company_country")
        cid = db.add_contact(
            search_id,
            source_channel=channel,
            company_country=country,
            company_country_source="list" if country else None,
            **lead,
        )
        added += cid is not None
    db.update_search(search_id, results_count=added, status="completed")
    result["added"] = added
    return result
