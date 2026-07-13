"""Headless image processing steps (validate, transform, export).

Owns image.* steps with ImageMagick and Pillow backends. Must never import ledger, web, cli, or llm.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import ClassVar, Literal, cast

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from ordine.core.steps import StepContext, StepResult
from ordine.core.workdir import is_safe_output_name, safe_output_path
from ordine.executors.headless.backends import (
    ImageBackendError,
    find_imagemagick,
    pick_backend,
    run_im,
)


class BackendParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["auto", "imagemagick", "pillow"] = "auto"
    timeout_seconds: float = Field(default=60.0, gt=0)


class ValidateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    formats: list[str] = Field(default_factory=lambda: ["png"])


class WhiteToAlphaParams(BackendParams):
    fuzz: float = Field(default=8.0, ge=0, le=100)


class TrimParams(BackendParams):
    border: int = Field(default=0, ge=0)


class ExportParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dest: str
    format: Literal["png", "webp"] = "png"
    filename: str | None = None
    use_reserved_name: bool = True
    on_collision: Literal["suffix", "replace", "fail"] = "suffix"


def _require_input(ctx: StepContext) -> Path | StepResult:
    if ctx.input_path is None:
        return StepResult(status="fail", message="input_path is required")
    return ctx.input_path


def _imagemagick_unavailable() -> StepResult:
    return StepResult(
        status="fail",
        message=(
            "imagemagick not installed (apt package: imagemagick); "
            "install it or use backend: pillow"
        ),
    )


def _output_png_path(ctx: StepContext, input_path: Path) -> Path:
    return ctx.step_dir / f"{input_path.stem}.png"


def _white_to_alpha_pillow(input_path: Path, output_path: Path, fuzz: float) -> None:
    threshold = round(255 * (1 - fuzz / 100))
    with Image.open(input_path) as img:
        rgba = img.convert("RGBA")
        pixels = cast(tuple[tuple[int, int, int, int], ...], rgba.get_flattened_data())
        new_pixels = [
            (r, g, b, 0 if r >= threshold and g >= threshold and b >= threshold else a)
            for r, g, b, a in pixels
        ]
        rgba.putdata(new_pixels)
        rgba.save(output_path, format="PNG")


def _white_to_alpha_im(
    input_path: Path,
    output_path: Path,
    fuzz: float,
    *,
    logger: logging.Logger,
    timeout: float,
) -> None:
    run_im(
        [
            str(input_path),
            "-alpha",
            "set",
            "-fuzz",
            f"{fuzz}%",
            "-transparent",
            "white",
            str(output_path),
        ],
        logger=logger,
        timeout=timeout,
    )


def _trim_content_bbox(input_path: Path) -> tuple[int, int, int, int] | None:
    with Image.open(input_path) as img:
        if "A" in img.getbands():
            rgba = img.convert("RGBA")
            bbox = rgba.getchannel("A").getbbox()
            if bbox is None:
                bbox = rgba.getbbox()
            return bbox
        return img.getbbox()


def _trim_pillow(
    input_path: Path, output_path: Path, border: int, bbox: tuple[int, int, int, int]
) -> None:
    with Image.open(input_path) as img:
        rgba = img.convert("RGBA")
        cropped = rgba.crop(bbox)
        if border > 0:
            w, h = cropped.size
            canvas = Image.new("RGBA", (w + 2 * border, h + 2 * border), (0, 0, 0, 0))
            canvas.paste(cropped, (border, border))
            cropped = canvas
        cropped.save(output_path, format="PNG")


def _trim_im(
    input_path: Path,
    output_path: Path,
    border: int,
    *,
    logger: logging.Logger,
    timeout: float,
) -> None:
    args = [str(input_path), "-trim", "+repage"]
    if border > 0:
        args.extend(["-bordercolor", "none", "-border", str(border)])
    args.append(str(output_path))
    run_im(args, logger=logger, timeout=timeout)


def _resolve_export_name(
    ctx: StepContext,
    params: ExportParams,
    input_path: Path,
    fmt: str,
) -> tuple[str, str | None, str]:
    """Return (final basename, replaced extension source, selected raw name)."""
    if params.filename is not None:
        name = params.filename
    elif (
        params.use_reserved_name
        and ctx.ordinal is not None
        and ctx.naming is not None
        and (reserved := ctx.naming.resolve(ctx.ordinal)) is not None
    ):
        name = reserved
    else:
        name = input_path.stem

    suffix = f".{fmt}"
    if name.lower().endswith(suffix):
        return name, None, name
    original = name
    stem = Path(name).stem
    replaced = Path(name).suffix != "" and not name.lower().endswith(suffix)
    return f"{stem}{suffix}", original if replaced else None, name


def _collision_path(dest_dir: Path, name: str, on_collision: str) -> Path | StepResult:
    final = safe_output_path(dest_dir, name)
    if final is None:
        return StepResult(
            status="fail",
            flag_kind="unsafe_name",
            message=f"unsafe output name from manifest/template: {name}",
        )
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
        candidate_name = f"{stem}-{n}{suffix}"
        candidate = safe_output_path(dest_dir, candidate_name)
        if candidate is None:
            return StepResult(
                status="fail",
                flag_kind="unsafe_name",
                message=f"unsafe output name from manifest/template: {candidate_name}",
            )
        if not candidate.exists():
            return candidate
        n += 1


class ValidateStep:
    id = "image.validate"
    engines = frozenset({"headless"})
    Params = ValidateParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        if not isinstance(params, ValidateParams):
            raise TypeError(f"expected ValidateParams, got {type(params)!r}")
        if (
            ctx.input_path is None
            or not ctx.input_path.exists()
            or ctx.input_path.stat().st_size == 0
        ):
            return StepResult(
                status="skip",
                flag_kind="corrupt_input",
                message="missing or empty input",
            )
        try:
            with Image.open(ctx.input_path) as img:
                img.verify()
            with Image.open(ctx.input_path) as img:
                fmt = (img.format or "").lower()
        except Exception as exc:
            return StepResult(
                status="skip",
                flag_kind="corrupt_input",
                message=f"unreadable or truncated image: {exc}",
            )
        allowed = {f.lower().lstrip(".") for f in params.formats}
        if fmt not in allowed:
            return StepResult(
                status="skip",
                flag_kind="corrupt_input",
                message=f"unexpected format {fmt}, wanted {sorted(allowed)}",
            )
        return StepResult(status="ok")


class WhiteToAlphaStep:
    id = "image.white_to_alpha"
    engines = frozenset({"headless"})
    Params = WhiteToAlphaParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        if not isinstance(params, WhiteToAlphaParams):
            raise TypeError(f"expected WhiteToAlphaParams, got {type(params)!r}")
        input_or_err = _require_input(ctx)
        if isinstance(input_or_err, StepResult):
            return input_or_err
        input_path = input_or_err
        if not input_path.exists():
            return StepResult(status="fail", message=f"input not found: {input_path}")

        backend = pick_backend(params.backend)
        if backend == "imagemagick" and find_imagemagick() is None:
            return _imagemagick_unavailable()

        output_path = _output_png_path(ctx, input_path)
        try:
            if backend == "imagemagick":
                _white_to_alpha_im(
                    input_path,
                    output_path,
                    params.fuzz,
                    logger=ctx.logger,
                    timeout=params.timeout_seconds,
                )
            else:
                _white_to_alpha_pillow(input_path, output_path, params.fuzz)
        except ImageBackendError as exc:
            return StepResult(status="fail", message=str(exc))
        return StepResult(status="ok", output_path=output_path)


class TrimStep:
    id = "image.trim"
    engines = frozenset({"headless"})
    Params = TrimParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        if not isinstance(params, TrimParams):
            raise TypeError(f"expected TrimParams, got {type(params)!r}")
        input_or_err = _require_input(ctx)
        if isinstance(input_or_err, StepResult):
            return input_or_err
        input_path = input_or_err
        if not input_path.exists():
            return StepResult(status="fail", message=f"input not found: {input_path}")

        backend = pick_backend(params.backend)
        if backend == "imagemagick" and find_imagemagick() is None:
            return _imagemagick_unavailable()

        output_path = _output_png_path(ctx, input_path)
        bbox = _trim_content_bbox(input_path)
        if bbox is None:
            return StepResult(status="fail", message="nothing to trim: image is fully transparent")
        try:
            if backend == "imagemagick":
                _trim_im(
                    input_path,
                    output_path,
                    params.border,
                    logger=ctx.logger,
                    timeout=params.timeout_seconds,
                )
            else:
                _trim_pillow(input_path, output_path, params.border, bbox)
        except ImageBackendError as exc:
            return StepResult(status="fail", message=str(exc))
        return StepResult(status="ok", output_path=output_path)


class ExportStep:
    id = "image.export"
    engines = frozenset({"headless"})
    Params = ExportParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset({"dest"})

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        if not isinstance(params, ExportParams):
            raise TypeError(f"expected ExportParams, got {type(params)!r}")
        input_or_err = _require_input(ctx)
        if isinstance(input_or_err, StepResult):
            return input_or_err
        input_path = input_or_err
        if not input_path.exists():
            return StepResult(status="fail", message=f"input not found: {input_path}")

        dest_dir = Path(params.dest).expanduser()
        name, replaced_from, selected_name = _resolve_export_name(
            ctx, params, input_path, params.format
        )
        if not is_safe_output_name(selected_name):
            return StepResult(
                status="fail",
                flag_kind="unsafe_name",
                message=f"unsafe output name from manifest/template: {selected_name}",
            )
        if replaced_from is not None:
            ctx.logger.warning(
                "export: replaced extension on %r with .%s", replaced_from, params.format
            )

        collision = _collision_path(dest_dir, name, params.on_collision)
        if isinstance(collision, StepResult):
            return collision
        final = collision

        tmp: Path | None = None
        try:
            with Image.open(input_path) as img:
                input_fmt = (img.format or "").lower()
            target_fmt = params.format.lower()
            same_format = target_fmt == input_fmt or input_path.suffix.lower() == f".{target_fmt}"

            if same_format:
                data = input_path.read_bytes()
            else:
                buf_path = dest_dir / f".tmp-{uuid.uuid4().hex}"
                tmp = buf_path
                with Image.open(input_path) as img:
                    if target_fmt == "webp":
                        img.save(buf_path, format="WEBP")
                    else:
                        img.save(buf_path, format="PNG")
                data = buf_path.read_bytes()
                buf_path.unlink()
                tmp = None

            dest_dir.mkdir(parents=True, exist_ok=True)
            write_tmp = dest_dir / f".tmp-{uuid.uuid4().hex}"
            tmp = write_tmp
            write_tmp.write_bytes(data)
            os.replace(write_tmp, final)
            tmp = None
            return StepResult(status="ok", output_path=final)
        except OSError as exc:
            return StepResult(status="fail", message=str(exc))
        finally:
            if tmp is not None and tmp.exists():
                tmp.unlink()
