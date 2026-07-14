# Ordine

**Self-healing task pipelines for your desktop.**

[![CI](https://github.com/Antikatoptis-Pareidolia/ordine/actions/workflows/ci.yml/badge.svg)](https://github.com/Antikatoptis-Pareidolia/ordine/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ordine)](https://pypi.org/project/ordine/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)

Ordine watches folders and manifests, runs your files through step
pipelines, and — when a step fails — recovers through the branches you
(or an AI you approve) taught it. Every task is exactly-once, every
output name is ordinal-true, and everything runs locally.

![Ordine demo](demo/demo.gif)

## Install

```bash
pipx install ordine
```

Image pipelines work best with ImageMagick installed (`sudo apt install imagemagick`).

Or install the `.deb` from [Releases](https://github.com/Antikatoptis-Pareidolia/ordine/releases):

```bash
sudo apt install ./ordine_*_amd64.deb imagemagick
```

See [docs/install.md](docs/install.md) for upgrades, systemd, and development setup.

## Quickstart

From a clone — also the contributor path (~3 minutes):

```bash
git clone https://github.com/Antikatoptis-Pareidolia/ordine.git
cd ordine && uv sync
uv run ordine example ~/ordine-demo
uv run ordine run ~/ordine-demo/png-cleanup.yml --oneshot
```

Six sample images are validated, made transparent, trimmed, renamed
from `assets.csv`, and exported to `exports/`. Then start the web UI:

```bash
uv run ordine serve   # → http://127.0.0.1:8484
```

With `pipx` or the `.deb`, drop the `uv run` prefix (`ordine example`, `ordine run`, `ordine serve`).

Press **Start** on a pipeline, drop a file into its watch folder, and
watch the task appear, process, and land — or flag, diagnose, and heal.

## Why Ordine

Any watch → transform → deliver workflow fits Ordine: shell commands, scripts, documents, images — steps are plugins.

Our founding example is a CSV of asset names and prompts. You want images generated for
each row, cleaned up (white background → transparent, cropped to
content), named **exactly** by their row — even when rows 3–6 fail — and
delivered to your game folder. Unattended. Resumable after a crash.
Fixable from the browser when something breaks at 2 AM.

That workflow ships as the built-in example and in `examples/chain/`. Nothing in the engine is image-specific.

- **Universal steps** — `shell.run` runs any command; write custom steps as tiny Python plugins (see [docs/plugin-guide.md](docs/plugin-guide.md))
- **Ordinal guarantee** — file 7 gets row 7's name, always. Failures in
  between never shift names (*ordine* is Italian for order; it's the
  soul of the tool).
- **Recovery branches** — declare fallback step sequences per step.
  Primary fails → branches run → flags escalate by ladder level when
  everything is exhausted.
- **Exactly-once** — a SQLite ledger dedups by content hash or manifest
  row. Rerun anything, anytime: nothing double-processes.
- **Dry-run lab** — rehearse playbooks on copied samples in a sandbox
  that never touches production data, step through execution, fix from
  the failing step, resume with the validated prefix replayed.
- **AI that drafts, never executes** — describe a pipeline and get a
  validated draft; let a model diagnose a failure and *propose* a
  recovery branch. Nothing applies without your explicit approval.
  Bring your own key (Anthropic, OpenAI, or any OpenAI-compatible
  endpoint — Ollama and DeepSeek included). Works fully without any
  key, too.
- **Local and quiet** — no telemetry, ever. No accounts. Your files,
  your machine, your keys.

## The chain example

The full founding workflow — manifest → image generation → cleanup —
ships in `examples/chain/` and runs **offline** with a deterministic
mock provider:

```bash
uv run ordine run examples/chain/gen-images.yml --oneshot   # CSV rows → images
uv run ordine run examples/chain/png-cleanup.yml --oneshot  # images → named, transparent assets
```

A document-only variant (shell commands, no image steps) lives in
`examples/docs-pipeline/`.

Edit a prompt in `assets.csv` and rerun both: exactly one image
regenerates, flows through cleanup, and replaces its predecessor —
same filename, new content, neighbors untouched. Swap `provider: mock`
for `openai` when you want real generations.

## How it fits together

```
trigger (folder_watch / manifest / manual)
   └─ task (ordinal, exactly-once dedup)
        └─ steps: validate → transform → rename_from_manifest → export
             └─ on_failure: retries → recovery branches → escalating flags
```

Playbooks are YAML, versioned immutably with diffs and one-click revert.
The web editor, the CLI, and the AI features all drive the same core —
which never imports the LLM layer (enforced by tests), so pipeline runs
stay deterministic.

## Documentation

Start at [docs/README.md](docs/README.md): install, playbook reference,
triggers, the dry-run lab, AI features, security posture
([docs/security.md](docs/security.md) — read this before running
playbooks from strangers: **playbooks are code**), and the plugin guide
for writing your own steps.

## Contributing

Built by Constantin Vlad with an AI plan/audit/implement workflow —
see CONTRIBUTING.md for how plans, audits, and reviews drive every commit.
Dev setup, conventions, and the plan/audit workflow live in
[CONTRIBUTING.md](CONTRIBUTING.md). CI runs lint, types, 390+ tests, and
installs the built `.deb` on a clean Ubuntu container for every push.

## License

MIT — Copyright (c) 2026 Constantin Vlad / Antikatoptis Pareidolia.
