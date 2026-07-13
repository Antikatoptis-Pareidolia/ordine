# Ordine — self-healing task pipelines for your desktop.

Watch folders or manifests, run ordered steps with exactly-once guarantees, recover from failures with branches, and optionally use LLMs to draft playbooks or diagnose flags — all on your machine.

[![CI](https://github.com/Antikatoptis-Pareidolia/ordine/actions/workflows/ci.yml/badge.svg)](https://github.com/Antikatoptis-Pareidolia/ordine/actions/workflows/ci.yml)

**Privacy:** Ordine collects no telemetry, crash reports, or usage analytics — ever — without explicit opt-in. There is none today.

## Quickstart (≤10 minutes)

### From source (works today)

```bash
git clone https://github.com/Antikatoptis-Pareidolia/ordine.git
cd ordine
uv sync --locked
sudo apt install -y imagemagick   # recommended for image pipelines

uv run ordine example ./ordine-demo
uv run ordine check ./ordine-demo/png-cleanup.yml
uv run ordine run ./ordine-demo/png-cleanup.yml --oneshot
uv run ordine serve   # open http://127.0.0.1:8484
```

No config file is required for this path; built-in XDG defaults are used. `ordine example` scaffolds six sample images, `assets.csv`, and a ready-to-run cleanup playbook. The runtime commands are exercised literally in CI (`tests/test_example_cmd.py`).

### Package installs (after the first release)

After 0.1.0 is published, install with `pipx install ordine` or use the `.deb`, then run the same `ordine example`, `ordine check`, `ordine run`, and `ordine serve` commands without the `uv run` prefix. See [docs/install.md](docs/install.md).

## Features

- **Exactly-once ledger** — SQLite task state machine with dedup keys, flags, and crash reconciliation
- **Triggers** — folder watch, manual scan, manifest rows (`assets.csv`)
- **Headless image steps** — validate, white→alpha, trim, export (ImageMagick + Pillow)
- **Recovery branches** — per-step and pipeline-level `on_failure` policies
- **Web UI** — dashboard, task detail with artifacts, playbook editor, dry-run lab
- **LLM assist (optional)** — draft playbooks, diagnose failures, suggest recovery branches (your keys, JSONL audit log)
- **Chained pipelines** — manifest → generate → move → cleanup (`examples/chain/`)

## Documentation

See [docs/README.md](docs/README.md) for the full index. Highlights:

- [Install](docs/install.md) — pipx, `.deb`, from source, systemd
- [Security](docs/security.md) — localhost posture, playbook trust, LLM data flow
- [Plugin guide](docs/plugin-guide.md) — write a step without forking core
- [Release checklist](docs/release-checklist.md) — maintainers

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Engineering rules live in [CONVENTIONS.md](CONVENTIONS.md).

## License

MIT — see [LICENSE](LICENSE).
