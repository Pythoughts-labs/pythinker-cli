from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from pythinker_code.auth.models_dev import CatalogResult, CatalogStatus
from pythinker_code.auth.oauth import (
    OAuthError,
    OAuthManager,
    OAuthToken,
    OAuthUnauthorized,
    _credentials_path,
    load_tokens,
    save_tokens,
)
from pythinker_code.auth.oauth_flows import LoopbackAuthorization
from pythinker_code.config import Config, LLMModel, LLMProvider, OAuthRef


def _mock_unavailable_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from pythinker_code.auth import snowflake

    async def _fake() -> CatalogResult:
        return CatalogResult({}, CatalogStatus.UNAVAILABLE, "none")

    monkeypatch.setattr(snowflake, "get_models_dev_catalog", _fake)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [CatalogStatus.OK, CatalogStatus.UNAVAILABLE])
async def test_discover_snowflake_models_logs_catalog_fallback(
    monkeypatch: pytest.MonkeyPatch,
    status: CatalogStatus,
) -> None:
    from pythinker_code.auth import snowflake

    messages: list[str] = []

    async def empty_catalog() -> CatalogResult:
        source = "network" if status is CatalogStatus.OK else "none"
        return CatalogResult({}, status, source)

    monkeypatch.setattr(snowflake, "get_models_dev_catalog", empty_catalog)
    monkeypatch.setattr(
        snowflake,
        "logger",
        SimpleNamespace(debug=lambda message, **_kwargs: messages.append(message)),
    )

    assert await snowflake._discover_snowflake_models() == snowflake.SNOWFLAKE_MODELS
    assert messages
    if status is CatalogStatus.OK:
        assert any("no usable" in message for message in messages)
    else:
        assert any("not authoritative" in message for message in messages)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://MYORG-acct.snowflakecomputing.com/", "MYORG-acct"),
        ("MYORG-acct", "MYORG-acct"),
        (" http://MYORG-acct.snowflakecomputing.com ", "MYORG-acct"),
        ("MYORG-acct///", "MYORG-acct"),
    ],
)
def test_normalize_account(raw: str, expected: str) -> None:
    from pythinker_code.auth.snowflake import normalize_account

    assert normalize_account(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "acct/../evil",
        "acct/path",
        "user@acct",
        "acct:443",
        "acct?query=1",
        "acct#fragment",
        "acct evil",
        "http://acct.snowflakecomputing.com:8443/",
        "//evil.example.com",
    ],
)
def test_normalize_account_rejects_hostile_input(raw: str) -> None:
    from pythinker_code.auth.snowflake import normalize_account

    # Authority/path/port/userinfo/query/fragment payloads must be rejected
    # before any URL is built, so they can never redirect the OAuth host.
    with pytest.raises(ValueError):
        normalize_account(raw)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (None, "refresh_token"),
        ("DATA_ENGINEER", "refresh_token session:role:DATA_ENGINEER"),
        ("Data Science/Admin", "refresh_token session:role-encoded:Data%20Science%2FAdmin"),
    ],
)
def test_scope(role: str | None, expected: str) -> None:
    from pythinker_code.auth.snowflake import _scope

    assert _scope(role) == expected


class _FormResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> _FormResponse:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def json(self, *, content_type: object = None) -> object:
        if isinstance(self.payload, ValueError):
            raise self.payload
        return self.payload


class _FormSession:
    def __init__(self, response: _FormResponse, calls: list[dict[str, Any]]) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self) -> _FormSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def post(
        self, endpoint: str, *, data: Mapping[str, str], headers: Mapping[str, str]
    ) -> _FormResponse:
        self.calls.append({"endpoint": endpoint, "data": dict(data), "headers": dict(headers)})
        return self.response


@pytest.mark.asyncio
async def test_refresh_uses_account_endpoint_basic_auth_and_default_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import snowflake

    calls: list[dict[str, Any]] = []
    response = _FormResponse(200, {"access_token": "access", "refresh_token": "refresh"})
    monkeypatch.setattr(snowflake, "new_client_session", lambda: _FormSession(response, calls))

    token = await snowflake.refresh_snowflake_cortex_token("myorg-acct", "old-refresh")

    assert token.expires_in == 600
    assert calls == [
        {
            "endpoint": "https://myorg-acct.snowflakecomputing.com/oauth/token-request",
            "data": {
                "grant_type": "refresh_token",
                "refresh_token": "old-refresh",
                "client_id": "LOCAL_APPLICATION",
            },
            "headers": {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Authorization": "Basic TE9DQUxfQVBQTElDQVRJT046TE9DQUxfQVBQTElDQVRJT04=",
            },
        }
    ]


