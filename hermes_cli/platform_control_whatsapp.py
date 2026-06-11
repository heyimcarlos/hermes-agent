"""WhatsApp-specific platform-control helpers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_dir


WHATSAPP_PLATFORM_ID = "whatsapp"

_WHATSAPP_PHONE_RE = re.compile(r"^[1-9]\d{5,19}$")
_WHATSAPP_MODES = ("bot", "self-chat")


def validate_whatsapp_payload(
    payload: Any,
    *,
    runtime_state_version: int,
    utc_now_iso: Callable[[], str],
) -> tuple[Optional[Dict[str, Any]], List[Dict[str, str]]]:
    if not isinstance(payload, dict):
        return None, [{"path": "", "message": "Request body must be a JSON object."}]

    errors: List[Dict[str, str]] = []
    mode = _whatsapp_mode_from_payload(payload, errors)
    allowed_users = _whatsapp_allowed_users_from_payload(payload, errors)
    dm_policy = _whatsapp_policy_from_payload(
        payload,
        "dm_policy",
        default="allowlist",
        allowed=("open", "allowlist", "disabled"),
        errors=errors,
    )
    group_policy = _whatsapp_policy_from_payload(
        payload,
        "group_policy",
        default="disabled",
        allowed=("open", "allowlist", "disabled"),
        errors=errors,
    )
    restart_pairing = _bool_from_payload(payload, "restart_pairing", default=False)

    if errors:
        return None, errors

    return {
        "version": runtime_state_version,
        "platform": WHATSAPP_PLATFORM_ID,
        "updated_at": utc_now_iso(),
        "credentials": {},
        "configuration": {
            "mode": mode,
            "allowed_users": allowed_users,
            "dm_policy": dm_policy,
            "group_policy": group_policy,
            "restart_pairing": restart_pairing,
        },
    }, []


def whatsapp_config_override(state: Dict[str, Any]) -> Dict[str, Any]:
    configuration = state.get("configuration") if isinstance(state, dict) else {}
    mode = (
        configuration.get("mode", "bot")
        if isinstance(configuration, dict)
        else "bot"
    )
    allowed_users = (
        configuration.get("allowed_users") if isinstance(configuration, dict) else []
    )
    dm_policy = (
        configuration.get("dm_policy", "allowlist")
        if isinstance(configuration, dict)
        else "allowlist"
    )
    group_policy = (
        configuration.get("group_policy", "disabled")
        if isinstance(configuration, dict)
        else "disabled"
    )

    extra: Dict[str, Any] = {
        "mode": mode,
        "dm_policy": dm_policy,
        "group_policy": group_policy,
    }
    if isinstance(allowed_users, list):
        extra["allow_from"] = [str(value) for value in allowed_users]

    return {
        "enabled": True,
        "extra": extra,
    }


def apply_whatsapp_env(
    state: Dict[str, Any],
    *,
    set_env: Callable[[str, str, bool], None],
    overwrite: bool,
) -> None:
    configuration = state.get("configuration") if isinstance(state, dict) else {}
    allowed_users = (
        configuration.get("allowed_users") if isinstance(configuration, dict) else []
    )
    set_env("WHATSAPP_ALLOWED_USERS", ",".join(str(v) for v in allowed_users), overwrite)
    if not isinstance(configuration, dict):
        return
    set_env("WHATSAPP_ENABLED", "true", overwrite)
    set_env("WHATSAPP_MODE", str(configuration.get("mode") or "bot"), overwrite)
    set_env(
        "WHATSAPP_DM_POLICY",
        str(configuration.get("dm_policy") or "allowlist"),
        overwrite,
    )
    set_env(
        "WHATSAPP_GROUP_POLICY",
        str(configuration.get("group_policy") or "disabled"),
        overwrite,
    )


def configure_whatsapp_platform_config(
    platform_config: Any,
    state: Dict[str, Any],
    _HomeChannel: Any,
) -> None:
    configuration = state["configuration"]
    platform_config.extra = dict(platform_config.extra or {})
    platform_config.extra["mode"] = str(configuration.get("mode") or "bot")
    platform_config.extra["allow_from"] = list(configuration.get("allowed_users") or [])
    platform_config.extra["dm_policy"] = str(
        configuration.get("dm_policy") or "allowlist"
    )
    platform_config.extra["group_policy"] = str(
        configuration.get("group_policy") or "disabled"
    )


def whatsapp_status(
    *,
    runner: Any = None,
    persisted_state: Optional[Dict[str, Any]] = None,
    runtime_platform_status: Callable[[str], Dict[str, Any]],
    runner_platform_config: Callable[[Any, str], Any],
    runner_platform_connected: Callable[[Any, str], bool],
    state_updated_at: Callable[[Optional[Dict[str, Any]]], Optional[str]],
    redact_state_secrets: Callable[[Optional[Dict[str, Any]], Any], Optional[str]],
) -> Dict[str, Any]:
    runtime = runtime_platform_status(WHATSAPP_PLATFORM_ID)
    runner_config = runner_platform_config(runner, WHATSAPP_PLATFORM_ID)
    configuration = (
        persisted_state.get("configuration") if isinstance(persisted_state, dict) else {}
    )
    allowed_users = whatsapp_allowed_users_for_status(persisted_state, runner_config)
    paired = whatsapp_session_paired(persisted_state)
    pairing = whatsapp_pairing_status(persisted_state)

    state = runtime.get("state") if runtime else None
    if runner_platform_connected(runner, WHATSAPP_PLATFORM_ID):
        state = "connected"
    if not state:
        if pairing and pairing.get("status") in {"qr_ready", "pairing"}:
            state = "pairing"
        elif paired:
            state = "paired"
        elif persisted_state:
            state = "not_paired"
        else:
            state = "disconnected"

    return {
        "id": WHATSAPP_PLATFORM_ID,
        "name": "WhatsApp",
        "supported": True,
        "configured": bool(persisted_state or paired),
        "state": state,
        "connected": state == "connected",
        "credential_fingerprint": None,
        "allowed_users": allowed_users,
        "home_channel": None,
        "pairing": pairing,
        "mode": (
            configuration.get("mode", "bot")
            if isinstance(configuration, dict)
            else "bot"
        ),
        "error_code": runtime.get("error_code") if runtime else (
            None if paired else "whatsapp_not_paired" if persisted_state else None
        ),
        "error_message": redact_state_secrets(
            persisted_state,
            runtime.get("error_message") if runtime else (
                None
                if paired or not persisted_state
                else "WhatsApp is enabled but not paired."
            ),
        ),
        "updated_at": runtime.get("updated_at") if runtime else state_updated_at(persisted_state),
        "capabilities": {
            "configure": True,
            "hot_apply": True,
        },
    }


def whatsapp_session_paired(state: Optional[Dict[str, Any]] = None) -> bool:
    return (whatsapp_session_dir(state) / "creds.json").exists()


def start_whatsapp_pairing(
    state: Dict[str, Any],
    *,
    apply_env: Callable[[str, Dict[str, Any], bool], None],
    utc_now_iso: Callable[[], str],
) -> Dict[str, Any]:
    configuration = state.get("configuration") if isinstance(state, dict) else {}
    session_dir = whatsapp_session_dir(state)
    session_dir.mkdir(parents=True, exist_ok=True)
    restart_pairing = (
        bool(configuration.get("restart_pairing"))
        if isinstance(configuration, dict)
        else False
    )
    if restart_pairing:
        clear_whatsapp_session(session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)

    apply_env(WHATSAPP_PLATFORM_ID, state, overwrite=True)
    pairing = ensure_whatsapp_pairing_process(state, utc_now_iso=utc_now_iso)
    return {
        "applied": False,
        "restart_required": False,
        "state": "pairing",
        "error_code": "whatsapp_not_paired",
        "error_message": (
            "WhatsApp is waiting for the owner to scan the pairing QR code."
        ),
        "pairing": pairing,
    }


def whatsapp_session_dir(state: Optional[Dict[str, Any]] = None) -> Path:
    configuration = state.get("configuration") if isinstance(state, dict) else {}
    session_path = (
        configuration.get("session_path") if isinstance(configuration, dict) else None
    )
    if isinstance(session_path, str) and session_path.strip():
        return Path(session_path).expanduser()
    return get_hermes_dir("platforms/whatsapp/session", "whatsapp/session")


def whatsapp_pairing_status(
    state: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    path = whatsapp_session_dir(state) / "pairing.json"
    if not path.exists():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    result: Dict[str, Any] = {
        "status": str(data.get("status") or "pairing"),
        "qr": data.get("qr_terminal") or data.get("qr"),
        "mode": data.get("mode"),
        "started_at": data.get("started_at"),
        "updated_at": data.get("updated_at"),
        "expires_at": data.get("expires_at"),
        "log_path": data.get("log_path"),
    }
    return {key: value for key, value in result.items() if value is not None}


def clear_whatsapp_session(session_dir: Path) -> None:
    children = session_dir.iterdir() if session_dir.exists() else ()
    for child in children:
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:
            pass


def ensure_whatsapp_pairing_process(
    state: Dict[str, Any],
    *,
    utc_now_iso: Callable[[], str],
) -> Optional[Dict[str, Any]]:
    session_dir = whatsapp_session_dir(state)
    pairing = whatsapp_pairing_status(state)
    if pairing and pairing.get("status") in {"qr_ready", "pairing"}:
        return pairing

    bridge_script = whatsapp_bridge_script()
    log_path = session_dir.parent / "pairing.log"
    if not bridge_script.exists():
        write_whatsapp_pairing_status(
            session_dir,
            {
                "status": "error",
                "error_message": f"WhatsApp bridge script missing at {bridge_script}.",
                "log_path": str(log_path),
            },
            utc_now_iso=utc_now_iso,
        )
        return whatsapp_pairing_status(state)

    node = shutil.which("node")
    if not node:
        write_whatsapp_pairing_status(
            session_dir,
            {
                "status": "error",
                "error_message": "Node.js is not installed.",
                "log_path": str(log_path),
            },
            utc_now_iso=utc_now_iso,
        )
        return whatsapp_pairing_status(state)

    pid_path = session_dir / "pairing.pid"
    if pidfile_process_running(pid_path):
        return whatsapp_pairing_status(state)

    mode = (
        state.get("configuration", {}).get("mode", "bot")
        if isinstance(state.get("configuration"), dict)
        else "bot"
    )
    write_whatsapp_pairing_status(
        session_dir,
        {
            "status": "pairing",
            "mode": mode,
            "started_at": utc_now_iso(),
            "log_path": str(log_path),
        },
        utc_now_iso=utc_now_iso,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                node,
                str(bridge_script),
                "--pair-only",
                "--session",
                str(session_dir),
                "--mode",
                str(mode),
            ],
            cwd=str(bridge_script.parent),
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            env=os.environ.copy(),
        )
    finally:
        log_fh.close()
    try:
        pid_path.write_text(str(process.pid), encoding="utf-8")
        os.chmod(pid_path, 0o600)
    except OSError:
        pass
    return whatsapp_pairing_status(state)


def whatsapp_bridge_script() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "whatsapp-bridge" / "bridge.js"


def pidfile_process_running(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def write_whatsapp_pairing_status(
    session_dir: Path,
    data: Dict[str, Any],
    *,
    utc_now_iso: Callable[[], str],
) -> None:
    import json

    path = session_dir / "pairing.json"
    existing: Dict[str, Any] = {}
    if path.exists():
        try:
            existing_data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing_data, dict):
                existing = existing_data
        except Exception:
            existing = {}
    merged = {
        **existing,
        **data,
        "updated_at": utc_now_iso(),
    }
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def whatsapp_allowed_users_for_status(
    state: Optional[Dict[str, Any]],
    platform_config: Any,
) -> List[str]:
    env_allowed = os.getenv("WHATSAPP_ALLOWED_USERS", "")
    if env_allowed:
        return [part.strip() for part in env_allowed.split(",") if part.strip()]

    allowed = None
    if platform_config is not None:
        extra = getattr(platform_config, "extra", None) or {}
        if isinstance(extra, dict):
            allowed = extra.get("allow_from")
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


def _payload_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = payload.get("configuration")
    return config if isinstance(config, dict) else {}


def _coerce_string_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(part).strip() for part in raw if str(part).strip()]
    return []


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


def _whatsapp_mode_from_payload(
    payload: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> str:
    config = _payload_config(payload)
    raw = payload.get("mode", config.get("mode", "bot"))
    mode = str(raw or "").strip().lower()
    if mode not in _WHATSAPP_MODES:
        errors.append(
            {
                "path": "mode",
                "message": "WhatsApp mode must be 'bot' or 'self-chat'.",
            }
        )
        return "bot"
    return mode


def _whatsapp_allowed_users_from_payload(
    payload: Dict[str, Any],
    errors: List[Dict[str, str]],
) -> List[str]:
    config = _payload_config(payload)
    raw = payload.get("allowed_users", config.get("allowed_users"))
    if raw is None:
        raw = payload.get("allow_from", config.get("allow_from"))

    values = _coerce_string_list(raw)
    normalized: List[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_whatsapp_phone(value)
        if not _WHATSAPP_PHONE_RE.match(cleaned):
            errors.append(
                {
                    "path": "allowed_users",
                    "message": "WhatsApp allowed users must be phone numbers with country code digits.",
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
                "message": "At least one WhatsApp allowed phone number is required.",
            }
        )

    return normalized


def _clean_whatsapp_phone(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _whatsapp_policy_from_payload(
    payload: Dict[str, Any],
    key: str,
    *,
    default: str,
    allowed: tuple[str, ...],
    errors: List[Dict[str, str]],
) -> str:
    config = _payload_config(payload)
    value = str(payload.get(key, config.get(key, default)) or "").strip().lower()
    if value in allowed:
        return value
    errors.append(
        {
            "path": key,
            "message": f"WhatsApp {key} must be one of: {', '.join(allowed)}.",
        }
    )
    return default
