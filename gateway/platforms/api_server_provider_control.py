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
_MODEL_POLICY_ACTIVATION_LOCK = asyncio.Lock()

Handler = Callable[[Any, Any], Awaitable[Any]]


def provider_control_capabilities() -> Dict[str, Dict[str, Any]]:
    """Return the capability fragment owned by these provider-control routes."""
    return {
        "features": {
            "provider_api_key": {
                "anthropic": {
                    "connect": "/api/providers/anthropic/api-key",
                    "disconnect": "/api/providers/anthropic",
                },
                "gemini": {
                    "connect": "/api/providers/gemini/api-key",
                    "disconnect": "/api/providers/gemini",
                },
                "openrouter": {
                    "connect": "/api/providers/openrouter/api-key",
                    "disconnect": "/api/providers/openrouter",
                },
                "xai": {
                    "connect": "/api/providers/xai/api-key",
                    "disconnect": "/api/providers/xai",
                },
            },
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
            "provider_api_key_connect": {
                "method": "POST",
                "path": "/api/providers/{provider}/api-key",
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
            "model_policy_activate": {
                "method": "POST",
                "path": "/api/model-policy/activate",
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
            "provider_api_key_openrouter_connect": {
                "method": "POST",
                "path": "/api/providers/openrouter/api-key",
            },
            "provider_api_key_anthropic_connect": {
                "method": "POST",
                "path": "/api/providers/anthropic/api-key",
            },
            "provider_api_key_gemini_connect": {
                "method": "POST",
                "path": "/api/providers/gemini/api-key",
            },
            "provider_api_key_xai_connect": {
                "method": "POST",
                "path": "/api/providers/xai/api-key",
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
    app.router.add_post(
        "/api/providers/{provider_id}/api-key",
        _bind(adapter, _handle_provider_api_key_connect),
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
    app.router.add_post(
        "/api/model-policy/activate",
        _bind(adapter, _handle_model_policy_activate),
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


async def _handle_model_policy_activate(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    body = await _json_body(request)
    if _is_response(body):
        return body

    async with _MODEL_POLICY_ACTIVATION_LOCK:
        try:
            from hermes_cli.provider_control import (
                apply_model_policy,
                restore_model_policy_config,
                snapshot_model_policy_config,
                validate_model_policy,
            )

            previous_config = await asyncio.to_thread(snapshot_model_policy_config)
            validation = await asyncio.to_thread(validate_model_policy, body)
            if not validation.get("valid"):
                return _json_response(
                    {
                        "ok": False,
                        "activated": False,
                        "applied": False,
                        **validation,
                    },
                    status=400,
                )

            apply_result = await asyncio.to_thread(apply_model_policy, body)
            if not apply_result.get("ok"):
                return _json_response(
                    {
                        "ok": False,
                        "activated": False,
                        **apply_result,
                    },
                    status=400,
                )

            try:
                await _refresh_runtime_model_policy(adapter)
                smoke = await _smoke_model_policy(adapter)
            except Exception as exc:
                restored = False
                restored_policy = None
                restore_error = None
                try:
                    restored_policy = await asyncio.to_thread(
                        restore_model_policy_config,
                        previous_config,
                    )
                    restored = True
                    await _refresh_runtime_model_policy(adapter)
                except Exception as rollback_exc:
                    logger.exception("model policy rollback failed")
                    restore_error = str(rollback_exc) or "Model policy rollback failed"

                logger.warning("model policy activation failed after apply: %s", exc)
                response = {
                    "ok": False,
                    "activated": False,
                    "applied": False,
                    "valid": True,
                    "errors": [],
                    "policy": restored_policy if restored else validation.get("policy"),
                    "attempted_policy": validation.get("policy"),
                    "restart_required": validation.get("restart_required", False),
                    "restored": restored,
                    "smoke": {
                        "ok": False,
                        "error": str(exc) or "Model policy activation failed",
                    },
                }
                if restore_error:
                    response["restore_error"] = restore_error
                return _json_response(response, status=500 if restore_error else 409)

            return _json_response(
                {
                    "ok": True,
                    "activated": True,
                    "applied": True,
                    "valid": True,
                    "errors": [],
                    "policy": apply_result.get("policy"),
                    "restart_required": apply_result.get("restart_required", False),
                    "smoke": smoke,
                }
            )
        except Exception as exc:
            logger.exception("model policy activation failed")
            return _json_response(
                _openai_error(str(exc) or "Model policy activation failed"),
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


async def _handle_provider_api_key_connect(adapter: Any, request: Any) -> Any:
    auth_err = _check_auth(adapter, request)
    if auth_err:
        return auth_err

    body = await _json_body(request)
    if _is_response(body):
        return body
    if not isinstance(body, dict):
        return _json_response(_openai_error("JSON object body is required"), status=400)

    provider_id = request.match_info.get("provider_id", "")
    api_key = body.get("api_key")
    try:
        from hermes_cli.provider_control import connect_provider_api_key

        return _json_response(
            await asyncio.to_thread(connect_provider_api_key, provider_id, api_key)
        )
    except ValueError as exc:
        return _json_response(_openai_error(str(exc)), status=400)
    except Exception as exc:
        logger.exception("provider API-key connect failed for %s", provider_id)
        return _json_response(
            _openai_error(str(exc) or "Provider API-key connect failed"),
            status=500,
        )


async def _refresh_runtime_model_policy(adapter: Any) -> None:
    refresh = getattr(adapter, "_refresh_runtime_model_policy", None)
    if callable(refresh):
        await asyncio.to_thread(refresh)

    runner = getattr(adapter, "gateway_runner", None)
    runner_refresh = getattr(runner, "_refresh_runtime_model_policy", None)
    if callable(runner_refresh):
        await asyncio.to_thread(runner_refresh)


async def _smoke_model_policy(adapter: Any) -> Dict[str, Any]:
    smoke = getattr(adapter, "_smoke_model_policy", None)
    if not callable(smoke):
        raise RuntimeError("API server adapter cannot smoke model policy")

    result = smoke()
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, dict):
        raise RuntimeError("Model policy smoke returned an invalid result")
    return result


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
