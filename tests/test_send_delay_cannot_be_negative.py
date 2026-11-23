"""The gap between sends cannot be configured below zero.

`delay_between_emails` and `send_jitter_seconds` had no validator, and the
dashboard's settings form accepted a negative value for either. A negative
delay made `send_delay()` return a negative number; the sleep clamped it to
zero, so pacing was no worse than a delay of 0 - but the dashboard showed a
setting that meant nothing. Both are rejected now, at startup from .env and
through the dashboard's override path. Zero stays valid for both: it is how
the suite and an operator switch pacing off.
"""

import pytest
from pydantic import ValidationError

from config import validate_override


@pytest.mark.parametrize("key", ["delay_between_emails", "send_jitter_seconds"])
def test_a_negative_gap_is_rejected_at_startup(settings_factory, key):
    with pytest.raises(ValidationError, match=key):
        settings_factory(**{key: -5.0})


@pytest.mark.parametrize("key", ["delay_between_emails", "send_jitter_seconds"])
def test_a_negative_gap_is_rejected_from_the_dashboard(settings_factory, key):
    with pytest.raises(ValidationError, match="cannot be negative"):
        validate_override(settings_factory(), key, -0.5)


@pytest.mark.parametrize("key", ["delay_between_emails", "send_jitter_seconds"])
def test_zero_still_switches_the_gap_off(settings_factory, key):
    assert getattr(validate_override(settings_factory(), key, 0.0), key) == 0.0
