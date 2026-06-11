"""Gateway platform-control helpers for API surfaces.

The control API is intentionally platform-generic. Telegram is the first
implementation, but storage, status projection, and response shapes are
structured so Discord and WhatsApp can be added without another route family.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_dir, get_hermes_home
from hermes_cli.platform_control_whatsapp import (
    WHATSAPP_PLATFORM_ID,
    apply_whatsapp_env,
    configure_whatsapp_platform_config,
    start_whatsapp_pairing,
    validate_whatsapp_payload,
    whatsapp_config_override,
    whatsapp_session_paired,
    whatsapp_status,
)


TELEGRAM_PLATFORM_ID = "telegram"
DISCORD_PLATFORM_ID = "discord"
SUPPORTED_PLATFORM_IDS = (
    TELEGRAM_PLATFORM_ID,
    DISCORD_PLATFORM_ID,
    WHATSAPP_PLATFORM_ID,
)
RUNTIME_STATE_VERSION = 1

_TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{20,}$")
_TELEGRAM_USER_ID_RE = re.compile(r"^[1-9]\d{0,19}$")
_TELEGRAM_CHAT_ID_RE = re.compile(r"^-?[1-9]\d{0,19}$")
_DISCORD_TOKEN_RE = re.compile(r"^[A-Za-z0-9._=-]{20,256}$")
_DISCORD_SNOWFLAKE_RE = re.compile(r"^[1-9]\d{4,24}$")
_DISCORD_DEFAULT_TOOLSETS = ("discord", "discord_admin")


class PlatformControlError(Exception):
    """Base error with a structured API response."""

    status_code = 400
    error_code = "platform_control_error"

    def __init__(self, message: str, *, platform: str = "") -> None:
        super().__init__(message)
        self.platform = platform
        self.message = message

    def to_response(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "platform": self.platform or None,
            "error_code": self.error_code,
            "error_message": self.message,
        }


class UnsupportedPlatformError(PlatformControlError):
    """Raised when a platform-control route targets an unsupported platform."""

    status_code = 400
    error_code = "unsupported_platform"

    def __init__(self, platform: str) -> None:
        super().__init__(f"Unsupported platform: {platform}", platform=platform)

    def to_response(self) -> Dict[str, Any]:
        data = super().to_response()
        data["supported"] = False
        return data


class PlatformValidationError(PlatformControlError):
    """Raised when a platform configuration payload is invalid."""

    status_code = 400
    error_code = "validation_failed"

    def __init__(self, platform: str, errors: List[Dict[str, str]]) -> None:
        super().__init__("Platform configuration validation failed", platform=platform)
        self.errors = errors

    def to_response(self) -> Dict[str, Any]:
        data = super().to_response()
        data.update({"valid": False, "errors": self.errors})
        return data


def platform_control_capabilities() -> Dict[str, Dict[str, Any]]:
    """Return the capability fragment owned by platform-control routes."""
    return {
        "features": {
            "platform_control": True,
            "platforms": {
                TELEGRAM_PLATFORM_ID: {
                    "configure": True,
                    "hot_apply": True,
                    "credentials": ["bot_token"],
                    "configuration": ["allowed_users", "home_channel"],
                },
                DISCORD_PLATFORM_ID: {
                    "configure": True,
                    "hot_apply": True,
                    "credentials": ["bot_token"],
                    "configuration": [
                        "allowed_users",
                        "allowed_roles",
                        "home_channel",
                        "require_mention",
                    ],
                    "toolsets": list(_DISCORD_DEFAULT_TOOLSETS),
                },
                WHATSAPP_PLATFORM_ID: {
                    "configure": True,
                    "hot_apply": True,
                    "credentials": [],
                    "configuration": [
                        "mode",
                        "allowed_users",
                        "dm_policy",
                        "group_policy",
                        "restart_pairing",
                    ],
                    "pairing": ["qr", "status"],
                },
            },
        },
        "endpoints": {
            "platforms": {"method": "GET", "path": "/api/platforms"},
            "platform": {"method": "GET", "path": "/api/platforms/{platform}"},
            "platform_configure": {
                "method": "POST",
                "path": "/api/platforms/{platform}/configure",
            },
            "telegram_configure": {
                "method": "POST",
                "path": "/api/platforms/telegram/configure",
            },
            "discord_configure": {
                "method": "POST",
                "path": "/api/platforms/discord/configure",
            },
            "whatsapp_configure": {
                "method": "POST",
                "path": "/api/platforms/whatsapp/configure",
            },
        },
    }


def runtime_platform_config_overrides() -> Dict[str, Dict[str, Any]]:
    """Return GatewayConfig-compatible platform blocks from control state."""
    overrides: Dict[str, Dict[str, Any]] = {}
    for platform in SUPPORTED_PLATFORM_IDS:
        state = _read_platform_state(platform)
        if not state:
            continue
        if platform == TELEGRAM_PLATFORM_ID:
            overrides[platform] = _telegram_config_override(state)
        elif platform == DISCORD_PLATFORM_ID:
            overrides[platform] = _discord_config_override(state)
        elif platform == WHATSAPP_PLATFORM_ID:
            overrides[platform] = whatsapp_config_override(state)
    return overrides


def apply_runtime_platform_env_overrides(*, overwrite: bool = False) -> None:
    """Apply env values needed by legacy gateway auth/gating code paths."""
    for platform in SUPPORTED_PLATFORM_IDS:
        state = _read_platform_state(platform)
        if not state:
            continue
        _apply_platform_env(platform, state, overwrite=overwrite)


def list_platforms(*, runner: Any = None) -> Dict[str, Any]:
    """Return non-secret summaries for all supported platform-control targets."""
    return {
        "platforms": [
            get_platform(platform, runner=runner) for platform in SUPPORTED_PLATFORM_IDS
        ]
    }


def get_platform(platform_id: str, *, runner: Any = None) -> Dict[str, Any]:
    """Return one platform's non-secret configured/live state."""
    platform = _normalize_platform(platform_id)
    if platform not in SUPPORTED_PLATFORM_IDS:
        raise UnsupportedPlatformError(platform)
    if platform == TELEGRAM_PLATFORM_ID:
        return _telegram_status(runner=runner)
    if platform == DISCORD_PLATFORM_ID:
        return _discord_status(runner=runner)
    if platform == WHATSAPP_PLATFORM_ID:
        return _whatsapp_status(runner=runner)
    raise UnsupportedPlatformError(platform)


