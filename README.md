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

<!-- DEMO GIF: demo/demo.tape recording goes here -->

## Why Ordine

You have a CSV of asset names and prompts. You want images generated for
each row, cleaned up (white background → transparent, cropped to
content), named **exactly** by their row — even when rows 3–6 fail — and
delivered to your game folder. Unattended. Resumable after a crash.
Fixable from the browser when something breaks at 2 AM.

That workflow is Ordine's founding use case, and it ships as the
built-in example. But nothing in the engine knows about images: any
watch-a-folder → transform → deliver workflow fits.

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

## Quickstart (from source, ~3 minutes)

```bash
git clone https://github.com/Antikatoptis-Pareidolia/ordine.git
cd ordine && uv sync
uv run ordine example ~/ordine-demo
uv run ordine run ~/ordine-demo/png-cleanup.yml --oneshot
```

Five sample images are validated, made transparent, trimmed, renamed
from `assets.csv`, and exported to `exports/`. Then start the web UI:

```bash
uv run ordine serve   # → http://127.0.0.1:8484
```

Press **Start** on a pipeline, drop a file into its watch folder, and
watch the task appear, process, and land — or flag, diagnose, and heal.

> After the first release: `pipx install ordine` or the `.deb` from
> [Releases](https://github.com/Antikatoptis-Pareidolia/ordine/releases)
> replace the clone.

## The chain example

The full founding workflow — manifest → image generation → cleanup —
ships in `examples/chain/` and runs **offline** with a deterministic
mock provider:

```bash
uv run ordine run examples/chain/gen-images.yml --oneshot   # CSV rows → images
uv run ordine run examples/chain/png-cleanup.yml --oneshot  # images → named, transparent assets
```

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

Dev setup, conventions, and the plan/audit workflow live in
[CONTRIBUTING.md](CONTRIBUTING.md). CI runs lint, types, 390+ tests, and
installs the built `.deb` on a clean Ubuntu container for every push.

## License

MIT — Copyright (c) 2026 Constantin Vlad / Antikatoptis Pareidolia.
