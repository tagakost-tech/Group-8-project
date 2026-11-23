"""Tests for config.Settings validation and helper methods."""
import pytest
from pydantic import ValidationError


def test_loop_delay_default(settings_factory):
    assert settings_factory().loop_delay_seconds == 300.0


def test_loop_delay_too_small_rejected(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(loop_delay_seconds=1)


def test_rate_limit_minimum_enforced(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(scrape_rate_limit=0.1)


def test_emails_per_hour_capped(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(emails_per_hour=500)


def test_get_target_queries_strips_and_splits(settings_factory):
    s = settings_factory(target_queries="a, b ,, c")
    assert s.get_target_queries() == ["a", "b", "c"]


def test_get_blocked_prefixes(settings_factory):
    s = settings_factory(blocked_email_prefixes="noreply, admin ,")
    assert s.get_blocked_prefixes() == ["noreply", "admin"]


def test_email_subject_formatting(settings_factory):
    s = settings_factory(company_name="SUPPLECO",
                         email_subject="Partnership - {company_name}")
    assert s.get_email_subject() == "Partnership - SUPPLECO"