@pytest.mark.asyncio
async def test_refresh_maps_invalid_grant_to_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import snowflake

    response = _FormResponse(400, {"error": "invalid_grant"})
    monkeypatch.setattr(snowflake, "new_client_session", lambda: _FormSession(response, []))

    with pytest.raises(OAuthUnauthorized):
        await snowflake.refresh_snowflake_cortex_token("myorg-acct", "revoked")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _FormResponse(401, {"error": "unauthorized"}),
        _FormResponse(500, {"error": "server_error"}),
        _FormResponse(200, ValueError("bad json")),
        _FormResponse(200, ["not", "a", "dict"]),
    ],
)
async def test_refresh_rejects_error_and_malformed_responses(
    monkeypatch: pytest.MonkeyPatch,
    response: _FormResponse,
) -> None:
    from pythinker_code.auth import snowflake

    monkeypatch.setattr(snowflake, "new_client_session", lambda: _FormSession(response, []))

    with pytest.raises(OAuthError):
        await snowflake.refresh_snowflake_cortex_token("myorg-acct", "old-refresh")


@pytest.mark.asyncio
async def test_login_snowflake_saves_account_scoped_token_provider_and_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth import snowflake

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    loopback_calls: list[dict[str, Any]] = []

    async def fake_loopback(**kwargs: Any) -> LoopbackAuthorization:
        loopback_calls.append(kwargs)
        return LoopbackAuthorization("auth-code", "verifier", "http://127.0.0.1:49231/")

    async def fake_exchange(
        account: str,
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        assert account == "MYORG-acct"
        assert code == "auth-code"
        assert code_verifier == "verifier"
        assert redirect_uri == "http://127.0.0.1:49231/"
        return snowflake._inject_default_expiry(
            {
                "access_token": "snowflake-access-secret",
                "refresh_token": "snowflake-refresh-secret",
                "token_type": "Bearer",
            }
        )

    monkeypatch.setattr(snowflake, "run_loopback_pkce_flow", fake_loopback)
    monkeypatch.setattr(snowflake, "_exchange_code_for_tokens", fake_exchange)
    _mock_unavailable_catalog(monkeypatch)

    events = [
        event
        async for event in snowflake.login_snowflake(
            config,
            " https://MYORG-acct.snowflakecomputing.com/ ",
            "DATA_ENGINEER",
        )
    ]

    assert [event.type for event in events] == ["waiting", "success"]
    call = loopback_calls[0]
    assert call["authorize_endpoint"] == (
        "https://MYORG-acct.snowflakecomputing.com/oauth/authorize"
    )
    assert call["client_id"] == "LOCAL_APPLICATION"
    assert call["scope"] == "refresh_token session:role:DATA_ENGINEER"
    assert call["redirect_path"] == "/"
    assert call["port"] == 0
    assert call["extra_authorize_params"] is None

    oauth_ref = OAuthRef(storage="file", key="oauth/snowflake-cortex/MYORG-acct")
    stored = load_tokens(oauth_ref)
    assert stored is not None
    assert stored.access_token == "snowflake-access-secret"
    assert stored.refresh_token == "snowflake-refresh-secret"
    assert stored.expires_in == 600
    assert _credentials_path(oauth_ref.key).is_file()

    provider = config.providers["managed:snowflake-cortex"]
    assert provider.type == "openai_legacy"
    assert provider.base_url == "https://MYORG-acct.snowflakecomputing.com/api/v2/cortex/v1"
    assert provider.api_key.get_secret_value() == ""
    assert provider.oauth == oauth_ref
    assert {model.provider for model in config.models.values()} == {"managed:snowflake-cortex"}
    # Snowflake registers its models but must NOT become the default: its Cortex
    # chat adapter is not implemented, so it cannot serve chat yet.
    assert config.default_model == ""
    assert not config.default_model.startswith("snowflake-cortex/")
    rendered_events = "\n".join(event.json for event in events)
    assert "snowflake-access-secret" not in rendered_events
    assert "snowflake-refresh-secret" not in rendered_events


@pytest.mark.asyncio
async def test_snowflake_persistence_errors_hide_internal_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import snowflake

    diagnostic = "permission denied at /private/credentials/snowflake.json"
    config = Config(is_from_default_location=True)

    async def fake_loopback(**_kwargs: Any) -> LoopbackAuthorization:
        return LoopbackAuthorization("auth-code", "verifier", "http://127.0.0.1:49231/")

    async def fake_exchange(
        _account: str,
        _code: str,
        _code_verifier: str,
        _redirect_uri: str,
    ) -> dict[str, Any]:
        return {
            "access_token": "snowflake-access",
            "refresh_token": "snowflake-refresh",
            "expires_in": 600,
        }

    async def fake_models() -> tuple[snowflake.SnowflakeModel, ...]:
        return snowflake.SNOWFLAKE_MODELS

    async def fail_persistence(*_args: object, **_kwargs: object) -> None:
        raise OSError(diagnostic)

    monkeypatch.setattr(snowflake, "run_loopback_pkce_flow", fake_loopback)
    monkeypatch.setattr(snowflake, "_exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr(snowflake, "_discover_snowflake_models", fake_models)
    monkeypatch.setattr(snowflake, "persist_login", fail_persistence)
    monkeypatch.setattr(snowflake, "persist_config_change", fail_persistence)

    login_events = [event async for event in snowflake.login_snowflake(config, "myorg-acct")]
    logout_events = [event async for event in snowflake.logout_snowflake(config)]

    assert login_events[-1].message == "Failed to save Snowflake Cortex login."
    assert logout_events[-1].message == "Failed to log out of Snowflake Cortex."
    assert diagnostic not in login_events[-1].json
    assert diagnostic not in logout_events[-1].json


@pytest.mark.asyncio
async def test_login_snowflake_fails_when_refresh_token_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth import snowflake

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_loopback(**_kwargs: Any) -> LoopbackAuthorization:
        return LoopbackAuthorization("auth-code", "verifier", "http://127.0.0.1:49231/")

    async def fake_exchange(
        _account: str,
        _code: str,
        _code_verifier: str,
        _redirect_uri: str,
    ) -> dict[str, Any]:
        return {"access_token": "access-only", "expires_in": 600}

    monkeypatch.setattr(snowflake, "run_loopback_pkce_flow", fake_loopback)
    monkeypatch.setattr(snowflake, "_exchange_code_for_tokens", fake_exchange)

    events = [event async for event in snowflake.login_snowflake(config, "myorg-acct")]

    assert [event.type for event in events] == ["waiting", "error"]
    assert "refresh token" in events[-1].message
    assert "managed:snowflake-cortex" not in config.providers
    assert load_tokens(OAuthRef(storage="file", key="oauth/snowflake-cortex/myorg-acct")) is None


@pytest.mark.asyncio
async def test_login_snowflake_rejects_empty_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth.snowflake import login_snowflake

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    events = [event async for event in login_snowflake(config, "   ")]

    assert [event.type for event in events] == ["error"]
    assert events[0].message == "Snowflake account identifier is required."
    assert "managed:snowflake-cortex" not in config.providers
    assert not (tmp_path / "credentials").exists()


@pytest.mark.asyncio
async def test_logout_snowflake_removes_account_token_provider_models_and_repairs_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth.snowflake import SNOWFLAKE_PROVIDER_KEY, logout_snowflake

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    oauth_ref = OAuthRef(storage="file", key="oauth/snowflake-cortex/myorg-acct")
    save_tokens(
        oauth_ref,
        OAuthToken.from_response(
            {
                "access_token": "snowflake-access",
                "refresh_token": "snowflake-refresh",
                "expires_in": 600,
            }
        ),
    )
    config = Config(
        is_from_default_location=True,
        default_model="snowflake-cortex/claude-sonnet-4-5",
        providers={
            SNOWFLAKE_PROVIDER_KEY: LLMProvider(
                type="openai_legacy",
                base_url="https://myorg-acct.snowflakecomputing.com/api/v2/cortex/v1",
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
            "snowflake-cortex/claude-sonnet-4-5": LLMModel(
                provider=SNOWFLAKE_PROVIDER_KEY,
                model="claude-sonnet-4-5",
                max_context_size=200_000,
            ),
            "fallback/model": LLMModel(
                provider="fallback",
                model="model",
                max_context_size=32_000,
            ),
        },
    )

    events = [event async for event in logout_snowflake(config)]

    assert [event.type for event in events] == ["success"]
    assert load_tokens(oauth_ref) is None
    assert not _credentials_path(oauth_ref.key).exists()
    assert SNOWFLAKE_PROVIDER_KEY not in config.providers
    assert all(model.provider != SNOWFLAKE_PROVIDER_KEY for model in config.models.values())
    assert config.default_model == "fallback/model"


@pytest.mark.asyncio
async def test_oauth_manager_routes_snowflake_refresh_with_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import snowflake

    calls: list[tuple[str, str]] = []

    async def fake_refresh(account: str, refresh_token: str) -> OAuthToken:
        calls.append((account, refresh_token))
        return OAuthToken.from_response(
            {
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 600,
            }
        )

    monkeypatch.setattr(snowflake, "refresh_snowflake_cortex_token", fake_refresh)
    manager = OAuthManager(Config())

    token = await manager._refresh_token_for_ref(
        OAuthRef(storage="file", key="oauth/snowflake-cortex/myorg-acct"),
        "old-refresh",
    )

    assert calls == [("myorg-acct", "old-refresh")]
    assert token.access_token == "new-access"
    assert token.refresh_token == "rotated-refresh"
