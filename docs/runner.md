# Pipeline runner

The runner claims pending tasks from the ledger and executes playbook steps through the
configured engine. It implements retries, recovery branches, flag escalation, skip handling,
and position-keyed naming via `LedgerNamingService`.

## Per-task algorithm

`PipelineRunner.run_task(task)`:

1. Create `TaskWorkdir`, `ledger.set_workdir`, write initial `task.json`.
2. `input_path = Path(task.source_ref)` if it exists else `None`.
3. For each primary step `i` (1-based): `result = run_with_policy(step_i, i, input_path)`.
   - `ok` → `input_path = result.output_path or input_path`; next step.
   - `skip` → task → `skipped`; flag(level=1, kind=`result.flag_kind or "task_skipped"`,
     message incl. step id); finalize; return.
   - `fail` (all groups exhausted) → task → `flagged` if effective policy `then == "mark_failed"`,
     else `skipped`; finalize; return.
4. All steps ok → task → `done`; finalize (rewrite `task.json` with full step log) and return.

Any *unexpected* exception anywhere in 1–4: log traceback, task → `failed`, flag(level=1,
kind=`"runner_error"`), continue with the next task — the worker must never die because of one task.

## `run_with_policy`

```
policy = step.on_failure if step.on_failure is not None else playbook.on_failure
groups = [(None, policy.retries, [step])] + [(b.name, b.retries, b.steps) for b in policy.branches]
last = None
for branch_no, (branch, retries, seq) in enumerate(groups):          # branch_no 0 = primary
    for attempt_no in range(1, retries + 2):                          # retries = ADDITIONAL attempts
        attempt_id = ledger.start_attempt(task_id, branch, attempt_no)
        last, last_step_id = run_sequence(seq, index, branch, branch_no, step_input)
        ledger.finish_attempt(attempt_id, ok=(last.status == "ok"), last_step_id=..., error=last.message)
        if last.status == "ok":   return last
        if last.status == "skip": return last                         # skip short-circuits: no retries, no branches
    level = ledger.next_flag_level(task_id)                           # 1 for primary, 2 for first branch, ...
    ledger.raise_flag(pipeline_id, task_id=task_id, level=level,
                      kind=last.flag_kind or "task_failure",
                      message=f"step {step.id} [{branch or 'primary'}] exhausted after {retries + 1} attempt(s): {last.message}")
return last
```

## `run_sequence`

Every step in a branch sequence gets the *same starting input the failed primary step had* as the
sequence's input, then threads outputs internally. Each step gets
`workdir.step_dir(index_within_seq, step.id, branch=branch, branch_no=branch_no)` (primary:
`step_dir(index, id)`). Params are validated via `registry.validate_params`. Engine dispatch via
`EngineRegistry.get(playbook.engine).run_step(...)`. `ctx.naming = LedgerNamingService(...)`.
Logger from `workdir.step_logger`.

## Semantics decisions

| Situation | Outcome |
|---|---|
| Step-level `on_failure` present | It *fully replaces* the pipeline default for that step (no merging) |
| Recovery branch succeeds | Its final output feeds the *next primary step*; `tasks.current_branch` records the branch name |
| `skip` result | Immediate task `skipped` + level-1 flag; branches/retries are for failures, not skips |
| Exhaustion with `then: mark_failed` | Task status `flagged` (flags carry the levels; `failed` is reserved for runner errors / reconcile-fail) |
| Exhaustion with `then: skip` | Task status `skipped` + the escalation flags already raised |
| User retries a `flagged`/`failed` task | Status → `pending` (Step 3 rule); on re-run, prior `branch_attempts` remain (history), escalation continues from `next_flag_level` |

## `task.json` schema

Written at claim; rewritten at terminal:

```json
{
  "task_id": 1,
  "pipeline": "png-cleanup",
  "playbook_version": "pv_0001",
  "source_ref": "...",
  "ordinal": 7,
  "status": "done",
  "steps": [
    {
      "seq": 1,
      "id": "image.white_to_alpha",
      "branch": null,
      "attempt": 1,
      "status": "ok",
      "output": ".../01_image.white_to_alpha/x.png",
      "message": null,
      "started_at": "...",
      "finished_at": "..."
    }
  ],
  "created_at": "...",
  "finished_at": "..."
}
```

## `PipelineService`

Composes reconcile (default `stale_after=15min`, `policy='retry'`), trigger start (`ledger_sink`,
arrival flag from trigger spec), and a worker thread looping `run_once` with idle sleep 0.5s.
`start()` / `stop()`; `stop` is graceful (finishes the in-flight task, then stops trigger). Never
uses asyncio.
