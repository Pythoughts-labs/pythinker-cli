from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from pydantic import SecretStr
from yarl import URL

from pythinker_code.auth.z_ai import (
    ZAI_API_ROUTE,
    ZAI_CODING_ROUTE,
    ZAI_MODELS,
    ZAI_ROUTES,
    ZaiCatalogResult,
    ZaiModel,
    ZaiRoute,
    _apply_z_ai_config,
    _discover_z_ai_catalog,
    _parse_discovered_models,
    _request_z_ai_models,
    apply_z_ai_models,
    get_z_ai_api_key_from_env,
    login_z_ai_api_key,
    login_z_ai_coding_api_key,
    logout_z_ai_api,
    logout_z_ai_coding,
    refresh_z_ai_models,
)
from pythinker_code.config import Config, load_config


def _request_info(route: ZaiRoute) -> aiohttp.RequestInfo:
    return aiohttp.RequestInfo(
        url=URL(route.models_url),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL(route.models_url),
    )


def _response_error(route: ZaiRoute, status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(
        _request_info(route),
        (),
        status=status,
        message="provider-private detail",
    )


def test_route_descriptors_are_explicit_and_independent() -> None:
    assert ZAI_ROUTES == (ZAI_CODING_ROUTE, ZAI_API_ROUTE)
    assert (
        ZaiRoute(
            platform_id="z-ai-coding",
            display_name="Z.AI Coding Plan",
            base_url="https://api.z.ai/api/coding/paas/v4",
            api_key_env="ZAI_CODING_API_KEY",
        )
        == ZAI_CODING_ROUTE
    )
    assert (
        ZaiRoute(
            platform_id="z-ai-api",
            display_name="Z.AI API",
            base_url="https://api.z.ai/api/paas/v4",
            api_key_env="ZAI_API_KEY",
        )
        == ZAI_API_ROUTE
    )
    assert ZAI_CODING_ROUTE.provider_key == "managed:z-ai-coding"
    assert ZAI_API_ROUTE.provider_key == "managed:z-ai-api"
    assert ZAI_CODING_ROUTE.models_url.endswith("/models")
    assert ZAI_API_ROUTE.models_url.endswith("/models")


def test_curated_catalog_comes_from_single_policy_table() -> None:
    expected = {
        "glm-5.2": 1_000_000,
        "glm-5.1": 204_800,
        "glm-5": 204_800,
        "glm-5-turbo": 204_800,
        "glm-4.7": 204_800,
        "glm-4.5-air": 131_072,
    }
    assert {model.model_id: model.max_context_size for model in ZAI_MODELS} == expected

    for route in ZAI_ROUTES:
        config = Config(is_from_default_location=True)
        _apply_z_ai_config(config, route, SecretStr("route-key"))
        assert {
            alias.removeprefix(f"{route.platform_id}/"): model.max_context_size
            for alias, model in config.models.items()
            if model.provider == route.provider_key
        } == expected
        assert all(
            model.capabilities == {"thinking"}
            for model in config.models.values()
            if model.provider == route.provider_key
        )


@pytest.mark.parametrize("route", ZAI_ROUTES)
def test_apply_route_uses_openai_transport(route: ZaiRoute) -> None:
    config = Config(is_from_default_location=True)

    _apply_z_ai_config(config, route, SecretStr("route-key"))

    provider = config.providers[route.provider_key]
    assert provider.type == "openai_legacy"
    assert provider.base_url == route.base_url
    assert provider.api_key.get_secret_value() == "route-key"
    assert config.default_model == f"{route.platform_id}/glm-5.2"
    assert config.models[config.default_model].capabilities == {"thinking"}
    assert config.default_thinking is True
    assert config.default_thinking_effort == "high"


def test_apply_route_preserves_explicit_thinking_choice() -> None:
    config = Config(
        is_from_default_location=True,
        default_thinking=False,
        default_thinking_effort="off",
    )

    _apply_z_ai_config(config, ZAI_API_ROUTE, SecretStr("route-key"))

    assert config.default_thinking is False
    assert config.default_thinking_effort == "off"


@pytest.mark.parametrize(
    ("route", "set_env", "other_env", "expected"),
    [
        (ZAI_CODING_ROUTE, "ZAI_CODING_API_KEY", "ZAI_API_KEY", "coding-key"),
        (ZAI_API_ROUTE, "ZAI_API_KEY", "ZAI_CODING_API_KEY", "api-key"),
    ],
)
def test_route_environment_keys_never_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    route: ZaiRoute,
    set_env: str,
    other_env: str,
    expected: str,
) -> None:
    monkeypatch.setenv(set_env, f"  {expected}  ")
    monkeypatch.setenv(other_env, "wrong-route-key")
    assert get_z_ai_api_key_from_env(route) == expected
    monkeypatch.delenv(set_env)
    assert get_z_ai_api_key_from_env(route) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, None),
        ({}, None),
        ({"data": "invalid"}, None),
        ({"data": []}, ()),
        ({"data": [{"id": "not-glm"}]}, ()),
    ],
)
def test_parse_discovered_models_distinguishes_malformed_and_empty(
    payload: object,
    expected: tuple[ZaiModel, ...] | None,
) -> None:
    assert _parse_discovered_models(payload) == expected


