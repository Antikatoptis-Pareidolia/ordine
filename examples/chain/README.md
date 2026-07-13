# Chain example — manifest → generate → move → cleanup

Two pipelines implement the full game-assets story:

1. **`gen-images.yml`** — reads `assets.csv`, generates `img_0001.png` … `img_0008.png` into `~/renders`
2. **`png-cleanup.yml`** — watches `~/renders`, trims/renames via manifest ordinals, exports to `~/game/assets`

## Offline rehearsal (mock provider)

```bash
cd conveyor   # repository root (where pyproject.toml lives)
uv run conveyor run examples/chain/gen-images.yml --oneshot
uv run conveyor run examples/chain/png-cleanup.yml --oneshot
ls ~/game/assets
```

`gen-images.yml` uses `provider: mock` — deterministic PNGs with no API key.

## Both pipelines under serve

Register both playbooks, then start each pipeline from the dashboard (or CLI). The manifest trigger rescans when `assets.csv` changes.

## Why row 7 stays row 7

Ordinals travel in filenames (`img_0007.png`). Even if rows 3–6 fail generation, row 7 still maps to the manifest row encoded in the filename. Downstream `ordinal_regex` resolves the digit → manifest row → reserved name.

## Regeneration

Edit a row's `prompt` (or `name`) in `assets.csv`. The manifest trigger computes a new dedup key and enqueues a fresh task for that ordinal. Unchanged rows stay `done` forever.
