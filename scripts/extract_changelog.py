#!/usr/bin/env python3
"""Extract a CHANGELOG section for a release version."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def extract(version: str, changelog_text: str) -> str:
    pattern = rf"## \[{re.escape(version)}\][^\n]*\n(.*?)(?=\n## \[|\Z)"
    match = re.search(pattern, changelog_text, re.DOTALL)
    if match is None:
        raise SystemExit(f"CHANGELOG section not found for {version}")
    return match.group(0).strip()


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: extract_changelog.py VERSION CHANGELOG.md", file=sys.stderr)
        return 2
    version = sys.argv[1]
    path = Path(sys.argv[2])
    text = path.read_text(encoding="utf-8")
    sys.stdout.write(extract(version, text) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
