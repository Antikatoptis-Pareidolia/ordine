"""Web security helpers for POST hardening and artifact path checks.

Owns CSRF-ish POST validation and workdir traversal guards. Must never contain business logic.
"""

from __future__ import annotations

from ipaddress import ip_address
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


def _host_is_allowed(request_host: str, configured_host: str) -> bool:
    request_host = request_host.lower().rstrip(".")
    configured_host = configured_host.lower().strip().strip("[]").rstrip(".")
    loopback_names = {"localhost", "127.0.0.1", "::1"}
    if configured_host in loopback_names:
        if request_host in loopback_names:
            return True
        try:
            return ip_address(request_host).is_loopback
        except ValueError:
            return False
    if configured_host in {"0.0.0.0", "::"}:
        if request_host == "localhost":
            return True
        try:
            ip_address(request_host)
        except ValueError:
            return False
        return True
    return request_host == configured_host


def request_host_is_allowed(request: Request, *, configured_host: str) -> bool:
    """Return whether the HTTP Host matches the configured bind boundary."""
    own_origin = _normalized_origin(str(request.base_url))
    return own_origin is not None and _host_is_allowed(own_origin[1], configured_host)


def post_is_allowed(request: Request, *, configured_host: str) -> bool:
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
    if own_origin is None or not request_host_is_allowed(request, configured_host=configured_host):
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
