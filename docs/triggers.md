# Triggers

Triggers turn filesystem arrivals into ledger tasks. Step 6 delivers `folder_watch` (with settle
detection), `manual` scan, and startup rescan. The runner (Step 7) claims and executes pending tasks;
triggers only insert them.

## Overview

| Trigger | Behavior |
|---------|----------|
| `folder_watch` | Watch a directory, settle files, emit `TaskCandidate`s continuously |
| `manual` | One-shot scan of a directory |
| `manifest` | Step 14 — `build_trigger_service` raises `TriggerError` |

Each trigger produces `TaskCandidate` records handed to a **sink**. The usual sink is `ledger_sink`,
which calls `Ledger.create_task` (or `Ledger.create_task_arrival` for arrival-order ordinals).
Duplicate dedup keys are dropped silently at DEBUG level.

## Settle detection (`folder_watch`)

A file is emitted only after:

1. Its size has been unchanged for `settle_seconds`, and
2. It can be opened for read.

Half-written files are never ingested. Watchdog handlers only update a settle tracker; hashing and sink
calls run in a dedicated poller thread.

Poller interval: `min(0.5, settle_seconds / 2)` (or `0.05` when `settle_seconds` is zero).

Files that disappear while settling are dropped. A file modified after emission is tracked again and
re-emitted after settling — under `content_hash` dedup a content change yields a new task; under
`filename` dedup the ledger drops the duplicate.

## Startup rescan

On `FolderWatchService.start()`, every matching file already on disk is **seeded into the settle
tracker** (sorted by name) — rescan does not emit directly. The poller emits each seeded file only
after the same stability and readability checks used for live arrivals. This prevents a restart
mid-write from hashing a partial file into a phantom task.

The ledger dedup key drops files already ingested — this reconciles folder contents after a crash or
restart. Files that arrived while the process was down become tasks once settled; known files are
ignored on re-emit.

`FolderWatchService.rescan()` seeds the settle tracker on demand (useful after recovery).

`ManualScanService` performs its own per-file size-stable and openable check before emitting (no
background poller).

## Ordinal sources

| Source | Config | Ordinal value |
|--------|--------|---------------|
| Regex | `ordinal_regex` with one capture group | `int` from group 1 on the bare filename; no match logs WARNING |
| Arrival order | `arrival_order_ordinals: true` | Assigned by `ledger_sink` via `Ledger.create_task_arrival` |
| None | neither set | `ordinal` is `null` |

Regex and arrival order are mutually exclusive (enforced by the playbook schema).

## Ignore rules

Applied **after** the playbook glob:

| Rule | Rationale |
|------|-----------|
| Hidden files (`name` starts with `.`) | Skip dotfiles |
| Temp exports (`name` starts with `.tmp-`) | Upstream `image.export` writes atomically via `.tmp-*`; downstream watchers must never ingest these |
| Directories | Only files are tasks |

Critical for chained pipelines: a downstream `folder_watch` must not pick up an upstream temp file.

## Dedup keys

| Playbook `dedup` | Key format |
|------------------|------------|
| `content_hash` | `sha256:{hex}` (streamed, 1 MiB chunks) |
| `filename` | `name:{basename}` |
| `none` | `null` |

## Arrival-order ordinals

When `arrival_order_ordinals` is enabled, `ledger_sink(..., arrival_order=True)` calls
`Ledger.create_task_arrival`, which atomically computes `max(ordinal)+1` and inserts the task in one
`BEGIN IMMEDIATE` transaction. This closes the race between separate ordinal read and insert calls.

## Supervision note

Exceptions inside watchdog/poller threads are logged with `logger.exception`. After five consecutive
poller crashes, the service stops itself and logs CRITICAL. Raising to the runner belongs to Step 7
supervision.

## Known limitations (v1)

- Top-level directory only — glob applies to immediate children, not recursive subdirectories.
- Default OS observer only — no polling fallback for network filesystems; inotify limits not tuned.
- Manifest trigger is not implemented (Step 14).

## Manual smoke test

```python
import tempfile
import time
from pathlib import Path

from conveyor.core.db import create_engine_for, init_db
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import FolderWatchTrigger, load_playbook
from conveyor.core.triggers import FolderWatchService, ledger_sink

watch = Path(tempfile.mkdtemp())
db = watch / "ledger.db"
engine = create_engine_for(db)
init_db(engine)
ledger = Ledger(engine)
playbook = load_playbook("tests/fixtures/playbooks/valid/v01_minimal.yml")
pipeline_id, _ = ledger.register_pipeline(playbook, Path("tests/fixtures/playbooks/valid/v01_minimal.yml").read_text())

spec = FolderWatchTrigger(type="folder_watch", path=str(watch), glob="*", settle_seconds=2.0)
service = FolderWatchService(spec, "content_hash", ledger_sink(ledger, pipeline_id))
service.start()
# Copy a large PNG into *watch* with your file manager; task appears once after copy completes.
time.sleep(5)
service.stop()
print(ledger.counts(pipeline_id))
```

Kill the process mid-copy, restart the snippet — rescan picks up the completed file exactly once.
Drop `partial.tmp-notused` and `.hidden.png` — ignored when glob is `*`.
