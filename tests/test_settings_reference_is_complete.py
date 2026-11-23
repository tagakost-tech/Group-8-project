"""docs/SETTINGS.md must list every setting config.py reads, as it reads it.

README and DEPLOY send the operator there for the full list. The settings
template once fell two months behind config.py: eighteen settings, among
them ERASURE_KEY, the country lock and the legal identity, were in the code
and in no list an operator would read, so a new install met each of them
first as a refusal. A setting added to Settings without a row fails here,
and so does a row that names a setting the code does not have, or whose
type, default or Dashboard mark no longer matches the code.
"""

import re
import typing
from pathlib import Path

from config import RUNTIME_EDITABLE, Settings

REFERENCE = Path(__file__).resolve().parent.parent / "docs" / "SETTINGS.md"

COLUMNS = ("setting", "type", "default", "required", "dashboard", "what")
TYPES = {bool: "true/false", int: "whole number", float: "number", str: "text"}


def _env_name(field_name: str) -> str:
    return (Settings.model_fields[field_name].alias or field_name).upper()


def _type(field_name: str) -> str:
    annotation = Settings.model_fields[field_name].annotation
    kinds = [a for a in typing.get_args(annotation) if a is not type(None)]
    return TYPES[kinds[0] if kinds else annotation]


def _default(field_name: str) -> str:
    value = Settings.model_fields[field_name].default
    if value is None or value == "":
        return "_blank_"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    return f"`{value}`"


def _rows() -> list[dict]:
    """The table rows whose first cell names a setting, in file order."""
    rows = []
    for line in REFERENCE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # A cell's own pipe is written \| (EMAIL_SUBJECT's default has one).
        cells = [
            c.strip().replace("\\|", "|")
            for c in re.split(r"(?<!\\)\|", line.strip("|"))
        ]
        if len(cells) == len(COLUMNS) and cells[0].startswith("`"):
            rows.append(dict(zip(COLUMNS, cells)))
    return rows


def _by_setting() -> dict[str, dict]:
    return {row["setting"].strip("`"): row for row in _rows()}


def test_every_setting_has_a_row():
    documented = _by_setting()
    missing = [
        _env_name(f) for f in Settings.model_fields if _env_name(f) not in documented
    ]
    assert not missing, (
        "these settings are read by config.py but have no row in "
        f"docs/SETTINGS.md: {', '.join(missing)}"
    )


def test_every_row_is_a_setting_and_appears_once():
    names = [row["setting"].strip("`") for row in _rows()]
    known = {_env_name(f) for f in Settings.model_fields}
    unknown = sorted(set(names) - known)
    repeated = sorted({n for n in names if names.count(n) > 1})
    assert not unknown, f"docs/SETTINGS.md lists settings config.py lacks: {unknown}"
    assert not repeated, f"docs/SETTINGS.md lists these more than once: {repeated}"


def test_each_row_has_the_type_and_default_of_the_code():
    documented = _by_setting()
    wrong = []
    for field_name in Settings.model_fields:
        row = documented.get(_env_name(field_name))
        if row is None:
            continue
        expected = (_type(field_name), _default(field_name))
        if (row["type"], row["default"]) != expected:
            wrong.append(
                f"{_env_name(field_name)}: the reference says "
                f"{row['type']} {row['default']}, the code {expected[0]} {expected[1]}"
            )
    assert not wrong, "docs/SETTINGS.md is out of date:\n  " + "\n  ".join(wrong)


def test_the_dashboard_mark_matches_what_the_panel_may_change():
    documented = _by_setting()
    wrong = []
    for field_name in Settings.model_fields:
        row = documented.get(_env_name(field_name))
        if row is None:
            continue
        if (row["dashboard"] == "yes") != (field_name in RUNTIME_EDITABLE):
            wrong.append(_env_name(field_name))
    assert not wrong, (
        "the Dashboard column disagrees with config.RUNTIME_EDITABLE for: "
        + ", ".join(wrong)
    )


def test_the_legal_fields_that_block_live_sending_are_marked_so():
    documented = _by_setting()
    unmarked = [
        _env_name(f)
        for f in Settings.REQUIRED_LEGAL_FIELDS
        if documented.get(_env_name(f), {}).get("required") != "Live"
    ]
    assert not unmarked, (
        "missing_legal_identity() refuses live sending without these, but "
        f"docs/SETTINGS.md does not mark them Live: {unmarked}"
    )
