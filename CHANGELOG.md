# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-14

### Added

- `shell.run` built-in step — arbitrary shell commands with template placeholders, captured stdout/stderr, and optional declared output files
- `examples/docs-pipeline/` — document stamping chain (shell → manifest rename → move)
- `ordine --version` prints the installed package version and exits 0
- README demo GIF (`demo/demo.gif`) recorded from `demo/demo.tape`

### Changed

- README reframes Ordine as a universal watch → transform → deliver engine; image/CSV workflow remains the founding example
- README quickstart correctly describes six sample images in the built-in example

## [0.1.0] - 2026-07-14

### Added

- Repo scaffold, tooling, CI (Step 1)
- Playbook schema, loader, JSON Schema export, and validation tests (Step 2)
- SQLite ledger with task state machine, branch attempts, flags, and name reservations (Step 3)
- Step contract, plugin registry, headless engine, and task workdirs (Step 4)
- Headless image steps (`image.validate`, `image.white_to_alpha`, `image.trim`, `image.export`) with ImageMagick/Pillow backends (Step 5)
- Folder watch and manual triggers with settle detection, startup rescan, and ledger sink (Step 6)
- `Ledger.create_task_arrival` — atomic arrival-order ordinal assignment + insert (planned Step 3 amendment, landed Step 6)
- Pipeline runner with retries, recovery branches, flag escalation, manifest naming, and `PipelineService` (Step 7)
- `file.rename_from_manifest` and `file.move` built-in steps; `LedgerNamingService`; job manifest parsing (CSV/JSON/txt)
- Typer CLI (`ordine`) with XDG app config, `--json` output contract, and `run --oneshot` (Step 8)
- Ledger read helpers for CLI (`list_pipelines`, `find_pipeline_id`, `list_branch_attempts`, `list_open_flags`)
- `StepRegistry.list_step_metadata()` for plugin visibility
- FastAPI web UI (`ordine serve`) with ServiceManager, dashboard, tasks, flags, settings, and vendored HTMX 2.0.4 (Step 9)
- Pipeline editor with form ⟷ YAML conversion, immutable version history, diffs, revert, and branch-from-version (Step 10)
- `dump_playbook()` in core with round-trip guarantee; `docs/editor.md`
- Form-tab recovery branch editing (step-level and pipeline-level) with HTMX add/remove fragments (Step 10 follow-up)
- Semantic version diffs, metadata-only diff notice, and editor form labeling polish (Step 10 follow-up 2)
- Structured change summary and side-by-side diff view (`web/diffing.py`; Step 10 follow-up 3)
- Dry-run lab with step-through, sandbox isolation, checkpoint resume, and `ordine dry-run` CLI (Step 11)
- Provider-agnostic LLM connector: raw REST adapters (Anthropic/OpenAI/compatible), keyring keys, JSONL audit log, token budget, settings UI, and `ordine llm check` (Step 12)
- AI drafting, failure diagnosis, and learned recovery branches with explicit approval only (Step 13)
- Manifest trigger (`ManifestTriggerService`) with row ordinals, mrow dedup keys, and reservation-at-creation sink (Step 14)
- `llm.generate_image` step with mock and OpenAI image providers, `ImageBudget`, and `session_image_cap` config (Step 14)
- Chained example under `examples/chain/` and `docs/chaining.md` (Step 14)
- Workdir retention cleanup (`ordine cleanup`, `[retention]` config, `Ledger.clear_workdir`) (Step 15)
- `ordine example` quickstart scaffolder with CI-guaranteed oneshot test (Step 15)
- Release tooling: `scripts/bump_version.py`, `scripts/build_deb.sh`, `release.yml`, deb-smoke CI, version sync test (Step 15)
- Docs index, install/security/release guides, community files, `demo/demo.tape`, naming checklist (Step 15)

### Changed

- Product renamed to **Ordine**: PyPI/deb/CLI/import package `ordine`, config/data dirs, keyring service, entry-point groups `ordine.steps`/`ordine.engines`, CI Python matrix via `uv python install` (Step 15 naming)
- Pre-public polish completed for service transitions, runtime checks under `python -O`, logging, and duplicate cleanup
- Repository identity transferred to `Antikatoptis-Pareidolia/ordine` with final license attribution and release URLs
- Final audit hardening added output-name containment, full-origin POST checks, HTMX restrictions, secret redaction, first-contact errors, documentation truth fixes, packaging assertions, and per-core-module coverage floors
- Pillow white-to-alpha uses `get_flattened_data()`; diagnosis repair calls use `repair_diagnose` purpose tag (Step 15)
- Flags inbox shows one-line hints per known flag kind (Step 15)

- Step 14 follow-up: manifest `build_trigger_service` rejects caller sinks; `manifest_sink` caches rows by mtime; trigger/chaining docs corrected
- Chain example uses `on_collision: replace` for regeneration; `ordinal_regex` skips non-matching files with explicit warning
- Mock image provider varies PNG bytes with prompt (sha256 fill + prompt text); deterministic for same ordinal/prompt/size
- Mock image provider keeps a white background with a prompt-keyed accent band so chain cleanup rehearsals exercise white_to_alpha/trim
- Test convention rule 21: no patching the subject under assertion; monkeypatch reserved for externals or delegating counters
- Step 13 follow-up: web wiring scope convention, full-stack draft MockTransport test, CI `llm_live` filter

### Fixed

- Deb build uses fpm `--deb-recommends` (not invalid `--recommends`); deb-smoke CI logs `fpm --version` (Step 15 follow-up)
- CI deb-smoke container steps use `shell: bash` so `set -euo pipefail` works (Step 15 follow-up 3)
- `/usr/bin/ordine` deb symlink uses absolute `/opt/ordine/bin/ordine`; build asserts package contents (Step 15 follow-up 4)
- Deb build/CI assertions capture producer output before `grep -q`/`head` to avoid SIGPIPE under pipefail (Step 15 follow-up 5)
- Deb venv uses `--copies` (real interpreter binary); removed self-referential python3 symlink (Step 15 follow-up 6)
- Sigint integration test prints full subprocess stderr on failure (Step 15 follow-up 6)
- Lab dry-run ordinals now mirror production trigger semantics (no false pass for ordinal-dependent playbooks)
- Drafting prompts instruct models to configure ordinal sources when manifest/numbered files are implied
- Dry-run and lab runner-equivalent paths now report real step failure messages (not "no attempts executed") when the primary attempt fails before branch retries
- Flag escalation groups primary attempts by failing step (`last_step_id`) so multi-step tasks raise level 1+ flags correctly
- Ladder-scoped flag escalation counts only the failing step's primary and branch groups; unfinished attempt rows are ignored
- Recovery branch names must be unique across the entire playbook (not only within one `on_failure` block)
- Dashboard start/pause shows pending disabled state; paused pipelines display "last ran" version wording

- Startup rescan seeds the settle tracker instead of emitting partial files (Step 6 follow-up)
