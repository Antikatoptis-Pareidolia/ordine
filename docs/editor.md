# Pipeline editor

The web editor provides two synchronized views of the same playbook: a structured **form** (steps, params, trigger, recovery policy) and a raw **YAML** tab. Conversion is server-side; HTMX handles row add/remove fragments.

## Form field naming

Flat indexed names in `web/forms.py`:

| Field pattern | Purpose |
|---------------|---------|
| `name`, `description`, `engine`, `dedup` | Playbook metadata |
| `trigger-type`, `trigger-path`, `trigger-glob`, `trigger-settle_seconds`, `trigger-poll_seconds`, `trigger-ordinal_regex`, `trigger-arrival_order_ordinals` | Trigger config |
| `steps-{i}-id` | Step id (select from registry) |
| `steps-{i}-params` | Step params as YAML textarea (empty → `{}`) |
| `steps-{i}-onfail-enabled`, `-retries`, `-then` | Per-step failure policy |
| `steps-{i}-onfail-branches-{j}-name`, `-retries` | Recovery branch metadata |
| `steps-{i}-onfail-branches-{j}-steps-{k}-id`, `-params` | Branch steps (no nested on_failure) |
| `onfail-*` | Pipeline-level failure policy (same shape) |
| `base_version` | Version the editor was opened from (hidden) |
| `from_lab` | Lab session id when fixing from a dry-run failure (hidden); suppresses auto-promotion to current on save |
| `note` | Optional version note on save |

Index gaps from row removal are tolerated; order follows ascending index.

Params stay YAML textareas — introspecting each step's `Params` model into typed inputs is future work.

## Comments not preserved

PyYAML `safe_load` discards comments. The YAML tab states this beside the textarea. Do not expect comment preservation through form round-trips; `dump_playbook` re-serializes from the parsed model.

## Default omission on save (normalization, not loss)

`dump_playbook` omits fields equal to their schema defaults when writing YAML. Examples:

- `settle_seconds: 2` on a `folder_watch` trigger disappears after a form-tab save because `2` is the default.
- Empty `on_failure`, default `dedup`, and default `engine` are likewise omitted.

The **stored** `yaml_text` on each version row reflects whatever serialization path created that version (paste vs form save). Semantically identical playbooks may look different in raw YAML; that is expected normalization, not data loss. Recovery branches, steps, and params are preserved (see `tests/test_branch_regression.py`).

The **Version note** field is always empty when the editor loads; it is never pre-filled from a prior version's note.

## Semantic diffs

`GET .../versions/{pv}/diff` compares versions by **meaning**, not raw text:

1. Each side is parsed with `loads_playbook` and re-serialized with `dump_playbook` before diffing.
2. The diff view is labeled **(formatting normalized)** when both sides parse successfully.
3. If a side fails to parse (should not happen for stored versions), that side falls back to raw `yaml_text`.
4. When canonical content is identical (e.g. metadata-only version rows with the same playbook), the view shows **no content changes (metadata-only version)** instead of an empty diff.

Stored `yaml_text` rows are never rewritten by the diff view — normalization applies only at display time.

### Structured change summary

The **What changed** card lists semantic edits detected by `web/diffing.py` on the parsed `Playbook` models: added/removed/changed badges for steps, params, branches, trigger fields, and pipeline metadata. Because the summary compares models, not raw YAML, compact vs long step serialization produces **no** spurious param items — only real edits appear.

Read the summary first for a quick audit; use the raw diff below for line-level context.

The summary is rendered directly above the side-by-side YAML diff.

### Side-by-side vs unified raw diff

The default **side-by-side** table aligns canonical YAML lines in two columns with line numbers and row coloring (green add, red delete, amber change). Append `?view=unified` for the classic unified `difflib` pre block. Both views work with JavaScript disabled.

## Version tree semantics

Every save creates an **immutable** `playbook_versions` row. There is no in-place edit and no version deletion.

```
pv_0001 (root)
└── pv_0002 (current)     ← edit + save while base == current
    └── pv_0003           ← revert to pv_0001 (parent = current)

pv_0001
├── pv_0002 (current)
└── pv_0004               ← branch-from: open pv_0001, save while current is pv_0002
    (parent = pv_0001, make_current=False)
```

- **Save:** `register_pipeline(..., parent_public_id=base_version, make_current=(base_version == current))`
- **Branch-from:** open an old version (`?version=pv_XXXX`); save with `base_version != current` → new version whose parent is the base, current unchanged, banner with **Make current**
- **Lab fix-from-here:** when the hidden `from_lab` field is present, `make_current` is forced **false** even if `base_version == current` — rehearse the fix in the lab, then promote from **History** (or the save banner's **Make current**) when ready
- **Revert:** new version copying target yaml, parent = current, note `revert to {pv}`, `make_current=True`
- **Make current:** `set_current_version`; if pipeline is running, flash "restart to apply"

## Routes

| Route | Purpose |
|-------|---------|
| `GET /pipelines/new` | Starter template |
| `GET /pipelines/{id}/edit?version=pv_XXXX` | Editor (default: current) |
| `POST .../edit/validate` | Validate only — never creates a version |
| `POST .../edit/to-yaml`, `/to-form` | Tab switch (refuses if invalid) |
| `POST .../edit/rows` | Add/remove step rows (HTMX) |
| `POST /pipelines/{id}/versions` | Save new version |
| `GET /pipelines/{id}/versions` | History tree |
| `GET .../versions/{pv}/diff?against={pv2}` | Semantic diff with change summary; side-by-side YAML by default (`?view=unified` for unified) |
| `POST .../versions/{pv}/make-current` | Point pipeline at version |
| `POST .../versions/{pv}/revert` | Revert via new version |

New pipeline save uses `POST /pipelines` (same as dashboard paste, with `editor_save=1`).
