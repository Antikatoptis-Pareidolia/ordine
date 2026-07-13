"""Release guard: package version single-sourcing."""

from __future__ import annotations

import re
from pathlib import Path

import conveyor


def test_version_matches_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert match is not None, "pyproject.toml missing version"
    assert conveyor.__version__ == match.group(1)
