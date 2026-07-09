"""Tests for headless image steps and backends."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image

from conveyor.core.registry import StepRegistry
from conveyor.core.steps import StepContext
from conveyor.core.workdir import TaskWorkdir
from conveyor.executors.headless.backends import find_imagemagick, pick_backend, run_im
from conveyor.executors.headless.steps import (
    ExportStep,
    TrimStep,
    ValidateStep,
    WhiteToAlphaStep,
)

IM_AVAILABLE = find_imagemagick() is not None
BACKENDS = ["pillow", "imagemagick"]


@dataclass
class StubNaming:
    names: dict[int, str]

    def resolve(self, ordinal: int) -> str | None:
        return self.names.get(ordinal)

    def bind(self, ordinal: int, name: str) -> str:
        return self.names.setdefault(ordinal, name)


def make_test_image(path: Path) -> Path:
    """64x64 white PNG with a 20x20 red square at (10, 12)."""
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    for y in range(12, 32):
        for x in range(10, 30):
            img.putpixel((x, y), (255, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return path


def truncate_image(path: Path) -> Path:
    """Write a valid PNG then drop the last 40% of bytes."""
    data = path.read_bytes()
    cut = int(len(data) * 0.6)
    truncated = path.with_name(f"{path.stem}-trunc{path.suffix}")
    truncated.write_bytes(data[:cut])
    return truncated


def _ctx(
    tmp_path: Path,
    *,
    input_path: Path | None = None,
    ordinal: int | None = None,
    naming: StubNaming | None = None,
    step_id: str = "image.validate",
) -> StepContext:
    workdir = TaskWorkdir.create(tmp_path, "demo", 1)
    step_dir = workdir.step_dir(1, step_id)
    logger = workdir.step_logger(step_dir)
    return StepContext(
        task_id=1,
        pipeline_name="demo",
        source_ref=str(input_path or ""),
        ordinal=ordinal,
        input_path=input_path,
        step_dir=step_dir,
        logger=logger,
        naming=naming,
    )


def _input_snapshot(path: Path) -> tuple[int, bytes]:
    stat = path.stat()
    return stat.st_mtime_ns, path.read_bytes()


def _run_white_to_alpha(
    tmp_path: Path,
    input_path: Path,
    backend: str,
) -> Path:
    ctx = _ctx(tmp_path, input_path=input_path, step_id="image.white_to_alpha")
    result = WhiteToAlphaStep().run(ctx, WhiteToAlphaStep.Params(backend=backend))
    assert result.status == "ok", result.message
    assert result.output_path is not None
    return result.output_path


def _assert_opaque_red(pixel: tuple[int, ...]) -> None:
    assert pixel[3] == 255
    assert abs(pixel[0] - 255) <= 10
    assert pixel[1] <= 10
    assert pixel[2] <= 10


def make_transparent_image(path: Path) -> Path:
    """Fully transparent PNG for trim failure tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(path, format="PNG")
    return path


def _pixel_agreement(a: Image.Image, b: Image.Image) -> tuple[float, int]:
    if a.size != b.size:
        raise ValueError("size mismatch")
    pa = list(a.convert("RGBA").getdata())
    pb = list(b.convert("RGBA").getdata())
    agree = 0
    max_rgb_diff = 0
    for (r1, g1, b1, a1), (r2, g2, b2, a2) in zip(pa, pb, strict=True):
        opaque1 = a1 > 127
        opaque2 = a2 > 127
        if opaque1 == opaque2:
            agree += 1
        if opaque1 and opaque2:
            max_rgb_diff = max(max_rgb_diff, abs(r1 - r2), abs(g1 - g2), abs(b1 - b2))
    return agree / len(pa), max_rgb_diff


def test_registry_lists_image_steps() -> None:
    ids = set(StepRegistry.load().ids())
    assert {"image.validate", "image.white_to_alpha", "image.trim", "image.export"} <= ids


def test_pick_backend_auto() -> None:
    if IM_AVAILABLE:
        assert pick_backend("auto") == "imagemagick"
    else:
        assert pick_backend("auto") == "pillow"


