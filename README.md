# Conveyor

Local-first, AI-assisted task automation pipelines for Linux.

**Status:** pre-alpha — not usable yet.

## What it will do

- Watch folders (or other triggers) and run a pipeline of steps on each task, with an exactly-once ledger so nothing is processed twice.
- Recovery branches per step so failures can be handled in order and pipelines can learn from successful resolutions.
- LLM-assisted authoring: describe a pipeline in plain language, review the draft, dry-run, and iterate.

## Dev setup

```bash
uv venv && uv pip install -e ".[dev]" && pre-commit install && pytest
```

## License

MIT — see [LICENSE](LICENSE).
