"""Tests for CLI output formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ordine.cli import output


@pytest.mark.parametrize("tz_name", ["UTC", "Europe/Moscow", "America/New_York"])
def test_format_age_naive_created_at_uses_utc(
    monkeypatch: pytest.MonkeyPatch, tz_name: str
) -> None:
    """Naive DB timestamps are UTC; age must not depend on local TZ."""
    monkeypatch.setenv("TZ", tz_name)
    created_at = datetime(2026, 7, 11, 10, 0, 0)
    now = datetime(2026, 7, 11, 10, 5, 0, tzinfo=UTC)
    assert output.format_age(created_at, now=now) == "5m"


def test_format_age_aware_created_at(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Moscow")
    created_at = datetime(2026, 7, 11, 10, 0, 0, tzinfo=UTC)
    now = datetime(2026, 7, 11, 10, 5, 0, tzinfo=UTC)
    assert output.format_age(created_at, now=now) == "5m"


def test_iso_timestamp_naive_as_utc() -> None:
    value = datetime(2026, 7, 11, 10, 0, 0)
    assert output.iso_timestamp(value) == "2026-07-11T10:00:00+00:00"
