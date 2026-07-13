# Chaining pipelines via folders

Step 14 connects **manifest-driven generation** to the existing **ordinal cleanup** pipeline through a handoff folder.

## Handoff pattern

```
assets.csv  →  [gen pipeline]  →  ~/renders/img_NNNN.png  →  [cleanup pipeline]  →  ~/game/assets/name.png
```

1. **Manifest trigger** — one task per CSV row; ordinal = row index; `dedup_key = mrow:{ordinal}:{sha256(name+prompt)[:32]}`
2. **`llm.generate_image`** — writes `img_{ordinal:04d}.png` into the task workdir (mock or OpenAI provider)
3. **`file.move`** — atomic publish into `~/renders` (downstream watcher never sees partial files)
4. **Cleanup pipeline** — `ordinal_regex: 'img_(\d+)\.png'` on the handoff folder; `file.rename_from_manifest` + export

## Dedup and regeneration

| Scenario | Behavior |
|----------|----------|
| Unchanged row | `dedup_key` matches an existing task → no new work (done forever) |
| Edited prompt | New `dedup_key`, same ordinal → regeneration task; reservation name unchanged |
| Edited name | New `dedup_key`, same ordinal → regeneration; `reserve_name` logs mismatch warning and keeps original name (documented limitation) |
| Appended row | New ordinal → new task + reservation |

Reservations are created **at task creation** (manifest sink wrapper), before any step runs.

Manifest triggers always enforce row-level exactly-once via the `mrow:` dedup keys on each
`TaskCandidate`, regardless of the playbook `dedup:` field. The chain example sets `dedup: none`
because file-hash or filename dedup does not apply — there is no watched file per row, only a
manifest row identity. Row unchanged ⇒ same `mrow:` key ⇒ `create_task` returns `None` and the row
stays done forever; edit the prompt or name and only that row is re-enqueued.

## Providers

`llm.generate_image` accepts `provider: mock | openai`. The `IMAGE_PROVIDERS` registry dict is the extension point for future backends (Stability, Replicate, etc.).

### Mock provider (first-class)

Deterministic Pillow PNG: white background, ordinal label, and a prompt-keyed accent band; output is byte-stable for the same prompt and ordinal. The accent band gives cleanup examples non-white content to preserve while removing the background. Use it for tests, CI, and offline rehearsal (`examples/chain/`).

### OpenAI provider

POST `{base}/v1/images/generations` with `response_format: b64_json`. Content-policy 400 responses map to `flag_kind=generation_refused` so operators can edit the manifest prompt and regenerate.

## Multi-pipeline serve

Register `gen-images.yml` and `png-cleanup.yml`, start both. Generation publishes into the folder the cleanup pipeline watches — no ledger-level cross-pipeline links.

## Failure modes

| Failure | Result |
|---------|--------|
| Manifest unreadable while polling | One `manifest_unreadable` flag (level 1) per bad mtime; service keeps polling |
| Row without prompt | Step fails cleanly; task flagged/skipped per policy |
| Missing manifest row | `flag_kind=manifest_exhausted` |
| Image budget exceeded | Fail before HTTP; queue continues |
| Generation refused (policy) | `flag_kind=generation_refused` with provider reason |

## Example

See [`examples/chain/README.md`](../examples/chain/README.md).
