"""Erasure and export must act on one address, never on "nothing".

With a blank input the whole-address pattern matched the empty string at
every position and LIKE '%%' selected every row, so one empty `gdpr-erase`
spliced the erasure token into every activity, SMTP and retry record, and
put "" on the suppression list. The access export had the same hole the
other way round: a blank request returned every activity-log row, about
everyone. The retention job reaches the same code with a contact whose
address is blank, so the damage did not even need an operator's typo.
"""

import pytest

from main import build_parser
from modules.database import Database
from modules.export import DataExporter, anonymise_contact_rows, records_held_about

OTHER = "someone.else@brand.de"


@pytest.fixture
def populated(tmp_path):
    """Records about another person, in every free-text column erasure edits."""
    db = Database(str(tmp_path / "g.db"))
    sid = db.log_search("q", source="test")
    cid = db.add_contact(sid, OTHER, name="Anna Rossi")
    db.log_email(
        cid,
        OTHER,
        "Subject",
        delivery_status="failed",
        smtp_response=f"550 5.1.1 <{OTHER}> rejected",
        error_message=f"recipient {OTHER} refused",
    )
    # As bounce lines are written: the address in angle brackets.
    db.log_activity("BOUNCE", f"Bounce for <{OTHER}>: mailbox full")
    return db


def _free_text(db):
    return [
        tuple(r)
        for r in db.conn.execute(
            "SELECT details FROM activity_log UNION ALL "
            "SELECT smtp_response FROM email_log UNION ALL "
            "SELECT error_message FROM email_log"
        ).fetchall()
    ]


@pytest.mark.parametrize("bad", ["", "   ", "not-an-address", "@"])
def test_erase_refuses_a_non_address_and_touches_nothing(populated, bad):
    before = _free_text(populated)
    with pytest.raises(ValueError):
        DataExporter(populated).gdpr_erase(bad)
    assert _free_text(populated) == before
    suppressed = populated.conn.execute("SELECT email FROM suppression_list").fetchall()
    assert suppressed == []


@pytest.mark.parametrize("bad", ["", "   ", "not-an-address"])
def test_shared_anonymiser_is_a_no_op_for_a_non_address(populated, bad):
    """The retention job calls this with whatever a contact row holds."""
    before = _free_text(populated)
    result = anonymise_contact_rows(populated, bad)
    populated.conn.commit()
    assert result == {"contact_ids": [], "actions": []}
    assert _free_text(populated) == before


@pytest.mark.parametrize("bad", ["", "   ", "not-an-address"])
def test_access_export_discloses_nothing_for_a_non_address(populated, bad):
    assert records_held_about(populated, bad) == {}
    with pytest.raises(ValueError):
        DataExporter(populated).gdpr_export(bad)


def test_access_export_ignores_surrounding_spaces(populated):
    held = records_held_about(populated, f"  {OTHER.upper()} ")
    assert [r["email"] for r in held["contacts"]] == [OTHER]


@pytest.mark.parametrize("command", ["gdpr-erase", "gdpr-export"])
def test_cli_rejects_a_non_address_before_opening_the_database(command, capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([command, "  "])
    assert exc.value.code == 2
    assert "not an email address" in capsys.readouterr().err


def test_cli_normalises_the_address():
    args = build_parser().parse_args(["gdpr-erase", f"  {OTHER.upper()} "])
    assert args.email == OTHER


def test_add_contact_refuses_a_blank_address(db):
    sid = db.log_search("q", source="test")
    assert db.add_contact(sid, "  ") is None
    assert db.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0

