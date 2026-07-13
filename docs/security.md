# Security

## Playbooks are code

A playbook is a program. Steps can read inputs, write outputs, call external tools, and (with explicit opt-in steps such as future `shell.run`) execute shell commands. **Never run playbooks from strangers without reading them.**

Treat playbook YAML like shell scripts: review triggers, destinations, and branch steps before `ordine serve` on a shared host.

## Web UI posture

- **Default bind:** `127.0.0.1:8484` — localhost only
- **No authentication** in 0.1 — anyone who can reach the port can control pipelines
- Binding to `0.0.0.0` prints a CLI warning; do not expose without a reverse proxy and auth

### Step 9 mitigations (still required)

| Control | Purpose |
|---------|---------|
| POST Origin / Host guard | Blocks drive-by form posts from arbitrary websites |
| HX-Request check | HTMX mutations require the HX header |
| Artifact path canonicalization | Prevents `..` escapes when serving task files |

See `src/ordine/web/security.py` and [web.md](web.md).

## LLM data flow

| Data | Leaves machine? | When |
|------|-----------------|------|
| Playbook draft description | Yes | User clicks AI draft |
| Task logs / error text | Yes | User clicks Diagnose or Suggest branch |
| Input image (optional checkbox) | Yes | User enables include-image on diagnose |
| API keys | Yes | HTTPS to provider only; never logged in plaintext |
| JSONL audit log | No (local) | `~/.local/share/ordine/llm/` |

Purposes in the audit log include `draft_playbook`, `diagnose_failure`, `repair_diagnose`, `repair_branch`, `generate_image`, `llm_check`. See [ai-features.md](ai-features.md) and [llm.md](llm.md).

**Vision / screenshots to LLM:** image generation and optional diagnose images; disable LLM provider (`none`) to keep all inference local.

## Key storage

1. Environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …)
2. OS keyring (`keyring` package) via Settings UI or `ordine` key helpers
3. Never commit keys; `.env` is user-local only

## Telemetry

**None.** Ordine does not phone home, crash-report, or analytics-track. See README privacy statement.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md) in the repo root.
