"""Tests for playbook schema, loader, and JSON Schema export."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ordine.core.errors import PlaybookSyntaxError, PlaybookValidationError
from ordine.core.playbook import Playbook, emit_json_schema, load_playbook, loads_playbook

FIXTURES = Path(__file__).parent / "fixtures" / "playbooks"
VALID_DIR = FIXTURES / "valid"
INVALID_DIR = FIXTURES / "invalid"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "playbook.schema.json"

VALID_FIXTURES = sorted(VALID_DIR.glob("*.yml"))
INVALID_FIXTURES = sorted(INVALID_DIR.glob("*.yml"))

INVALID_EXPECTATIONS: dict[str, dict[str, object]] = {
    "i01_syntax.yml": {
        "exc": PlaybookSyntaxError,
        "line_min": 1,
    },
    "i02_unknown_trigger.yml": {
        "exc": PlaybookValidationError,
        "path_start": "trigger",
    },
    "i03_bad_step_id.yml": {
        "exc": PlaybookValidationError,
        "path_contains": "steps.0",
    },
    "i04_dup_branch_names.yml": {
        "exc": PlaybookValidationError,
        "message_contains": "duplicate recovery branch name",
    },
    "i05_nested_branch.yml": {
        "exc": PlaybookValidationError,
        "message_contains": "no nesting",
    },
    "i06_negative_retries.yml": {
        "exc": PlaybookValidationError,
        "message_contains": "greater than or equal to 0",
    },
    "i07_no_steps.yml": {
        "exc": PlaybookValidationError,
        "path_contains": "steps",
    },
    "i08_extra_key.yml": {
        "exc": PlaybookValidationError,
        "message_contains": "Extra inputs are not permitted",
    },
    "i09_wrong_version.yml": {
        "exc": PlaybookValidationError,
        "path_contains": "version",
    },
    "i10_ordinal_conflict.yml": {
        "exc": PlaybookValidationError,
        "message_contains": "not both",
    },
    "i11_bad_ordinal_regex.yml": {
        "exc": PlaybookValidationError,
        "message_contains": "capture group",
    },
    "i12_cross_step_branch_dup.yml": {
        "exc": PlaybookValidationError,
        "message_contains": "branch names must be unique across the playbook",
    },
}


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_valid_fixtures_load_and_round_trip(path: Path) -> None:
    playbook = load_playbook(path)
    restored = Playbook.model_validate(playbook.model_dump())
    assert restored == playbook


def test_v02_flagship_deep() -> None:
    playbook = load_playbook(VALID_DIR / "v02_flagship.yml")

    assert playbook.version == 1
    assert playbook.name == "png-cleanup"
    assert playbook.dedup == "content_hash"
    assert playbook.engine == "headless"

    trigger = playbook.trigger
    assert trigger.type == "folder_watch"
    assert trigger.path == "~/renders"
    assert trigger.glob == "*.png"
    assert trigger.settle_seconds == 2.0

    assert len(playbook.steps) == 4
    assert playbook.steps[0].id == "image.white_to_alpha"
    assert playbook.steps[0].params == {"fuzz": 8}
    assert playbook.steps[1].id == "image.trim"
    assert playbook.steps[1].params == {}
    assert playbook.steps[2].id == "file.rename_from_manifest"
    assert playbook.steps[2].params == {"manifest": "~/renders/assets.csv"}
    assert playbook.steps[3].id == "image.export"
    assert playbook.steps[3].params == {"dest": "~/output", "format": "png"}

    assert playbook.on_failure.retries == 1
    assert playbook.on_failure.then == "mark_failed"
    assert playbook.on_failure.branches == []


@pytest.mark.parametrize(
    ("yaml_text", "expected_id", "expected_params"),
    [
        (
            "version: 1\nname: t\ntrigger:\n  type: manual\n  path: ~/x\nsteps:\n  - image.trim\n",
            "image.trim",
            {},
        ),
        (
            "version: 1\nname: t\ntrigger:\n  type: manual\n  path: ~/x\nsteps:\n  - image.white_to_alpha: {fuzz: 8}\n",
            "image.white_to_alpha",
            {"fuzz": 8},
        ),
        (
            "version: 1\nname: t\ntrigger:\n  type: manual\n  path: ~/x\nsteps:\n  - id: image.trim\n    params: {border: 1}\n",
            "image.trim",
            {"border": 1},
        ),
    ],
    ids=["form1-string", "form2-single-key", "form3-long"],
)
def test_step_form_normalization(
    yaml_text: str, expected_id: str, expected_params: dict[str, object]
) -> None:
    playbook = loads_playbook(yaml_text)
    assert playbook.steps[0].id == expected_id
    assert playbook.steps[0].params == expected_params


def test_step_form3_id_field_not_treated_as_form2() -> None:
    yaml_text = """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - id: image.trim
