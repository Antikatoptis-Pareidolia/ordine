"""Deterministic-runs guarantee: core and executors must never import ordine.llm."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from ordine.core.db import create_engine_for, init_db
from ordine.core.engines import EngineRegistry, HeadlessEngine
from ordine.core.ledger import Ledger
from ordine.core.registry import StepRegistry
from tests.test_runner_e2e import (
    ASSET_NAMES,
    _game_assets_yaml,
    _runner_for,
    _scan_tasks,
    _seed_images,
    _write_manifest,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "ordine"
_SCAN_ROOTS = (_SRC / "core", _SRC / "executors")


def _llm_import_violations() -> list[tuple[Path, str]]:
    violations: list[tuple[Path, str]] = []
    for root in _SCAN_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mod = alias.name
                        if mod == "ordine.llm" or mod.startswith("ordine.llm."):
                            violations.append((path, mod))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                    if mod == "ordine.llm" or mod.startswith("ordine.llm."):
                        violations.append((path, mod))
    return violations


def test_no_llm_imports_in_core_or_executors() -> None:
    violations = _llm_import_violations()
    assert not violations, violations


class _LLMImportBlocker:
    """Block any import of ordine.llm* at runtime."""

    def find_module(self, fullname: str, path: object | None = None) -> _LLMImportBlocker | None:
        if fullname == "ordine.llm" or fullname.startswith("ordine.llm."):
            return self
        return None

    def load_module(self, fullname: str) -> None:
        raise ImportError(f"blocked import: {fullname}")


def test_flagship_e2e_runs_without_llm_module(tmp_path: Path) -> None:
    """Step 7 flagship e2e must complete with ordine.llm blocked on sys.meta_path."""
    eng = create_engine_for(tmp_path / "ledger.db")
    init_db(eng)
    ledger = Ledger(eng)
    registry = StepRegistry.load()
    engines = EngineRegistry()
    engines.register(HeadlessEngine())

    blocker = _LLMImportBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        watch = tmp_path / "in"
        manifest = tmp_path / "assets.csv"
        output = tmp_path / "out"
        originals = _seed_images(watch, corrupt_ordinals={3, 4, 5, 6})
        _write_manifest(manifest, ASSET_NAMES)
        yaml_text = _game_assets_yaml(watch=watch, manifest=manifest, output=output)

        runner, pipeline_id = _runner_for(ledger, registry, engines, yaml_text, tmp_path / "work")
        _scan_tasks(ledger, pipeline_id, watch, yaml_text)
        assert runner.run_until_idle() == 8

        counts = ledger.counts(pipeline_id)
        assert counts["done"] == 4
        assert counts["skipped"] == 4

        for ordinal in (3, 4, 5, 6):
            task = next(t for t in ledger.list_tasks(pipeline_id, limit=20) if t.ordinal == ordinal)
            assert task.status == "skipped"

        for ordinal, data in originals.items():
            assert (watch / f"img_{ordinal:04d}.png").read_bytes() == data
    finally:
        sys.meta_path.remove(blocker)
