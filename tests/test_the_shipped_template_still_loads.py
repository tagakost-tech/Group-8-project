"""A new install starts from .env.example, so the template must still load.

Kept apart from tests/test_llm_provider_is_read_whatever_its_case.py so that
the mutations named there are measured whatever the template holds: the
template is edited by hand (docs/review/ENV_EXAMPLE_CHANGES.md).
"""

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from config import PROJECT_ROOT, Settings


def test_the_shipped_template_names_a_provider_that_still_loads(settings_factory):
    """A new install starts from .env.example, so a name it ships that is now
    refused would stop every fresh installation at startup, as would a cloud
    AI key line it still ships."""
    shipped = {
        key.lower(): value
        for key, value in dotenv_values(PROJECT_ROOT / ".env.example").items()
    }
    try:
        settings_factory(llm_provider=shipped.get("llm_provider") or "")
    except ValidationError:
        pytest.fail("the LLM_PROVIDER .env.example ships is refused", pytrace=False)
    retired = sorted(k.upper() for k in shipped if k.upper() in Settings.RETIRED_SETTINGS)
    assert not retired, f".env.example still ships {', '.join(retired)}"
