#!/usr/bin/env python3
"""Bump ordine version in pyproject.toml, __init__.py, and CHANGELOG."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: bump_version.py X.Y.Z", file=sys.stderr)
        return 2
    version = sys.argv[1]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"invalid version: {version!r}", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parents[1]
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    unreleased_match = re.search(
        r"## \[Unreleased\]\n\n(### Added\n(?:.*?\n)*?)(?=\n### |\n## |\Z)",
        text,
        re.DOTALL,
    )
    if unreleased_match is None:
        print("CHANGELOG missing [Unreleased] section", file=sys.stderr)
        return 1
    body = unreleased_match.group(1).strip()
    if not body or body == "### Added" or body.endswith("### Added"):
        # check if any bullets under Added/Changed/Fixed
        section = text.split("## [Unreleased]", 1)[1].split("## [", 1)[0]
        if not re.search(r"^- ", section, re.MULTILINE):
            print("refusing bump: [Unreleased] has no bullet entries", file=sys.stderr)
            return 1

    pyproject = root / "pyproject.toml"
    py_text = pyproject.read_text(encoding="utf-8")
    py_new, count = re.subn(
        r'^version = "[^"]+"',
        f'version = "{version}"',
        py_text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        print("failed to update pyproject.toml version", file=sys.stderr)
        return 1
    pyproject.write_text(py_new, encoding="utf-8")

    init_py = root / "src" / "ordine" / "__init__.py"
    init_text = init_py.read_text(encoding="utf-8")
    init_new, count = re.subn(
        r'^__version__ = "[^"]+"',
        f'__version__ = "{version}"',
        init_text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        print("failed to update __init__.py version", file=sys.stderr)
        return 1
    init_py.write_text(init_new, encoding="utf-8")

    today = __import__("datetime").date.today().isoformat()
    new_section = f"## [{version}] - {today}\n\n"
    updated = text.replace("## [Unreleased]\n\n", f"## [Unreleased]\n\n{new_section}", 1)
    changelog.write_text(updated, encoding="utf-8")
    print(f"bumped to {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
