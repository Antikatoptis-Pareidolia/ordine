"""D3: strengthen core layer boundaries beyond no-llm-in-core."""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "ordine"
_CORE = _SRC / "core"
_FORBIDDEN_PREFIXES = (
    "ordine.executors",
    "ordine.llm",
    "ordine.web",
    "ordine.cli",
)


def _import_violations() -> list[tuple[Path, str]]:
    violations: list[tuple[Path, str]] = []
    for path in _CORE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for mod in names:
                if any(
                    mod == prefix or mod.startswith(prefix + ".") for prefix in _FORBIDDEN_PREFIXES
                ):
                    violations.append((path, mod))
    return violations


def test_core_does_not_import_executors_llm_web_or_cli() -> None:
    violations = _import_violations()
    assert not violations, violations
