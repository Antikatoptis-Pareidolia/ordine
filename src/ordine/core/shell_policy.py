"""Lab/dry-run shell execution policy (stub vs real).

Owns the contextvar consulted by ``shell.run``. Lives in core so dry-run can
set policy without importing executors. Must never import executors, web, cli, or llm.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

ShellMode = Literal["execute", "stub"]

_shell_mode: ContextVar[ShellMode] = ContextVar("ordine_shell_mode", default="execute")


@contextmanager
def shell_mode(mode: ShellMode) -> Iterator[None]:
    """Temporarily set shell.run execution mode (lab/dry-run stub support)."""
    token = _shell_mode.set(mode)
    try:
        yield
    finally:
        _shell_mode.reset(token)


def current_shell_mode() -> ShellMode:
    """Return the active shell.run mode for this context."""
    return _shell_mode.get()
