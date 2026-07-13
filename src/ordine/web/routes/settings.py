"""Settings page routes including LLM configuration.

Owns HTTP parsing for /settings and keyring key forms. Must never implement LLM business logic.
"""

from __future__ import annotations

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ordine.core.config import AppConfig, load_config, save_llm_settings, save_web_runner_settings
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
    request: Request, *, error: str | None, saved: bool = False
) -> dict[str, object]:
    config = _config(request)
    provider = config.llm_provider
    return {
        "request": request,
        "config": config,
        "error": error,
        "saved": saved,
        "llm_key_label": key_presence_label(provider),
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
    web_host: Annotated[str, Form()],
    web_port: Annotated[int, Form()],
    llm_provider: Annotated[str, Form()] = "none",
    llm_model: Annotated[str, Form()] = "",
    llm_base_url: Annotated[str, Form()] = "",
    llm_max_tokens: Annotated[int, Form()] = 1024,
    llm_session_token_cap: Annotated[int, Form()] = 200_000,
    autostart_pipelines: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    config = _config(request)
    templates = _templates(request)
    autostart = autostart_pipelines == "on"
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
    try:
        save_web_runner_settings(
            config.config_file,
            stale_after_minutes=stale_after_minutes,
            reconcile_policy=reconcile_policy,
            web_host=web_host,
            web_port=web_port,
            autostart_pipelines=autostart,
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
        _settings_context(request, error=None, saved=True),
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
