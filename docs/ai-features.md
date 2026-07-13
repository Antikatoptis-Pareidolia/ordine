# AI features (Step 13)

Three user-initiated features on top of the Step 12 LLM connector. **The LLM drafts, never executes** — no auto-save, auto-run, or auto-apply.

## Features

| Feature | Trigger | Output |
|---------|---------|--------|
| Draft | Editor “Draft with AI” / `conveyor draft` | Unsaved YAML in editor (or stdout/`--out`) |
| Diagnose | Task page / `conveyor diagnose` | Structured cause + evidence in UI or JSON |
| Suggest branch | Flagged/failed task | Approval panel with semantic diff; Approve saves a new version |

## Privacy table (what leaves the machine)

| Data | Draft | Diagnose | Suggest branch |
|------|-------|----------|----------------|
| Step catalog (ids + JSON Schema) | yes | yes | yes |
| Current playbook YAML | revise only | yes | yes |
| Task log tails (truncated) | no | yes | yes |
| Input image (optional checkbox) | no | optional | no |
| API keys / env secrets | **never** | **never** | **never** |
| Other tasks' data | no | no | no |

Context is capped at 30,000 characters; oldest log sections drop first.

## Guarantees

- Every model output passes `loads_playbook` + `check_playbook` (drafts) or pydantic validation + full-playbook re-check (branches) before display.
- Draft robustness: one repair round-trip, then problems surface in the YAML tab — never raised for model badness.
- `apply_branch` runs **only** from explicit Approve; Step 10’s restart-to-apply badge handles runner refresh.
- Diagnosis persists as `{task_workdir}/_diagnosis/flag_{flag_id}.json` (no DB migration).
- All calls logged to `$DATA_DIR/llm_log/` with a `purpose` tag (`draft_playbook`, `diagnose_failure`, `suggest_branch`, etc.).

## Prompt versioning

Prompt templates live in `src/conveyor/llm/prompts.py` with `*_VERSION` constants. Bump the version when changing instructions materially.

## Local usage

```bash
conveyor draft "watch ~/in, clear white, rename from assets.csv, export to ~/out"
conveyor diagnose TASK_ID --json
```

Configure the LLM in [Settings](/settings) or `[llm]` in `config.toml`.

## Threat model (v1)

Context includes only the user’s own playbook, task logs, and optionally one input image. No remote/untrusted file ingestion, no RAG, no screenshots. Callers must not paste secrets into descriptions.

## Out of scope

Auto-detection of recurring failures, streaming UI, background queues, GUI/screenshot context (Steps 16–17), image generation (Step 14).
