"""Unit tests for ledger-backed naming."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from conveyor.core.db import create_engine_for, init_db
from conveyor.core.ledger import Ledger
from conveyor.core.naming import LedgerNamingService
from conveyor.core.playbook import load_playbook

FIXTURE_YAML = Path(__file__).parent / "fixtures" / "playbooks" / "valid" / "v01_minimal.yml"


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine_for(tmp_path / "ledger.db")
    init_db(eng)
    return eng


@pytest.fixture
def ledger(engine) -> Ledger:
    return Ledger(engine)


def test_bind_logs_warning_when_reserved_name_mismatches(
    ledger: Ledger, caplog: pytest.LogCaptureFixture
) -> None:
    playbook = load_playbook(FIXTURE_YAML)
    yaml_text = FIXTURE_YAML.read_text(encoding="utf-8")
    pipeline_id, _ = ledger.register_pipeline(playbook, yaml_text)
    task_id = ledger.create_task(pipeline_id, "/in.png", "k1")
    assert task_id is not None
    ledger.reserve_name(pipeline_id, 1, "goat.png", task_id)
    naming = LedgerNamingService(ledger, pipeline_id, task_id)
    with caplog.at_level(logging.WARNING):
        effective = naming.bind(1, "sheep.png")
    assert effective == "goat.png"
    assert any(
        "goat.png" in record.message and "sheep.png" in record.message for record in caplog.records
    )
