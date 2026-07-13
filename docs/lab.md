# Dry-run lab

The dry-run lab rehearses a playbook on **copied sample files** in a fully isolated sandbox. It is the authoring checkpoint surface: step through execution, inspect artifacts, and on failure branch into the editor to fix from the last good step, then resume with the prefix replayed.

## Isolation guarantees

| Guarantee | Mechanism |
|-----------|-----------|
| Production DB untouched | Sessions use an ephemeral in-memory SQLite `Ledger` (`StaticPool`); no writes to `config.db_path` |
| Source samples unchanged | Files are **copied** into `sandbox/samples/`; originals are read-only |
| Real output dirs protected | Every step `OUTPUT_DIR_PARAMS` destination is rewritten to `sandbox/outputs/{basename}/`; mappings are shown on the setup page |
| Sandbox cleanup | `close()` and app shutdown delete the session tree under `{workdir_root}/lab/` |

Artifact caching across resumed sessions is **not** implemented in v1 — `resume()` replays the validated prefix on the new playbook (≤20 samples).

## Lab vs runner semantics

| Control | Semantics |
|---------|-----------|
| **Next step** | One primary step only — no retries, no branches; failure **pauses** |
| **Retry** | Re-run the paused primary step once |
| **Run branches** | Execute declared recovery branches with runner-equivalent retries |
| **Run to end / Run all** | Runner-equivalent (primary retries + auto-branches); stops on exhaustion |

Do not expect lab step-through to mirror production retry behavior — that difference is intentional.

## Checkpoint walkthrough (step 3 of 5)

1. Start a lab session from **Versions → Lab** (or `/pipelines/{id}/lab`).
2. **Next step** through steps 1–2 (ok), then step 3 fails and the timeline pauses.
3. Click **Fix from here** → editor opens at `?version={session version}&anchor=steps-2&from_lab={sid}`.
4. Edit step 3, save as a branch-from version. Saves that carry `from_lab` are **never** auto-promoted to current — rehearse the fix in the lab first, then promote deliberately from **History** when ready.
5. Click **Resume lab** on the editor banner → `POST /lab/{sid}/resume` replays steps 1–2 as `replayed`, continues on the new version.
6. Complete the run; the new version retains the intact prefix from steps 1–2.

## Rehearse, then promote

Lab-driven saves (`from_lab` in the editor) always create a new version **without** changing which version is current. This is intentional: you finish the rehearsal loop (fix → resume → green) before deciding to promote.

When the rehearsed version is ready for production, open **History** (`/pipelines/{id}/versions`) and use **Make current** on that version. The editor save banner also offers **Make current** after a branch-from save, but lab fix-from-here saves skip auto-promotion even when you were editing the current version.

## Routes

| Route | Purpose |
|-------|---------|
| `GET /pipelines/{id}/lab` | Setup: sample dir, glob, version, redirection warnings |
| `POST /pipelines/{id}/lab` | Create session (one active per pipeline) |
| `GET /lab/{sid}` | Session view: timeline, controls, artifacts |
| `POST /lab/{sid}/tasks/{ix}/next\|retry\|branches\|to-end` | Step controls (redirect, no JS required) |
| `POST /lab/{sid}/run-all` | Runner-equivalent all tasks |
| `POST /lab/{sid}/resume` | Resume after editor fix (`version_id` in form) |
| `GET /lab/{sid}/artifacts/{rel}` | Traversal-safe sandbox artifact serving |
| `POST /lab/{sid}/close` | Close session and delete sandbox |

## CLI

```bash
conveyor dry-run PLAYBOOK --sample ./samples [--glob '*'] [--json]
```

Runs `run_all()` in a temp sandbox, prints a table or JSON `report()`, then closes. Exit `0` all ok, `1` any fail/skip, `2` unreadable playbook. Never touches the production database.

## shell.run warning

Playbooks containing `shell.run` show a loud warning on the lab setup page. Dry-run still executes those commands for real — only output paths are redirected, not command execution.
