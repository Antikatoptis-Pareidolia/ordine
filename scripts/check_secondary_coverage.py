"""Optional secondary coverage floors for high-risk modules (B3).

Modest gates for ``web/security.py`` and ``executors/builtin/shell.py``.
Does not replace the core coverage gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Keep floors achievable; raise gradually as coverage grows.
FLOORS: dict[str, float] = {
    "src/ordine/web/security.py": 80.0,
    "src/ordine/executors/builtin/shell.py": 75.0,
}


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    failures: list[tuple[str, float, float]] = []
    for filename, floor in sorted(FLOORS.items()):
        details = report["files"].get(filename)
        if details is None:
            print(f"{filename}: MISSING from coverage report", file=sys.stderr)
            failures.append((filename, 0.0, floor))
            continue
        percent = float(details["summary"]["percent_covered"])
        print(f"{filename}: {percent:.2f}% (floor {floor:.0f}%)")
        if percent < floor:
            failures.append((filename, percent, floor))
    if failures:
        print("secondary coverage floor failures:", file=sys.stderr)
        for filename, percent, floor in failures:
            print(f"  {filename}: {percent:.2f}% < {floor:.0f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
