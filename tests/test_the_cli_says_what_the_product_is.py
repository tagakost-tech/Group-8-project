"""The CLI says what the product is: Omega IFS's outreach agent, with people
deciding what is sent.

The help is among the first things IT reads. The template this agent came
from once called itself "pharmaceutical"; neither that nor the word
"autonomous" describes an agent whose emails a person approves.
"""

import argparse
import re

import pytest

import main

PHARMA = re.compile(r"pharma", re.IGNORECASE)


def _flat(text: str) -> str:
    """argparse wraps to the terminal's width, which can split a phrase."""
    return " ".join(text.split())


def _help_pages(parser: argparse.ArgumentParser) -> list[str]:
    """Every page --help prints: the top level, each command and each action."""
    pages = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                pages += _help_pages(sub)
    return pages


def test_help_names_omega_and_human_review_never_pharmaceuticals(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["main.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 0

    out = _flat(capsys.readouterr().out)

    assert not PHARMA.search(out), out
    assert "Omega IFS" in out, out
    assert "cold-formed" in out, out
    assert "human-in-the-loop" in out.lower(), out
    assert "autonomous" not in out.lower(), out


def test_no_command_or_option_help_mentions_pharma():
    said = [page for page in _help_pages(main.build_parser()) if PHARMA.search(page)]

    assert said == [], said


def test_the_module_docstring_names_omega_not_pharmaceuticals():
    doc = _flat(main.__doc__)

    assert not PHARMA.search(doc), doc
    assert "Omega IFS" in doc, doc
    assert "human-in-the-loop" in doc, doc
