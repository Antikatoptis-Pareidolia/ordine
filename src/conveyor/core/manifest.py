"""Job manifest parsing for position-keyed naming.

Owns CSV/JSON/txt manifest loading. Must never write manifests or import executors/web/cli.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from conveyor.core.errors import ManifestError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManifestRow:
    """One manifest data row with a 1-based ordinal position."""

    ordinal: int
    name: str
    prompt: str | None
    extras: dict[str, str]


def _parse_csv(path: Path) -> list[ManifestRow]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None or "name" not in reader.fieldnames:
        raise ManifestError("manifest CSV needs a 'name' column")
    rows: list[ManifestRow] = []
    for ordinal, raw in enumerate(reader, start=1):
        name = (raw.get("name") or "").strip()
        if not name:
            raise ManifestError(f"manifest CSV row {ordinal + 1}: empty name")
        prompt_val = raw.get("prompt")
        prompt = (
            None if prompt_val is None or str(prompt_val).strip() == "" else str(prompt_val).strip()
        )
        extras = {
            key: str(value).strip()
            for key, value in raw.items()
            if key not in {"name", "prompt"} and value is not None and str(value).strip() != ""
        }
        rows.append(ManifestRow(ordinal=ordinal, name=name, prompt=prompt, extras=extras))
    return rows


def _parse_json(path: Path) -> list[ManifestRow]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest JSON invalid: {exc}") from exc
    if not isinstance(data, list):
        raise ManifestError("manifest JSON must be an array")
    rows: list[ManifestRow] = []
    for index, item in enumerate(data, start=1):
        if isinstance(item, str):
            name = item.strip()
            if not name:
                raise ManifestError(f"manifest JSON row {index}: empty name")
            rows.append(ManifestRow(ordinal=index, name=name, prompt=None, extras={}))
            continue
        if not isinstance(item, dict):
            raise ManifestError(f"manifest JSON row {index}: must be an object or string")
        name_val = item.get("name")
        if not isinstance(name_val, str) or not name_val.strip():
            raise ManifestError(f"manifest JSON row {index}: missing or empty name")
        name = name_val.strip()
        prompt_val = item.get("prompt")
        prompt = None
        if prompt_val is not None:
            if not isinstance(prompt_val, str):
                raise ManifestError(f"manifest JSON row {index}: prompt must be a string")
            prompt = prompt_val.strip() or None
        extras = {
            str(key): str(value).strip()
            for key, value in item.items()
            if key not in {"name", "prompt"} and value is not None and str(value).strip() != ""
        }
        rows.append(ManifestRow(ordinal=index, name=name, prompt=prompt, extras=extras))
    return rows


def _parse_txt(path: Path) -> list[ManifestRow]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    rows: list[ManifestRow] = []
    ordinal = 0
    for _line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        ordinal += 1
        rows.append(ManifestRow(ordinal=ordinal, name=stripped, prompt=None, extras={}))
    return rows


def load_manifest(path: Path) -> list[ManifestRow]:
    """Load a job manifest from *path* (CSV, JSON, or TXT)."""
    expanded = path.expanduser()
    suffix = expanded.suffix.lower()
    if suffix == ".csv":
        rows = _parse_csv(expanded)
    elif suffix == ".json":
        rows = _parse_json(expanded)
    elif suffix == ".txt":
        rows = _parse_txt(expanded)
    else:
        raise ManifestError(f"unsupported manifest format: {expanded.suffix or '(no extension)'}")

    seen: dict[str, int] = {}
    for row in rows:
        count = seen.get(row.name, 0) + 1
        seen[row.name] = count
        if count > 1:
            logger.warning("duplicate manifest name %r (ordinal %s)", row.name, row.ordinal)
    return rows
