"""Provider and model-policy control helpers for API surfaces."""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional


CODEX_PROVIDER_ID = "openai-codex"
OPENROUTER_PROVIDER_ID = "openrouter"


def list_providers() -> Dict[str, Any]:
    """Return supported provider status without exposing credentials."""
    policy = get_model_policy()
    return {
        "providers": [
            _provider_status(CODEX_PROVIDER_ID, policy),
            _provider_status(OPENROUTER_PROVIDER_ID, policy),
        ]
    }


def disconnect_provider(provider_id: str) -> Dict[str, Any]:
    """Clear provider credentials without mutating the active model policy."""
    provider = _normalize_provider(provider_id)
    if provider != CODEX_PROVIDER_ID:
        raise ValueError(f"Unsupported provider disconnect: {provider_id}")

    policy = get_model_policy()
    active_policy_affected = _policy_mentions_provider(policy, provider)

    from hermes_cli.provider_oauth import disconnect_codex

    credential_removed = bool(disconnect_codex())
    return {
        "ok": True,
        "disconnected": True,
        "credential_removed": credential_removed,
        "provider": provider,
        "active_policy_affected": active_policy_affected,
    }


def get_model_policy() -> Dict[str, Any]:
    """Read the current deployment-level model policy from config.yaml."""
    return _model_policy_from_config(_read_raw_config())


def validate_model_policy(payload: Any) -> Dict[str, Any]:
    """Validate a requested policy without persisting it."""
    config = _read_raw_config()
    current = _model_policy_from_config(config)
    requested, parse_errors = _parse_policy_request(payload)
    errors: List[Dict[str, str]] = list(parse_errors)

    primary = None
    if requested["primary"] is not None:
        primary = _validate_policy_entry(
            requested["primary"],
            path="primary",
            current=current,
            config=config,
            errors=errors,
        )

    fallbacks: List[Dict[str, Any]] = []
    for index, entry in enumerate(requested["fallbacks"]):
        validated = _validate_policy_entry(
            entry,
            path=f"fallbacks[{index}]",
            current=current,
            config=config,
            errors=errors,
        )
        if validated is not None:
            fallbacks.append(validated)

    if primary is not None:
        for index, fallback in enumerate(fallbacks):
            if (
                fallback.get("provider") == primary.get("provider")
                and fallback.get("model") == primary.get("model")
            ):
                errors.append(
                    {
                        "path": f"fallbacks[{index}]",
                        "message": "Fallback cannot match the primary provider and model.",
                    }
                )

    return {
        "valid": not errors,
        "errors": errors,
        "policy": {
            "primary": primary,
            "fallbacks": fallbacks,
        },
        "restart_required": False,
    }


def apply_model_policy(payload: Any) -> Dict[str, Any]:
    """Validate and persist a requested deployment-level model policy."""
    validation = validate_model_policy(payload)
    if not validation["valid"]:
        return {
            "ok": False,
            "applied": False,
            **validation,
        }

    policy = validation["policy"]
    primary = policy["primary"]
    if primary is None:
        return {
            "ok": False,
            "applied": False,
            "valid": False,
            "errors": [
                {
                    "path": "primary",
                    "message": "Primary provider and model are required.",
                }
            ],
            "policy": policy,
            "restart_required": False,
        }

    config = _read_raw_config()
    model_cfg = _model_config_dict(config.get("model"))
    model_cfg["provider"] = primary["provider"]
    model_cfg["default"] = primary["model"]
    _set_optional_model_key(model_cfg, "base_url", primary.get("base_url"))
    _set_optional_model_key(model_cfg, "api_mode", primary.get("api_mode"))
    model_cfg.pop("api_key", None)
    config["model"] = model_cfg

    config["fallback_providers"] = [
        _persistable_policy_entry(entry) for entry in policy["fallbacks"]
    ]
    config.pop("fallback_model", None)

    from hermes_cli.config import save_config

    save_config(config)

    return {
        "ok": True,
        "applied": True,
        "valid": True,
        "errors": [],
        "policy": get_model_policy(),
        "restart_required": False,
    }


def snapshot_model_policy_config() -> Dict[str, Any]:
    """Return the raw config snapshot used for model-policy rollback."""
    return _read_raw_config()


