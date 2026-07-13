"""Scaffold the ordine example demo directory.

Owns filesystem layout for ``ordine example``. Must never import web or executors.
"""

from __future__ import annotations

from pathlib import Path

from ordine.core.errors import ConfigError
from ordine.llm.steps import render_mock_image

_DEMO_ROWS: list[tuple[str, str]] = [
    ("goat.png", "a friendly goat mascot"),
    ("jug.png", "a clay water jug"),
    ("crown.png", "a golden crown"),
    ("ring.png", "a silver ring"),
    ("sword.png", "a steel sword"),
    ("shield.png", "a wooden shield"),
]


def scaffold_example(target: Path) -> list[str]:
    """Create the quickstart demo tree under *target*.

    Returns the exact next commands to print for the operator.
    Raises ConfigError when *target* exists and is not empty.
    """
    expanded = target.expanduser().resolve()
    if expanded.exists():
        if any(expanded.iterdir()):
            raise ConfigError(f"refusing to scaffold into non-empty directory: {expanded}")
    else:
        expanded.mkdir(parents=True)

    samples = expanded / "samples"
    exports = expanded / "exports"
    chain = expanded / "chain"
    chain_renders = chain / "renders"
    chain_exports = chain / "exports"
    for path in (samples, exports, chain_renders, chain_exports):
        path.mkdir(parents=True, exist_ok=True)

    for index, (_name, prompt) in enumerate(_DEMO_ROWS, start=1):
        png = render_mock_image(size="256x256", prompt=prompt, ordinal=index)
        (samples / f"img_{index:04d}.png").write_bytes(png)

    assets = expanded / "assets.csv"
    assets.write_text(
        "name,prompt\n" + "\n".join(f"{name},{prompt}" for name, prompt in _DEMO_ROWS) + "\n",
        encoding="utf-8",
    )

    chain_assets = chain / "assets.csv"
    chain_assets.write_text(assets.read_text(encoding="utf-8"), encoding="utf-8")

    cleanup = expanded / "png-cleanup.yml"
    cleanup.write_text(
        _cleanup_yaml(
            name="demo-cleanup",
            watch=samples,
            manifest=assets,
            output=exports,
        ),
        encoding="utf-8",
    )

    (chain / "gen-images.yml").write_text(
        _gen_yaml(manifest=chain_assets, handoff=chain_renders),
        encoding="utf-8",
    )
    (chain / "png-cleanup.yml").write_text(
        _cleanup_yaml(
            name="chain-cleanup",
            watch=chain_renders,
            manifest=chain_assets,
            output=chain_exports,
        ),
        encoding="utf-8",
    )
    (chain / "README.md").write_text(
        "# Chain variant (offline mock)\n\n"
        f"1. `ordine run {chain / 'gen-images.yml'} --oneshot`\n"
        f"2. `ordine run {chain / 'png-cleanup.yml'} --oneshot`\n",
        encoding="utf-8",
    )

    return [
        "ordine check png-cleanup.yml",
        f"ordine run {cleanup} --oneshot",
        "ordine serve",
    ]


def _cleanup_yaml(*, name: str, watch: Path, manifest: Path, output: Path) -> str:
    return f"""version: 1
name: {name}
description: Demo cleanup on ordinal-named samples
trigger:
  type: manual
  path: {watch}
  glob: "*.png"
  ordinal_regex: 'img_(\\d+)\\.png'
steps:
  - image.validate
  - image.white_to_alpha
  - image.trim
  - file.rename_from_manifest:
      manifest: {manifest}
  - id: image.export
    params:
      dest: {output}
      use_reserved_name: true
"""


def _gen_yaml(*, manifest: Path, handoff: Path) -> str:
    return f"""version: 1
name: chain-gen
description: Generate ordinal images from assets.csv (mock provider)
trigger:
  type: manifest
  path: {manifest}
  poll_seconds: 0
dedup: none
steps:
  - id: llm.generate_image
    params:
      manifest: {manifest}
      provider: mock
      size: 256x256
  - id: file.move
    params:
      dest: {handoff}
      on_collision: replace
"""
