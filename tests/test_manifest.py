"""Tests for job manifest parsing."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ordine.core.errors import ManifestError
from ordine.core.manifest import load_manifest


def test_csv_happy_path(tmp_path: Path) -> None:
    path = tmp_path / "assets.csv"
    path.write_text(
        "name,prompt,tag\ngoat.png,goat prompt,alpha\njug.png,,beta\n", encoding="utf-8"
    )
    rows = load_manifest(path)
    assert len(rows) == 2
    assert rows[0].ordinal == 1
    assert rows[0].name == "goat.png"
    assert rows[0].prompt == "goat prompt"
    assert rows[0].extras == {"tag": "alpha"}
    assert rows[1].ordinal == 2
    assert rows[1].prompt is None
    assert rows[1].extras == {"tag": "beta"}


def test_csv_utf8_sig_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbfname,prompt\ngoat.png,x\n")
    rows = load_manifest(path)
    assert rows[0].name == "goat.png"


def test_csv_missing_name_column(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("prompt\nx\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="needs a 'name' column"):
        load_manifest(path)


def test_json_objects(tmp_path: Path) -> None:
    path = tmp_path / "assets.json"
    path.write_text(
        '[{"name": "goat.png", "prompt": "p", "extra": "1"}, {"name": "jug.png"}]',
        encoding="utf-8",
    )
    rows = load_manifest(path)
    assert rows[0].name == "goat.png"
    assert rows[0].extras == {"extra": "1"}
    assert rows[1].prompt is None


def test_json_string_array(tmp_path: Path) -> None:
    path = tmp_path / "names.json"
    path.write_text('["goat.png", "jug.png"]', encoding="utf-8")
    rows = load_manifest(path)
    assert [r.name for r in rows] == ["goat.png", "jug.png"]
    assert [r.ordinal for r in rows] == [1, 2]


def test_txt_comments_and_blanks(tmp_path: Path) -> None:
    path = tmp_path / "names.txt"
    path.write_text("# header\n\ngoat.png\n\n# skip\njug.png\n", encoding="utf-8")
    rows = load_manifest(path)
    assert [r.ordinal for r in rows] == [1, 2]
    assert [r.name for r in rows] == ["goat.png", "jug.png"]


def test_empty_name_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("name\n  \n", encoding="utf-8")
    with pytest.raises(ManifestError, match="empty name"):
        load_manifest(path)


def test_duplicate_names_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "dup.csv"
    path.write_text("name\ngoat.png\ngoat.png\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        rows = load_manifest(path)
    assert len(rows) == 2
    assert any("duplicate manifest name" in r.message for r in caplog.records)
