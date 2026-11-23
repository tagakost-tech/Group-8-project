"""Dotted initials written without the final dot must still collapse.

SUPPLECO's competitor list writes "S.I.I.T" (no trailing dot). When
_DOTTED_ACRONYM_RE required every letter to carry a trailing dot
(r"(?<!\\w)((?:[A-Z]\\.){2,})(?!\\w)"), normalise("S.I.I.T") turned the
dots into spaces ("S I I T"), _distinctive dropped every single-character
token, and the company's identity became empty - so "S.I.I.T" could never
match "SIIT S.r.l." or "S.I.I.T. S.r.l.".
"""

import pytest

from modules.clients import ClientRegister, normalise


def test_siit_without_final_dot_normalises_like_with_final_dot_and_plain():
    assert normalise("S.I.I.T") == "SIIT"
    assert normalise("S.I.I.T.") == "SIIT"
    assert normalise("SIIT") == "SIIT"
    assert normalise("S.I.I.T") == normalise("S.I.I.T.") == normalise("SIIT") == "SIIT"


def test_register_with_dotless_end_acronym_matches_plain_and_dotted_srl():
    reg = ClientRegister(["S.I.I.T"])
    assert reg.match("SIIT S.r.l.") == "S.I.I.T"
    assert reg.match("S.I.I.T. S.r.l.") == "S.I.I.T"
    assert reg.match("S.I.I.T S.r.l.") == "S.I.I.T"


@pytest.mark.parametrize(
    "without_final_dot,with_final_dot,expected",
    [
        ("A.C.R.A.F", "A.C.R.A.F.", "ACRAF"),
        ("A.C.R.A.F S.P.A", "A.C.R.A.F. S.P.A.", "ACRAF"),
        ("S.A.L.F", "S.A.L.F.", "SALF"),
        ("I.B.S.A", "I.B.S.A.", "IBSA"),
        ("U.S.A", "U.S.A.", "USA"),
        ("B.V", "B.V.", ""),
    ],
)
def test_acronyms_without_final_dot_collapse_like_dotted_counterparts(
    without_final_dot, with_final_dot, expected
):
    assert normalise(without_final_dot) == normalise(with_final_dot) == expected


def test_spa_and_srl_without_final_dot_behave_like_dotted_legal_forms():
    """S.P.A and S.R.L collapse to SPA and SRL, which are in _LEGAL_FORMS and
    therefore dropped, just like S.P.A. and S.R.L."""
    assert normalise("S.P.A") == normalise("S.P.A.") == ""
    assert normalise("S.R.L") == normalise("S.R.L.") == ""
    assert normalise("ZETA FARMACEUTICI S.P.A") == "ZETA FARMACEUTICI"
    assert normalise("ZETA FARMACEUTICI S.P.A.") == "ZETA FARMACEUTICI"
    reg = ClientRegister(["S.P.A"])
    assert "S.P.A" in reg.unmatchable


def test_single_initial_and_saint_abbreviations_are_unchanged():
    """A single initial followed by a space ("A. MENARINI", "S. MARTINO") is not
    a dotted acronym and must not be glued to the word after it."""
    menarini = "A. MENARINI INDUSTRIE FARMACEUTICHE RIUNITE SRL"
    assert normalise(menarini) == "A MENARINI INDUSTRIE FARMACEUTICHE RIUNITE"
    assert ClientRegister([menarini]).match("Menarini") == menarini

    assert normalise("S. MARTINO SRL") == "S MARTINO"
    assert ClientRegister(["S. MARTINO SRL"]).match("Martino") == "S. MARTINO SRL"
    assert ClientRegister(["S. MARTINO SRL"]).match("S. Martino S.r.l.") == "S. MARTINO SRL"


def test_lowercase_and_trailing_dot_inputs_are_unchanged():
    assert normalise("s.i.i.t") == "SIIT"
    assert normalise("s.i.i.t.") == "SIIT"
    assert normalise("a.c.r.a.f") == "ACRAF"
    assert normalise("U.S.A.") == "USA"
    assert normalise("u.s.a.") == "USA"
    assert ClientRegister(["s.i.i.t"]).match("siit s.r.l.") == "s.i.i.t"


def test_words_joined_by_a_dot_are_not_treated_as_initials():
    """Domain-like strings ("food.be", "EXAMPLE.COM") have multi-letter sides
    around the dot; word-boundary anchors must keep them from collapsing."""
    assert normalise("food.be") == "FOOD BE"
    assert normalise("A.XY") == "A XY"
    assert normalise("XY.C") == "XY C"
    assert normalise("XY.ZW") == "XY ZW"
