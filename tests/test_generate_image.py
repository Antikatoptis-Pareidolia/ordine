"""Tests for llm.generate_image and image providers."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from conveyor.core.steps import StepContext
from conveyor.core.workdir import TaskWorkdir
from conveyor.llm.steps import (
    IMAGE_PROVIDERS,
    GenerateImageStep,
    ImageGenerationFailure,
    ImageGenerationOutcome,
    generate_openai_image,
    render_mock_image,
    reset_image_budget_for_tests,
)


def _ctx(tmp_path: Path, *, ordinal: int | None = 1) -> StepContext:
    workdir = TaskWorkdir.create(tmp_path, "gen", 1)
    step_dir = workdir.step_dir(1, "llm.generate_image")
    return StepContext(
        task_id=1,
        pipeline_name="gen",
        source_ref="manifest:/tmp/assets.csv#row1",
        ordinal=ordinal,
        input_path=None,
        step_dir=step_dir,
        logger=__import__("logging").getLogger("test"),
        naming=None,
    )


def _manifest(path: Path, rows: list[tuple[str, str]]) -> None:
    path.write_text(
        "name,prompt\n" + "\n".join(f"{n},{p}" for n, p in rows) + "\n",
        encoding="utf-8",
    )


def test_mock_provider_deterministic_bytes() -> None:
    same_prompt_a = render_mock_image(size="256x256", prompt="goat", ordinal=7)
    same_prompt_b = render_mock_image(size="256x256", prompt="goat", ordinal=7)
    assert same_prompt_a == same_prompt_b
    different_prompt = render_mock_image(size="256x256", prompt="different", ordinal=7)
    assert same_prompt_a != different_prompt
    assert same_prompt_a != render_mock_image(size="256x256", prompt="goat", ordinal=8)
    image = Image.open(__import__("io").BytesIO(same_prompt_a))
    assert image.size == (256, 256)
    assert image.getpixel((image.width - 1, image.height - 1)) == (255, 255, 255)


def test_generate_image_mock_filename_template(tmp_path: Path) -> None:
    manifest = tmp_path / "assets.csv"
    _manifest(manifest, [("goat.png", "a goat")])
    reset_image_budget_for_tests()
    step = GenerateImageStep()
    params = step.Params(manifest=str(manifest), provider="mock", size="128x128")
    result = step.run(_ctx(tmp_path), params)
    assert result.status == "ok"
    assert result.output_path is not None
    assert result.output_path.name == "img_0001.png"


def test_generate_image_filename_uses_name_stem(tmp_path: Path) -> None:
    manifest = tmp_path / "assets.csv"
    _manifest(manifest, [("goat.png", "a goat")])
    reset_image_budget_for_tests()
    step = GenerateImageStep()
    params = step.Params(
        manifest=str(manifest),
        provider="mock",
        size="128x128",
        filename_template="img_{ordinal:04d}_{name}",
    )
    result = step.run(_ctx(tmp_path), params)
    assert result.status == "ok"
    assert result.output_path is not None
    assert result.output_path.name == "img_0001_goat.png"


def test_openai_provider_request_shape_and_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("conveyor.llm.steps.get_key", lambda _provider: "test-key")
    png = render_mock_image(size="64x64", prompt="goat", ordinal=1)
    encoded = base64.b64encode(png).decode()

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = generate_openai_image(
        model="gpt-image-1",
        prompt="draw a goat",
        size="1024x1024",
        background="white",
        timeout_seconds=30.0,
        client=client,
    )
    assert isinstance(outcome, ImageGenerationOutcome)
    assert outcome.png_bytes == png
    assert captured["url"] == "https://api.openai.com/v1/images/generations"
    headers = captured["headers"]
    assert headers["authorization"].startswith("Bearer ")
    body = captured["body"]
    assert body["model"] == "gpt-image-1"
    assert body["prompt"] == "draw a goat"
    assert body["size"] == "1024x1024"
    assert body["n"] == 1
    Image.open(__import__("io").BytesIO(outcome.png_bytes))


def test_openai_policy_refusal_maps_to_generation_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("conveyor.llm.steps.get_key", lambda _provider: "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "content policy violation: unsafe prompt"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = generate_openai_image(
        model="gpt-image-1",
        prompt="bad",
        size="256x256",
        background="default",
        timeout_seconds=10.0,
        client=client,
    )
    assert isinstance(outcome, ImageGenerationFailure)
    assert outcome.flag_kind == "generation_refused"
    assert "policy" in outcome.message.lower()


def test_openai_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("conveyor.llm.steps.get_key", lambda _provider: "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = generate_openai_image(
        model="gpt-image-1",
        prompt="x",
        size="256x256",
        background="default",
        timeout_seconds=10.0,
        client=client,
    )
    assert isinstance(outcome, ImageGenerationFailure)
    assert "authentication" in outcome.message.lower()


def test_image_budget_blocks_before_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("conveyor.llm.steps.get_key", lambda _provider: "test-key")
    manifest = tmp_path / "assets.csv"
    _manifest(manifest, [("a.png", "1"), ("b.png", "2"), ("c.png", "3")])
    reset_image_budget_for_tests(cap=2)
    calls = 0
    real_openai = generate_openai_image

    def counting_openai(**kwargs: object) -> ImageGenerationOutcome | ImageGenerationFailure:
        nonlocal calls
        calls += 1
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"b64_json": png}]})

        kwargs["client"] = httpx.Client(transport=httpx.MockTransport(handler))
        return real_openai(**kwargs)

    monkeypatch.setitem(IMAGE_PROVIDERS, "openai", counting_openai)
    step = GenerateImageStep()
    for ordinal in (1, 2, 3):
        params = step.Params(manifest=str(manifest), provider="openai", size="256x256")
        ctx = _ctx(tmp_path, ordinal=ordinal)
        result = step.run(ctx, params)
        if ordinal <= 2:
            assert result.status == "ok"
        else:
            assert result.status == "fail"
            assert "image budget exceeded" in (result.message or "")
    assert calls == 2


def test_missing_prompt_row(tmp_path: Path) -> None:
    manifest = tmp_path / "assets.csv"
    manifest.write_text("name,prompt\nempty.png,\n", encoding="utf-8")
    reset_image_budget_for_tests()
    step = GenerateImageStep()
    params = step.Params(manifest=str(manifest), provider="mock")
    result = step.run(_ctx(tmp_path), params)
    assert result.status == "fail"
    assert result.message == "row 1 has no prompt"


def test_missing_manifest_row(tmp_path: Path) -> None:
    manifest = tmp_path / "assets.csv"
    _manifest(manifest, [("a.png", "one")])
    reset_image_budget_for_tests()
    step = GenerateImageStep()
    params = step.Params(manifest=str(manifest), provider="mock")
    result = step.run(_ctx(tmp_path, ordinal=9), params)
    assert result.status == "fail"
    assert result.flag_kind == "manifest_exhausted"


def test_no_ordinal(tmp_path: Path) -> None:
    manifest = tmp_path / "assets.csv"
    _manifest(manifest, [("a.png", "one")])
    reset_image_budget_for_tests()
    step = GenerateImageStep()
    params = step.Params(manifest=str(manifest), provider="mock")
    result = step.run(_ctx(tmp_path, ordinal=None), params)
    assert result.status == "fail"
    assert "no ordinal" in (result.message or "")


@pytest.mark.llm_live
def test_llm_live_openai_generation(tmp_path: Path) -> None:
    """One real OpenAI image generation; skipped without API key."""
    from conveyor.llm.keys import get_key

    if not get_key("openai"):
        pytest.skip("OPENAI_API_KEY not set")
    manifest = tmp_path / "assets.csv"
    _manifest(manifest, [("live.png", "a simple red circle on white")])
    reset_image_budget_for_tests()
    step = GenerateImageStep()
    params = step.Params(manifest=str(manifest), provider="openai", size="256x256")
    result = step.run(_ctx(tmp_path), params)
    assert result.status == "ok"
    assert result.output_path is not None
    Image.open(result.output_path)
