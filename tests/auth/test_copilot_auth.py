from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from pydantic import SecretStr

from pythinker_code.auth.models_dev import CatalogResult, CatalogStatus
from pythinker_code.auth.oauth import (
    OAuthError,
    OAuthManager,
    OAuthToken,
    OAuthUnauthorized,
    load_tokens,
    save_tokens,
)
from pythinker_code.auth.oauth_flows import DeviceCode
from pythinker_code.config import Config, LLMModel, LLMProvider, OAuthRef


def _mock_unavailable_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythinker_code.auth import copilot

    async def _fake() -> CatalogResult:
        return CatalogResult({}, CatalogStatus.UNAVAILABLE, "none")

    monkeypatch.setattr(copilot, "get_models_dev_catalog", _fake)


@pytest.mark.asyncio
async def test_discover_copilot_models_logs_empty_authoritative_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import copilot

    messages: list[str] = []

    async def empty_catalog() -> CatalogResult:
        return CatalogResult({}, CatalogStatus.OK, "network")

    monkeypatch.setattr(copilot, "get_models_dev_catalog", empty_catalog)
    monkeypatch.setattr(
        copilot,
        "logger",
        SimpleNamespace(debug=lambda message, **_kwargs: messages.append(message)),
    )

    assert await copilot._discover_copilot_models() == copilot.GITHUB_COPILOT_MODELS
    assert any("no usable" in message for message in messages)


@pytest.mark.asyncio
async def test_login_copilot_saves_two_tokens_provider_and_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from pythinker_code.auth import copilot

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    device_calls: list[dict[str, Any]] = []
    poll_calls: list[dict[str, Any]] = []

    async def fake_request_device_code(**kwargs: Any) -> DeviceCode:
        device_calls.append(kwargs)
        return DeviceCode(
            user_code="ABCD-EFGH",
            verification_uri="https://github.com/login/device",
            device_code="device-secret",
            interval=5,
            expires_in=900,
        )

    async def fake_poll_device_token(**kwargs: Any) -> dict[str, Any]:
        poll_calls.append(kwargs)
        return {"access_token": "github-oauth-token", "token_type": "bearer"}

    async def fake_refresh_copilot_token(github_token: str) -> OAuthToken:
        assert github_token == "github-oauth-token"
        return OAuthToken(
            access_token="copilot-bearer",
            refresh_token="",
            expires_at=2_000_000_000,
            scope="read:user",
            token_type="Bearer",
            expires_in=1500,
        )

    monkeypatch.setattr(copilot, "request_device_code", fake_request_device_code)
    monkeypatch.setattr(copilot, "poll_device_token", fake_poll_device_token)
    monkeypatch.setattr(copilot, "refresh_copilot_token", fake_refresh_copilot_token)
    _mock_unavailable_catalog(monkeypatch)

    events = [event async for event in copilot.login_copilot(config, open_browser=False)]

    assert [event.type for event in events] == ["verification_url", "waiting", "success"]
    assert device_calls[0]["headers"] == {"Accept": "application/json"}
    assert poll_calls[0]["headers"] == {"Accept": "application/json"}
    oauth_ref = OAuthRef(storage="file", key="oauth/github-copilot")
    stored = load_tokens(oauth_ref)
    assert stored is not None
    assert stored.access_token == "copilot-bearer"
    assert stored.refresh_token == "github-oauth-token"

    provider = config.providers["managed:copilot"]
    assert provider.type == "openai_legacy"
    assert provider.base_url == "https://api.githubcopilot.com"
    assert provider.oauth == oauth_ref
    assert provider.custom_headers is not None
    assert provider.custom_headers["Copilot-Integration-Id"] == "vscode-chat"
    assert provider.custom_headers["X-GitHub-Api-Version"] == "2025-04-01"
    assert "Authorization" not in provider.custom_headers
    assert {model.provider for model in config.models.values()} == {"managed:copilot"}


