"""Platform-control routes for the API server adapter."""

from __future__ import annotations

import importlib
import json
import logging
from typing import Any, Awaitable, Callable

try:
    web: Any = importlib.import_module("aiohttp.web")
except ImportError:  # pragma: no cover - api_server gates startup on aiohttp
    web = None


logger = logging.getLogger(__name__)

Handler = Callable[[Any, Any], Awaitable[Any]]


def register_platform_control_routes(app: Any, adapter: Any) -> None:
    """Register generic platform-control routes on an API server app."""
    app.router.add_get("/api/platforms", _bind(adapter, _handle_list_platforms))
    app.router.add_get(
        "/api/platforms/{platform_id}",
        _bind(adapter, _handle_get_platform),
    )
    app.router.add_post(
        "/api/platforms/{platform_id}/configure",
        _bind(adapter, _handle_configure_platform),
    )


def _bind(adapter: Any, handler: Handler) -> Callable[[Any], Awaitable[Any]]:
    async def bound(request: Any) -> Any:
        return await handler(adapter, request)

    return bound


def _check_auth(adapter: Any, request: Any) -> Any:
    return adapter._check_auth(request)


def _web() -> Any:
    if web is None:  # pragma: no cover - api_server refuses startup first
        raise RuntimeError("aiohttp is required for API server platform-control routes")
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


async def _handle_list_platforms(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    try:
        from hermes_cli.platform_control import list_platforms

        return _json_response(
            list_platforms(runner=getattr(adapter, "gateway_runner", None))
        )
    except Exception as exc:
        logger.exception("platform status listing failed")
        return _json_response(
            _openai_error(str(exc) or "Platform status listing failed"),
            status=500,
        )


async def _handle_get_platform(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    platform_id = request.match_info.get("platform_id", "")
    try:
        from hermes_cli.platform_control import PlatformControlError, get_platform

        return _json_response(
            get_platform(platform_id, runner=getattr(adapter, "gateway_runner", None))
        )
    except PlatformControlError as exc:
        return _json_response(exc.to_response(), status=exc.status_code)
    except Exception as exc:
        logger.exception("platform status read failed for %s", platform_id)
        return _json_response(
            _openai_error(str(exc) or "Platform status read failed"),
            status=500,
        )


async def _handle_configure_platform(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    body = await _json_body(request)
    if _is_response(body):
        return body

    platform_id = request.match_info.get("platform_id", "")
    try:
        from hermes_cli.platform_control import (
            PlatformControlError,
            configure_platform,
        )

        result = await configure_platform(
            platform_id,
            body,
            runner=getattr(adapter, "gateway_runner", None),
        )
        status = 200
        if result.get("restart_required"):
            status = 409
        elif result.get("ok") is False and result.get("state") != "pairing":
            status = 500
        return _json_response(result, status=status)
    except PlatformControlError as exc:
        return _json_response(exc.to_response(), status=exc.status_code)
    except Exception as exc:
        logger.exception("platform configure failed for %s", platform_id)
        return _json_response(
            _openai_error(str(exc) or "Platform configure failed"),
            status=500,
        )


def _openai_error(
    message: str,
    err_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }
