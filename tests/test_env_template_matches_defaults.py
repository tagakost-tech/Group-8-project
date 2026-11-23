"""The shipped .env.example must not quietly change how the agent behaves.

A new operator is told - by the README quick start and docs/DEPLOY.md - to
`copy .env.example .env` and fill in credentials.
Every other line in that file then becomes their live configuration. So a
value there that differs from the vetted default in config.py is not a
harmless example: it is a silent behaviour change that only affects new
installs, and one nobody would think to look for.

The case that prompted this: .env.example blocked "info" and "sales" as
generic mailboxes. For a small European manufacturer, info@ is routinely the
only published address and is genuinely monitored - measured on real target
sites, blocking those two discarded 5 of the 11 addresses the contact-page
crawl finds. The running config used config.py's default and kept them, so
the live agent was fine and only a fresh install would have been crippled.
"""

from pathlib import Path

from config import Settings

ROOT = Path(__file__).resolve().parent.parent


def _template_values() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip().lower()] = value.strip()
    return out


# Settings whose template value is DELIBERATELY different from the default:
# credentials and identity, which exist to be filled in, and switches a new
# operator is expected to turn on for themselves.
EXPECTED_TO_DIFFER = {
    "smtp_host", "smtp_port", "smtp_username", "smtp_password",
    "imap_host", "imap_port", "imap_username", "imap_password", "imap_enabled",
    "sender_name", "company_name", "target_queries", "db_path", "pdf_path",
    "unsubscribe_secret", "dashboard_password", "dashboard_username",
    "llm_provider",
    "lead_alert_email", "client_list_path", "profile_url", "sequences_enabled",
    "loop_delay_seconds",
}


def test_the_template_does_not_silently_change_behaviour():
    """Every template value that maps to a real setting must either match the
    vetted default or be listed above as a deliberate difference."""
    defaults = Settings(_env_file=None)
    drifted = []
    for key, raw in _template_values().items():
        if key in EXPECTED_TO_DIFFER or not hasattr(defaults, key):
            continue
        default = getattr(defaults, key)
        if raw == "":
            continue
        # Numbers compare as numbers: "0" and 0.0 are the same setting, and a
        # check that reports them as drift is a check people learn to ignore.
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                if float(raw) == float(default):
                    continue
            except ValueError:
                pass
        if str(default).lower() != raw.lower():
            drifted.append(f"{key}: template={raw!r} default={default!r}")
    assert not drifted, (
        "these .env.example values would change behaviour on a fresh install:\n  "
        + "\n  ".join(drifted)
    )


def test_the_template_does_not_block_the_mailboxes_that_carry_real_leads():
    """Named explicitly, because this is the one that actually bit: info@ and
    sales@ are where European manufacturers publish their buying contact."""
    raw = _template_values().get("blocked_email_prefixes", "")
    blocked = {p.strip().lower() for p in raw.split(",") if p.strip()}
    assert not (blocked & {"info", "sales", "contact", "kontakt"}), (
        f"the template blocks real buying mailboxes: {sorted(blocked)}"
    )
