# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- Typer CLI (`conveyor`) with XDG app config, `--json` output contract, and `run --oneshot` (Step 8)
- Ledger read helpers for CLI (`list_pipelines`, `find_pipeline_id`, `list_branch_attempts`, `list_open_flags`)
- `StepRegistry.list_step_metadata()` for plugin visibility
- FastAPI web UI (`conveyor serve`) with ServiceManager, dashboard, tasks, flags, settings, and vendored HTMX 2.0.4 (Step 9)
- Pipeline editor with form ⟷ YAML conversion, immutable version history, diffs, revert, and branch-from-version (Step 10)
- `dump_playbook()` in core with round-trip guarantee; `docs/editor.md`
- Form-tab recovery branch editing (step-level and pipeline-level) with HTMX add/remove fragments (Step 10 follow-up)
- Semantic version diffs, metadata-only diff notice, and editor form labeling polish (Step 10 follow-up 2)
- Structured change summary and side-by-side diff view (`web/diffing.py`; Step 10 follow-up 3)
- Dry-run lab with step-through, sandbox isolation, checkpoint resume, and `conveyor dry-run` CLI (Step 11)
- Provider-agnostic LLM connector: raw REST adapters (Anthropic/OpenAI/compatible), keyring keys, JSONL audit log, token budget, settings UI, and `conveyor llm check` (Step 12)
- AI drafting, failure diagnosis, and learned recovery branches with explicit approval only (Step 13)
- Manifest trigger (`ManifestTriggerService`) with row ordinals, mrow dedup keys, and reservation-at-creation sink (Step 14)
- `llm.generate_image` step with mock and OpenAI image providers, `ImageBudget`, and `session_image_cap` config (Step 14)
- Chained example under `examples/chain/` and `docs/chaining.md` (Step 14)

### Changed

- Step 14 follow-up: manifest `build_trigger_service` rejects caller sinks; `manifest_sink` caches rows by mtime; trigger/chaining docs corrected
- Chain example uses `on_collision: replace` for regeneration; `ordinal_regex` skips non-matching files with explicit warning
- Mock image provider varies PNG bytes with prompt (sha256 fill + prompt text); deterministic for same ordinal/prompt/size
- Step 13 follow-up: web wiring scope convention, full-stack draft MockTransport test, CI `llm_live` filter

### Fixed

- Lab dry-run ordinals now mirror production trigger semantics (no false pass for ordinal-dependent playbooks)
- Drafting prompts instruct models to configure ordinal sources when manifest/numbered files are implied
- Dry-run and lab runner-equivalent paths now report real step failure messages (not "no attempts executed") when the primary attempt fails before branch retries
- Flag escalation groups primary attempts by failing step (`last_step_id`) so multi-step tasks raise level 1+ flags correctly
- Ladder-scoped flag escalation counts only the failing step's primary and branch groups; unfinished attempt rows are ignored
- Recovery branch names must be unique across the entire playbook (not only within one `on_failure` block)
- Dashboard start/pause shows pending disabled state; paused pipelines display "last ran" version wording

### Fixed

- Startup rescan seeds the settle tracker instead of emitting partial files (Step 6 follow-up)
