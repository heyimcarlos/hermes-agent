"""Provider OAuth/device-code flows shared by Hermes HTTP surfaces."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx


CODEX_PROVIDER_ID = "openai-codex"
_CODEX_ISSUER = "https://auth.openai.com"
_SESSION_TTL_SECONDS = 15 * 60

_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def start_provider_oauth(provider_id: str) -> Dict[str, Any]:
    """Start an OAuth flow for a supported provider."""
    provider = _normalize_provider(provider_id)
    if provider == CODEX_PROVIDER_ID:
        return start_codex_device_login()
    raise ValueError(f"Unsupported provider OAuth flow: {provider_id}")


def poll_provider_oauth(provider_id: str, session_id: str) -> Dict[str, Any]:
    """Return non-secret OAuth session status for a supported provider."""
    provider = _normalize_provider(provider_id)
    if provider == CODEX_PROVIDER_ID:
        return poll_codex_device_login(session_id)
    raise ValueError(f"Unsupported provider OAuth flow: {provider_id}")


def start_codex_device_login() -> Dict[str, Any]:
    """Start OpenAI Codex device-code login and poll in the background."""
    _gc_sessions()
    session_id, session = _new_session(CODEX_PROVIDER_ID)
    worker = threading.Thread(
        target=_codex_device_worker,
        args=(session_id,),
        daemon=True,
        name=f"provider-oauth-codex-{session_id[:6]}",
    )
    worker.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with _sessions_lock:
            session = _sessions.get(session_id, {})
            if session.get("user_code") or session.get("status") != "pending":
                break
        time.sleep(0.1)

    with _sessions_lock:
        session = dict(_sessions.get(session_id, session))

    if session.get("status") == "error":
        raise RuntimeError(str(session.get("error_message") or "Codex login failed."))
    if not session.get("user_code"):
        raise TimeoutError("Codex device-code request timed out before returning a user code.")

    return {
        "session_id": session_id,
        "flow": "device_code",
        "user_code": session["user_code"],
        "verification_url": session["verification_url"],
        "verification_uri": session["verification_url"],
        "verification_url_complete": session["verification_url"],
        "verification_uri_complete": session["verification_url"],
        "expires_in": int(session.get("expires_in") or _SESSION_TTL_SECONDS),
        "expires_at": session.get("expires_at"),
        "poll_interval": int(session.get("interval") or 5),
        "interval": int(session.get("interval") or 5),
    }


def poll_codex_device_login(session_id: str) -> Dict[str, Any]:
    """Return non-secret status for a Codex device-code login session."""
    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session:
        raise KeyError("Provider OAuth session not found or expired.")
    if session.get("provider") != CODEX_PROVIDER_ID:
        raise ValueError("Provider mismatch for OAuth session.")

    credential_fingerprint: Optional[str] = None
    if session.get("status") == "approved":
        credential_fingerprint = _codex_token_fingerprint()

    return {
        "session_id": session_id,
        "status": session.get("status", "pending"),
        "message": session.get("error_message"),
        "error_message": session.get("error_message"),
        "credential_fingerprint": credential_fingerprint,
        "token_preview": credential_fingerprint,
        "expires_at": session.get("expires_at"),
    }


def disconnect_codex() -> bool:
    """Clear persisted OpenAI Codex credentials from Hermes auth state."""
    from hermes_cli.auth import clear_provider_auth

    return bool(clear_provider_auth(CODEX_PROVIDER_ID))


def cancel_session(session_id: str) -> bool:
    with _sessions_lock:
        return _sessions.pop(session_id, None) is not None


def _new_session(provider_id: str) -> tuple[str, Dict[str, Any]]:
    session_id = secrets.token_urlsafe(16)
    session = {
        "session_id": session_id,
        "provider": provider_id,
        "created_at": time.time(),
        "status": "pending",
        "error_message": None,
    }
    with _sessions_lock:
        _sessions[session_id] = session
    return session_id, session


def _gc_sessions() -> None:
    cutoff = time.time() - _SESSION_TTL_SECONDS
    with _sessions_lock:
        stale = [
            session_id
            for session_id, session in _sessions.items()
            if float(session.get("created_at") or 0) < cutoff
        ]
        for session_id in stale:
            _sessions.pop(session_id, None)


def _codex_device_worker(session_id: str) -> None:
    try:
        device_data = _request_codex_user_code()
        user_code = str(device_data.get("user_code") or "")
        device_auth_id = str(device_data.get("device_auth_id") or "")
        poll_interval = max(3, int(device_data.get("interval") or 5))
        if not user_code or not device_auth_id:
            raise RuntimeError("Codex device-code response missing user_code or device_auth_id.")

        expires_at = time.time() + _SESSION_TTL_SECONDS
        with _sessions_lock:
            session = _sessions.get(session_id)
            if not session:
                return
            session["user_code"] = user_code
            session["verification_url"] = f"{_CODEX_ISSUER}/codex/device"
            session["device_auth_id"] = device_auth_id
            session["interval"] = poll_interval
            session["expires_in"] = _SESSION_TTL_SECONDS
            session["expires_at"] = expires_at

        code_data = _poll_codex_authorization(
            device_auth_id=device_auth_id,
            user_code=user_code,
            interval_seconds=poll_interval,
            expires_at=expires_at,
        )
        if code_data is None:
            _set_session_status(session_id, "expired", "Device code expired before approval.")
            return

        tokens = _exchange_codex_authorization(code_data)
        access_token = str(tokens.get("access_token") or "")
        refresh_token = str(tokens.get("refresh_token") or "")
        if not access_token:
            raise RuntimeError("Codex token exchange did not return access_token.")

        last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _persist_codex_device_tokens(access_token, refresh_token, last_refresh)
        _set_session_status(session_id, "approved", None)
    except Exception as exc:
        _set_session_status(session_id, "error", str(exc))


def _request_codex_user_code() -> Dict[str, Any]:
    from hermes_cli.auth import CODEX_OAUTH_CLIENT_ID

    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        response = client.post(
            f"{_CODEX_ISSUER}/api/accounts/deviceauth/usercode",
            json={"client_id": CODEX_OAUTH_CLIENT_ID},
            headers={"Content-Type": "application/json"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"Codex device-code request returned status {response.status_code}.")
    return response.json()


def _poll_codex_authorization(
    *,
    device_auth_id: str,
    user_code: str,
    interval_seconds: int,
    expires_at: float,
) -> Optional[Dict[str, Any]]:
    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        while time.time() < expires_at:
            time.sleep(interval_seconds)
            response = client.post(
                f"{_CODEX_ISSUER}/api/accounts/deviceauth/token",
                json={"device_auth_id": device_auth_id, "user_code": user_code},
                headers={"Content-Type": "application/json"},
            )
            if response.status_code == 200:
                return response.json()
            if response.status_code in {403, 404}:
                continue
            raise RuntimeError(f"Codex device-code poll returned status {response.status_code}.")
    return None


def _exchange_codex_authorization(code_data: Dict[str, Any]) -> Dict[str, Any]:
    from hermes_cli.auth import CODEX_OAUTH_CLIENT_ID, CODEX_OAUTH_TOKEN_URL

    authorization_code = str(code_data.get("authorization_code") or "")
    code_verifier = str(code_data.get("code_verifier") or "")
    if not authorization_code or not code_verifier:
        raise RuntimeError("Codex device-auth response missing authorization_code or code_verifier.")

    with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
        response = client.post(
            CODEX_OAUTH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": f"{_CODEX_ISSUER}/deviceauth/callback",
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"Codex token exchange returned status {response.status_code}.")
    return response.json()


def _persist_codex_device_tokens(
    access_token: str,
    refresh_token: str,
    last_refresh: str,
) -> None:
    from agent.credential_pool import (
        AUTH_TYPE_OAUTH,
        SOURCE_MANUAL,
        PooledCredential,
        label_from_token,
        load_pool,
    )
    from hermes_cli.auth import DEFAULT_CODEX_BASE_URL, unsuppress_credential_source

    unsuppress_credential_source(CODEX_PROVIDER_ID, "device_code")
    pool = load_pool(CODEX_PROVIDER_ID)
    label = label_from_token(
        access_token,
        f"OpenAI Codex {len(pool.entries()) + 1}",
    )
    entry = PooledCredential(
        provider=CODEX_PROVIDER_ID,
        id=uuid.uuid4().hex[:6],
        label=label,
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source=f"{SOURCE_MANUAL}:device_code",
        access_token=access_token,
        refresh_token=refresh_token or None,
        base_url=DEFAULT_CODEX_BASE_URL,
        last_refresh=last_refresh,
    )
    pool.add_entry(entry)


def _set_session_status(session_id: str, status: str, message: Optional[str]) -> None:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session:
            session["status"] = status
            session["error_message"] = message


def _codex_token_fingerprint() -> Optional[str]:
    try:
        from hermes_cli.auth import get_codex_auth_status

        raw = get_codex_auth_status()
        token = str(raw.get("api_key") or "")
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"sha256:{digest[:16]}"
    except Exception:
        return None


def _normalize_provider(provider_id: str) -> str:
    from hermes_cli.providers import normalize_provider

    return normalize_provider((provider_id or "").strip())
