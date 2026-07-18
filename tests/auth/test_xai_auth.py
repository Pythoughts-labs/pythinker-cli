from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from pythinker_code.auth.oauth import (
    OAuthManager,
    OAuthToken,
    OAuthUnauthorized,
    load_tokens,
    save_tokens,
)
from pythinker_code.auth.oauth_flows import DeviceCode, LoopbackAuthorization
from pythinker_code.config import Config, LLMModel, LLMProvider, OAuthRef


@pytest.mark.asyncio
async def test_login_xai_browser_saves_token_provider_and_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth import xai

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    loopback_calls: list[dict[str, Any]] = []

    async def fake_loopback(**kwargs: Any) -> LoopbackAuthorization:
        loopback_calls.append(kwargs)
        return LoopbackAuthorization("auth-code", "verifier", "http://127.0.0.1:56121/callback")

    async def fake_exchange(code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
        assert code == "auth-code"
        assert code_verifier == "verifier"
        assert redirect_uri == "http://127.0.0.1:56121/callback"
        return {
            "access_token": "xai-access",
            "refresh_token": "xai-refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    monkeypatch.setattr(xai, "run_loopback_pkce_flow", fake_loopback)
    monkeypatch.setattr(xai, "_exchange_code_for_tokens", fake_exchange)

    events = [event async for event in xai.login_xai_browser(config)]

    assert [event.type for event in events] == ["waiting", "success"]
    call = loopback_calls[0]
    assert call["port"] == 56121
    assert call["redirect_path"] == "/callback"
    assert call["extra_authorize_params"]["plan"] == "generic"
    assert call["extra_authorize_params"]["nonce"]

    oauth_ref = OAuthRef(storage="file", key="oauth/xai")
    stored = load_tokens(oauth_ref)
    assert stored is not None
    assert stored.access_token == "xai-access"
    assert stored.refresh_token == "xai-refresh"

    provider = config.providers["managed:xai"]
    assert provider.type == "openai_legacy"
    assert provider.base_url == "https://api.x.ai/v1"
    assert provider.oauth == oauth_ref
    assert not provider.custom_headers
    assert {model.provider for model in config.models.values()} == {"managed:xai"}
    assert config.default_model == "xai/grok-4"


@pytest.mark.asyncio
async def test_login_xai_headless_uses_device_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth import xai

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    device_calls: list[dict[str, Any]] = []
    poll_calls: list[dict[str, Any]] = []

    async def fake_request_device_code(**kwargs: Any) -> DeviceCode:
        device_calls.append(kwargs)
        return DeviceCode(
            user_code="GROK-CODE",
            verification_uri="https://auth.x.ai/activate",
            device_code="device-secret",
            interval=5,
            expires_in=900,
        )

    async def fake_poll_device_token(**kwargs: Any) -> dict[str, Any]:
        poll_calls.append(kwargs)
        return {
            "access_token": "xai-access",
            "refresh_token": "xai-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(xai, "request_device_code", fake_request_device_code)
    monkeypatch.setattr(xai, "poll_device_token", fake_poll_device_token)

    events = [event async for event in xai.login_xai_headless(config)]

    assert [event.type for event in events] == ["verification_url", "waiting", "success"]
    assert device_calls[0]["device_authorization_endpoint"] == xai.XAI_DEVICE_CODE_URL
    assert device_calls[0]["headers"] == {"Accept": "application/json"}
    assert poll_calls[0]["token_endpoint"] == xai.XAI_TOKEN_URL
    assert poll_calls[0]["headers"] == {"Accept": "application/json"}
    stored = load_tokens(OAuthRef(storage="file", key="oauth/xai"))
    assert stored is not None
    assert stored.refresh_token == "xai-refresh"


class _TokenResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> _TokenResponse:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def json(self, *, content_type: object = None) -> object:
        return self.payload


class _TokenSession:
    def __init__(
        self,
        response: _TokenResponse,
        calls: list[tuple[str, Mapping[str, str]]],
    ) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self) -> _TokenSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def post(self, endpoint: str, *, data: Mapping[str, str]) -> _TokenResponse:
        self.calls.append((endpoint, data))
        return self.response


@pytest.mark.asyncio
async def test_refresh_xai_token_returns_rotated_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythinker_code.auth import xai

    calls: list[tuple[str, Mapping[str, str]]] = []
    response = _TokenResponse(
        200,
        {
            "access_token": "new-access",
            "refresh_token": "rotated-refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    monkeypatch.setattr(xai, "new_client_session", lambda: _TokenSession(response, calls))

    token = await xai.refresh_xai_token("old-refresh")

    assert token.access_token == "new-access"
    assert token.refresh_token == "rotated-refresh"
    assert calls == [
        (
            "https://auth.x.ai/oauth2/token",
            {
                "grant_type": "refresh_token",
                "refresh_token": "old-refresh",
                "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_refresh_xai_token_raises_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    from pythinker_code.auth import xai

    response = _TokenResponse(status, {"error_description": "revoked"})
    monkeypatch.setattr(xai, "new_client_session", lambda: _TokenSession(response, []))

    with pytest.raises(OAuthUnauthorized):
        await xai.refresh_xai_token("revoked-refresh")


@pytest.mark.asyncio
async def test_oauth_manager_routes_xai_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythinker_code.auth import xai

    async def fake_refresh(refresh_token: str) -> OAuthToken:
        assert refresh_token == "old-refresh"
        return OAuthToken.from_response(
            {
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
            }
        )

    monkeypatch.setattr(xai, "refresh_xai_token", fake_refresh)
    manager = OAuthManager(Config())

    token = await manager._refresh_token_for_ref(
        OAuthRef(storage="file", key="oauth/xai"), "old-refresh"
    )

    assert token.access_token == "new-access"
    assert token.refresh_token == "rotated-refresh"


@pytest.mark.asyncio
async def test_logout_xai_removes_tokens_provider_models_and_repairs_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth.xai import XAI_OAUTH_KEY, XAI_PROVIDER_KEY, logout_xai

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    oauth_ref = OAuthRef(storage="file", key=XAI_OAUTH_KEY)
    save_tokens(
        oauth_ref,
        OAuthToken.from_response(
            {
                "access_token": "xai-access",
                "refresh_token": "xai-refresh",
                "expires_in": 3600,
            }
        ),
    )
    config = Config(
        is_from_default_location=True,
        default_model="xai/grok-4",
        providers={
            XAI_PROVIDER_KEY: LLMProvider(
                type="openai_legacy",
                base_url="https://api.x.ai/v1",
                api_key=SecretStr(""),
                oauth=oauth_ref,
            ),
            "fallback": LLMProvider(
                type="openai_legacy",
                base_url="https://example.test/v1",
                api_key=SecretStr("test"),
            ),
        },
        models={
            "xai/grok-4": LLMModel(
                provider=XAI_PROVIDER_KEY,
                model="grok-4",
                max_context_size=256_000,
            ),
            "fallback/model": LLMModel(
                provider="fallback",
                model="model",
                max_context_size=32_000,
            ),
        },
    )

    events = [event async for event in logout_xai(config)]

    assert [event.type for event in events] == ["success"]
    assert load_tokens(oauth_ref) is None
    assert XAI_PROVIDER_KEY not in config.providers
    assert all(model.provider != XAI_PROVIDER_KEY for model in config.models.values())
    assert config.default_model == "fallback/model"
