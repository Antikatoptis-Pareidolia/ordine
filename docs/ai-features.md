# AI features (Step 13)

Four opt-in features use the LLM connector. Text-model output never auto-saves, auto-runs, or auto-applies; image generation runs only when a playbook explicitly includes `llm.generate_image`.

## Features

| Feature | Trigger | Output |
|---------|---------|--------|
| Draft | Editor “Draft with AI” / `ordine draft` | Unsaved YAML in editor (or stdout/`--out`) |
| Diagnose | Task page / `ordine diagnose` | Structured cause + evidence in UI or JSON |
| Suggest branch | Flagged/failed task | Approval panel with semantic diff; Approve saves a new version |
| Generate image | `llm.generate_image` playbook step | PNG in the task workdir (mock/offline or OpenAI) |

## Privacy table (what leaves the machine)

| Data | Draft | Diagnose | Suggest branch | Generate image |
|------|-------|----------|----------------|----------------|
| Step catalog (ids + JSON Schema) | yes | yes | yes | no |
| Current playbook YAML | revise only | yes | yes | no |
| Task log tails (truncated) | no | yes | yes | no |
| Input image (optional checkbox) | no | optional | no | no |
| Manifest prompt | no | no | no | yes (remote provider only) |
| API keys / env secrets | **never in prompt** | **never in prompt** | **never in prompt** | sent only as provider auth |
| Other tasks' data | no | no | no | no |

Context is capped at 30,000 characters; oldest log sections drop first.

## Guarantees

- Every model output passes `loads_playbook` + `check_playbook` (drafts) or pydantic validation + full-playbook re-check (branches) before display.
- Draft robustness: one repair round-trip, then problems surface in the YAML tab — never raised for model badness.
- `apply_branch` runs **only** from explicit Approve; Step 10’s restart-to-apply badge handles runner refresh.
- Diagnosis persists as `{task_workdir}/_diagnosis/flag_{flag_id}.json` (no DB migration).
- All calls logged to `$DATA_DIR/llm_log/` with a `purpose` tag (`draft_playbook`, `diagnose_failure`, `repair_diagnose`, `suggest_branch`, etc.).
- Image generation enforces the process-wide `session_image_cap`; generated filenames remain contained in the task workdir.

## Prompt versioning

Prompt templates live in `src/ordine/llm/prompts.py` with `*_VERSION` constants. Bump the version when changing instructions materially.

## Local usage

```bash
ordine draft "watch ~/in, clear white, rename from assets.csv, export to ~/out"
ordine diagnose TASK_ID --json
```

Configure the LLM in [Settings](/settings) or `[llm]` in `config.toml`.

## Threat model (v1)

Text context includes only the user’s own playbook, task logs, and optionally one input image. Image generation sends the selected manifest prompt to its configured provider. No RAG or screenshots are collected. Callers must not paste secrets into descriptions or prompts.

## Out of scope

Auto-detection of recurring failures, streaming UI, background LLM queues, and GUI/screenshot context.