@pytest.mark.asyncio
async def test_copilot_persistence_errors_hide_internal_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import copilot

    diagnostic = "permission denied at /private/credentials/github-copilot.json"
    config = Config(is_from_default_location=True)

    async def fake_request_device_code(**_kwargs: Any) -> DeviceCode:
        return DeviceCode(
            user_code="ABCD-EFGH",
            verification_uri="https://github.com/login/device",
            device_code="device-secret",
            interval=5,
            expires_in=900,
        )

    async def fake_poll_device_token(**_kwargs: Any) -> dict[str, Any]:
        return {"access_token": "github-oauth-token"}

    async def fake_refresh_copilot_token(_github_token: str) -> OAuthToken:
        return OAuthToken.from_response(
            {
                "access_token": "copilot-bearer",
                "refresh_token": "github-oauth-token",
                "expires_in": 1500,
            }
        )

    async def fail_persistence(*_args: object, **_kwargs: object) -> None:
        raise OSError(diagnostic)

    monkeypatch.setattr(copilot, "request_device_code", fake_request_device_code)
    monkeypatch.setattr(copilot, "poll_device_token", fake_poll_device_token)
    monkeypatch.setattr(copilot, "refresh_copilot_token", fake_refresh_copilot_token)
    monkeypatch.setattr(copilot, "persist_login", fail_persistence)
    monkeypatch.setattr(copilot, "persist_logout", fail_persistence)
    _mock_unavailable_catalog(monkeypatch)

    login_events = [event async for event in copilot.login_copilot(config, open_browser=False)]
    logout_events = [event async for event in copilot.logout_copilot(config)]

    assert login_events[-1].message == "Failed to save GitHub Copilot login."
    assert logout_events[-1].message == "Failed to log out of GitHub Copilot."
    assert diagnostic not in login_events[-1].json
    assert diagnostic not in logout_events[-1].json


class _ExchangeResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> _ExchangeResponse:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def json(self, *, content_type: object = None) -> object:
        return self.payload


class _ExchangeSession:
    def __init__(
        self,
        response: _ExchangeResponse,
        calls: list[tuple[str, Mapping[str, str]]],
    ) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self) -> _ExchangeSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def get(self, endpoint: str, *, headers: Mapping[str, str]) -> _ExchangeResponse:
        self.calls.append((endpoint, headers))
        return self.response


@pytest.mark.asyncio
async def test_refresh_copilot_token_exchanges_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import copilot

    calls: list[tuple[str, Mapping[str, str]]] = []
    response = _ExchangeResponse(
        200,
        {"token": "copilot-bearer", "expires_at": 2_000_000_000, "refresh_in": 1500},
    )
    monkeypatch.setattr(
        copilot,
        "new_client_session",
        lambda: _ExchangeSession(response, calls),
    )

    token = await copilot.refresh_copilot_token("github-oauth-token")

    assert token.access_token == "copilot-bearer"
    assert token.refresh_token == "github-oauth-token"
    assert token.expires_at == 2_000_000_000
    assert token.expires_in == 1500
    assert calls[0][0] == "https://api.github.com/copilot_internal/v2/token"
    assert calls[0][1]["Authorization"] == "token github-oauth-token"
    assert calls[0][1]["Editor-Version"] == "vscode/1.99.0"
    assert calls[0][1]["Editor-Plugin-Version"] == "copilot-chat/0.26.7"
    assert calls[0][1]["User-Agent"] == "GitHubCopilotChat/0.26.7"
    assert calls[0][1]["X-GitHub-Api-Version"] == "2025-04-01"


