"""Fail when any src/ordine/core module falls below the required coverage floor."""

from __future__ import annotations

import json
import sys
from pathlib import Path

FLOOR = 85.0
CORE_PREFIX = "src/ordine/core/"


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    failures: list[tuple[str, float]] = []
    for filename, details in sorted(report["files"].items()):
        if not filename.startswith(CORE_PREFIX) or not filename.endswith(".py"):
            continue
        percent = float(details["summary"]["percent_covered"])
        print(f"{filename}: {percent:.2f}%")
        if percent < FLOOR:
            failures.append((filename, percent))
    if failures:
        print(f"core coverage floor is {FLOOR:.0f}%; failing modules:", file=sys.stderr)
        for filename, percent in failures:
            print(f"  {filename}: {percent:.2f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