def restore_model_policy_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Restore a raw config snapshot after failed policy activation."""
    from hermes_cli.config import save_config

    save_config(dict(config))
    return get_model_policy()


def _provider_status(provider: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    if provider == CODEX_PROVIDER_ID:
        return _codex_status(policy)
    if provider == OPENROUTER_PROVIDER_ID:
        return _openrouter_status(policy)
    raise ValueError(f"Unsupported provider: {provider}")


def _codex_status(policy: Dict[str, Any]) -> Dict[str, Any]:
    from hermes_cli.auth import get_codex_auth_status

    raw = get_codex_auth_status()
    token = raw.get("api_key") if isinstance(raw, dict) else None
    connected = bool(isinstance(raw, dict) and raw.get("logged_in"))
    error = _string_or_none(raw.get("error") if isinstance(raw, dict) else None)
    return {
        "id": CODEX_PROVIDER_ID,
        "name": "OpenAI Codex",
        "connection_kind": "oauth_device_code",
        "status": "connected" if connected else "error" if error else "disconnected",
        "connected": connected,
        "credential_fingerprint": _secret_fingerprint(token),
        "last_error_message": error,
        "active_policy": _policy_mentions_provider(policy, CODEX_PROVIDER_ID),
        "oauth": {
            "flow": "device_code",
            "start": f"/api/providers/oauth/{CODEX_PROVIDER_ID}/start",
            "poll": f"/api/providers/oauth/{CODEX_PROVIDER_ID}/poll/{{session_id}}",
        },
        "model_policy": True,
    }


def _openrouter_status(policy: Dict[str, Any]) -> Dict[str, Any]:
    token = _openrouter_api_key()
    connected = bool(token)
    return {
        "id": OPENROUTER_PROVIDER_ID,
        "name": "OpenRouter",
        "connection_kind": "api_key",
        "status": "connected" if connected else "disconnected",
        "connected": connected,
        "credential_fingerprint": _secret_fingerprint(token),
        "last_error_message": None,
        "active_policy": _policy_mentions_provider(policy, OPENROUTER_PROVIDER_ID),
        "oauth": None,
        "model_policy": True,
    }


def _openrouter_api_key() -> str:
    try:
        from hermes_cli.config import get_env_value

        value = get_env_value("OPENROUTER_API_KEY")
        if value:
            return str(value)
    except Exception:
        pass
    return os.getenv("OPENROUTER_API_KEY", "")


def _read_raw_config() -> Dict[str, Any]:
    from hermes_cli.config import read_raw_config

    config = read_raw_config()
    return dict(config) if isinstance(config, dict) else {}


def _model_policy_from_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "primary": _read_primary(config),
        "fallbacks": _read_fallbacks(config),
        "restart_required": False,
    }


def _read_primary(config: Dict[str, Any]) -> Dict[str, Any]:
    model_cfg = config.get("model")
    if isinstance(model_cfg, str):
        return {
            "provider": None,
            "model": model_cfg.strip() or None,
            "base_url": None,
            "api_mode": None,
        }

    cfg = _model_config_dict(model_cfg)
    provider = _string_or_none(cfg.get("provider"))
    return {
        "provider": _normalize_provider(provider) if provider else None,
        "model": _string_or_none(cfg.get("default") or cfg.get("model")),
        "base_url": _string_or_none(cfg.get("base_url")),
        "api_mode": _string_or_none(cfg.get("api_mode")),
    }


def _read_fallbacks(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    chain = config.get("fallback_providers") or []
    if isinstance(chain, dict):
        chain = [chain]
    if not isinstance(chain, list):
        chain = []

    result = [_normal_policy_entry(entry) for entry in chain if isinstance(entry, dict)]
    result = [entry for entry in result if entry.get("provider") and entry.get("model")]
    if result:
        return result

    legacy = config.get("fallback_model")
    if isinstance(legacy, dict):
        entry = _normal_policy_entry(legacy)
        if entry.get("provider") and entry.get("model"):
            return [entry]
    if isinstance(legacy, list):
        entries = [
            _normal_policy_entry(entry)
            for entry in legacy
            if isinstance(entry, dict)
        ]
        return [
            entry for entry in entries if entry.get("provider") and entry.get("model")
        ]
    return []


def _parse_policy_request(payload: Any) -> tuple[Dict[str, Any], List[Dict[str, str]]]:
    if not isinstance(payload, dict):
        return {
            "primary": None,
            "fallbacks": [],
        }, [{"path": "body", "message": "JSON object body is required."}]

    primary_raw = payload.get("primary")
    if isinstance(primary_raw, dict):
        primary = _normal_policy_entry(primary_raw)
    else:
        primary = _normal_policy_entry(
            {
                "provider": payload.get("primary_provider", payload.get("provider")),
                "model": payload.get("primary_model", payload.get("model")),
            }
        )

    errors: List[Dict[str, str]] = []
    if not primary.get("provider"):
        errors.append({"path": "primary.provider", "message": "Provider is required."})
    if not primary.get("model"):
        errors.append({"path": "primary.model", "message": "Model is required."})

    fallback_raw = payload.get("fallbacks", payload.get("fallback_providers"))
    if fallback_raw is None:
        fallback_raw = payload.get("fallback")
    if fallback_raw is None:
        fallback_items: List[Any] = []
    elif isinstance(fallback_raw, dict):
        fallback_items = [fallback_raw]
    elif isinstance(fallback_raw, list):
        fallback_items = fallback_raw
    else:
        fallback_items = []
        errors.append(
            {
                "path": "fallbacks",
                "message": "Fallbacks must be an array or object.",
            }
        )

    fallbacks: List[Dict[str, Any]] = []
    for index, entry in enumerate(fallback_items):
        if not isinstance(entry, dict):
            errors.append(
                {
                    "path": f"fallbacks[{index}]",
                    "message": "Fallback entry must be an object.",
                }
            )
            continue
        normal = _normal_policy_entry(entry)
        if not normal.get("provider"):
            errors.append(
                {
                    "path": f"fallbacks[{index}].provider",
                    "message": "Provider is required.",
                }
            )
        if not normal.get("model"):
            errors.append(
                {
                    "path": f"fallbacks[{index}].model",
                    "message": "Model is required.",
                }
            )
        fallbacks.append(normal)

    return {
        "primary": primary if primary.get("provider") and primary.get("model") else None,
        "fallbacks": fallbacks,
    }, errors


def _validate_policy_entry(
    entry: Dict[str, Any],
    *,
    path: str,
    current: Dict[str, Any],
    config: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    provider = _string_or_none(entry.get("provider"))
    model = _string_or_none(entry.get("model"))
    if not provider or not model:
        return None

    primary = current.get("primary") or {}
    current_provider = _string_or_none(primary.get("provider")) or OPENROUTER_PROVIDER_ID
    current_model = _string_or_none(primary.get("model")) or ""
    current_base_url = _string_or_none(primary.get("base_url")) or ""
    user_providers = config.get("providers")
    if not isinstance(user_providers, dict):
        user_providers = {}

    try:
        from hermes_cli.config import get_compatible_custom_providers
        from hermes_cli.model_switch import switch_model

        result = switch_model(
            raw_input=model,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            current_api_key="",
            is_global=False,
            explicit_provider=provider,
            user_providers=user_providers,
            custom_providers=get_compatible_custom_providers(config),
        )
    except Exception as exc:
        errors.append({"path": path, "message": str(exc)})
        return None

    if not result.success:
        errors.append(
            {
                "path": path,
                "message": result.error_message or "Model policy entry is invalid.",
            }
        )
        return None

    return _switch_result_policy_entry(result)


def _switch_result_policy_entry(result: Any) -> Dict[str, Any]:
    entry = {
        "provider": _normalize_provider(result.target_provider),
        "model": result.new_model,
        "base_url": _string_or_none(result.base_url),
        "api_mode": _string_or_none(result.api_mode),
    }
    return {key: value for key, value in entry.items() if value is not None}


def _normal_policy_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    provider = _string_or_none(entry.get("provider"))
    model = _string_or_none(entry.get("model") or entry.get("default"))
    return {
        "provider": _normalize_provider(provider) if provider else None,
        "model": model,
        "base_url": _string_or_none(entry.get("base_url")),
        "api_mode": _string_or_none(entry.get("api_mode")),
    }


def _persistable_policy_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in {
            "provider": entry.get("provider"),
            "model": entry.get("model"),
            "base_url": entry.get("base_url"),
            "api_mode": entry.get("api_mode"),
        }.items()
        if value
    }


def _model_config_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        return {"default": value.strip()}
    return {}


def _set_optional_model_key(model_cfg: Dict[str, Any], key: str, value: Any) -> None:
    cleaned = _string_or_none(value)
    if cleaned:
        model_cfg[key] = cleaned
    else:
        model_cfg.pop(key, None)


def _policy_mentions_provider(policy: Dict[str, Any], provider: str) -> bool:
    primary = policy.get("primary") or {}
    if _normalize_provider(str(primary.get("provider") or "")) == provider:
        return True
    for entry in policy.get("fallbacks") or []:
        if (
            isinstance(entry, dict)
            and _normalize_provider(str(entry.get("provider") or "")) == provider
        ):
            return True
    return False


def _secret_fingerprint(secret: Any) -> Optional[str]:
    if not isinstance(secret, str) or not secret:
        return None
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def _normalize_provider(provider: str) -> str:
    from hermes_cli.providers import normalize_provider

    return normalize_provider(provider)


def _string_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
