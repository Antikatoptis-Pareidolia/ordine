# Playbook Reference

Version 1 playbook format for Ordine pipelines. YAML documents are validated against `playbook.schema.json`.

## Step forms

Steps accept three YAML shapes, normalized to `{id, params, on_failure}`:

1. **String** — `image.trim`
2. **Single-key mapping** — `image.white_to_alpha: {fuzz: 8}` (value may be `{}` or omitted as null)
3. **Long form** — `{id: image.trim, params: {...}, on_failure: {...}}`

If a single-key mapping uses a reserved key (`id`, `params`, `on_failure`), it is treated as long form.

## Flag escalation

When a step or pipeline fails, `on_failure.retries` are exhausted first (flag level 1). Each recovery branch is then tried in order; when a branch's retries are exhausted, the flag level rises by 1. There is no manual `escalate` field.

## Playbook

| Field | Type | Description |
|---|---|---|
| `version` | `1` | Schema version (required) |
| `name` | slug | Pipeline identifier |
| `description` | string | Optional human description (default absent) |
| `trigger` | Trigger | How tasks enter the pipeline |
| `dedup` | enum | `content_hash` (default), `filename`, or `none` |
| `engine` | slug | Executor engine name (default `headless`) |
| `steps` | StepSpec[] | Ordered steps (min 1) |
| `on_failure` | FailurePolicy | Pipeline-level failure policy (defaults to no retries/branches, then `mark_failed`) |
| `meta` | PlaybookMeta | Authoring version metadata |

## StepSpec

| Field | Type | Description |
|---|---|---|
| `id` | step id | Dotted lowercase id, e.g. `image.trim` |
| `params` | object | Opaque step parameters (default `{}`; validated by the step registry) |
| `on_failure` | FailurePolicy | Optional step-level failure policy (default absent) |

## FailurePolicy

| Field | Type | Description |
|---|---|---|
| `retries` | int ≥ 0 | Primary retries before branches (default `0`) |
| `branches` | RecoveryBranch[] | Ordered recovery branches (default `[]`) |
| `then` | enum | `mark_failed` (default) or `skip` after all branches are exhausted |

## RecoveryBranch

| Field | Type | Description |
|---|---|---|
| `name` | slug | Branch name; must be unique across every step and pipeline policy in the playbook |
| `retries` | int ≥ 0 | Retries for this branch's step sequence (default `0`) |
| `steps` | StepSpec[] | Alternative steps (min 1; no nested `on_failure`) |

## Triggers

### folder_watch

| Field | Type | Description |
|---|---|---|
| `type` | `folder_watch` | Discriminator |
| `path` | string | Directory to watch |
| `glob` | string | File glob (default `*`) |
| `settle_seconds` | float ≥ 0 | Wait for stable file size (default 2) |
| `ordinal_regex` | string | Regex with one capture group for filename ordinal |
| `arrival_order_ordinals` | bool | Opt-in arrival-order ordinal fallback |

`ordinal_regex` and `arrival_order_ordinals` are mutually exclusive.

### manual

Same fields as `folder_watch` except `type: manual` and no `settle_seconds`.

### manifest

| Field | Type | Description |
|---|---|---|
| `type` | `manifest` | Discriminator |
| `path` | string | Job manifest file (.csv / .json / .txt) |
| `poll_seconds` | float ≥ 0 | Re-read interval (default `30`); `0` = scan once at start |

## Built-in steps

### `shell.run`

| Param | Type | Default | Description |
|---|---|---|---|
| `cmd` | string | (required) | Shell command; placeholders `{input}`, `{step_dir}`, `{ordinal}`, `{source}` |
| `timeout_seconds` | float > 0 | `120` | Wall-clock timeout |
| `expect_exit` | int | `0` | Required exit code |
| `output` | string | absent | Basename under `step_dir` that the command must create; becomes `output_path` |

When `output` is omitted, the step passes through `input_path`. See [security.md](security.md#shellrun).

## PlaybookMeta

| Field | Type | Description |
|---|---|---|
| `version_id` | string | This playbook version id |
| `parent_version_id` | string | Parent version id when branched |
