# Security

## Playbooks are code

A playbook is a program. Steps can read inputs, write outputs, call external tools, and execute arbitrary installed plugin code. **Never run playbooks from strangers without reading them.**

Treat playbook YAML like shell scripts: review triggers, destinations, and branch steps before `ordine serve` on a shared host.

### `shell.run`

Ordine ships `shell.run`, which executes arbitrary shell commands **by design** (`subprocess.run(..., shell=True)` with `cwd` set to the step directory). Template placeholders in `cmd` (`{input}`, `{step_dir}`, `{ordinal}`, `{source}`) are supplied through subprocess environment variables and expanded as quoted data, so their contents are not reparsed as shell source. Commands that combine placeholders with heredocs are rejected because heredoc quoting follows different shell grammar; static heredocs remain available. Static command text remains fully trusted code. Stdout and stderr are always captured to `stdout.txt` / `stderr.txt` in the step directory for the task-detail view.

**Environment inheritance:** by default the child process receives the operator's full environment (`os.environ`) plus placeholder values. API keys and other secrets present in the environment are visible to the command. Opt in to `env_mode: allowlist` with `env_allowlist: [...]` on the step to pass only listed variables (plus Ordine placeholders). Treat playbook `cmd` text accordingly.

**Lab / dry-run:** `shell.run` is **stubbed by default** (no subprocess). Pass CLI `--allow-shell` or enable “Allow real shell.run” in the lab UI (with the danger acknowledgment) to execute for real. Only declared output paths are redirected — there is still no command sandbox when execution is allowed. Dashboard register and AI Approve continue to surface a danger callout (with a second confirm when Approve would save `shell.run`).

## Web UI posture

- **Default bind:** `127.0.0.1:8484` — localhost only (`web.bind`)
- **Host allowlist:** `web.allowed_hosts` (separate from bind). Wildcards `0.0.0.0` / `::` are refused as allowlist entries — they are listen addresses, not Host names. CLI `--host` overrides bind only.
- **No authentication** in 0.2 — anyone who can reach the port can control pipelines
- **Non-loopback bind is refused** unless `web.i_understand_no_auth = true` or `ordine serve --i-understand-no-auth` (recorded acknowledgment). Do not expose without a reverse proxy and auth. Bind/port changes require restarting `ordine serve`; allowlist changes from Settings apply on save.
- **Single writer per DB:** `serve` / `run` take an exclusive lock; a second writer exits non-zero. `status` is read-only (no write lock).

### Current mitigations

| Control | Purpose |
|---------|---------|
| Host allowlist (`web.allowed_hosts`) | Rejects requests whose Host is outside the configured list |
| POST Origin / Host guard | Blocks drive-by form posts from arbitrary websites |
| HX-Request check | HTMX mutations require the HX header |
| Artifact path canonicalization | Prevents `..` escapes when serving task files |
| Shell trust UX | Lab stubs `shell.run` by default; real execution needs allow-shell + ack; register / AI Approve still require ack |
| Non-loopback bind ack | `i_understand_no_auth` config / CLI flag required to listen off-loopback |
| Single-instance lock | Exclusive flock on `{db}.lock` for writers |

See `src/ordine/web/security.py` and [web.md](web.md).

## LLM data flow

| Data | Leaves machine? | When |
|------|-----------------|------|
| Playbook draft description | Yes | User clicks AI draft |
| Task logs / error text | Yes | User clicks Diagnose or Suggest branch |
| Input image (optional checkbox) | Yes | User enables include-image on diagnose |
| API keys | Yes | Sent to the configured provider endpoint (which may be HTTP for a local compatible server); never logged |
| JSONL audit log | No (local) | `~/.local/share/ordine/llm_log/` |

Purpose tags are `draft_playbook`, `revise_playbook`, `repair_playbook`, `diagnose_failure`, `repair_diagnose`, `suggest_branch`, `repair_branch`, `generate_image`, and `llm_check`. See [ai-features.md](ai-features.md) and [llm.md](llm.md).

**Vision / screenshots to LLM:** image generation and optional diagnose images; disable LLM provider (`none`) to keep all inference local.

## Key storage

1. OS keyring (`keyring` package) via Settings UI or `ordine` key helpers — **preferred**
2. Environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …)
3. `~/.config/ordine/.env` as a plaintext fallback; never commit it

Prefer the keyring. Plaintext `.env` is world-readable to your user account and combines with `shell.run` environment inheritance. Settings shows a warning when the active key source is `.env`. If the keyring backend is unavailable for reads, Ordine logs a warning and continues through the environment and `.env` fallbacks. Keyring write/delete operations still report an actionable error.

## Plugins are code

Steps discovered via `ordine.steps` entry points run **in-process** with the same privileges as Ordine. Installing a plugin package is equivalent to installing executable code: review sources before `pip install`, and treat third-party plugins like untrusted playbooks. See [plugin-guide.md](plugin-guide.md).

## Telemetry

**None.** Ordine does not phone home, crash-report, or analytics-track. See README privacy statement.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md) in the repo root.

## Hardening roadmap

- **CSRF tokens:** deferred because every request requires an allowed Host, POSTs additionally
  require same-origin Origin/Referer or the non-simple `HX-Request` header, and Ordine enables no
  CORS while remaining localhost-first.
- **`base_url` SSRF gating:** deferred because the endpoint is user-owned configuration; provider data flow and credential forwarding are documented above and in [llm.md](llm.md).
- **Artifact-serving TOCTOU hardening:** deferred because the symlink-swap window requires a concurrent local actor in the current single-user threat model.
- **JSONL retention configuration:** deferred; [llm.md](llm.md) documents manual cleanup in the meantime.
- **Dedicated CI integration job:** deferred because the current full matrix remains within the release budget; splitting it changes workflow topology rather than product correctness.
