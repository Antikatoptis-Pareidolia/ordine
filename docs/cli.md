# Conveyor CLI

Thin command-line interface over the Conveyor core. The CLI parses arguments, loads configuration, calls ledger/runner/registry APIs, and formats output. It contains no pipeline business logic.

Install the package (`uv pip install -e .` or `pip install -e .`), then invoke `conveyor`.

## Global options

| Option | Description |
|--------|-------------|
| `--config PATH` | Config TOML path (overrides `$CONVEYOR_CONFIG` and the default file) |
| `-v`, `--verbose` | Debug logging on **stderr** only |

Config precedence: `--config` > `$CONVEYOR_CONFIG` > `$XDG_CONFIG_HOME/conveyor/config.toml` > built-in defaults (no file required).

## Output contract

All human-readable output is **plain text by design** (fixed-width tables and simple lines); the CLI does not use Rich or other TUI rendering even though Typer may install Rich as a transitive dependency.

- Human-readable tables and lines go to **stdout**.
- With `--json`, **stdout** carries exactly one JSON object and nothing else.
- All logging goes to **stderr**, including when `-v` is set.
- JSON keys are `snake_case`. Timestamps are ISO-8601 UTC strings.

## Commands

### `conveyor init [--config PATH]`

Create the config file (default or `--config` path), database parent directory, and workdir root. Refuses to overwrite an existing config file.

Exit codes: `0` success; `1` if the config file already exists.

### `conveyor check PLAYBOOK [--json]`

Load and statically validate a playbook (`load_playbook` + `StepRegistry.check_playbook`).

Exit codes: `0` valid; `1` validation problems; `2` unreadable or syntactically invalid.

Plain output on success:

```
png-cleanup: valid (4 steps, trigger=folder_watch)
```

**`--json` shape:**

```json
{
  "valid": true,
  "problems": [
    {"path": "steps.0.id", "message": "unknown step id: unknown.step"}
  ]
}
```

### `conveyor run PLAYBOOK [--oneshot] [--note TEXT] [--json]`

Load and check the playbook, auto-register a playbook version when YAML text differs from the current stored version (unchanged YAML does not create a new version), reconcile stale tasks, then:

- **`--oneshot`**: manual scan (or rescan for `folder_watch` playbooks via the manual-scan path), `run_until_idle`, print summary, exit.
- **Default**: start `PipelineService` in the foreground until SIGINT/SIGTERM. First signal stops gracefully (finish in-flight task, stop trigger); second signal exits immediately with code `130`.

Exit codes: `0` success; `1` check problems; `2` runtime error.

**`--oneshot --json` shape:**

```json
{
  "pipeline": "cli-game-assets",
  "version": "01HX…",
  "scanned": 5,
  "processed": 5
}
```

**Default mode stop `--json` shape:**

```json
{
  "pipeline": "cli-game-assets",
  "version": "01HX…",
  "status": "stopped"
}
```

### `conveyor status [--json]`

Summarize all registered pipelines. Plain output lists every **non-zero** task status count, plus open-flag totals.

Example plain line:

```
cli-game-assets pv_0001 done=3 skipped=2 flags=2 max_level=1
```

**`--json` shape:**

```json
{
  "pipelines": [
    {
      "name": "cli-game-assets",
      "current_version": "01HX…",
      "counts": {
        "pending": 0,
        "processing": 0,
        "done": 5,
        "skipped": 0,
        "failed": 0,
        "flagged": 0
      },
      "open_flags": 0,
      "max_flag_level": 0
    }
  ]
}
```

### `conveyor tasks PIPELINE [--status STATUS] [--limit N] [--json]`

List tasks for a pipeline.

**`--json` shape:**

```json
{
  "pipeline": "cli-game-assets",
  "tasks": [
    {
      "id": 1,
      "ordinal": 1,
      "status": "done",
      "source": "img_0001.png",
      "updated_at": "2026-07-10T19:00:00+00:00"
    }
  ]
}
```

### `conveyor task ID [--json]`

Show one task with branch attempts and open flags (ledger reads only).

