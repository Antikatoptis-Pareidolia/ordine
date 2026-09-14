"""Settings page routes including LLM configuration.

Owns HTTP parsing for /settings and keyring key forms. Must never implement LLM business logic.
"""

from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ordine.core.config import (
    AppConfig,
    is_loopback_bind,
    is_wildcard_host,
    load_config,
    normalize_allowed_hosts,
    save_llm_settings,
    save_web_runner_settings,
)
from ordine.core.errors import ConfigError
from ordine.llm.errors import LLMError
from ordine.llm.keys import clear_key, key_presence_label, set_key

logger = logging.getLogger(__name__)

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return Jinja2Templates(directory=str(request.app.state.templates_dir))


def _config(request: Request) -> AppConfig:
    return cast(AppConfig, request.app.state.config)


def _flash(request: Request) -> dict[str, str | None]:
    return {
        "flash": request.query_params.get("flash"),
        "flash_level": request.query_params.get("flash_level", "info"),
    }


def _settings_context(
    request: Request,
    *,
    error: str | None,
    saved: bool = False,
    bind_restart_notice: bool = False,
) -> dict[str, object]:
    config = _config(request)
    provider = config.llm_provider
    key_label = key_presence_label(provider)
    return {
        "request": request,
        "config": config,
        "allowed_hosts_text": ", ".join(config.web_allowed_hosts),
        "error": error,
        "saved": saved,
        "bind_restart_notice": bind_restart_notice,
        "llm_key_label": key_label,
        "llm_key_from_dotenv": key_label.endswith("(.env file)"),
        **_flash(request),
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request) -> HTMLResponse:
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(request, error=None),
    )


@router.post("/settings")
async def settings_post(
    request: Request,
    stale_after_minutes: Annotated[int, Form()],
    reconcile_policy: Annotated[str, Form()],
    web_bind: Annotated[str, Form()],
    web_allowed_hosts: Annotated[str, Form()],
    web_port: Annotated[int, Form()],
    llm_provider: Annotated[str, Form()] = "none",
    llm_model: Annotated[str, Form()] = "",
    llm_base_url: Annotated[str, Form()] = "",
    llm_max_tokens: Annotated[int, Form()] = 1024,
    llm_session_token_cap: Annotated[int, Form()] = 200_000,
    autostart_pipelines: Annotated[str | None, Form()] = None,
    i_understand_no_auth: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    config = _config(request)
    templates = _templates(request)
    autostart = autostart_pipelines == "on"
    no_auth_ack = i_understand_no_auth == "on"
    if reconcile_policy not in ("retry", "fail"):
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error="reconcile_policy must be retry or fail"),
            status_code=200,
        )
    if stale_after_minutes < 1:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error="stale_after_minutes must be at least 1"),
            status_code=200,
        )
    if not web_bind.strip():
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error="web.bind must not be empty"),
            status_code=200,
        )
    try:
        allowed = normalize_allowed_hosts(web_allowed_hosts)
    except ConfigError as exc:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error=str(exc)),
            status_code=200,
        )
    wildcards = [host for host in allowed if is_wildcard_host(host)]
    if wildcards:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                request,
                error=(
                    "Refusing to save 0.0.0.0/:: as Host allowlist entries. "
                    "Use web.bind for the listen address and list concrete Host names "
                    f"in allowed_hosts (rejected: {', '.join(wildcards)})."
                ),
            ),
            status_code=200,
        )
    if not 1 <= web_port <= 65535:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error="web.port must be between 1 and 65535"),
            status_code=200,
        )
    if llm_provider not in {"none", "anthropic", "openai", "openai_compatible"}:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error="invalid LLM provider"),
            status_code=200,
        )
    if llm_max_tokens < 1:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error="llm.max_tokens must be at least 1"),
            status_code=200,
        )
    if llm_session_token_cap < 1:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error="llm.session_token_cap must be at least 1"),
            status_code=200,
        )
    if config.config_file is None:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error="No config file on disk; create one with ordine init"),
            status_code=200,
        )
    if not is_loopback_bind(web_bind.strip()) and not no_auth_ack:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                request,
                error=(
                    "Refusing non-loopback bind without i_understand_no_auth. "
                    "Ordine has no authentication — check the acknowledgment box to record "
                    "that you accept exposing the control plane."
                ),
            ),
            status_code=200,
        )
    bind_changed = web_bind.strip() != config.web_bind or web_port != config.web_port
    try:
        save_web_runner_settings(
            config.config_file,
            stale_after_minutes=stale_after_minutes,
            reconcile_policy=reconcile_policy,
            web_bind=web_bind.strip(),
            web_allowed_hosts=allowed,
            web_port=web_port,
            autostart_pipelines=autostart,
            i_understand_no_auth=no_auth_ack,
        )
        save_llm_settings(
            config.config_file,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            llm_max_tokens=llm_max_tokens,
            llm_session_token_cap=llm_session_token_cap,
        )
    except ConfigError as exc:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error=str(exc)),
            status_code=200,
        )
    updated = load_config(config.config_file)
    request.app.state.config = updated
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            request,
            error=None,
            saved=True,
            bind_restart_notice=bind_changed,
        ),
    )


@router.post("/settings/llm-key")
async def settings_llm_key(
    request: Request,
    llm_provider: Annotated[str, Form()],
    api_key: Annotated[str, Form()] = "",
    action: Annotated[str, Form()] = "set",
) -> HTMLResponse:
    templates = _templates(request)
    provider = llm_provider.strip().lower()
    if provider in ("", "none"):
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, error="Select an LLM provider before managing keys"),
            status_code=200,
        )
    try:
        if action == "clear":
            clear_key(provider)
        elif api_key.strip():
            set_key(provider, api_key.strip())
        else:
            return templates.TemplateResponse(
                request,
                "settings.html",
                _settings_context(request, error="API key cannot be empty"),
                status_code=200,
            )
    except LLMError as exc:
        ctx = _settings_context(request, error=str(exc))
        return templates.TemplateResponse(
            request,
            "settings.html",
            ctx,
            status_code=200,
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(request, error=None, saved=True),
    )