async def configure_platform(
    platform_id: str,
    payload: Any,
    *,
    runner: Any = None,
) -> Dict[str, Any]:
    """Validate, persist, and hot-apply one platform's configuration."""
    platform = _normalize_platform(platform_id)
    if platform not in SUPPORTED_PLATFORM_IDS:
        raise UnsupportedPlatformError(platform)
    if platform == TELEGRAM_PLATFORM_ID:
        state = _validate_telegram_payload(payload)
    elif platform == DISCORD_PLATFORM_ID:
        state = _validate_discord_payload(payload)
    elif platform == WHATSAPP_PLATFORM_ID:
        state = _validate_whatsapp_payload(payload)
    else:
        raise UnsupportedPlatformError(platform)
    _write_platform_state(platform, state)

    if runner is None:
        status = (
            _telegram_status(runner=None, persisted_state=state)
            if platform == TELEGRAM_PLATFORM_ID
            else _discord_status(runner=None, persisted_state=state)
            if platform == DISCORD_PLATFORM_ID
            else _whatsapp_status(runner=None, persisted_state=state)
        )
        status["state"] = "disconnected"
        status["connected"] = False
        return {
            "ok": False,
            "platform": platform,
            "configured": True,
            "applied": False,
            "restart_required": True,
            "state": "restart_required",
            "error_code": "restart_required",
            "error_message": "Gateway runner is unavailable for platform hot apply.",
            "status": status,
        }

    if platform == WHATSAPP_PLATFORM_ID and not whatsapp_session_paired(state):
        apply_result = _start_whatsapp_pairing(state)
    else:
        apply_result = (
            await _hot_apply_telegram(state, runner)
            if platform == TELEGRAM_PLATFORM_ID
            else await _hot_apply_discord(state, runner)
            if platform == DISCORD_PLATFORM_ID
            else await _hot_apply_whatsapp(state, runner)
        )
    status = (
        _telegram_status(runner=runner, persisted_state=state)
        if platform == TELEGRAM_PLATFORM_ID
        else _discord_status(runner=runner, persisted_state=state)
        if platform == DISCORD_PLATFORM_ID
        else _whatsapp_status(runner=runner, persisted_state=state)
    )
    error_message = _redact_state_secrets(state, apply_result.get("error_message"))
    ok = bool(apply_result.get("applied") and not apply_result.get("restart_required"))
    return {
        "ok": ok,
        "platform": platform,
        "configured": True,
        "applied": bool(apply_result.get("applied")),
        "restart_required": bool(apply_result.get("restart_required")),
        "state": apply_result.get("state") or status.get("state"),
        "error_code": apply_result.get("error_code"),
        "error_message": error_message,
        "status": status,
    }


def _normalize_platform(platform_id: Any) -> str:
    return str(platform_id or "").strip().lower()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _platform_state_path(platform: str) -> Path:
    return get_hermes_home() / "gateway" / "platform-control" / f"{platform}.json"


