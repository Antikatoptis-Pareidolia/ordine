"""ImageMagick discovery and subprocess runner for headless image steps.

Owns ImageBackendError and backend selection helpers. Must never import ledger, web, cli, or llm.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from functools import lru_cache

from conveyor.core.errors import ConveyorError


class ImageBackendError(ConveyorError):
    """Raised when an ImageMagick subprocess fails or times out."""


@lru_cache(maxsize=1)
def find_imagemagick() -> list[str] | None:
    """Return the base command: ``['magick']`` (IM7) or ``['convert']`` (IM6), else None."""
    if shutil.which("magick") is not None:
        return ["magick"]
    if shutil.which("convert") is not None:
        return ["convert"]
    return None


def pick_backend(requested: str) -> str:
    """Resolve ``auto`` to imagemagick when available, otherwise pillow."""
    if requested == "auto":
        return "imagemagick" if find_imagemagick() is not None else "pillow"
    return requested


def run_im(
    args: list[str],
    *,
    logger: logging.Logger,
    timeout: float = 60.0,
) -> None:
    """Run ImageMagick with *args* appended to the discovered base command."""
    base = find_imagemagick()
    if base is None:
        raise ImageBackendError("imagemagick not installed")
    try:
        proc = subprocess.run(
            [*base, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImageBackendError("timeout") from exc
    if proc.returncode == 0:
        if proc.stdout:
            logger.debug("imagemagick stdout: %s", proc.stdout.rstrip())
        if proc.stderr:
            logger.debug("imagemagick stderr: %s", proc.stderr.rstrip())
        return
    stderr_tail = (proc.stderr or proc.stdout or "").strip()
    if len(stderr_tail) > 500:
        stderr_tail = stderr_tail[-500:]
    raise ImageBackendError(f"exit {proc.returncode}: {stderr_tail}")
