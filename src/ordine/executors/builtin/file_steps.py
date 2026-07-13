"""Built-in file utility steps for manifest naming and moves.

Owns file.* steps. Must never import ledger, web, cli, or llm.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from ordine.core.errors import ManifestError
from ordine.core.manifest import load_manifest
from ordine.core.steps import StepContext, StepResult
from ordine.core.workdir import safe_output_path


class RenameFromManifestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: str
    on_missing_row: Literal["fail", "passthrough"] = "fail"


class MoveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dest: str
    on_collision: Literal["suffix", "replace", "fail"] = "suffix"


def _collision_path(dest_dir: Path, name: str, on_collision: str) -> Path | StepResult:
    final = dest_dir / name
    if not final.exists():
        return final
    if on_collision == "replace":
        return final
    if on_collision == "fail":
        return StepResult(status="fail", message=f"destination exists: {final}")
    stem = Path(name).stem
    suffix = Path(name).suffix
    n = 2
    while True:
        candidate = dest_dir / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


class RenameFromManifestStep:
    id = "file.rename_from_manifest"
    engines = frozenset({"headless"})
    Params = RenameFromManifestParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        if not isinstance(params, RenameFromManifestParams):
            raise TypeError(f"expected RenameFromManifestParams, got {type(params)!r}")
        if ctx.ordinal is None:
            return StepResult(
                status="fail",
                message=(
                    "task has no ordinal; configure ordinal_regex / arrival_order_ordinals "
                    "or a manifest trigger"
                ),
            )
        if ctx.naming is None:
            return StepResult(status="fail", message="naming service unavailable")
        if ctx.input_path is None:
            return StepResult(status="fail", message="rename requires an input artifact")

        try:
            rows = load_manifest(Path(params.manifest).expanduser())
        except ManifestError as exc:
            return StepResult(status="fail", message=str(exc))
        if ctx.ordinal > len(rows):
            message = f"manifest has {len(rows)} rows, task ordinal is {ctx.ordinal}"
            if params.on_missing_row == "passthrough":
                return StepResult(status="ok", output_path=ctx.input_path)
            return StepResult(
                status="fail",
                flag_kind="manifest_exhausted",
                message=message,
            )

        row = rows[ctx.ordinal - 1]
        effective = ctx.naming.bind(ctx.ordinal, row.name)
        output = safe_output_path(ctx.step_dir, effective)
        if output is None:
            return StepResult(
                status="fail",
                flag_kind="unsafe_name",
                message=f"unsafe output name from manifest/template: {effective}",
            )
        shutil.copy2(ctx.input_path, output)
        return StepResult(status="ok", output_path=output)


class MoveStep:
    id = "file.move"
    engines = frozenset({"headless"})
    Params = MoveParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset({"dest"})

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        if not isinstance(params, MoveParams):
            raise TypeError(f"expected MoveParams, got {type(params)!r}")
        if ctx.input_path is None:
            return StepResult(status="fail", message="move requires an input artifact")
        if not ctx.input_path.exists():
            return StepResult(status="fail", message=f"input not found: {ctx.input_path}")

        dest_dir = Path(params.dest).expanduser()
        collision = _collision_path(dest_dir, ctx.input_path.name, params.on_collision)
        if isinstance(collision, StepResult):
            return collision
        final = collision

        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp: Path | None = None
        try:
            write_tmp = dest_dir / f".tmp-{uuid.uuid4().hex}"
            tmp = write_tmp
            try:
                os.replace(ctx.input_path, write_tmp)
            except OSError:
                shutil.copy2(ctx.input_path, write_tmp)
                ctx.input_path.unlink()
            os.replace(write_tmp, final)
            tmp = None
            return StepResult(status="ok", output_path=final)
        except OSError as exc:
            return StepResult(status="fail", message=str(exc))
        finally:
            if tmp is not None and tmp.exists():
                tmp.unlink()
