# Workdir Layout

Per-task artifact directories are created under an explicit root passed by the runner (Step 7)
or tests. Layout is a **public contract** — the UI and audit tools depend on it.

```
{workdir_root}/{pipeline_name}/task_{task_id:06d}/
├── task.json                    # written by the runner (Step 7) — NOT by individual steps
├── 01_image.white_to_alpha/
│   ├── log.txt
│   └── ...step outputs...
├── 02_image.trim/
└── b1_fallback-pillow/01_.../   # recovery-branch attempts get a 'b{n}_{branch}' prefix dir
```

## Directory naming

| Part | Pattern | Notes |
|---|---|---|
| Pipeline | sanitized playbook `name` | Non `[a-z0-9._-]` → `_` |
| Task | `task_{id:06d}` | Zero-padded task id |
| Step | `{index:02d}_{step_id}` | Step id keeps dots; other unsafe chars → `_` |
| Branch prefix | `b{n}_{branch_name}` | Sanitized branch slug under the task dir |

## Who writes what

| Path | Writer |
|---|---|
| Task directory | `TaskWorkdir.create()` |
| Step directories | `TaskWorkdir.step_dir()` |
| `log.txt` | `TaskWorkdir.step_logger()` (called by runner/engine) |
| Step outputs | The step's `run()` method, under `ctx.step_dir` |
| `task.json` | Pipeline runner (Step 7) |

## API

```python
from pathlib import Path
from ordine.core.workdir import TaskWorkdir

workdir = TaskWorkdir.create(Path("~/ordine-work"), "png-cleanup", task_id=42)
step_dir = workdir.step_dir(1, "image.white_to_alpha")
logger = workdir.step_logger(step_dir)
branch_dir = workdir.step_dir(1, "image.export", branch="fallback-pillow", branch_no=1)
```

All `create` / `step_dir` calls are idempotent (`mkdir(parents=True, exist_ok=True)`).

## Step data flow

Steps receive `ctx.input_path` (current artifact, read-only) and return `StepResult.output_path`.
`None` output means passthrough — the next step sees the same input path.

## Retention cleanup

Terminal task workdirs can grow without bound. `ordine cleanup` (and optional serve-start retention) deletes on-disk directories only — **never** export destinations or ledger rows beyond clearing `tasks.workdir`.

| Config (`[retention]`) | Default | Meaning |
|---|---|---|
| `days` | `30` | Delete workdirs for tasks finished longer ago |
| `keep_failed` | `true` | Keep `failed` and `flagged` workdirs as evidence |
| `on_serve_start` | `false` | Run cleanup once when `ordine serve` starts |

```bash
ordine cleanup --dry-run --json
ordine cleanup --days 7 --include-failed
```

After cleanup, task detail shows **workdir cleaned** when the path was cleared. Implementation: `ordine.core.retention.cleanup_workdirs` + `Ledger.clear_workdir`.