@pytest.mark.skipif(not IM_AVAILABLE, reason="ImageMagick not installed")
def test_run_im_success(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.im")
    with caplog.at_level(logging.DEBUG):
        run_im(["-version"], logger=logger, timeout=5.0)
    assert any(
        "imagemagick" in r.message.lower() or "version" in r.message.lower() for r in caplog.records
    )


def test_validate_happy_path(tmp_path: Path) -> None:
    src = make_test_image(tmp_path / "good.png")
    ctx = _ctx(tmp_path, input_path=src)
    result = ValidateStep().run(ctx, ValidateStep.Params())
    assert result.status == "ok"
    assert result.output_path is None


def test_validate_truncated(tmp_path: Path) -> None:
    src = truncate_image(make_test_image(tmp_path / "good.png"))
    ctx = _ctx(tmp_path, input_path=src)
    result = ValidateStep().run(ctx, ValidateStep.Params())
    assert result.status == "skip"
    assert result.flag_kind == "corrupt_input"
    assert "unreadable or truncated" in (result.message or "")


def test_validate_zero_byte(tmp_path: Path) -> None:
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    ctx = _ctx(tmp_path, input_path=empty)
    result = ValidateStep().run(ctx, ValidateStep.Params())
    assert result.status == "skip"
    assert result.flag_kind == "corrupt_input"
    assert "missing or empty input" in (result.message or "")


def test_validate_fake_png(tmp_path: Path) -> None:
    fake = tmp_path / "fake.png"
    fake.write_text("not an image", encoding="utf-8")
    ctx = _ctx(tmp_path, input_path=fake)
    result = ValidateStep().run(ctx, ValidateStep.Params())
    assert result.status == "skip"
    assert result.flag_kind == "corrupt_input"


def test_validate_wrong_format(tmp_path: Path) -> None:
    src = tmp_path / "img.webp"
    img = Image.new("RGB", (8, 8), (255, 0, 0))
    img.save(src, format="WEBP")
    ctx = _ctx(tmp_path, input_path=src)
    result = ValidateStep().run(ctx, ValidateStep.Params(formats=["png"]))
    assert result.status == "skip"
    assert result.flag_kind == "corrupt_input"
    assert "unexpected format" in (result.message or "")


@pytest.mark.parametrize("backend", BACKENDS)
def test_white_to_alpha_golden(tmp_path: Path, backend: str) -> None:
    if backend == "imagemagick" and not IM_AVAILABLE:
        pytest.skip("ImageMagick not installed")
    src = make_test_image(tmp_path / "in.png")
    ctx = _ctx(tmp_path, input_path=src, step_id="image.white_to_alpha")
    result = WhiteToAlphaStep().run(ctx, WhiteToAlphaStep.Params(backend=backend))
    assert result.status == "ok", result.message
    assert result.output_path is not None
    with Image.open(result.output_path) as out:
        out_rgba = out.convert("RGBA")
        corner = out_rgba.getpixel((0, 0))
        center = out_rgba.getpixel((20, 22))
    assert corner[3] == 0
    assert center[3] == 255
    assert abs(center[0] - 255) <= 10
    assert center[1] <= 10
    assert center[2] <= 10


@pytest.mark.parametrize("backend", BACKENDS)
def test_trim_golden(tmp_path: Path, backend: str) -> None:
    if backend == "imagemagick" and not IM_AVAILABLE:
        pytest.skip("ImageMagick not installed")
    src = make_test_image(tmp_path / "in.png")
    alpha_path = _run_white_to_alpha(tmp_path, src, backend)

    ctx0 = _ctx(tmp_path, input_path=alpha_path, step_id="image.trim")
    r0 = TrimStep().run(ctx0, TrimStep.Params(backend=backend, border=0))
    assert r0.status == "ok", r0.message
    assert r0.output_path is not None
    with Image.open(r0.output_path) as trimmed:
        assert trimmed.size == (20, 20)
        rgba = trimmed.convert("RGBA")
        w, h = rgba.size
        for corner in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            _assert_opaque_red(rgba.getpixel(corner))

    ctx3 = _ctx(tmp_path, input_path=alpha_path, step_id="image.trim")
    r3 = TrimStep().run(ctx3, TrimStep.Params(backend=backend, border=3))
    assert r3.status == "ok", r3.message
    assert r3.output_path is not None
    with Image.open(r3.output_path) as bordered:
        assert bordered.size == (26, 26)


@pytest.mark.parametrize("backend", BACKENDS)
def test_trim_fully_transparent_fails(tmp_path: Path, backend: str) -> None:
    if backend == "imagemagick" and not IM_AVAILABLE:
        pytest.skip("ImageMagick not installed")
    src = make_transparent_image(tmp_path / "empty.png")
    ctx = _ctx(tmp_path, input_path=src, step_id="image.trim")
    result = TrimStep().run(ctx, TrimStep.Params(backend=backend))
    assert result.status == "fail"
    assert result.message is not None
    assert "nothing to trim" in result.message


def test_export_collision_replace(tmp_path: Path) -> None:
    src = make_test_image(tmp_path / "in.png")
    dest = tmp_path / "out"
    dest.mkdir()
    existing = dest / "goat.png"
    existing.write_bytes(b"existing")
    ctx = _ctx(tmp_path, input_path=src, step_id="image.export")
    result = ExportStep().run(
        ctx,
        ExportStep.Params(
            dest=str(dest),
            format="png",
            filename="goat.png",
            on_collision="replace",
        ),
    )
    assert result.status == "ok"
    assert result.output_path == existing
    assert existing.read_bytes() == src.read_bytes()
    assert list(dest.glob(".tmp-*")) == []


@pytest.mark.skipif(not IM_AVAILABLE, reason="ImageMagick not installed")
def test_cross_backend_tolerance(tmp_path: Path) -> None:
    src = make_test_image(tmp_path / "in.png")
    pillow_out = _run_white_to_alpha(tmp_path / "pillow", src, "pillow")
    im_out = _run_white_to_alpha(tmp_path / "im", src, "imagemagick")
    with Image.open(pillow_out) as a, Image.open(im_out) as b:
        agreement, max_rgb = _pixel_agreement(a, b)
    assert agreement >= 0.97
    assert max_rgb <= 12


def test_imagemagick_missing_returns_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "conveyor.executors.headless.steps.find_imagemagick",
        lambda: None,
    )
    find_imagemagick.cache_clear()
    src = make_test_image(tmp_path / "in.png")
    ctx = _ctx(tmp_path, input_path=src, step_id="image.white_to_alpha")
    result = WhiteToAlphaStep().run(ctx, WhiteToAlphaStep.Params(backend="imagemagick"))
    find_imagemagick.cache_clear()
    assert result.status == "fail"
    assert result.message is not None
    assert "imagemagick" in result.message.lower()


