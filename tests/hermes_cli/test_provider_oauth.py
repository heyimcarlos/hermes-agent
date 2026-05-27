"""Tests for Gateway-shared provider OAuth flows."""

from __future__ import annotations

import json
import time

from hermes_cli import provider_oauth


def test_codex_device_tokens_persist_to_credential_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True)
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {},
                "suppressed_sources": {"openai-codex": ["device_code"]},
            }
        ),
        encoding="utf-8",
    )

    provider_oauth._persist_codex_device_tokens(
        "access-secret-token",
        "refresh-secret-token",
        "2026-01-01T00:00:00Z",
    )

    payload = json.loads(auth_path.read_text(encoding="utf-8"))
    assert "openai-codex" not in payload.get("suppressed_sources", {})
    assert payload.get("providers", {}) == {}
    entries = payload["credential_pool"]["openai-codex"]
    assert len(entries) == 1
    assert entries[0]["source"] == "manual:device_code"
    assert entries[0]["access_token"] == "access-secret-token"
    assert entries[0]["refresh_token"] == "refresh-secret-token"
    assert entries[0]["base_url"] == "https://chatgpt.com/backend-api/codex"


def test_codex_device_login_persists_tokens_without_returning_them(monkeypatch):
    saved = {}

    monkeypatch.setattr(
        provider_oauth,
        "_request_codex_user_code",
        lambda: {
            "user_code": "ABCD-1234",
            "device_auth_id": "device-1",
            "interval": 3,
        },
    )
    monkeypatch.setattr(
        provider_oauth,
        "_poll_codex_authorization",
        lambda **_: {
            "authorization_code": "auth-code",
            "code_verifier": "verifier",
        },
    )
    monkeypatch.setattr(
        provider_oauth,
        "_exchange_codex_authorization",
        lambda _: {
            "access_token": "access-secret-token",
            "refresh_token": "refresh-secret-token",
        },
    )
    monkeypatch.setattr(
        provider_oauth,
        "_persist_codex_device_tokens",
        lambda access_token, refresh_token, last_refresh: saved.update(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "last_refresh": last_refresh,
            }
        ),
    )
    monkeypatch.setattr(
        provider_oauth,
        "_codex_token_fingerprint",
        lambda: "sha256:9e0a7f2a5c4f8b21",
    )

    started = provider_oauth.start_codex_device_login()

    deadline = time.monotonic() + 2
    polled = {"status": "pending"}
    while time.monotonic() < deadline:
        polled = provider_oauth.poll_codex_device_login(started["session_id"])
        if polled["status"] == "approved":
            break
        time.sleep(0.01)

    assert started["user_code"] == "ABCD-1234"
    assert "access-secret-token" not in str(started)
    assert "refresh-secret-token" not in str(started)
    assert polled["status"] == "approved"
    assert polled["credential_fingerprint"] == "sha256:9e0a7f2a5c4f8b21"
    assert "access-secret-token" not in str(polled)
    assert saved["access_token"] == "access-secret-token"
    assert saved["refresh_token"] == "refresh-secret-token"