def _read_platform_state(platform: str) -> Optional[Dict[str, Any]]:
    path = _platform_state_path(platform)
    if not path.exists():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_platform_state(platform: str, state: Dict[str, Any]) -> None:
    path = _platform_state_path(platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    from utils import atomic_json_write

    atomic_json_write(path, state, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _secret_fingerprint(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def _redact_state_secrets(
    state: Optional[Dict[str, Any]],
    message: Any,
    *,
    fallback_secrets: Optional[List[str]] = None,
) -> Optional[str]:
    if message is None:
        return None
    text = str(message)
    secrets: List[str] = []
    credentials = state.get("credentials") if isinstance(state, dict) else {}
    if isinstance(credentials, dict):
        for value in credentials.values():
            if isinstance(value, str) and value:
                secrets.append(value)
    if fallback_secrets:
        secrets.extend(secret for secret in fallback_secrets if secret)
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def _validate_telegram_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformValidationError(
            TELEGRAM_PLATFORM_ID,
            [{"path": "", "message": "Request body must be a JSON object."}],
        )

    errors: List[Dict[str, str]] = []
    bot_token = _telegram_bot_token_from_payload(payload, errors)
    allowed_users = _telegram_allowed_users_from_payload(payload, errors)
    home_channel = _telegram_home_channel_from_payload(payload, errors)

    if errors:
        raise PlatformValidationError(TELEGRAM_PLATFORM_ID, errors)

    return {
        "version": RUNTIME_STATE_VERSION,
        "platform": TELEGRAM_PLATFORM_ID,
        "updated_at": _utc_now_iso(),
        "credentials": {
            "bot_token": bot_token,
        },
        "configuration": {
            "allowed_users": allowed_users,
            "home_channel": home_channel,
        },
    }


def _payload_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = payload.get("configuration")
    return config if isinstance(config, dict) else {}


def _payload_credentials(payload: Dict[str, Any]) -> Dict[str, Any]:
    credentials = payload.get("credentials")
    return credentials if isinstance(credentials, dict) else {}


def _telegram_bot_token_from_payload(
    payload: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> str:
    credentials = _payload_credentials(payload)
    raw = (
        payload.get("bot_token")
        or payload.get("token")
        or credentials.get("bot_token")
        or credentials.get("token")
    )
    if not isinstance(raw, str) or not raw.strip():
        errors.append(
            {"path": "bot_token", "message": "Telegram bot token is required."}
        )
        return ""

    token = raw.strip()
    if not _TELEGRAM_TOKEN_RE.match(token):
        errors.append(
            {
                "path": "bot_token",
                "message": "Telegram bot token must use the BotFather token format.",
            }
        )
        return ""
    return token


def _telegram_allowed_users_from_payload(
    payload: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> List[str]:
    config = _payload_config(payload)
    raw = payload.get("allowed_users", config.get("allowed_users"))
    if raw is None:
        raw = payload.get("allow_from", config.get("allow_from"))

    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(part).strip() for part in raw]
    else:
        values = []

    normalized: List[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        if not _TELEGRAM_USER_ID_RE.match(value):
            errors.append(
                {
                    "path": "allowed_users",
                    "message": "Telegram allowed user IDs must be positive integers.",
                }
            )
            continue
        if value not in seen:
            seen.add(value)
            normalized.append(value)

    if not normalized:
        errors.append(
            {
                "path": "allowed_users",
                "message": "At least one Telegram allowed user ID is required.",
            }
        )

    return normalized


def _telegram_home_channel_from_payload(
    payload: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> Dict[str, str]:
    config = _payload_config(payload)
    raw = payload.get("home_channel", config.get("home_channel"))

    if isinstance(raw, dict):
        chat_id = raw.get("chat_id") or raw.get("id")
        name = raw.get("name") or "Home"
        thread_id = raw.get("thread_id")
    else:
        chat_id = raw
        name = "Home"
        thread_id = None

    chat_id_text = str(chat_id).strip() if chat_id is not None else ""
    if not chat_id_text:
        errors.append(
            {"path": "home_channel", "message": "Telegram home channel is required."}
        )
        return {"platform": TELEGRAM_PLATFORM_ID, "chat_id": "", "name": "Home"}

    if not _TELEGRAM_CHAT_ID_RE.match(chat_id_text):
        errors.append(
            {
                "path": "home_channel",
                "message": "Telegram home channel must be a numeric chat ID.",
            }
        )

    result = {
        "platform": TELEGRAM_PLATFORM_ID,
        "chat_id": chat_id_text,
        "name": str(name or "Home"),
    }
    if thread_id is not None and str(thread_id).strip():
        result["thread_id"] = str(thread_id).strip()
    return result


def _discord_snowflake_list_from_payload(
    payload: Dict[str, Any],
    errors: List[Dict[str, str]],
    *,
    keys: tuple[str, ...],
    path: str,
    label: str,
    required: bool,
) -> List[str]:
    config = _payload_config(payload)
    raw = None
    for key in keys:
        if key in payload:
            raw = payload.get(key)
            break
        if key in config:
            raw = config.get(key)
            break

    values = _coerce_string_list(raw)
    normalized: List[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_discord_id(value)
        if not _DISCORD_SNOWFLAKE_RE.match(cleaned):
            errors.append(
                {
                    "path": path,
                    "message": f"{label} must be Discord numeric IDs.",
                }
            )
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)

    if required and not normalized:
        errors.append(
            {
                "path": path,
                "message": f"At least one {label.lower()} entry is required.",
            }
        )

    return normalized


def _coerce_string_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(part).strip() for part in raw if str(part).strip()]
    return []


def _telegram_config_override(state: Dict[str, Any]) -> Dict[str, Any]:
    credentials = state.get("credentials") if isinstance(state, dict) else {}
    configuration = state.get("configuration") if isinstance(state, dict) else {}
    token = credentials.get("bot_token") if isinstance(credentials, dict) else None
    allowed_users = (
        configuration.get("allowed_users") if isinstance(configuration, dict) else []
    )
    home_channel = (
        configuration.get("home_channel") if isinstance(configuration, dict) else None
    )

    extra: Dict[str, Any] = {}
    if isinstance(allowed_users, list):
        extra["allow_from"] = [str(value) for value in allowed_users]
        extra["allowed_users"] = [str(value) for value in allowed_users]

    override: Dict[str, Any] = {
        "enabled": bool(token),
        "extra": extra,
    }
    if isinstance(token, str) and token:
        override["token"] = token
    if isinstance(home_channel, dict) and home_channel.get("chat_id"):
        override["home_channel"] = dict(home_channel)
    return override


def _validate_discord_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlatformValidationError(
            DISCORD_PLATFORM_ID,
            [{"path": "", "message": "Request body must be a JSON object."}],
        )

    errors: List[Dict[str, str]] = []
    bot_token = _discord_bot_token_from_payload(payload, errors)
    allowed_users = _discord_allowed_users_from_payload(payload, errors)
    allowed_roles = _discord_snowflake_list_from_payload(
        payload,
        errors,
        keys=("allowed_roles", "roles", "role_ids"),
        path="allowed_roles",
        label="Discord allowed role IDs",
        required=False,
    )
    home_channel = _discord_home_channel_from_payload(payload, errors)
    enabled_toolsets = _discord_toolsets_from_payload(payload)
    allowed_channels = _discord_channel_list_from_payload(
        payload,
        "allowed_channels",
        default_home_channel=home_channel,
        errors=errors,
    )
    free_response_channels = _discord_channel_list_from_payload(
        payload,
        "free_response_channels",
        default_home_channel=home_channel,
        errors=errors,
    )
    require_mention = _bool_from_payload(payload, "require_mention", default=True)

    if errors:
        raise PlatformValidationError(DISCORD_PLATFORM_ID, errors)

    configuration: Dict[str, Any] = {
        "allowed_users": allowed_users,
        "allowed_roles": allowed_roles,
        "enabled_toolsets": enabled_toolsets,
        "allowed_channels": allowed_channels,
        "free_response_channels": free_response_channels,
        "require_mention": require_mention,
    }
    if home_channel is not None:
        configuration["home_channel"] = home_channel

    return {
        "version": RUNTIME_STATE_VERSION,
        "platform": DISCORD_PLATFORM_ID,
        "updated_at": _utc_now_iso(),
        "credentials": {
            "bot_token": bot_token,
        },
        "configuration": configuration,
    }


def _discord_bot_token_from_payload(
    payload: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> str:
    credentials = _payload_credentials(payload)
    raw = (
        payload.get("bot_token")
        or payload.get("token")
        or credentials.get("bot_token")
        or credentials.get("token")
    )
    if not isinstance(raw, str) or not raw.strip():
        errors.append(
            {"path": "bot_token", "message": "Discord bot token is required."}
        )
        return ""

    token = raw.strip()
    if not _DISCORD_TOKEN_RE.match(token):
        errors.append(
            {
                "path": "bot_token",
                "message": "Discord bot token must look like a bot token.",
            }
        )
        return ""
    return token


def _discord_allowed_users_from_payload(
    payload: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> List[str]:
    config = _payload_config(payload)
    raw = payload.get("allowed_users", config.get("allowed_users"))
    if raw is None:
        raw = payload.get("allow_from", config.get("allow_from"))

    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(part).strip() for part in raw]
    else:
        values = []

    normalized: List[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        cleaned = _clean_discord_id(value)
        if not _DISCORD_SNOWFLAKE_RE.match(cleaned):
            errors.append(
                {
                    "path": "allowed_users",
                    "message": "Discord allowed user IDs must be numeric Discord IDs.",
                }
            )
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)

    if not normalized:
        errors.append(
            {
                "path": "allowed_users",
                "message": "At least one Discord allowed user ID is required.",
            }
        )

    return normalized


def _discord_home_channel_from_payload(
    payload: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    config = _payload_config(payload)
    raw = payload.get("home_channel", config.get("home_channel"))

    if raw is None or raw == "":
        return None

    if isinstance(raw, dict):
        chat_id = raw.get("chat_id") or raw.get("id")
        name = raw.get("name") or "Home"
        thread_id = raw.get("thread_id")
    else:
        chat_id = raw
        name = "Home"
        thread_id = None

    chat_id_text = _clean_discord_id(str(chat_id).strip() if chat_id is not None else "")
    if not chat_id_text:
        return None

    if not _DISCORD_SNOWFLAKE_RE.match(chat_id_text):
        errors.append(
            {
                "path": "home_channel",
                "message": "Discord home channel must be a numeric Discord channel ID.",
            }
        )

    result = {
        "platform": DISCORD_PLATFORM_ID,
        "chat_id": chat_id_text,
        "name": str(name or "Home"),
    }
    if thread_id is not None and str(thread_id).strip():
        thread_id_text = _clean_discord_id(str(thread_id).strip())
        if not _DISCORD_SNOWFLAKE_RE.match(thread_id_text):
            errors.append(
                {
                    "path": "home_channel.thread_id",
                    "message": "Discord home channel thread ID must be numeric.",
                }
            )
        else:
            result["thread_id"] = thread_id_text
    return result


def _discord_toolsets_from_payload(payload: Dict[str, Any]) -> List[str]:
    config = _payload_config(payload)
    raw = (
        payload.get("enabled_toolsets")
        or payload.get("toolsets")
        or config.get("enabled_toolsets")
        or config.get("toolsets")
    )
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = [str(part).strip() for part in raw]
    else:
        candidates = list(_DISCORD_DEFAULT_TOOLSETS)

    allowed = set(_DISCORD_DEFAULT_TOOLSETS)
    normalized = [item for item in candidates if item in allowed]
    if not normalized:
        return list(_DISCORD_DEFAULT_TOOLSETS)

    result: List[str] = []
    for item in normalized:
        if item not in result:
            result.append(item)
    return result


def _discord_channel_list_from_payload(
    payload: Dict[str, Any],
    key: str,
    *,
    default_home_channel: Optional[Dict[str, str]],
    errors: List[Dict[str, str]],
) -> List[str]:
    config = _payload_config(payload)
    raw = payload.get(key, config.get(key))
    if raw is None:
        if default_home_channel and default_home_channel.get("chat_id"):
            return [default_home_channel["chat_id"]]
        return []

    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(part).strip() for part in raw]
    else:
        values = []

    normalized: List[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        cleaned = _clean_discord_id(value)
        if not _DISCORD_SNOWFLAKE_RE.match(cleaned):
            errors.append(
                {
                    "path": key,
                    "message": f"Discord {key} values must be numeric channel IDs.",
                }
            )
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


def _bool_from_payload(
    payload: Dict[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    config = _payload_config(payload)
    raw = payload.get(key, config.get(key, default))
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _clean_discord_id(value: str) -> str:
    text = value.strip()
    for prefix, suffix in (("<@", ">"), ("<@!", ">"), ("<#", ">")):
        if text.startswith(prefix) and text.endswith(suffix):
            return text[len(prefix):-len(suffix)].strip()
    return text


def _discord_config_override(state: Dict[str, Any]) -> Dict[str, Any]:
    credentials = state.get("credentials") if isinstance(state, dict) else {}
    configuration = state.get("configuration") if isinstance(state, dict) else {}
    token = credentials.get("bot_token") if isinstance(credentials, dict) else None
    allowed_users = (
        configuration.get("allowed_users") if isinstance(configuration, dict) else []
    )
    allowed_roles = (
        configuration.get("allowed_roles") if isinstance(configuration, dict) else []
    )
    home_channel = (
        configuration.get("home_channel") if isinstance(configuration, dict) else None
    )
    allowed_channels = (
        configuration.get("allowed_channels") if isinstance(configuration, dict) else []
    )
    free_response_channels = (
        configuration.get("free_response_channels")
        if isinstance(configuration, dict)
        else []
    )

    extra: Dict[str, Any] = {}
    if isinstance(allowed_users, list):
        extra["allow_from"] = [str(value) for value in allowed_users]
        extra["allowed_users"] = [str(value) for value in allowed_users]
    if isinstance(allowed_roles, list) and allowed_roles:
        extra["allowed_roles"] = [str(value) for value in allowed_roles]
    if isinstance(allowed_channels, list) and allowed_channels:
        extra["allowed_channels"] = [str(value) for value in allowed_channels]
    if isinstance(free_response_channels, list) and free_response_channels:
        extra["free_response_channels"] = [
            str(value) for value in free_response_channels
        ]
    if isinstance(configuration, dict) and "require_mention" in configuration:
        extra["require_mention"] = bool(configuration["require_mention"])

    override: Dict[str, Any] = {
        "enabled": bool(token),
        "extra": extra,
    }
    if isinstance(token, str) and token:
        override["token"] = token
    if isinstance(home_channel, dict) and home_channel.get("chat_id"):
        override["home_channel"] = dict(home_channel)
    return override


def _validate_whatsapp_payload(payload: Any) -> Dict[str, Any]:
    state, errors = validate_whatsapp_payload(
        payload,
        runtime_state_version=RUNTIME_STATE_VERSION,
        utc_now_iso=_utc_now_iso,
    )
    if state is None:
        raise PlatformValidationError(WHATSAPP_PLATFORM_ID, errors)
    return state


def _apply_platform_env(
    platform: str,
    state: Dict[str, Any],
    *,
    overwrite: bool,
    include_token: bool = True,
) -> None:
    configuration = state.get("configuration") if isinstance(state, dict) else {}
    credentials = state.get("credentials") if isinstance(state, dict) else {}
    allowed_users = (
        configuration.get("allowed_users") if isinstance(configuration, dict) else []
    )
    if platform == WHATSAPP_PLATFORM_ID:
        apply_whatsapp_env(state, set_env=_set_env, overwrite=overwrite)
        return
    if platform == TELEGRAM_PLATFORM_ID:
        prefix = "TELEGRAM"
    elif platform == DISCORD_PLATFORM_ID:
        prefix = "DISCORD"
    else:
        return

    allowed_roles = (
        configuration.get("allowed_roles") if isinstance(configuration, dict) else []
    )
    home_channel = (
        configuration.get("home_channel") if isinstance(configuration, dict) else {}
    )
    token = credentials.get("bot_token") if isinstance(credentials, dict) else None

    _set_env(f"{prefix}_ALLOWED_USERS", ",".join(str(v) for v in allowed_users), overwrite)
    if platform == DISCORD_PLATFORM_ID and isinstance(configuration, dict):
        if isinstance(allowed_roles, list):
            _set_env(
                "DISCORD_ALLOWED_ROLES",
                ",".join(str(v) for v in allowed_roles),
                overwrite,
            )
        allowed_channels = configuration.get("allowed_channels")
        free_response_channels = configuration.get("free_response_channels")
        if isinstance(allowed_channels, list):
            _set_env(
                "DISCORD_ALLOWED_CHANNELS",
                ",".join(str(v) for v in allowed_channels),
                overwrite,
            )
        if isinstance(free_response_channels, list):
            _set_env(
                "DISCORD_FREE_RESPONSE_CHANNELS",
                ",".join(str(v) for v in free_response_channels),
                overwrite,
            )
        if "require_mention" in configuration:
            _set_env(
                "DISCORD_REQUIRE_MENTION",
                str(bool(configuration["require_mention"])).lower(),
                overwrite,
            )
    if isinstance(home_channel, dict):
        _set_env(f"{prefix}_HOME_CHANNEL", str(home_channel.get("chat_id") or ""), overwrite)
        if home_channel.get("name"):
            _set_env(
                f"{prefix}_HOME_CHANNEL_NAME",
                str(home_channel.get("name") or "Home"),
                overwrite,
            )
        _set_env(
            f"{prefix}_HOME_CHANNEL_THREAD_ID",
            str(home_channel.get("thread_id") or ""),
            overwrite,
        )
    elif overwrite:
        _set_env(f"{prefix}_HOME_CHANNEL", "", overwrite)
        _set_env(f"{prefix}_HOME_CHANNEL_NAME", "", overwrite)
        _set_env(f"{prefix}_HOME_CHANNEL_THREAD_ID", "", overwrite)
    if include_token and isinstance(token, str):
        _set_env(f"{prefix}_BOT_TOKEN", token, overwrite)


def _set_env(key: str, value: str, overwrite: bool) -> None:
    if not value:
        if overwrite:
            os.environ.pop(key, None)
        return
    if overwrite or not os.getenv(key):
        os.environ[key] = value


async def _hot_apply_telegram(state: Dict[str, Any], runner: Any) -> Dict[str, Any]:
    return await _hot_apply_platform(
        state,
        runner,
        platform_id=TELEGRAM_PLATFORM_ID,
        label="Telegram",
        configure_platform_config=_configure_telegram_platform_config,
    )


async def _hot_apply_discord(state: Dict[str, Any], runner: Any) -> Dict[str, Any]:
    return await _hot_apply_platform(
        state,
        runner,
        platform_id=DISCORD_PLATFORM_ID,
        label="Discord",
        configure_platform_config=_configure_discord_platform_config,
    )


async def _hot_apply_whatsapp(state: Dict[str, Any], runner: Any) -> Dict[str, Any]:
    return await _hot_apply_platform(
        state,
        runner,
        platform_id=WHATSAPP_PLATFORM_ID,
        label="WhatsApp",
        configure_platform_config=configure_whatsapp_platform_config,
        token_key=None,
    )


async def _hot_apply_platform(
    state: Dict[str, Any],
    runner: Any,
    *,
    platform_id: str,
    label: str,
    configure_platform_config: Any,
    token_key: Optional[str] = "bot_token",
) -> Dict[str, Any]:
    required_attrs = ("config", "adapters", "_create_adapter")
    if not all(hasattr(runner, attr) for attr in required_attrs):
        return _restart_required_result("Gateway runner does not expose hot-apply hooks.")

    try:
        from gateway.config import HomeChannel, Platform, PlatformConfig

        platform = Platform(platform_id)
        platform_config = runner.config.platforms.get(platform)
        if platform_config is None:
            platform_config = PlatformConfig()
            runner.config.platforms[platform] = platform_config

        credentials = state.get("credentials") or {}
        _apply_platform_env(
            platform_id,
            state,
            overwrite=True,
            include_token=False,
        )
        configure_platform_config(platform_config, state, HomeChannel)

        platform_config.enabled = True
        if token_key is not None:
            platform_config.token = credentials[token_key]
        _apply_platform_env(platform_id, state, overwrite=True)

        existing = runner.adapters.get(platform)
        if existing is not None:
            safe_disconnect = getattr(runner, "_safe_adapter_disconnect", None)
            if safe_disconnect is not None:
                result = safe_disconnect(existing, platform)
                if asyncio.iscoroutine(result):
                    await result
            else:
                await existing.disconnect()
            runner.adapters.pop(platform, None)

        failed_platforms = getattr(runner, "_failed_platforms", None)
        if isinstance(failed_platforms, dict):
            failed_platforms.pop(platform, None)

        adapter = runner._create_adapter(platform, platform_config)
        if adapter is None:
            _update_platform_runtime_status(
                runner,
                platform.value,
                state="startup_failed",
                error_code="adapter_unavailable",
                error_message=f"{label} adapter could not be created.",
            )
            return {
                "applied": False,
                "restart_required": False,
                "state": "startup_failed",
                "error_code": "adapter_unavailable",
                "error_message": f"{label} adapter could not be created.",
            }

        _wire_adapter(runner, adapter)
        success = await _connect_adapter(runner, adapter, platform)
        if success:
            runner.adapters[platform] = adapter
            _sync_runner_delivery_router(runner, platform_id)
            _update_platform_runtime_status(
                runner,
                platform.value,
                state="connected",
                error_code=None,
                error_message=None,
            )
            await _rebuild_channel_directory(runner)
            return {
                "applied": True,
                "restart_required": False,
                "state": "connected",
                "error_code": None,
                "error_message": None,
            }

        error_code = getattr(adapter, "fatal_error_code", None) or "platform_apply_failed"
        error_message = _redact_state_secrets(
            state,
            getattr(adapter, "fatal_error_message", None)
            or f"{label} adapter failed to connect.",
        )
        state_name = (
            "fatal"
            if getattr(adapter, "has_fatal_error", False)
            and not getattr(adapter, "fatal_error_retryable", True)
            else "startup_failed"
        )
        _update_platform_runtime_status(
            runner,
            platform.value,
            state=state_name,
            error_code=error_code,
            error_message=error_message,
        )
        return {
            "applied": False,
            "restart_required": False,
            "state": state_name,
            "error_code": error_code,
            "error_message": error_message,
        }
    except Exception as exc:
        error_message = _redact_state_secrets(
            state,
            str(exc) or f"{label} platform apply failed.",
        )
        _update_platform_runtime_status(
            runner,
            platform_id,
            state="startup_failed",
            error_code="platform_apply_failed",
            error_message=error_message,
        )
        return {
            "applied": False,
            "restart_required": False,
            "state": "startup_failed",
            "error_code": "platform_apply_failed",
            "error_message": error_message,
        }


def _configure_telegram_platform_config(
    platform_config: Any,
    state: Dict[str, Any],
    HomeChannel: Any,
) -> None:
    configuration = state["configuration"]
    platform_config.extra = dict(platform_config.extra or {})
    platform_config.extra["allow_from"] = list(configuration["allowed_users"])
    platform_config.extra["allowed_users"] = list(configuration["allowed_users"])
    platform_config.home_channel = HomeChannel.from_dict(
        dict(configuration["home_channel"])
    )


def _configure_discord_platform_config(
    platform_config: Any,
    state: Dict[str, Any],
    HomeChannel: Any,
) -> None:
    configuration = state["configuration"]
    platform_config.extra = dict(platform_config.extra or {})
    platform_config.extra["allow_from"] = list(configuration["allowed_users"])
    platform_config.extra["allowed_users"] = list(configuration["allowed_users"])
    platform_config.extra["allowed_roles"] = list(
        configuration.get("allowed_roles") or []
    )
    platform_config.extra["allowed_channels"] = list(
        configuration.get("allowed_channels") or []
    )
    platform_config.extra["free_response_channels"] = list(
        configuration.get("free_response_channels") or []
    )
    platform_config.extra["require_mention"] = bool(
        configuration.get("require_mention", True)
    )
    home_channel = configuration.get("home_channel")
    if isinstance(home_channel, dict) and home_channel.get("chat_id"):
        platform_config.home_channel = HomeChannel.from_dict(dict(home_channel))
    else:
        platform_config.home_channel = None


def _restart_required_result(message: str) -> Dict[str, Any]:
    return {
        "applied": False,
        "restart_required": True,
        "state": "restart_required",
        "error_code": "restart_required",
        "error_message": message,
    }


def _wire_adapter(runner: Any, adapter: Any) -> None:
    handlers = (
        ("set_message_handler", "_handle_message"),
        ("set_fatal_error_handler", "_handle_adapter_fatal_error"),
        ("set_session_store", "session_store"),
        ("set_busy_session_handler", "_handle_active_session_busy_message"),
    )
    for setter_name, attr_name in handlers:
        setter = getattr(adapter, setter_name, None)
        value = getattr(runner, attr_name, None)
        if setter is not None and value is not None:
            setter(value)


async def _connect_adapter(runner: Any, adapter: Any, platform: Any) -> bool:
    connect_with_timeout = getattr(runner, "_connect_adapter_with_timeout", None)
    if connect_with_timeout is not None:
        result = connect_with_timeout(adapter, platform)
    else:
        result = adapter.connect()
    if asyncio.iscoroutine(result):
        result = await result
    return bool(result)


def _sync_runner_delivery_router(
    runner: Any,
    platform_id: str = TELEGRAM_PLATFORM_ID,
) -> None:
    sync_voice = getattr(runner, "_sync_voice_mode_state_to_adapter", None)
    platform = _platform_enum(platform_id)
    if sync_voice is not None and platform is not None:
        adapter = runner.adapters.get(platform)
        if adapter is not None:
            try:
                sync_voice(adapter)
            except Exception:
                pass

    delivery_router = getattr(runner, "delivery_router", None)
    if delivery_router is not None:
        try:
            delivery_router.adapters = runner.adapters
        except Exception:
            pass


async def _rebuild_channel_directory(runner: Any) -> None:
    try:
        from gateway.channel_directory import build_channel_directory

        result = build_channel_directory(runner.adapters)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


def _update_platform_runtime_status(
    runner: Any,
    platform: str,
    *,
    state: str,
    error_code: Optional[str],
    error_message: Optional[str],
) -> None:
    updater = getattr(runner, "_update_platform_runtime_status", None)
    if updater is not None:
        try:
            updater(
                platform,
                platform_state=state,
                error_code=error_code,
                error_message=error_message,
            )
            return
        except Exception:
            pass
    try:
        from gateway.status import write_runtime_status

        write_runtime_status(
            platform=platform,
            platform_state=state,
            error_code=error_code,
            error_message=error_message,
        )
    except Exception:
        pass


def _telegram_status(
    *,
    runner: Any = None,
    persisted_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    persisted_state = persisted_state or _read_platform_state(TELEGRAM_PLATFORM_ID)
    runtime = _runtime_platform_status(TELEGRAM_PLATFORM_ID)
    runner_config = _runner_platform_config(runner, TELEGRAM_PLATFORM_ID)

    token = _telegram_token_for_status(persisted_state, runner_config)
    allowed_users = _telegram_allowed_users_for_status(persisted_state, runner_config)
    home_channel = _telegram_home_channel_for_status(persisted_state, runner_config)

    state = runtime.get("state") if runtime else None
    if _runner_platform_connected(runner, TELEGRAM_PLATFORM_ID):
        state = "connected"
    if not state:
        state = "disconnected"

    return {
        "id": TELEGRAM_PLATFORM_ID,
        "name": "Telegram",
        "supported": True,
        "configured": bool(token),
        "state": state,
        "connected": state == "connected",
        "credential_fingerprint": _secret_fingerprint(token),
        "allowed_users": allowed_users,
        "home_channel": home_channel,
        "error_code": runtime.get("error_code") if runtime else None,
        "error_message": _redact_state_secrets(
            persisted_state,
            runtime.get("error_message") if runtime else None,
            fallback_secrets=[token],
        ),
        "updated_at": runtime.get("updated_at") if runtime else _state_updated_at(persisted_state),
        "capabilities": {
            "configure": True,
            "hot_apply": True,
        },
    }


def _discord_status(
    *,
    runner: Any = None,
    persisted_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    persisted_state = persisted_state or _read_platform_state(DISCORD_PLATFORM_ID)
    runtime = _runtime_platform_status(DISCORD_PLATFORM_ID)
    runner_config = _runner_platform_config(runner, DISCORD_PLATFORM_ID)

    token = _discord_token_for_status(persisted_state, runner_config)
    allowed_users = _discord_allowed_users_for_status(persisted_state, runner_config)
    allowed_roles = _discord_allowed_roles_for_status(persisted_state, runner_config)
    home_channel = _discord_home_channel_for_status(persisted_state, runner_config)

    state = runtime.get("state") if runtime else None
    if _runner_platform_connected(runner, DISCORD_PLATFORM_ID):
        state = "connected"
    if not state:
        state = "disconnected"

    return {
        "id": DISCORD_PLATFORM_ID,
        "name": "Discord",
        "supported": True,
        "configured": bool(token),
        "state": state,
        "connected": state == "connected",
        "credential_fingerprint": _secret_fingerprint(token),
        "allowed_users": allowed_users,
        "allowed_roles": allowed_roles,
        "home_channel": home_channel,
        "error_code": runtime.get("error_code") if runtime else None,
        "error_message": _redact_state_secrets(
            persisted_state,
            runtime.get("error_message") if runtime else None,
            fallback_secrets=[token],
        ),
        "updated_at": runtime.get("updated_at") if runtime else _state_updated_at(persisted_state),
        "capabilities": {
            "configure": True,
            "hot_apply": True,
        },
    }


def _whatsapp_status(
    *,
    runner: Any = None,
    persisted_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    persisted_state = persisted_state or _read_platform_state(WHATSAPP_PLATFORM_ID)
    return whatsapp_status(
        runner=runner,
        persisted_state=persisted_state,
        runtime_platform_status=_runtime_platform_status,
        runner_platform_config=_runner_platform_config,
        runner_platform_connected=_runner_platform_connected,
        state_updated_at=_state_updated_at,
        redact_state_secrets=_redact_state_secrets,
    )


def _state_updated_at(state: Optional[Dict[str, Any]]) -> Optional[str]:
    if isinstance(state, dict):
        updated_at = state.get("updated_at")
        if isinstance(updated_at, str):
            return updated_at
    return None


def _runtime_platform_status(platform: str) -> Dict[str, Any]:
    try:
        from gateway.status import read_runtime_status

        runtime = read_runtime_status() or {}
    except Exception:
        runtime = {}
    platforms = runtime.get("platforms") if isinstance(runtime, dict) else {}
    if not isinstance(platforms, dict):
        return {}
    record = platforms.get(platform) or {}
    return dict(record) if isinstance(record, dict) else {}


def _runner_platform_config(runner: Any, platform: str) -> Any:
    enum_value = _platform_enum(platform)
    config = getattr(runner, "config", None)
    platforms = getattr(config, "platforms", None)
    if enum_value is None or not isinstance(platforms, dict):
        return None
    return platforms.get(enum_value)


def _platform_enum(platform: str) -> Any:
    try:
        from gateway.config import Platform

        return Platform(platform)
    except Exception:
        return None


def _runner_platform_connected(runner: Any, platform: str) -> bool:
    enum_value = _platform_enum(platform)
    adapters = getattr(runner, "adapters", None)
    if enum_value is None or not isinstance(adapters, dict):
        return False
    adapter = adapters.get(enum_value)
    return bool(adapter is not None and getattr(adapter, "is_connected", False))


def _telegram_token_for_status(
    state: Optional[Dict[str, Any]],
    platform_config: Any,
) -> str:
    config_token = getattr(platform_config, "token", None)
    if isinstance(config_token, str) and config_token:
        return config_token
    env_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if env_token:
        return env_token
    credentials = state.get("credentials") if isinstance(state, dict) else {}
    token = credentials.get("bot_token") if isinstance(credentials, dict) else None
    if isinstance(token, str) and token:
        return token
    return ""


def _telegram_allowed_users_for_status(
    state: Optional[Dict[str, Any]],
    platform_config: Any,
) -> List[str]:
    env_allowed = os.getenv("TELEGRAM_ALLOWED_USERS", "")
    if env_allowed:
        return [part.strip() for part in env_allowed.split(",") if part.strip()]

    allowed = None
    if platform_config is not None:
        extra = getattr(platform_config, "extra", None) or {}
        if isinstance(extra, dict):
            allowed = extra.get("allow_from") or extra.get("allowed_users")
    if allowed is None:
        configuration = state.get("configuration") if isinstance(state, dict) else {}
        allowed = (
            configuration.get("allowed_users") if isinstance(configuration, dict) else None
        )
    if isinstance(allowed, str):
        return [part.strip() for part in allowed.split(",") if part.strip()]
    if isinstance(allowed, (list, tuple, set)):
        return [str(part).strip() for part in allowed if str(part).strip()]
    return []


def _telegram_home_channel_for_status(
    state: Optional[Dict[str, Any]],
    platform_config: Any,
) -> Optional[Dict[str, Any]]:
    env_home = os.getenv("TELEGRAM_HOME_CHANNEL")
    if env_home:
        home = {
            "platform": TELEGRAM_PLATFORM_ID,
            "chat_id": env_home,
            "name": os.getenv("TELEGRAM_HOME_CHANNEL_NAME", "Home"),
        }
        thread_id = os.getenv("TELEGRAM_HOME_CHANNEL_THREAD_ID")
        if thread_id:
            home["thread_id"] = thread_id
        return _public_home_channel(home)

    config_home = getattr(platform_config, "home_channel", None)
    if config_home is not None:
        to_dict = getattr(config_home, "to_dict", None)
        if to_dict is not None:
            try:
                return _public_home_channel(to_dict())
            except Exception:
                pass

    configuration = state.get("configuration") if isinstance(state, dict) else {}
    home = configuration.get("home_channel") if isinstance(configuration, dict) else None
    if isinstance(home, dict) and home.get("chat_id"):
        return _public_home_channel(home)
    return None


def _discord_token_for_status(
    state: Optional[Dict[str, Any]],
    platform_config: Any,
) -> str:
    config_token = getattr(platform_config, "token", None)
    if isinstance(config_token, str) and config_token:
        return config_token
    env_token = os.getenv("DISCORD_BOT_TOKEN", "")
    if env_token:
        return env_token
    credentials = state.get("credentials") if isinstance(state, dict) else {}
    token = credentials.get("bot_token") if isinstance(credentials, dict) else None
    if isinstance(token, str) and token:
        return token
    return ""


def _discord_allowed_users_for_status(
    state: Optional[Dict[str, Any]],
    platform_config: Any,
) -> List[str]:
    env_allowed = os.getenv("DISCORD_ALLOWED_USERS", "")
    if env_allowed:
        return [part.strip() for part in env_allowed.split(",") if part.strip()]

    allowed = None
    if platform_config is not None:
        extra = getattr(platform_config, "extra", None) or {}
        if isinstance(extra, dict):
            allowed = extra.get("allow_from") or extra.get("allowed_users")
    if allowed is None:
        configuration = state.get("configuration") if isinstance(state, dict) else {}
        allowed = (
            configuration.get("allowed_users") if isinstance(configuration, dict) else None
        )
    if isinstance(allowed, str):
        return [part.strip() for part in allowed.split(",") if part.strip()]
    if isinstance(allowed, (list, tuple, set)):
        return [str(part).strip() for part in allowed if str(part).strip()]
    return []


def _discord_allowed_roles_for_status(
    state: Optional[Dict[str, Any]],
    platform_config: Any,
) -> List[str]:
    env_allowed = os.getenv("DISCORD_ALLOWED_ROLES", "")
    if env_allowed:
        return [part.strip() for part in env_allowed.split(",") if part.strip()]

    allowed = None
    if platform_config is not None:
        extra = getattr(platform_config, "extra", None) or {}
        if isinstance(extra, dict):
            allowed = extra.get("allowed_roles")
    if allowed is None:
        configuration = state.get("configuration") if isinstance(state, dict) else {}
        allowed = (
            configuration.get("allowed_roles") if isinstance(configuration, dict) else None
        )
    if isinstance(allowed, str):
        return [part.strip() for part in allowed.split(",") if part.strip()]
    if isinstance(allowed, (list, tuple, set)):
        return [str(part).strip() for part in allowed if str(part).strip()]
    return []


def _discord_home_channel_for_status(
    state: Optional[Dict[str, Any]],
    platform_config: Any,
) -> Optional[Dict[str, Any]]:
    env_home = os.getenv("DISCORD_HOME_CHANNEL")
    if env_home:
        home = {
            "platform": DISCORD_PLATFORM_ID,
            "chat_id": env_home,
            "name": os.getenv("DISCORD_HOME_CHANNEL_NAME", "Home"),
        }
        thread_id = os.getenv("DISCORD_HOME_CHANNEL_THREAD_ID")
        if thread_id:
            home["thread_id"] = thread_id
        return _public_home_channel(home, default_platform=DISCORD_PLATFORM_ID)

    config_home = getattr(platform_config, "home_channel", None)
    if config_home is not None:
        to_dict = getattr(config_home, "to_dict", None)
        if to_dict is not None:
            try:
                return _public_home_channel(
                    to_dict(),
                    default_platform=DISCORD_PLATFORM_ID,
                )
            except Exception:
                pass

    configuration = state.get("configuration") if isinstance(state, dict) else {}
    home = configuration.get("home_channel") if isinstance(configuration, dict) else None
    if isinstance(home, dict) and home.get("chat_id"):
        return _public_home_channel(home, default_platform=DISCORD_PLATFORM_ID)
    return None


def _start_whatsapp_pairing(state: Dict[str, Any]) -> Dict[str, Any]:
    return start_whatsapp_pairing(
        state,
        apply_env=_apply_platform_env,
        utc_now_iso=_utc_now_iso,
    )


def _public_home_channel(
    home: Dict[str, Any],
    *,
    default_platform: str = TELEGRAM_PLATFORM_ID,
) -> Dict[str, Any]:
    result = {
        "platform": str(home.get("platform") or default_platform),
        "chat_id": str(home.get("chat_id") or ""),
        "name": str(home.get("name") or "Home"),
    }
    if home.get("thread_id"):
        result["thread_id"] = str(home["thread_id"])
    return result