@pytest.mark.asyncio
async def test_oauth_manager_preserves_github_token_across_copilot_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import copilot

    responses = [
        _ExchangeResponse(
            200,
            {"token": "copilot-bearer-1", "expires_at": 2_000_000_000, "refresh_in": 1500},
        ),
        _ExchangeResponse(
            200,
            {"token": "copilot-bearer-2", "expires_at": 2_000_001_500, "refresh_in": 1500},
        ),
    ]
    calls: list[tuple[str, Mapping[str, str]]] = []
    monkeypatch.setattr(
        copilot,
        "new_client_session",
        lambda: _ExchangeSession(responses.pop(0), calls),
    )
    manager = OAuthManager(Config())
    oauth_ref = OAuthRef(storage="file", key=copilot.GITHUB_COPILOT_OAUTH_KEY)

    first = await manager._refresh_token_for_ref(oauth_ref, "github-oauth-token")
    second = await manager._refresh_token_for_ref(oauth_ref, first.refresh_token)

    assert first.access_token == "copilot-bearer-1"
    assert first.refresh_token == "github-oauth-token"
    assert second.access_token == "copilot-bearer-2"
    assert second.refresh_token == "github-oauth-token"
    assert [headers["Authorization"] for _, headers in calls] == [
        "token github-oauth-token",
        "token github-oauth-token",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_refresh_copilot_token_raises_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    from pythinker_code.auth import copilot

    response = _ExchangeResponse(status, {"message": "Bad credentials"})
    monkeypatch.setattr(
        copilot,
        "new_client_session",
        lambda: _ExchangeSession(response, []),
    )

    with pytest.raises(OAuthUnauthorized):
        await copilot.refresh_copilot_token("revoked-github-token")


class _RaisingSession:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def __aenter__(self) -> _RaisingSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def get(self, endpoint: str, *, headers: Mapping[str, str]) -> object:
        raise self.exc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _ExchangeResponse(200, ["not", "a", "dict"]),  # malformed body
        _ExchangeResponse(200, {"expires_at": 2_000_000_000, "refresh_in": 1500}),  # no token
        _ExchangeResponse(200, {"token": "", "expires_at": 2_000_000_000, "refresh_in": 1500}),
        _ExchangeResponse(500, {"message": "server error"}),  # non-auth HTTP failure
    ],
)
async def test_refresh_copilot_token_rejects_malformed_responses(
    monkeypatch: pytest.MonkeyPatch,
    response: _ExchangeResponse,
) -> None:
    from pythinker_code.auth import copilot

    monkeypatch.setattr(copilot, "new_client_session", lambda: _ExchangeSession(response, []))

    with pytest.raises(OAuthError):
        await copilot.refresh_copilot_token("github-oauth-token")


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [aiohttp.ClientError(), TimeoutError(), OSError()])
async def test_refresh_copilot_token_wraps_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
) -> None:
    from pythinker_code.auth import copilot

    monkeypatch.setattr(copilot, "new_client_session", lambda: _RaisingSession(exc))

    with pytest.raises(OAuthError):
        await copilot.refresh_copilot_token("github-oauth-token")


@pytest.mark.asyncio
async def test_logout_copilot_removes_tokens_provider_models_and_repairs_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from pythinker_code.auth.copilot import (
        GITHUB_COPILOT_OAUTH_KEY,
        GITHUB_COPILOT_PROVIDER_KEY,
        logout_copilot,
    )

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    oauth_ref = OAuthRef(storage="file", key=GITHUB_COPILOT_OAUTH_KEY)
    save_tokens(
        oauth_ref,
        OAuthToken(
            access_token="copilot-bearer",
            refresh_token="github-oauth-token",
            expires_at=2_000_000_000,
            scope="read:user",
            token_type="Bearer",
            expires_in=1500,
        ),
    )
    config = Config(
        is_from_default_location=True,
        default_model="copilot/gpt-4.1",
        providers={
            GITHUB_COPILOT_PROVIDER_KEY: LLMProvider(
                type="openai_legacy",
                base_url="https://api.githubcopilot.com",
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
            "copilot/gpt-4.1": LLMModel(
                provider=GITHUB_COPILOT_PROVIDER_KEY,
                model="gpt-4.1",
                max_context_size=1_000_000,
            ),
            "copilot/gpt-4o": LLMModel(
                provider=GITHUB_COPILOT_PROVIDER_KEY,
                model="gpt-4o",
                max_context_size=128_000,
            ),
            "fallback/model": LLMModel(
                provider="fallback",
                model="model",
                max_context_size=32_000,
            ),
        },
    )

    events = [event async for event in logout_copilot(config)]

    assert [event.type for event in events] == ["success"]
    assert load_tokens(oauth_ref) is None
    assert GITHUB_COPILOT_PROVIDER_KEY not in config.providers
    assert all(model.provider != GITHUB_COPILOT_PROVIDER_KEY for model in config.models.values())
    assert config.default_model == "fallback/model"
