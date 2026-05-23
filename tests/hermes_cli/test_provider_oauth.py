"""Tests for Gateway-shared provider OAuth flows."""

from __future__ import annotations

import time

from hermes_cli import provider_oauth


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
        "hermes_cli.auth._save_codex_tokens",
        lambda tokens, last_refresh=None: saved.update(
            {"tokens": tokens, "last_refresh": last_refresh}
        ),
    )
    monkeypatch.setattr(
        provider_oauth,
        "_codex_token_preview",
        lambda: "acce...oken",
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
    assert polled["credential_fingerprint"] == "acce...oken"
    assert "access-secret-token" not in str(polled)
    assert saved["tokens"] == {
        "access_token": "access-secret-token",
        "refresh_token": "refresh-secret-token",
    }