def test_export_reserved_name(tmp_path: Path) -> None:
    src = make_test_image(tmp_path / "in.png")
    dest = tmp_path / "out"
    naming = StubNaming({3: "goat.png"})
    ctx = _ctx(
        tmp_path,
        input_path=src,
        ordinal=3,
        naming=naming,
        step_id="image.export",
    )
    result = ExportStep().run(
        ctx,
        ExportStep.Params(dest=str(dest), format="png"),
    )
    assert result.status == "ok"
    assert result.output_path == dest / "goat.png"
    assert result.output_path.read_bytes() == src.read_bytes()
    assert list(dest.glob(".tmp-*")) == []


def test_export_collision_suffix(tmp_path: Path) -> None:
    src = make_test_image(tmp_path / "in.png")
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "goat.png").write_bytes(b"existing")
    naming = StubNaming({1: "goat.png"})
    ctx = _ctx(tmp_path, input_path=src, ordinal=1, naming=naming, step_id="image.export")
    result = ExportStep().run(ctx, ExportStep.Params(dest=str(dest), format="png"))
    assert result.status == "ok"
    assert result.output_path == dest / "goat-2.png"
    assert (dest / "goat.png").read_bytes() == b"existing"


def test_export_collision_fail(tmp_path: Path) -> None:
    src = make_test_image(tmp_path / "in.png")
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "goat.png").write_bytes(b"existing")
    ctx = _ctx(tmp_path, input_path=src, step_id="image.export")
    result = ExportStep().run(
        ctx,
        ExportStep.Params(dest=str(dest), format="png", filename="goat.png", on_collision="fail"),
    )
    assert result.status == "fail"
    assert "destination exists" in (result.message or "")


def test_export_no_tmp_leftover_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = make_test_image(tmp_path / "in.png")
    dest = tmp_path / "out"

    def boom(_self: Path, _data: bytes) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(Path, "write_bytes", boom)
    ctx = _ctx(tmp_path, input_path=src, step_id="image.export")
    result = ExportStep().run(ctx, ExportStep.Params(dest=str(dest), format="png"))
    assert result.status == "fail"
    assert list(dest.glob(".tmp-*")) == [] if dest.exists() else True


@pytest.mark.parametrize(
    ("step_cls", "step_id", "run_fn"),
    [
        (ValidateStep, "image.validate", lambda tmp, src: (src, ValidateStep.Params())),
        (
            WhiteToAlphaStep,
            "image.white_to_alpha",
            lambda tmp, src: (src, WhiteToAlphaStep.Params(backend="pillow")),
        ),
        (
            TrimStep,
            "image.trim",
            lambda tmp, src: (
                _run_white_to_alpha(tmp, src, "pillow"),
                TrimStep.Params(backend="pillow", border=0),
            ),
        ),
        (
            ExportStep,
            "image.export",
            lambda tmp, src: (
                src,
                ExportStep.Params(dest=str(tmp / "dest"), format="png"),
            ),
        ),
    ],
)
def test_original_untouched(
    tmp_path: Path,
    step_cls: type,
    step_id: str,
    run_fn,
) -> None:
    original = make_test_image(tmp_path / "in.png")
    before = _input_snapshot(original)
    input_path, params = run_fn(tmp_path, original)
    ctx = _ctx(tmp_path, input_path=input_path, step_id=step_id)
    step_cls().run(ctx, params)
    after = _input_snapshot(original)
    assert before == after
