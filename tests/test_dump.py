"""Tests for dump_playbook serialization and round-trip guarantees."""

from __future__ import annotations

from pathlib import Path

import pytest

from conveyor.core.playbook import dump_playbook, load_playbook, loads_playbook

VALID_DIR = Path(__file__).parent / "fixtures" / "playbooks" / "valid"
VALID_FIXTURES = sorted(VALID_DIR.glob("*.yml"))


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.name)
def test_round_trip_all_valid_fixtures(path: Path) -> None:
    playbook = load_playbook(path)
    restored = loads_playbook(dump_playbook(playbook))
    assert restored == playbook


def test_minimal_fixture_omits_default_fields() -> None:
    playbook = load_playbook(VALID_DIR / "v01_minimal.yml")
    text = dump_playbook(playbook)
    assert "description" not in text
    assert "meta" not in text
    assert "dedup" not in text
    assert "engine" not in text
    assert "on_failure" not in text


def test_compact_step_forms() -> None:
    playbook = load_playbook(VALID_DIR / "v02_flagship.yml")
    text = dump_playbook(playbook)
    assert "- image.white_to_alpha:" in text or "image.white_to_alpha:" in text
    assert "- image.trim\n" in text or "- image.trim\n" in text
    assert "id:" not in text.split("steps:")[1].split("on_failure:")[0]

    branchy = load_playbook(VALID_DIR / "v03_step_on_failure.yml")
    branchy_text = dump_playbook(branchy)
    assert "on_failure:" in branchy_text.split("steps:")[1]


def test_dump_key_order_stable() -> None:
    playbook = load_playbook(VALID_DIR / "v04_pipeline_branches.yml")
    first = dump_playbook(playbook)
    second = dump_playbook(playbook)
    assert first == second
    lines = [line for line in first.splitlines() if line and not line.startswith(" ")]
    assert lines[0].startswith("version:")
    assert lines[1].startswith("name:")
    assert any(line.startswith("trigger:") for line in lines)
    assert any(line.startswith("steps:") for line in lines)


def test_meta_and_non_default_fields_preserved() -> None:
    playbook = load_playbook(VALID_DIR / "v07_meta.yml")
    restored = loads_playbook(dump_playbook(playbook))
    assert restored.meta is not None
    assert restored.meta.version_id == "pv_0002"
    assert restored.dedup == "filename"
