# Shell caution example

**Danger:** this playbook uses `shell.run`, which executes arbitrary shell with your privileges.

- Lab / `ordine dry-run` **stub** `shell.run` by default; pass `--allow-shell` (CLI) or enable real shell in the lab UI to execute.
- This example sets `env_mode: allowlist` so only listed environment variables are inherited (plus Ordine placeholders).
- Never run untrusted playbooks containing `shell.run`.

See [security.md](../../docs/security.md) and [lab.md](../../docs/lab.md).
