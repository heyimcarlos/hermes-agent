"""Tests for generic API-server platform-control routes."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.api_server_platform_control import register_platform_control_routes


VALID_TELEGRAM_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd12345"
VALID_DISCORD_TOKEN = "MTAxMDExMDExMDExMDExMDEx.Mabcde.valid-discord-token"


@pytest.fixture(autouse=True)
def isolated_hermes_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USERS",
        "TELEGRAM_HOME_CHANNEL",
        "TELEGRAM_HOME_CHANNEL_NAME",
        "TELEGRAM_HOME_CHANNEL_THREAD_ID",
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_HOME_CHANNEL",
        "DISCORD_HOME_CHANNEL_NAME",
        "DISCORD_HOME_CHANNEL_THREAD_ID",
        "WHATSAPP_ENABLED",
        "WHATSAPP_MODE",
        "WHATSAPP_ALLOWED_USERS",
        "WHATSAPP_DM_POLICY",
        "WHATSAPP_GROUP_POLICY",
    ):
        monkeypatch.delenv(key, raising=False)
    return hermes_home


def _make_adapter(api_key: str = "sk-secret") -> APIServerAdapter:
    config = PlatformConfig(enabled=True, extra={"key": api_key})
    return APIServerAdapter(config)


def _create_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    register_platform_control_routes(app, adapter)
    return app


def _telegram_payload() -> dict:
    return {
        "bot_token": VALID_TELEGRAM_TOKEN,
        "allowed_users": ["1001", "1002"],
        "home_channel": {"chat_id": "1001", "name": "Owner DM"},
    }


def _discord_payload() -> dict:
    return {
        "bot_token": VALID_DISCORD_TOKEN,
        "allowed_users": ["119991111111111111"],
        "home_channel": {
            "chat_id": "229992222222222222",
            "name": "#agent",
            "thread_id": "339993333333333333",
        },
    }


def _whatsapp_payload() -> dict:
    return {
        "mode": "bot",
        "allowed_users": ["15551234567"],
        "dm_policy": "allowlist",
        "group_policy": "disabled",
    }


class FakePlatformAdapter:
    def __init__(
        self,
        *,
        fatal_error: bool = False,
        fatal_error_code: str | None = None,
        fatal_error_message: str | None = None,
        fatal_error_retryable: bool = True,
    ) -> None:
        self._connected = False
        self.disconnected = False
        self.has_fatal_error = fatal_error
        self.fatal_error_code = fatal_error_code
        self.fatal_error_message = fatal_error_message
        self.fatal_error_retryable = fatal_error_retryable
        self.message_handler = None
        self.fatal_error_handler = None
        self.session_store = None
        self.busy_session_handler = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def set_message_handler(self, handler):
        self.message_handler = handler

    def set_fatal_error_handler(self, handler):
        self.fatal_error_handler = handler

    def set_session_store(self, session_store):
        self.session_store = session_store

    def set_busy_session_handler(self, handler):
        self.busy_session_handler = handler

    async def disconnect(self) -> None:
        self.disconnected = True
        self._connected = False


class FakeGatewayRunner:
    def __init__(
        self,
        *,
        connect_success: bool = True,
        created_adapter: FakePlatformAdapter | None = None,
    ) -> None:
        self.config = GatewayConfig(platforms={})
        self.adapters = {}
        self._failed_platforms = {}
        self.delivery_router = SimpleNamespace(adapters={})
        self.session_store = object()
        self.connect_success = connect_success
        self.created_adapter = created_adapter or FakePlatformAdapter()
        self.created_configs = []
        self.status_updates = []

    def _handle_message(self, *_args, **_kwargs):
        return None

    def _handle_adapter_fatal_error(self, *_args, **_kwargs):
        return None

    def _handle_active_session_busy_message(self, *_args, **_kwargs):
        return None

    def _sync_voice_mode_state_to_adapter(self, *_args, **_kwargs):
        return None

    def _create_adapter(self, platform, config):
        self.created_configs.append((platform, config))
        return self.created_adapter

    async def _connect_adapter_with_timeout(self, adapter, _platform):
        if self.connect_success:
            adapter._connected = True
        return self.connect_success

    async def _safe_adapter_disconnect(self, adapter, _platform):
        await adapter.disconnect()

    def _update_platform_runtime_status(
        self,
        platform,
        *,
        platform_state=None,
        error_code=None,
        error_message=None,
    ):
        self.status_updates.append(
            {
                "platform": platform,
                "state": platform_state,
                "error_code": error_code,
                "error_message": error_message,
            }
        )


@pytest.mark.asyncio
async def test_configure_telegram_hot_applies_and_redacts_token(isolated_hermes_home):
    adapter = _make_adapter()
    runner = FakeGatewayRunner()
    adapter.gateway_runner = runner

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/telegram/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json=_telegram_payload(),
        )
        data = await resp.json()

    assert resp.status == 200
    assert data["ok"] is True
    assert data["applied"] is True
    assert data["restart_required"] is False
    assert data["state"] == "connected"
    assert data["status"]["state"] == "connected"
    assert data["status"]["allowed_users"] == ["1001", "1002"]
    assert data["status"]["home_channel"]["chat_id"] == "1001"
    assert VALID_TELEGRAM_TOKEN not in str(data)

    state_path = (
        isolated_hermes_home / "gateway" / "platform-control" / "telegram.json"
    )
    assert VALID_TELEGRAM_TOKEN in state_path.read_text(encoding="utf-8")

    created_config = runner.created_configs[0][1]
    assert created_config.extra["allow_from"] == ["1001", "1002"]
    assert created_config.home_channel.chat_id == "1001"
    assert created_config.token == VALID_TELEGRAM_TOKEN
    assert runner.delivery_router.adapters is runner.adapters


@pytest.mark.asyncio
async def test_configure_telegram_reconnects_only_telegram_adapter():
    adapter = _make_adapter()
    new_telegram = FakePlatformAdapter()
    runner = FakeGatewayRunner(created_adapter=new_telegram)
    old_telegram = FakePlatformAdapter()
    discord_adapter = object()
    runner.adapters[Platform.TELEGRAM] = old_telegram
    runner.adapters[Platform.DISCORD] = discord_adapter
    adapter.gateway_runner = runner

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/telegram/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json=_telegram_payload(),
        )
        await resp.json()

    assert resp.status == 200
    assert old_telegram.disconnected is True
    assert runner.adapters[Platform.TELEGRAM] is new_telegram
    assert runner.adapters[Platform.DISCORD] is discord_adapter


@pytest.mark.asyncio
async def test_configure_discord_hot_applies_and_enables_toolsets(isolated_hermes_home):
    from hermes_cli.tools_config import _get_platform_tools

    adapter = _make_adapter()
    runner = FakeGatewayRunner()
    adapter.gateway_runner = runner

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/discord/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json=_discord_payload(),
        )
        data = await resp.json()

    assert resp.status == 200
    assert data["ok"] is True
    assert data["platform"] == "discord"
    assert data["applied"] is True
    assert data["restart_required"] is False
    assert data["state"] == "connected"
    assert data["status"]["id"] == "discord"
    assert data["status"]["state"] == "connected"
    assert data["status"]["allowed_users"] == ["119991111111111111"]
    assert data["status"]["home_channel"]["chat_id"] == "229992222222222222"
    assert VALID_DISCORD_TOKEN not in str(data)

    state_path = (
        isolated_hermes_home / "gateway" / "platform-control" / "discord.json"
    )
    state_text = state_path.read_text(encoding="utf-8")
    assert VALID_DISCORD_TOKEN in state_text
    assert "229992222222222222" in state_text

    created_config = runner.created_configs[0][1]
    assert runner.created_configs[0][0] == Platform.DISCORD
    assert created_config.extra["allow_from"] == ["119991111111111111"]
    assert created_config.extra["allowed_channels"] == ["229992222222222222"]
    assert created_config.extra["free_response_channels"] == ["229992222222222222"]
    assert created_config.extra["require_mention"] is True
    assert created_config.home_channel.chat_id == "229992222222222222"
    assert created_config.home_channel.thread_id == "339993333333333333"
    assert created_config.token == VALID_DISCORD_TOKEN
    assert runner.delivery_router.adapters is runner.adapters

    enabled_toolsets = _get_platform_tools({}, "discord")
    assert "discord" in enabled_toolsets
    assert "discord_admin" in enabled_toolsets


@pytest.mark.asyncio
async def test_configure_discord_owner_dm_does_not_require_home_channel():
    adapter = _make_adapter()
    runner = FakeGatewayRunner()
    adapter.gateway_runner = runner

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/discord/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json={
                "bot_token": VALID_DISCORD_TOKEN,
                "allowed_users": ["<@119991111111111111>"],
            },
        )
        data = await resp.json()

    assert resp.status == 200
    assert data["status"]["allowed_users"] == ["119991111111111111"]
    assert data["status"]["home_channel"] is None
    created_config = runner.created_configs[0][1]
    assert created_config.home_channel is None
    assert created_config.extra["allowed_channels"] == []
    assert created_config.extra["free_response_channels"] == []


@pytest.mark.asyncio
async def test_configure_discord_owner_dm_reconfigure_clears_server_channel_state():
    adapter = _make_adapter()
    runner = FakeGatewayRunner()
    adapter.gateway_runner = runner

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        first = await cli.post(
            "/api/platforms/discord/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json=_discord_payload(),
        )
        await first.json()

        second = await cli.post(
            "/api/platforms/discord/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json={
                "bot_token": VALID_DISCORD_TOKEN,
                "allowed_users": ["119991111111111111"],
            },
        )
        data = await second.json()

    assert first.status == 200
    assert second.status == 200
    assert data["status"]["home_channel"] is None
    assert os.getenv("DISCORD_HOME_CHANNEL") is None
    assert os.getenv("DISCORD_HOME_CHANNEL_NAME") is None
    assert os.getenv("DISCORD_HOME_CHANNEL_THREAD_ID") is None
    assert os.getenv("DISCORD_ALLOWED_CHANNELS") is None
    assert os.getenv("DISCORD_FREE_RESPONSE_CHANNELS") is None

    created_config = runner.created_configs[-1][1]
    assert created_config.home_channel is None
    assert created_config.extra["allowed_channels"] == []
    assert created_config.extra["free_response_channels"] == []


@pytest.mark.asyncio
async def test_configure_telegram_requires_auth():
    adapter = _make_adapter()
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/platforms/telegram/configure", json=_telegram_payload())

    assert resp.status == 401


@pytest.mark.asyncio
async def test_configure_telegram_validation_failure_is_typed():
    adapter = _make_adapter()
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/telegram/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json={
                "bot_token": "not-a-token",
                "allowed_users": ["abc"],
                "home_channel": "not-a-chat-id",
            },
        )
        data = await resp.json()

    assert resp.status == 400
    assert data["ok"] is False
    assert data["error_code"] == "validation_failed"
    assert data["valid"] is False
    paths = {error["path"] for error in data["errors"]}
    assert {"bot_token", "allowed_users", "home_channel"}.issubset(paths)
    assert "not-a-token" not in str(data)


@pytest.mark.asyncio
async def test_configure_whatsapp_starts_pairing_without_session(monkeypatch):
    from hermes_cli import platform_control_whatsapp

    adapter = _make_adapter()
    runner = FakeGatewayRunner()
    adapter.gateway_runner = runner
    app = _create_app(adapter)

    def fake_pairing_status(state, *, utc_now_iso):
        session_dir = platform_control_whatsapp.whatsapp_session_dir(state)
        platform_control_whatsapp.write_whatsapp_pairing_status(
            session_dir,
            {
                "status": "qr_ready",
                "qr": "terminal qr",
                "mode": "bot",
            },
            utc_now_iso=utc_now_iso,
        )
        return platform_control_whatsapp.whatsapp_pairing_status(state)

    monkeypatch.setattr(
        platform_control_whatsapp,
        "ensure_whatsapp_pairing_process",
        fake_pairing_status,
    )

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/whatsapp/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json=_whatsapp_payload(),
        )
        data = await resp.json()

    assert resp.status == 200
    assert data["ok"] is False
    assert data["platform"] == "whatsapp"
    assert data["configured"] is True
    assert data["applied"] is False
    assert data["restart_required"] is False
    assert data["state"] == "pairing"
    assert data["error_code"] == "whatsapp_not_paired"
    assert data["status"]["id"] == "whatsapp"
    assert data["status"]["configured"] is True
    assert data["status"]["state"] == "pairing"
    assert data["status"]["allowed_users"] == ["15551234567"]
    assert data["status"]["pairing"]["status"] == "qr_ready"
    assert data["status"]["pairing"]["qr"] == "terminal qr"
    assert os.getenv("WHATSAPP_ENABLED") == "true"
    assert os.getenv("WHATSAPP_MODE") == "bot"
    assert os.getenv("WHATSAPP_ALLOWED_USERS") == "15551234567"
    assert os.getenv("WHATSAPP_GROUP_POLICY") == "disabled"


@pytest.mark.asyncio
async def test_unsupported_platform_returns_typed_response():
    adapter = _make_adapter()
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/signal/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json={},
        )
        data = await resp.json()

    assert resp.status == 400
    assert data["ok"] is False
    assert data["platform"] == "signal"
    assert data["supported"] is False
    assert data["error_code"] == "unsupported_platform"


@pytest.mark.asyncio
async def test_configure_without_gateway_runner_returns_restart_required():
    adapter = _make_adapter()
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/telegram/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json=_telegram_payload(),
        )
        data = await resp.json()

    assert resp.status == 409
    assert data["ok"] is False
    assert data["restart_required"] is True
    assert data["applied"] is False
    assert data["status"]["connected"] is False
    assert VALID_TELEGRAM_TOKEN not in str(data)


@pytest.mark.asyncio
async def test_configure_telegram_apply_failure_is_typed():
    adapter = _make_adapter()
    runner = FakeGatewayRunner(
        connect_success=False,
        created_adapter=FakePlatformAdapter(
            fatal_error=True,
            fatal_error_code="telegram_auth_failed",
            fatal_error_message="token rejected",
            fatal_error_retryable=False,
        ),
    )
    adapter.gateway_runner = runner
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/telegram/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json=_telegram_payload(),
        )
        data = await resp.json()

    assert resp.status == 500
    assert data["ok"] is False
    assert data["applied"] is False
    assert data["restart_required"] is False
    assert data["state"] == "fatal"
    assert data["error_code"] == "telegram_auth_failed"
    assert data["error_message"] == "token rejected"
    assert data["status"]["connected"] is False
    assert VALID_TELEGRAM_TOKEN not in str(data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["connected", "disconnected", "retrying", "paused", "fatal", "startup_failed"],
)
async def test_get_telegram_preserves_raw_runtime_state(state, monkeypatch):
    adapter = _make_adapter()
    app = _create_app(adapter)
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "platforms": {
                "telegram": {
                    "state": state,
                    "error_code": "telegram_code",
                    "error_message": "telegram message",
                    "updated_at": "2026-05-31T18:00:00Z",
                }
            }
        },
    )

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(
            "/api/platforms/telegram",
            headers={"Authorization": "Bearer sk-secret"},
        )
        data = await resp.json()

    assert resp.status == 200
    assert data["id"] == "telegram"
    assert data["state"] == state
    assert data["connected"] is (state == "connected")
    assert data["error_code"] == "telegram_code"
    assert data["error_message"] == "telegram message"
    assert data["updated_at"] == "2026-05-31T18:00:00Z"


@pytest.mark.asyncio
async def test_list_platforms_returns_non_secret_summary():
    adapter = _make_adapter()
    runner = FakeGatewayRunner()
    runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
        enabled=True,
        token=VALID_TELEGRAM_TOKEN,
        extra={"allow_from": ["1001"]},
    )
    adapter.gateway_runner = runner
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(
            "/api/platforms",
            headers={"Authorization": "Bearer sk-secret"},
        )
        data = await resp.json()

    assert resp.status == 200
    assert data["platforms"][0]["id"] == "telegram"
    assert data["platforms"][0]["configured"] is True
    assert data["platforms"][0]["credential_fingerprint"].startswith("sha256:")
    assert VALID_TELEGRAM_TOKEN not in str(data)


@pytest.mark.asyncio
async def test_runtime_platform_state_loads_into_gateway_config():
    from hermes_cli.platform_control import configure_platform
    from gateway.config import load_gateway_config

    result = await configure_platform("telegram", _telegram_payload(), runner=None)
    assert result["restart_required"] is True

    config = load_gateway_config()
    telegram = config.platforms[Platform.TELEGRAM]
    assert telegram.enabled is True
    assert telegram.token == VALID_TELEGRAM_TOKEN
    assert telegram.extra["allow_from"] == ["1001", "1002"]
    assert telegram.home_channel.chat_id == "1001"

    discord_result = await configure_platform("discord", _discord_payload(), runner=None)
    assert discord_result["restart_required"] is True

    config = load_gateway_config()
    discord = config.platforms[Platform.DISCORD]
    assert discord.enabled is True
    assert discord.token == VALID_DISCORD_TOKEN
    assert discord.extra["allow_from"] == ["119991111111111111"]
    assert discord.extra["allowed_channels"] == ["229992222222222222"]
    assert discord.extra["free_response_channels"] == ["229992222222222222"]
    assert discord.extra["require_mention"] is True
    assert discord.home_channel.chat_id == "229992222222222222"

    whatsapp_result = await configure_platform("whatsapp", _whatsapp_payload(), runner=None)
    assert whatsapp_result["restart_required"] is True

    config = load_gateway_config()
    whatsapp = config.platforms[Platform.WHATSAPP]
    assert whatsapp.enabled is True
    assert whatsapp.extra["mode"] == "bot"
    assert whatsapp.extra["allow_from"] == ["15551234567"]
    assert whatsapp.extra["dm_policy"] == "allowlist"
    assert whatsapp.extra["group_policy"] == "disabled"
