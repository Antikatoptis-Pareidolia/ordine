# Plugin Guide

Conveyor steps are Python plugins discovered via **entry points**. Install a plugin package
and its steps appear in `StepRegistry.load()` with no core code changes.

## Contract

Every step is a class implementing:

| Attribute | Requirement |
|---|---|
| `id` | Dotted lowercase id matching `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$` (e.g. `myplugin.resize`) |
| `engines` | `frozenset` of engine names the step supports (v1: include `"headless"`) |
| `Params` | Pydantic v2 model with `model_config = ConfigDict(extra="forbid")` |
| `OUTPUT_DIR_PARAMS` | Optional `frozenset` of param names that are output directories (default empty) |
| `run(ctx, params)` | Returns `StepResult`; see rules below |

### Rules (non-negotiable)

1. **Never raise for expected failures.** Return `StepResult(status="fail", message=...)` or
   `status="skip"`. Unexpected exceptions are converted to fail results by the engine so one
   bad plugin cannot kill the worker.
2. **Filesystem boundaries:** read only `ctx.input_path`; write only under `ctx.step_dir` and
   paths explicitly named in params (e.g. an export destination). Never modify `input_path`
   in place.
3. **No forbidden imports:** never import `conveyor.core.ledger`, `conveyor.web`, `conveyor.cli`,
   or `conveyor.llm`.

## Minimal example

```python
from pydantic import BaseModel, ConfigDict
from conveyor.core.steps import StepContext, StepResult

class EchoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str

class EchoStep:
    id = "myplugin.echo"
    engines = frozenset({"headless"})
    Params = EchoParams
    OUTPUT_DIR_PARAMS = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        assert isinstance(params, EchoParams)
        out = ctx.step_dir / "echo.txt"
        out.write_text(params.text, encoding="utf-8")
        return StepResult(status="ok", output_path=out)
```

## Publishing

Add an entry point to your `pyproject.toml`:

```toml
[project.entry-points."conveyor.steps"]
my_echo = "my_plugin.steps:EchoStep"
```

Install editable during development:

```bash
uv pip install -e ".[dev]" -e path/to/my-plugin
```

Verify discovery:

```python
from conveyor.core.registry import StepRegistry
print(StepRegistry.load().ids())
```

Param JSON Schema (for docs and LLM catalogs in Step 13):

```python
StepRegistry.load().param_schema("myplugin.echo")
```

## Built-in test utilities

Shipped with Conveyor for tests and dry-runs:

- `util.noop` — passthrough
- `util.fail` — configurable failure (supports `times` counter for branch tests)
- `util.copy` — copies `input_path` into `step_dir`

See `conveyor.executors.builtin.steps` for reference implementations.
