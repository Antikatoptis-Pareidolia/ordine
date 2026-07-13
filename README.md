# Conveyor

Local-first, AI-assisted task automation pipelines for Linux. Watch folders or manifests, run ordered steps with exactly-once guarantees, recover from failures with branches, and optionally use LLMs to draft playbooks or diagnose flags — all on your machine.

[![CI](https://github.com/OWNER/conveyor/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/conveyor/actions/workflows/ci.yml)

**Privacy:** Conveyor collects no telemetry, crash reports, or usage analytics — ever — without explicit opt-in. There is none today.

## Quickstart (≤10 minutes)

```bash
# pipx (recommended) or install the .deb — see docs/install.md
pipx install conveyor-automation
sudo apt install -y imagemagick   # recommended for image pipelines

conveyor example ~/conveyor-demo
cd ~/conveyor-demo
conveyor check png-cleanup.yml
conveyor run png-cleanup.yml --oneshot
conveyor serve   # open http://127.0.0.1:8484
```

`conveyor example` scaffolds six sample images, `assets.csv`, and a ready-to-run cleanup playbook. The quickstart path is also exercised in CI (`tests/test_example_cmd.py`).

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
