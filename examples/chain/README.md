# Chain example — manifest → generate → move → cleanup

Two pipelines implement the full game-assets story:

1. **`gen-images.yml`** — reads `assets.csv`, generates `img_0001.png` … `img_0008.png` into `~/renders`
2. **`png-cleanup.yml`** — watches `~/renders`, trims/renames via manifest ordinals, exports to `~/game/assets`

## Offline rehearsal (mock provider)

```bash
cd ordine   # repository root (where pyproject.toml lives)
uv run ordine run examples/chain/gen-images.yml --oneshot
uv run ordine run examples/chain/png-cleanup.yml --oneshot
ls ~/game/assets
```

`gen-images.yml` uses `provider: mock` — deterministic PNGs with no API key.

## Both pipelines under serve

Register both playbooks, then start each pipeline from the dashboard (or CLI). The manifest trigger rescans when `assets.csv` changes.

## Regeneration

Edit a row's `prompt` in `assets.csv`, then rerun **both** pipeline legs:

```bash
uv run ordine run examples/chain/gen-images.yml --oneshot
uv run ordine run examples/chain/png-cleanup.yml --oneshot
```

The manifest trigger enqueues a new task for the edited row only; unchanged rows stay `done`.

**Collision policy:** `file.move` and `image.export` default to `on_collision: suffix`, which would write `img_0001-2.png` into `~/renders` on regeneration. The downstream `ordinal_regex` (`img_(\d+)\.png`) correctly ignores that file — so the regenerated asset would never reach `~/game/assets`. This chain sets `on_collision: replace` on **both** steps so regenerated content overwrites the same handoff and output filenames.

## Why row 7 stays row 7

Ordinals travel in filenames (`img_0007.png`). Even if rows 3–6 fail generation, row 7 still maps to the manifest row encoded in the filename. Downstream `ordinal_regex` resolves the digit → manifest row → reserved name.
