# Pipeline editor

The web editor provides two synchronized views of the same playbook: a structured **form** (steps, params, trigger, recovery policy) and a raw **YAML** tab. Conversion is server-side; HTMX handles row add/remove fragments.

## Form field naming

Flat indexed names in `web/forms.py`:

| Field pattern | Purpose |
|---------------|---------|
| `name`, `description`, `engine`, `dedup` | Playbook metadata |
| `trigger-type`, `trigger-path`, `trigger-glob`, `trigger-settle_seconds`, `trigger-ordinal_regex`, `trigger-arrival_order_ordinals` | Trigger config |
| `steps-{i}-id` | Step id (select from registry) |
| `steps-{i}-params` | Step params as YAML textarea (empty → `{}`) |
| `steps-{i}-onfail-enabled`, `-retries`, `-then` | Per-step failure policy |
| `steps-{i}-onfail-branches-{j}-name`, `-retries` | Recovery branch metadata |
| `steps-{i}-onfail-branches-{j}-steps-{k}-id`, `-params` | Branch steps (no nested on_failure) |
| `onfail-*` | Pipeline-level failure policy (same shape) |
| `base_version` | Version the editor was opened from (hidden) |
| `note` | Optional version note on save |

Index gaps from row removal are tolerated; order follows ascending index.

Params stay YAML textareas — introspecting each step's `Params` model into typed inputs is future work.

## Comments not preserved

PyYAML `safe_load` discards comments. The YAML tab states this beside the textarea. Do not expect comment preservation through form round-trips; `dump_playbook` re-serializes from the parsed model.

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
| `GET .../versions/{pv}/diff?against={pv2}` | Unified diff (`difflib`) |
| `POST .../versions/{pv}/make-current` | Point pipeline at version |
| `POST .../versions/{pv}/revert` | Revert via new version |

New pipeline save uses `POST /pipelines` (same as dashboard paste, with `editor_save=1`).
