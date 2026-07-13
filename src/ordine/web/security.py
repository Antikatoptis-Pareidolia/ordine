"""Web security helpers for POST hardening and artifact path checks.

Owns CSRF-ish POST validation and workdir traversal guards. Must never contain business logic.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from starlette.requests import Request


def _normalized_origin(value: str | None) -> tuple[str, str, int] | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return None
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        port = parsed.port or default_port
    except ValueError:
        return None
    return parsed.scheme, parsed.hostname.lower(), port


def post_is_allowed(request: Request) -> bool:
    """Validate same-origin POSTs; HX suffices when Origin is absent on localhost.

    Browsers cannot attach the non-simple ``HX-Request`` header cross-origin without a
    successful CORS preflight, and Ordine enables no CORS middleware. When browsers do send
    Origin or Referer, the full normalized scheme/host/port must match the request itself.
    """
    own_origin = _normalized_origin(str(request.base_url))
    supplied = [
        value
        for value in (
            request.headers.get("origin"),
            request.headers.get("referer"),
        )
        if value is not None
    ]
    if own_origin is None:
        return False
    if any(_normalized_origin(value) != own_origin for value in supplied):
        return False
    if request.headers.get("HX-Request") == "true":
        return True
    return bool(supplied)


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
