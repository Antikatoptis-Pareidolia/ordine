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

## How Ordine was built

Ordine was built by one human and two AIs in deliberately separated roles,
and this workflow remains the contribution model:

- **Constantin Vlad** — product owner, final reviewer, and human QA: every
  feature was manually tested step-by-step in a live environment before
  acceptance, and several of the project's most serious bugs (lab fidelity,
  branch-stripping saves, escalation levels) were caught by that testing.
- **Claude (Anthropic)** — architect and management layer: wrote the PRD,
  the 17-step masterplan, every per-step implementation plan, and reviewed
  every audit — approving, rejecting, or amending with plan patches. Plans
  are the contract: **out-of-scope creep is a bug.**
- **Cursor** — implementer: turned plans into code and reported back with
  audit reports (deliverables table, contract spot-checks, numbered
  deviations with severity, raw test output).

The loop for every step: plan → implement → audit → review verdict →
follow-ups → human QA → next step. Honesty rules are enforced structurally:
tests may never patch the subject they assert (rule 21), audits must report
deviations rather than hide them, and review-demanded regression tests run
against pre-fix code first to confirm the bug was real. `CHANGELOG.md` is
the project's full biography under this process.

Historical step plans live in `Plans/conveyor-step-NN-plan.md` (the
filenames keep the pre-rename project name as planning provenance). When
contributing:

1. Implement only what a plan or issue scopes (+ rule-20 web wiring if applicable)
2. Run the full gate
3. Update `CHANGELOG.md` `[Unreleased]`
4. Describe deviations explicitly in the PR body

## Pull requests

- One logical change per PR when possible
- Link the step or issue
- Note any new dependencies with one-line justification in the PR body
- Do not weaken CI/lint config to greenwash failures

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
