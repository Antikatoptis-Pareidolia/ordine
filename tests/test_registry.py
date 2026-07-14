"""Tests for step registry discovery and playbook checking."""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel, ConfigDict

from ordine.core.errors import StepError, StepParamError, UnknownStepError
from ordine.core.playbook import loads_playbook
from ordine.core.registry import StepRegistry
from ordine.executors.builtin.steps import NoopStep

PLUGIN_INSTALLED = False
try:
    import ordine_test_plugin  # noqa: F401

    PLUGIN_INSTALLED = True
except ImportError:
    pass


@pytest.fixture
def registry() -> StepRegistry:
    return StepRegistry.load()


def test_discovery_finds_builtins(registry: StepRegistry) -> None:
    ids = set(registry.ids())
    assert {"util.noop", "util.fail", "util.copy", "shell.run"} <= ids


@pytest.mark.skipif(not PLUGIN_INSTALLED, reason="ordine_test_plugin not installed")
def test_discovery_finds_fixture_plugin(registry: StepRegistry) -> None:
    assert "test.echo" in registry.ids()


def test_register_duplicate_id_keeps_first(caplog: pytest.LogCaptureFixture) -> None:
    reg = StepRegistry()
    with caplog.at_level(logging.WARNING):

        class DuplicateNoop:
            id = "util.noop"
            engines = frozenset({"headless"})

            class Params(BaseModel):
                model_config = ConfigDict(extra="forbid")

            OUTPUT_DIR_PARAMS = frozenset()

            def run(self, ctx, params):  # type: ignore[no-untyped-def]
                del ctx, params
                return None

        reg.register(NoopStep, source="first")
        reg.register(DuplicateNoop, source="duplicate")
    assert reg.get("util.noop") is NoopStep
    assert any("duplicate step id" in record.message for record in caplog.records)


def test_validate_params_type_error(registry: StepRegistry) -> None:
    with pytest.raises(StepParamError) as exc_info:
        registry.validate_params("util.fail", {"times": "x"})
    assert any(error.path == "times" for error in exc_info.value.errors)


def test_check_playbook_unknown_step(registry: StepRegistry) -> None:
    playbook = loads_playbook(
        """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - unknown.step
"""
    )
    errors = registry.check_playbook(playbook)
    assert any("unknown step id" in error.message for error in errors)
    assert any(error.path == "steps.0.id" for error in errors)


def test_check_playbook_bad_param_in_branch(registry: StepRegistry) -> None:
    playbook = loads_playbook(
        """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - id: util.noop
    on_failure:
      branches:
        - name: fallback
          steps:
            - util.fail:
                times: x
"""
    )
    errors = registry.check_playbook(playbook)
    assert any(
        error.path == "steps.0.on_failure.branches.0.steps.0.params.times" for error in errors
    )


def test_check_playbook_engine_mismatch(registry: StepRegistry) -> None:
    playbook = loads_playbook(
        """\
version: 1
name: t
engine: gimp
trigger:
  type: manual
  path: ~/x
steps:
  - util.noop
"""
    )
    errors = registry.check_playbook(playbook)
    assert any(
        error.path == "steps.0.id" and "does not support engine gimp" in error.message
        for error in errors
    )


def test_registration_missing_params_raises_step_error() -> None:
    class BrokenStep:
        id = "broken.step"
        engines = frozenset({"headless"})
        OUTPUT_DIR_PARAMS = frozenset()

        def run(self, ctx, params):  # type: ignore[no-untyped-def]
            del ctx, params
            return None

    reg = StepRegistry()
    with pytest.raises(StepError):
        reg.register(BrokenStep)


def test_get_unknown_step(registry: StepRegistry) -> None:
    with pytest.raises(UnknownStepError):
        registry.get("missing.step")


def test_param_schema(registry: StepRegistry) -> None:
    schema = registry.param_schema("util.fail")
    assert schema["title"] == "FailParams"


def test_check_playbook_empty_when_valid(registry: StepRegistry) -> None:
    playbook = loads_playbook(
        """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - util.noop
"""
    )
    assert registry.check_playbook(playbook) == []


def test_check_playbook_pipeline_level_branch(registry: StepRegistry) -> None:
    playbook = loads_playbook(
        """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - util.noop
on_failure:
  branches:
    - name: fallback
      steps:
        - util.fail:
            times: x
"""
    )
    errors = registry.check_playbook(playbook)
    assert any("on_failure.branches.0.steps.0.params.times" in error.path for error in errors)


def test_assert_contract_invalid_id() -> None:
    class BadId:
        id = "Bad"
        engines = frozenset({"headless"})

        class Params(BaseModel):
            model_config = ConfigDict(extra="forbid")

        OUTPUT_DIR_PARAMS = frozenset()

        def run(self, ctx, params):  # type: ignore[no-untyped-def]
            del ctx, params
            return None

    reg = StepRegistry()
    with pytest.raises(StepError):
        reg.register(BadId)  # type: ignore[arg-type]
