"""Web security helpers for POST hardening and artifact path checks.

Owns CSRF-ish POST validation and workdir traversal guards. Must never contain business logic.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from starlette.requests import Request


def _header_host(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return parsed.hostname


def post_is_allowed(request: Request, *, serve_host: str) -> bool:
    """Return True when a POST passes HX-Request / Origin checks (§3.3)."""
    origin_host = _header_host(request.headers.get("origin"))
    referer_host = _header_host(request.headers.get("referer"))
    foreign = False
    for host in (origin_host, referer_host):
        if host is not None and host not in ("127.0.0.1", "localhost", serve_host):
            foreign = True
            break
    if foreign:
        return False
    if request.headers.get("HX-Request") == "true":
        return True
    return not (origin_host is None and referer_host is None)


def resolve_artifact(workdir: Path, rel_path: str) -> Path | None:
    """Resolve *rel_path* inside *workdir*; return None when traversal is attempted."""
    base = workdir.expanduser().resolve()
    target = (base / rel_path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target
