# Text stamp (non-image)

Minimal markdown pipeline with **no** `shell.run` and no image steps: copy → rename from manifest → move.

```bash
uv run ordine check examples/text-stamp/pipeline.yml
# Adjust trigger path / destinations to absolute paths under a writable demo dir before run.
```
