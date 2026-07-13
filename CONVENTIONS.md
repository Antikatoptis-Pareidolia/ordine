# Ordine Engineering Conventions
Every change in this repo MUST comply. If a task conflicts with these rules, stop and flag it instead of improvising.

## Architecture rules
1. `ordine.core` is domain-generic: NO image-, browser-, GIMP-, or use-case-specific code, imports, or naming in core. Use-case logic lives in `executors/*` plugins and `examples/`.
2. Dependency direction: `cli`/`web` → `core` ← `executors`/`llm`. Core never imports from cli, web, executors, or llm.
3. All cross-layer contracts are typed (dataclasses / pydantic models / Protocols) and live in `core`. No dict-shaped "stringly typed" payloads across layers.
4. Steps are pure with respect to global state: inputs = typed params + task context; outputs = StepResult + artifacts in the task work dir. Never mutate user source files; originals are read-only. Writes to destination folders use temp-file + atomic rename.
5. Everything user-facing is idempotent and crash-safe: persist state transitions before side effects where possible; assume the process can die between any two lines.

## Code rules
6. Python ≥3.11, full type hints everywhere; `mypy` strict must pass for `ordine.core`.
7. Errors: raise specific exceptions from `ordine.core.errors` (created in Step 2+); never bare `except:`; never silence exceptions without logging and a comment saying why.
8. Logging via stdlib `logging` with module-level loggers; no `print` outside `cli`.
9. No global mutable state, no singletons except explicitly documented registries.
10. Every module starts with a docstring stating its contract (what it owns, what it must never do).
11. Public functions get docstrings with Args/Returns/Raises. Comments explain WHY, not what.
12. Tests: every behavior added in a step ships with pytest tests in the same PR; bug fixes ship with a regression test.
13. Filesystem paths are `pathlib.Path`, expanded (`expanduser`) at the boundary (config load), never deep inside logic.
14. Timestamps are timezone-aware UTC (`datetime.now(tz=UTC)`).
15. New third-party dependencies require a one-line justification in the PR description and must be added to `pyproject.toml` (never ad-hoc installs).

## Process rules
16. Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`).
17. Keep `CHANGELOG.md` Unreleased section updated in every PR.
18. Do not modify CI, lint, or typecheck configs to make a failing check pass; fix the code. Config changes require explicit instruction.
19. Out-of-scope creep is a bug: implement only what the current step plan lists. If something seems missing, flag it; don't build it.
20. When a step plan adds a web surface, the standard wiring files (`web/app.py` router includes, template additions, route-module extraction) are implicitly in scope; audits list them explicitly but they are not deviations.
21. A test must never replace, wrap, or patch the subject whose behavior it asserts. Monkeypatching is reserved for true externals (network transports, clocks, filesystem boundaries, environment) or for counting wrappers that FULLY delegate to the real subject. If an assertion only holds because of the patch, the test is invalid.
