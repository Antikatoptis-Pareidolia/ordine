# Security

## Playbooks are code

A playbook is a program. Steps can read inputs, write outputs, call external tools, and execute arbitrary installed plugin code. A plugin may provide a `shell.run` step, but Ordine 0.1 does not ship one. **Never run playbooks from strangers without reading them.**

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
| API keys | Yes | Sent to the configured provider endpoint (which may be HTTP for a local compatible server); never logged |
| JSONL audit log | No (local) | `~/.local/share/ordine/llm_log/` |

Purpose tags are `draft_playbook`, `revise_playbook`, `repair_playbook`, `diagnose_failure`, `repair_diagnose`, `suggest_branch`, `repair_branch`, `generate_image`, and `llm_check`. See [ai-features.md](ai-features.md) and [llm.md](llm.md).

**Vision / screenshots to LLM:** image generation and optional diagnose images; disable LLM provider (`none`) to keep all inference local.

## Key storage

1. OS keyring (`keyring` package) via Settings UI or `ordine` key helpers
2. Environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …)
3. `~/.config/ordine/.env` as a plaintext fallback; never commit it

## Telemetry

**None.** Ordine does not phone home, crash-report, or analytics-track. See README privacy statement.

## Reporting vulnerabilities

See [SECURITY.md](../SECURITY.md) in the repo root.

## Hardening roadmap

- **CSRF tokens:** deferred because `HX-Request` is a non-simple header that a cross-origin browser cannot send without a successful CORS preflight, and Ordine enables no CORS while remaining localhost-first.
- **`base_url` SSRF gating:** deferred because the endpoint is user-owned configuration; provider data flow and credential forwarding are documented above and in [llm.md](llm.md).
- **Artifact-serving TOCTOU hardening:** deferred because the symlink-swap window requires a concurrent local actor in the current single-user threat model.
- **JSONL retention configuration:** deferred to avoid expanding retention semantics late in 0.1; [llm.md](llm.md) documents manual cleanup in the meantime.
- **Dedicated CI integration job:** deferred because the current full matrix remains within the release budget; splitting it changes workflow topology rather than product correctness.
- **Starlette/httpx deprecation:** deferred until the upstream FastAPI/Starlette transition stabilizes; the current pinned-compatible stack passes the full suite.
