# Conveyor documentation index

Read these in any order; each doc is scoped to one concern. This index was reconciled during Step 15 against the current codebase.

## Getting started

| Doc | Topic |
|-----|-------|
| [install.md](install.md) | pipx, `.deb`, from source, systemd user unit |
| [cli.md](cli.md) | `conveyor` commands, `--json`, config paths |
| [chaining.md](chaining.md) | manifest → generate → cleanup chain |

## Core platform

| Doc | Topic |
|-----|-------|
| [playbook-reference.md](playbook-reference.md) | YAML schema, triggers, steps, branches |
| [ledger.md](ledger.md) | task state machine, dedup, flags |
| [runner.md](runner.md) | retries, recovery branches, escalation |
| [triggers.md](triggers.md) | folder watch, manual, manifest |
| [workdir-layout.md](workdir-layout.md) | per-task directories, retention cleanup |

## Steps & plugins

| Doc | Topic |
|-----|-------|
| [steps-image.md](steps-image.md) | `image.*` headless steps |
| [plugin-guide.md](plugin-guide.md) | authoring entry-point steps |

## Web & lab

| Doc | Topic |
|-----|-------|
| [web.md](web.md) | `conveyor serve`, routes, security mitigations |
| [editor.md](editor.md) | playbook editor, versions, diffs |
| [lab.md](lab.md) | dry-run lab, checkpoints |

## LLM (optional)

| Doc | Topic |
|-----|-------|
| [llm.md](llm.md) | providers, keys, budgets, `llm check` |
| [ai-features.md](ai-features.md) | draft, diagnose, learned branches |

## Operations

| Doc | Topic |
|-----|-------|
| [security.md](security.md) | consolidated security posture |
| [release-checklist.md](release-checklist.md) | version bump, VM gate, publish |
| [naming-checklist.md](naming-checklist.md) | **human checkpoint** before 0.1.0 brand cut |

## Examples in the repo

- `examples/chain/` — offline mock chain (8 rows)
- `conveyor example DIR` — six-file quickstart scaffold (CI-guaranteed)
