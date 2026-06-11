"""Provider/model-policy control routes for the API server adapter."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import Any, Awaitable, Callable, Dict

try:
    web: Any = importlib.import_module("aiohttp.web")
except ImportError:  # pragma: no cover - api_server gates startup on aiohttp
    web = None


logger = logging.getLogger(__name__)

Handler = Callable[[Any, Any], Awaitable[Any]]


def provider_control_capabilities() -> Dict[str, Dict[str, Any]]:
    """Return the capability fragment owned by these provider-control routes."""
    return {
        "features": {
            "provider_oauth": {
                "openai-codex": {
                    "flow": "device_code",
                    "start": "/api/providers/oauth/openai-codex/start",
                    "poll": "/api/providers/oauth/openai-codex/poll/{session_id}",
                    "disconnect": "/api/providers/openai-codex",
                }
            },
            "provider_status": True,
            "model_policy": True,
        },
        "endpoints": {
            "providers": {"method": "GET", "path": "/api/providers"},
            "provider_oauth_start": {
                "method": "POST",
                "path": "/api/providers/oauth/{provider}/start",
            },
            "provider_oauth_poll": {
                "method": "GET",
                "path": "/api/providers/oauth/{provider}/poll/{session_id}",
            },
            "provider_disconnect": {
                "method": "DELETE",
                "path": "/api/providers/{provider}",
            },
            "model_policy": {"method": "GET", "path": "/api/model-policy"},
            "model_policy_validate": {
                "method": "POST",
                "path": "/api/model-policy/validate",
            },
            "model_policy_apply": {
                "method": "POST",
                "path": "/api/model-policy/apply",
            },
            "provider_oauth_codex_start": {
                "method": "POST",
                "path": "/api/providers/oauth/openai-codex/start",
            },
            "provider_oauth_codex_poll": {
                "method": "GET",
                "path": "/api/providers/oauth/openai-codex/poll/{session_id}",
            },
            "provider_oauth_codex_disconnect": {
                "method": "DELETE",
                "path": "/api/providers/openai-codex",
            },
        },
    }


def register_provider_control_routes(app: Any, adapter: Any) -> None:
    """Register provider OAuth and model-policy routes on an API server app."""
    app.router.add_get("/api/providers", _bind(adapter, _handle_list_providers))
    app.router.add_post(
        "/api/providers/oauth/{provider_id}/start",
        _bind(adapter, _handle_provider_oauth_start),
    )
    app.router.add_get(
        "/api/providers/oauth/{provider_id}/poll/{session_id}",
        _bind(adapter, _handle_provider_oauth_poll),
    )
    app.router.add_delete(
        "/api/providers/{provider_id}",
        _bind(adapter, _handle_provider_disconnect),
    )
    app.router.add_delete(
        "/api/providers/oauth/{provider_id}",
        _bind(adapter, _handle_provider_disconnect),
    )
    app.router.add_get("/api/model-policy", _bind(adapter, _handle_model_policy))
    app.router.add_post(
        "/api/model-policy/validate",
        _bind(adapter, _handle_model_policy_validate),
    )
    app.router.add_post(
        "/api/model-policy/apply",
        _bind(adapter, _handle_model_policy_apply),
    )


def _bind(adapter: Any, handler: Handler) -> Callable[[Any], Awaitable[Any]]:
    async def bound(request: Any) -> Any:
        return await handler(adapter, request)

    return bound


def _check_auth(adapter: Any, request: Any) -> Any:
    return adapter._check_auth(request)


def _web() -> Any:
    if web is None:  # pragma: no cover - api_server refuses startup first
        raise RuntimeError("aiohttp is required for API server provider-control routes")
    return web


def _json_response(data: Any, *, status: int = 200) -> Any:
    return _web().json_response(data, status=status)


def _is_response(value: Any) -> bool:
    return isinstance(value, _web().Response)


async def _json_body(request: Any) -> Any:
    try:
        return await request.json()
    except (json.JSONDecodeError, Exception):
        return _json_response(_openai_error("Invalid JSON in request body"), status=400)


async def _handle_list_providers(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    try:
        from hermes_cli.provider_control import list_providers

        return _json_response(await asyncio.to_thread(list_providers))
    except Exception as exc:
        logger.exception("provider status listing failed")
        return _json_response(
            _openai_error(str(exc) or "Provider status listing failed"),
            status=500,
        )


async def _handle_model_policy(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    try:
        from hermes_cli.provider_control import get_model_policy

        return _json_response(await asyncio.to_thread(get_model_policy))
    except Exception as exc:
        logger.exception("model policy read failed")
        return _json_response(
            _openai_error(str(exc) or "Model policy read failed"),
            status=500,
        )


async def _handle_model_policy_validate(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    body = await _json_body(request)
    if _is_response(body):
        return body

    try:
        from hermes_cli.provider_control import validate_model_policy

        result = await asyncio.to_thread(validate_model_policy, body)
        return _json_response(result, status=200 if result.get("valid") else 400)
    except Exception as exc:
        logger.exception("model policy validation failed")
        return _json_response(
            _openai_error(str(exc) or "Model policy validation failed"),
            status=500,
        )


async def _handle_model_policy_apply(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    body = await _json_body(request)
    if _is_response(body):
        return body

    try:
        from hermes_cli.provider_control import apply_model_policy

        result = await asyncio.to_thread(apply_model_policy, body)
        return _json_response(result, status=200 if result.get("ok") else 400)
    except Exception as exc:
        logger.exception("model policy apply failed")
        return _json_response(
            _openai_error(str(exc) or "Model policy apply failed"),
            status=500,
        )


async def _handle_provider_oauth_start(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    provider_id = request.match_info.get("provider_id", "")
    try:
        from hermes_cli.provider_oauth import start_provider_oauth

        result = await asyncio.to_thread(start_provider_oauth, provider_id)
        return _json_response(result)
    except ValueError as exc:
        return _json_response(_openai_error(str(exc)), status=400)
    except Exception as exc:
        logger.exception("provider oauth start failed for %s", provider_id)
        return _json_response(
            _openai_error(str(exc) or "Provider OAuth start failed"),
            status=500,
        )


async def _handle_provider_oauth_poll(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    provider_id = request.match_info.get("provider_id", "")
    session_id = request.match_info.get("session_id", "")
    try:
        from hermes_cli.provider_oauth import poll_provider_oauth

        result = await asyncio.to_thread(poll_provider_oauth, provider_id, session_id)
        return _json_response(result)
    except KeyError:
        return _json_response(
            _openai_error("Provider OAuth session not found or expired"),
            status=404,
        )
    except ValueError as exc:
        return _json_response(_openai_error(str(exc)), status=400)
    except Exception as exc:
        logger.exception("provider oauth poll failed for %s", provider_id)
        return _json_response(
            _openai_error(str(exc) or "Provider OAuth poll failed"),
            status=500,
        )


async def _handle_provider_disconnect(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    provider_id = request.match_info.get("provider_id", "")
    try:
        from hermes_cli.provider_control import disconnect_provider

        return _json_response(await asyncio.to_thread(disconnect_provider, provider_id))
    except ValueError as exc:
        return _json_response(_openai_error(str(exc)), status=400)
    except Exception as exc:
        logger.exception("provider oauth disconnect failed for %s", provider_id)
        return _json_response(
            _openai_error(str(exc) or "Provider OAuth disconnect failed"),
            status=500,
        )


def _openai_error(
    message: str,
    err_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> Dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }
