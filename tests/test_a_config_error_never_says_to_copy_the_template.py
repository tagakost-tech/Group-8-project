"""A configuration error never tells the operator to copy the template over .env.

After any invalid setting the CLI printed "Copy from .env.example: copy
.env.example .env", whether or not .env existed. In Windows PowerShell `copy`
is Copy-Item, which overwrites without asking, so an operator who mistyped a
country name and followed the advice wiped the filled .env: the legal
identity, the SMTP credentials, UNSUBSCRIBE_SECRET and ERASURE_KEY. The
template has no erasure key, and a database that has recorded the old key's
fingerprint refuses a new one. Now, with a .env present, the CLI names the
invalid setting and says to correct it there and not to copy the template;
only with no .env at all does it say how to create one from the template.
"""

import pytest

import main


def _run(settings_factory, monkeypatch, capsys, root, **bad):
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(main, "load_settings", lambda: settings_factory(**bad))
    monkeypatch.setattr("sys.argv", ["main.py", "status"])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1
    return capsys.readouterr().out


def test_with_a_filled_env_it_names_the_setting_and_never_says_copy(
    settings_factory, monkeypatch, capsys, tmp_path
):
    (tmp_path / ".env").write_text("SENDER_NAME=Filled\n", encoding="utf-8")

    out = _run(settings_factory, monkeypatch, capsys, tmp_path, ENABLED_COUNTRIES="France")

    assert "copy .env.example .env" not in out, out
    assert f"Correct ENABLED_COUNTRIES in {tmp_path / '.env'}" in out, out
    assert "Do not copy .env.example over it" in out, out


def test_a_field_named_in_lower_case_is_given_as_its_env_name(
    settings_factory, monkeypatch, capsys, tmp_path
):
    (tmp_path / ".env").write_text("SENDER_NAME=Filled\n", encoding="utf-8")

    out = _run(
        settings_factory,
        monkeypatch,
        capsys,
        tmp_path,
        send_window_start_hour=20,
        send_window_end_hour=8,
    )

    assert "Correct SEND_WINDOW_END_HOUR in" in out, out


def test_without_an_env_it_says_how_to_create_one(
    settings_factory, monkeypatch, capsys, tmp_path
):
    out = _run(settings_factory, monkeypatch, capsys, tmp_path, ENABLED_COUNTRIES="France")

    assert "copy .env.example .env" in out, out