Plain output example:

```
task 2 status=skipped ordinal=2
source: /tmp/in/img_0002.png
workdir: /tmp/workdirs/2
skip: cannot identify image file '/tmp/in/img_0002.png'
attempts:
  #1 branch=- ok=false last=image.validate error=cannot identify image file '/tmp/in/img_0002.png'
flags:
  #1 level=0 kind=corrupt_input cannot identify image file '/tmp/in/img_0002.png'
```

**`--json` shape:**

```json
{
  "id": 1,
  "pipeline_id": 1,
  "status": "done",
  "ordinal": 1,
  "source_ref": "/tmp/in/img_0001.png",
  "workdir": "/tmp/workdirs/1",
  "current_branch": null,
  "attempts": 1,
  "error": null,
  "created_at": "2026-07-10T19:00:00+00:00",
  "updated_at": "2026-07-10T19:00:01+00:00",
  "branch_attempts": [
    {
      "id": 1,
      "branch": null,
      "attempt": 1,
      "ok": true,
      "last_step_id": "image.export",
      "error": null,
      "finished_at": "2026-07-10T19:00:01+00:00"
    }
  ],
  "flags": []
}
```

### `conveyor retry ID [--json]`

Re-queue a failed or flagged task (`transition` to `pending`).

Exit codes: `0` success; `1` illegal transition or not found.

**`--json` shape:**

```json
{
  "id": 3,
  "status": "pending"
}
```

### `conveyor flags [--pipeline NAME] [--min-level N] [--json]`

List open flags.

**`--json` shape:**

```json
{
  "flags": [
    {
      "id": 1,
      "pipeline_id": 1,
      "level": 1,
      "kind": "manifest_exhausted",
      "task_id": 6,
      "message": "no manifest name for ordinal 6",
      "age": "2m",
      "created_at": "2026-07-10T19:00:00+00:00"
    }
  ]
}
```

### `conveyor resolve-flag ID --note TEXT [--json]`

Resolve an open flag.

Exit codes: `0` success; `1` not found.

**`--json` shape:**

```json
{
  "id": 1,
  "resolved": true,
  "resolution": "ignored for now"
}
```

### `conveyor steps [--json]`

List registered step plugins.

**`--json` shape:**

```json
{
  "steps": [
    {
      "id": "image.validate",
      "engines": ["headless"],
      "origin": "conveyor.executors.headless.steps.ValidateStep"
    }
  ]
}
```

## Flagship walkthrough

```bash
# Isolated config and data under ./local
export XDG_CONFIG_HOME="$PWD/local/config"
export XDG_DATA_HOME="$PWD/local/data"
mkdir -p inbox out

conveyor init
conveyor check tests/fixtures/playbooks/valid/v02_flagship.yml

# Prepare five images and a manifest (example)
python - <<'PY'
from pathlib import Path
from PIL import Image
watch = Path("inbox")
watch.mkdir(exist_ok=True)
for i in range(1, 6):
    p = watch / f"img_{i:04d}.png"
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    img.save(p)
Path("assets.csv").write_text("name\ngoat.png\njug.png\ncrown.png\nring.png\nsword.png\n")
PY

# Edit a manual-trigger copy of the playbook pointing at inbox/, assets.csv, out/
conveyor run my-playbook.yml --oneshot
conveyor status --json | jq .
conveyor tasks my-pipeline --json | jq .
conveyor task 1 --json | jq .
```

## Exit code summary

| Command | 0 | 1 | 2 |
|---------|---|---|---|
| `init` | created | config exists | — |
| `check` | valid | problems | unreadable |
| `run` | success | check problems | runtime |
| `status` | always | — | — |
| `tasks` | always | unknown pipeline | — |
| `task` | found | not found | — |
| `retry` | re-queued | illegal / not found | — |
| `flags` | always | unknown pipeline | — |
| `resolve-flag` | resolved | not found | — |
| `steps` | always | — | — |
| global `--config` | — | — | config error |

Second SIGINT/SIGTERM during `run` (default mode): exit `130`.
