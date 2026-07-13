# Contributing to Ordine

Thank you for helping improve Ordine. This project follows explicit step plans and strict conventions — read both before opening a PR.

## Development setup

```bash
sudo apt install imagemagick
git clone https://github.com/Antikatoptis-Pareidolia/ordine.git && cd ordine
uv venv && uv sync --locked --extra dev
uv pip install -e tests/fixtures/ordine_test_plugin
pre-commit install
```

## Quality gate (must pass)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -m "not llm_live"
```

Pre-commit runs Ruff and repository hygiene hooks. `mypy` remains a required CI/manual gate rather than a local pre-commit hook.

## Conventions

All changes must comply with [CONVENTIONS.md](CONVENTIONS.md). Highlights:

- `ordine.core` stays domain-generic (no image/LLM/web imports)
- Typed contracts across layers
- Tests ship with behavior changes; no monkeypatching the subject under test (rule 21)
- Conventional Commits; update `CHANGELOG.md` `[Unreleased]`

## Plans & audit workflow

Features are scoped by historical step plans in `Plans/conveyor-step-NN-plan.md` in the parent repo. Those filenames intentionally retain the pre-Ordine project name as planning provenance. **Out-of-scope creep is a bug** — if something seems missing, open an issue instead of improvising.

When a step completes:

1. Implement only what the plan lists (+ rule-20 web wiring if applicable)
2. Run the full gate
3. Update `CHANGELOG.md`
4. Post an audit report (deliverables table, deviations, raw test output)

Follow-up work uses separate commits per follow-up plan.

## Pull requests

- One logical change per PR when possible
- Link the step or issue
- Note any new dependencies with one-line justification in the PR body
- Do not weaken CI/lint config to greenwash failures

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
