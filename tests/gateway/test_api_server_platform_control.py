"""Tests for generic API-server platform-control routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.api_server_platform_control import register_platform_control_routes


VALID_TELEGRAM_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd12345"


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
async def test_unsupported_platform_returns_typed_response():
    adapter = _make_adapter()
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/platforms/discord/configure",
            headers={"Authorization": "Bearer sk-secret"},
            json={},
        )
        data = await resp.json()

    assert resp.status == 400
    assert data["ok"] is False
    assert data["platform"] == "discord"
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