def test_parse_discovered_models_pins_known_values_and_keeps_unknown_conservative() -> None:
    result = _parse_discovered_models(
        {
            "data": [
                {"id": "glm-5.1", "context_length": 400_000},
                {"id": "glm-5.1", "context_length": 1},
                {"id": "glm-future", "context_length": 512_000},
            ]
        }
    )
    assert result is not None
    by_id = {model.model_id: model for model in result}
    assert by_id["glm-5.1"].max_context_size == 400_000
    assert by_id["glm-future"] == ZaiModel(
        model_id="glm-future",
        display_name="GLM-Future",
        max_context_size=512_000,
    )


class _FakeResponse:
    status = 200

    async def json(self, *, content_type: None = None) -> object:
        assert content_type is None
        return {"data": [{"id": "glm-5.2"}]}


class _FakeRequestContext(AbstractAsyncContextManager[_FakeResponse]):
    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        raise_for_status: bool,
    ) -> _FakeRequestContext:
        assert raise_for_status is False
        self.calls.append((url, headers))
        return _FakeRequestContext()


class _FakeSessionContext(AbstractAsyncContextManager[_FakeSession]):
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


@pytest.mark.parametrize("route", ZAI_ROUTES)
async def test_model_request_uses_route_models_url_and_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
    route: ZaiRoute,
) -> None:
    from pythinker_code.auth import z_ai

    session = _FakeSession()
    monkeypatch.setattr(
        z_ai,
        "new_client_session",
        lambda **_kwargs: _FakeSessionContext(session),
    )

    payload = await _request_z_ai_models(route, "route-key")

    assert payload == {"data": [{"id": "glm-5.2"}]}
    assert session.calls == [(route.models_url, {"Authorization": "Bearer route-key"})]


@pytest.mark.parametrize(
    ("outcome", "status", "failure"),
    [
        (_response_error(ZAI_API_ROUTE, 401), "unauthorized", "unauthorized"),
        (_response_error(ZAI_API_ROUTE, 403), "unauthorized", "unauthorized"),
        (_response_error(ZAI_API_ROUTE, 503), "degraded", "http"),
        (TimeoutError(), "degraded", "timeout"),
        (aiohttp.ClientConnectionError(), "degraded", "transport"),
        ({"unexpected": []}, "degraded", "malformed"),
        ({"data": []}, "degraded", "empty"),
    ],
)
async def test_catalog_failure_categories_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
    status: str,
    failure: str,
) -> None:
    from pythinker_code.auth import z_ai

    async def fake_request(_route: ZaiRoute, _api_key: str) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(z_ai, "_request_z_ai_models", fake_request)

    result = await _discover_z_ai_catalog(ZAI_API_ROUTE, "route-key")

    assert result.status == status
    assert result.failure == failure
    assert result.models is None


@pytest.mark.parametrize("route", ZAI_ROUTES)
async def test_refresh_unconfigured_route_does_not_call_network(
    monkeypatch: pytest.MonkeyPatch,
    route: ZaiRoute,
) -> None:
    from pythinker_code.auth import z_ai

    request = AsyncMock()
    monkeypatch.setattr(z_ai, "_request_z_ai_models", request)

    result = await refresh_z_ai_models(Config(is_from_default_location=True), route)

    assert result == ZaiCatalogResult(
        status="unconfigured",
        models=None,
        failure="unconfigured",
    )
    request.assert_not_awaited()


