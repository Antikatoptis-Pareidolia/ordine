"""Tests for engine registry and headless execution."""

from __future__ import annotations

import logging

import pytest

from ordine.core.engines import EngineRegistry, HeadlessEngine
from ordine.core.errors import UnknownEngineError


def test_engine_registry_loads_headless() -> None:
    registry = EngineRegistry.load()
    assert "headless" in registry.names()
    engine = registry.get("headless")
    assert isinstance(engine, HeadlessEngine)


def test_engine_registry_register_duplicate(caplog: pytest.LogCaptureFixture) -> None:
    registry = EngineRegistry()
    with caplog.at_level(logging.WARNING):
        registry.register(HeadlessEngine())
        registry.register(HeadlessEngine())
    assert any("duplicate engine" in record.message for record in caplog.records)


def test_unknown_engine_raises() -> None:
    registry = EngineRegistry()
    with pytest.raises(UnknownEngineError):
        registry.get("missing")
