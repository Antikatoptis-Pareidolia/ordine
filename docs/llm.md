# LLM connector

Step 12 provides provider-agnostic plumbing for text completions. Features that *use* the LLM (drafting, diagnosis) arrive in Step 13; image generation in Step 14.

## Providers

| Provider | API | Key env var | Notes |
|----------|-----|-------------|-------|
| `none` | — | — | Default; pipelines never need LLM |
| `anthropic` | `https://api.anthropic.com/v1/messages` | `ANTHROPIC_API_KEY` | Claude models |
| `openai` | `{base}/v1/chat/completions` | `OPENAI_API_KEY` | Default base `https://api.openai.com` |
| `openai_compatible` | `{base}/v1/chat/completions` | `ORDINE_LLM_API_KEY` (optional) | Ollama, LM Studio, vLLM |

Configure provider, model, `base_url` (compatible only), `max_tokens`, and `session_token_cap` in **Settings** (`/settings`) or `[llm]` in `config.toml`.

## API keys

Precedence (highest first):

1. System keyring (`ordine` service, provider name)
2. Environment variable (see table)
3. `~/.config/ordine/.env` (`KEY=VALUE` lines; `#` comments)

Set or clear keys from the settings page (stored in keyring). The UI shows **key present: yes/no** only — never the secret.

If keyring is unavailable, use the env var named in the error message.

`openai_compatible` may run without a key (local Ollama); requests use `Bearer none`.

## Local model quickstart (Ollama)

```toml
[llm]
provider = "openai_compatible"
model = "llama3"
base_url = "http://localhost:11434/v1"
max_tokens = 1024
session_token_cap = 200000
```

```bash
ordine llm check
```

## Token budget

`session_token_cap` is a **process-wide** cumulative limit (thread-safe). Before each call, the client reserves `max_tokens` output tokens; after a successful call, actual `input_tokens + output_tokens` are charged. Exceeding the cap raises `LLMBudgetError` before any HTTP request.

## Audit log

Each completion appends one JSON line to `$DATA_DIR/llm_log/{YYYY-MM}.jsonl`:

- `ts`, `provider`, `model`, `purpose`, `duration_s`, `usage`, `messages`, `response_text`
- Image parts are summarized as placeholders (length + media type)
- `response_text` truncates at 20k characters with `"truncated": true`
- API keys never appear in the log

Logging failures are reported to stderr and do not fail the call.

## CLI smoke test

```bash
ordine llm check          # human-readable ok/latency/usage
ordine llm check --json   # machine-readable
```

Exit codes: `0` success, `1` not configured or auth failure, `2` other errors.

## Out of scope (Step 12)

Tool use, streaming, embeddings, dollar-cost accounting, per-pipeline budgets, proxy/CA customization. Provider SDK packages are not used — adapters speak raw REST via `httpx`.