@pytest.mark.parametrize(
    ("route", "login"),
    [
        (ZAI_CODING_ROUTE, login_z_ai_coding_api_key),
        (ZAI_API_ROUTE, login_z_ai_api_key),
    ],
)
async def test_login_degraded_catalog_is_explicit_and_route_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    route: ZaiRoute,
    login: Any,
) -> None:
    from pythinker_code.auth import z_ai

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        z_ai,
        "_discover_z_ai_catalog",
        AsyncMock(
            return_value=ZaiCatalogResult(
                status="degraded",
                models=None,
                failure="transport",
            )
        ),
    )
    config = Config(is_from_default_location=True)

    events = [event async for event in login(config, "route-key")]

    assert [event.type for event in events] == ["info", "success"]
    assert route.provider_key in config.providers
    assert f"{route.platform_id}/glm-5.2" in config.models
    other_route = ZAI_API_ROUTE if route is ZAI_CODING_ROUTE else ZAI_CODING_ROUTE
    assert other_route.provider_key not in config.providers


@pytest.mark.parametrize("status", ["unauthorized"])
async def test_login_auth_failure_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    status: str,
) -> None:
    from pythinker_code.auth import z_ai

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        z_ai,
        "_discover_z_ai_catalog",
        AsyncMock(
            return_value=ZaiCatalogResult(
                status=status,  # type: ignore[arg-type]
                models=None,
                failure="unauthorized",
            )
        ),
    )
    config = Config(is_from_default_location=True)

    events = [event async for event in login_z_ai_api_key(config, "bad-key")]

    assert events[-1].type == "error"
    assert config.providers == {}
    assert config.models == {}
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize(
    ("login", "route"),
    [
        (login_z_ai_coding_api_key, ZAI_CODING_ROUTE),
        (login_z_ai_api_key, ZAI_API_ROUTE),
    ],
)
async def test_login_missing_key_does_not_call_network(
    monkeypatch: pytest.MonkeyPatch,
    login: Any,
    route: ZaiRoute,
) -> None:
    from pythinker_code.auth import z_ai

    monkeypatch.delenv(route.api_key_env, raising=False)
    discover = AsyncMock()
    monkeypatch.setattr(z_ai, "_discover_z_ai_catalog", discover)

    events = [event async for event in login(Config(is_from_default_location=True), "")]

    assert events[-1].type == "error"
    discover.assert_not_awaited()


async def test_both_zai_routes_coexist_and_logout_is_owner_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    _apply_z_ai_config(config, ZAI_CODING_ROUTE, SecretStr("coding"))
    _apply_z_ai_config(config, ZAI_API_ROUTE, SecretStr("api"))
    config.default_model = "z-ai-coding/glm-5.2"

    events = [event async for event in logout_z_ai_coding(config)]

    assert events[-1].type == "success"
    assert ZAI_CODING_ROUTE.provider_key not in config.providers
    assert not any(
        model.provider == ZAI_CODING_ROUTE.provider_key for model in config.models.values()
    )
    assert ZAI_API_ROUTE.provider_key in config.providers
    assert "z-ai-api/glm-5.2" in config.models
    assert config.default_model == "z-ai-api/glm-5.2"
    reloaded = load_config()
    assert reloaded.default_model == config.default_model
    assert set(reloaded.providers) == set(config.providers)
    assert set(reloaded.models) == set(config.models)


async def test_logout_only_route_repairs_default_to_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    _apply_z_ai_config(config, ZAI_API_ROUTE, SecretStr("api"))

    events = [event async for event in logout_z_ai_api(config)]

    assert events[-1].type == "success"
    assert config.default_model == ""
    assert config.providers == {}
    assert config.models == {}
    assert load_config().default_model == ""


def test_apply_models_is_route_scoped_and_unknown_models_get_no_capabilities() -> None:
    config = Config(is_from_default_location=True)
    _apply_z_ai_config(config, ZAI_CODING_ROUTE, SecretStr("coding"))
    _apply_z_ai_config(config, ZAI_API_ROUTE, SecretStr("api"))
    before_api = {
        key: value.model_copy(deep=True)
        for key, value in config.models.items()
        if value.provider == ZAI_API_ROUTE.provider_key
    }

    changed = apply_z_ai_models(
        config,
        ZAI_CODING_ROUTE,
        (
            ZaiModel("glm-5.1", "GLM-5.1 Live", 400_000),
            ZaiModel("glm-future", "GLM-Future", 512_000),
        ),
    )

    assert changed is True
    assert config.models["z-ai-coding/glm-5.1"].max_context_size == 400_000
    assert config.models["z-ai-coding/glm-future"].capabilities is None
    after_api = {
        key: value
        for key, value in config.models.items()
        if value.provider == ZAI_API_ROUTE.provider_key
    }
    assert after_api == before_api
