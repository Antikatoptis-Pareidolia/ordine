# Docs pipeline example

A non-image chain that shows Ordine's universality: shell commands, manifest naming, and file delivery.

From the repo root:

```bash
uv run ordine run examples/docs-pipeline/pipeline.yml --oneshot
ls examples/docs-pipeline/publish/
```

The manual trigger scans `samples/*.md`, stamps each file with a `# Published` header via `shell.run`, renames from `manifest.csv`, and moves results into `publish/`.