"""
    playbook = loads_playbook(yaml_text)
    assert playbook.steps[0].id == "image.trim"
    assert playbook.steps[0].params == {}


@pytest.mark.parametrize("path", INVALID_FIXTURES, ids=lambda p: p.name)
def test_invalid_fixtures_raise(path: Path) -> None:
    expectations = INVALID_EXPECTATIONS[path.name]
    exc_type = expectations["exc"]
    assert isinstance(exc_type, type)

    with pytest.raises(exc_type) as exc_info:
        load_playbook(path)

    exc = exc_info.value
    if "line_min" in expectations:
        assert isinstance(exc, PlaybookSyntaxError)
        assert exc.line is not None
        assert exc.line >= int(expectations["line_min"])
    if "path_start" in expectations:
        assert isinstance(exc, PlaybookValidationError)
        assert any(e.path.startswith(str(expectations["path_start"])) for e in exc.errors)
    if "path_contains" in expectations:
        assert isinstance(exc, PlaybookValidationError)
        needle = str(expectations["path_contains"])
        assert any(needle in e.path for e in exc.errors)
    if "message_contains" in expectations:
        needle = str(expectations["message_contains"])
        if isinstance(exc, PlaybookValidationError):
            assert any(needle in e.message for e in exc.errors) or needle in str(exc)
        else:
            assert needle in str(exc)


def test_syntax_error_carries_line_and_column() -> None:
    with pytest.raises(PlaybookSyntaxError) as exc_info:
        loads_playbook("version: 1\nname: x\nsteps:\n  - [")
    exc = exc_info.value
    assert exc.line is not None
    assert exc.line >= 1
    assert exc.column is not None


def test_emit_json_schema_writes_parseable_json(tmp_path: Path) -> None:
    dest = tmp_path / "s.json"
    emit_json_schema(dest)
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert "$defs" in data
    assert data["title"] == "Playbook"


def test_committed_schema_matches_fresh_emission(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.json"
    emit_json_schema(fresh)
    assert SCHEMA_PATH.read_text(encoding="utf-8") == fresh.read_text(encoding="utf-8")


def test_invalid_step_format_raises() -> None:
    yaml_text = """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - 42
"""
    with pytest.raises(PlaybookValidationError) as exc_info:
        loads_playbook(yaml_text)
    assert any("step must be" in e.message for e in exc_info.value.errors)


def test_single_key_step_with_null_params() -> None:
    yaml_text = """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - image.trim:
"""
    playbook = loads_playbook(yaml_text)
    assert playbook.steps[0].id == "image.trim"
    assert playbook.steps[0].params == {}


def test_non_mapping_root_raises() -> None:
    with pytest.raises(PlaybookValidationError) as exc_info:
        loads_playbook("just a string")
    assert exc_info.value.errors[0].path == "$"
    assert "mapping" in exc_info.value.errors[0].message


def test_invalid_playbook_name_slug() -> None:
    yaml_text = """\
version: 1
name: Bad Name
trigger:
  type: manual
  path: ~/x
steps:
  - image.trim
"""
    with pytest.raises(PlaybookValidationError):
        loads_playbook(yaml_text)


def test_manual_trigger_ordinal_conflict() -> None:
    yaml_text = """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
  ordinal_regex: 'img_(\\d+)'
  arrival_order_ordinals: true
steps:
  - image.trim
"""
    with pytest.raises(PlaybookValidationError) as exc_info:
        loads_playbook(yaml_text)
    assert any("not both" in e.message for e in exc_info.value.errors)


def test_invalid_ordinal_regex_syntax() -> None:
    yaml_text = """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
  ordinal_regex: '[unclosed'
steps:
  - image.trim
"""
    with pytest.raises(PlaybookValidationError) as exc_info:
        loads_playbook(yaml_text)
    assert any("capture group" in e.message for e in exc_info.value.errors)


def test_emit_schema_module_main(tmp_path: Path) -> None:
    dest = tmp_path / "cli-schema.json"
    subprocess.run(
        [sys.executable, "-m", "ordine.core.playbook", "--emit-schema", str(dest)],
        check=True,
    )
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["title"] == "Playbook"


def test_reserved_params_key_is_long_form() -> None:
    yaml_text = """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - params:
      fuzz: 8
"""
    with pytest.raises(PlaybookValidationError):
        loads_playbook(yaml_text)


def test_multi_key_step_without_id_raises() -> None:
    yaml_text = """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - image.trim: {}
    extra: true
"""
    with pytest.raises(PlaybookValidationError) as exc_info:
        loads_playbook(yaml_text)
    assert any("step must be" in e.message for e in exc_info.value.errors)


def test_steps_must_be_a_list() -> None:
    yaml_text = """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps: not-a-list
"""
    with pytest.raises(PlaybookValidationError):
        loads_playbook(yaml_text)


def test_branch_steps_must_be_a_list() -> None:
    yaml_text = """\
version: 1
name: t
trigger:
  type: manual
  path: ~/x
steps:
  - image.trim
on_failure:
  branches:
    - name: fallback
      steps: not-a-list
"""
    with pytest.raises(PlaybookValidationError):
        loads_playbook(yaml_text)
