from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest
from pydantic import SecretStr

from pythinker_code.config import Config, LLMModel, LLMProvider


class _RouterResponse:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload

    async def __aenter__(self) -> _RouterResponse:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def json(self, *, content_type: object = None) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _RouterSession:
    def __init__(
        self, response: _RouterResponse, calls: list[tuple[str, Mapping[str, str]]]
    ) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self) -> _RouterSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def get(self, url: str, *, headers: Mapping[str, str]) -> _RouterResponse:
        self.calls.append((url, dict(headers)))
        return self.response


class _RaisingRouterSession:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def __aenter__(self) -> _RaisingRouterSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def get(self, url: str, *, headers: Mapping[str, str]) -> object:
        raise self.exc


class _CallbackWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def test_apply_digitalocean_config_writes_provider_models_and_default() -> None:
    from pythinker_code.auth.digitalocean import (
        DIGITALOCEAN_BASE_URL,
        DIGITALOCEAN_PROVIDER_KEY,
        _apply_digitalocean_config,
    )

    config = Config(is_from_default_location=True)

    _apply_digitalocean_config(config, SecretStr("do-test"), ("production", "staging"))

    assert set(config.providers) == {DIGITALOCEAN_PROVIDER_KEY}
    provider = config.providers[DIGITALOCEAN_PROVIDER_KEY]
    assert provider.type == "openai_legacy"
    assert provider.base_url == DIGITALOCEAN_BASE_URL
    assert provider.api_key.get_secret_value() == "do-test"
    assert provider.oauth is None
    assert config.models["digitalocean/router:production"].provider == DIGITALOCEAN_PROVIDER_KEY
    assert config.models["digitalocean/router:production"].model == "router:production"
    assert config.models["digitalocean/router:production"].max_context_size == 128_000
    assert config.models["digitalocean/router:production"].display_name == "production"
    assert config.models["digitalocean/router:staging"].model == "router:staging"
    assert config.default_model == "digitalocean/router:production"


def test_apply_digitalocean_config_handles_empty_router_catalog() -> None:
    from pythinker_code.auth.digitalocean import (
        DIGITALOCEAN_PROVIDER_KEY,
        _apply_digitalocean_config,
    )

    fallback_alias = "openai/existing"
    config = Config(
        is_from_default_location=True,
        default_model=fallback_alias,
        providers={
            "managed:openai": LLMProvider(
                type="openai_legacy",
                base_url="https://example.test/v1",
                api_key=SecretStr("existing"),
            )
        },
        models={
            fallback_alias: LLMModel(
                provider="managed:openai",
                model="existing",
                max_context_size=1,
            )
        },
    )

    _apply_digitalocean_config(config, SecretStr("tok"), ())

    assert DIGITALOCEAN_PROVIDER_KEY in config.providers
    assert not any(model.provider == DIGITALOCEAN_PROVIDER_KEY for model in config.models.values())
    assert config.default_model == fallback_alias
    assert config.default_model in config.models


@pytest.mark.asyncio
async def test_fetch_router_catalog_returns_ok_with_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import digitalocean as do

    calls: list[tuple[str, Mapping[str, str]]] = []
    response = _RouterResponse(
        200,
        {"model_routers": [{"name": "primary"}, {"name": "fallback"}]},
    )
    monkeypatch.setattr(do, "new_client_session", lambda: _RouterSession(response, calls))

    catalog = await do._fetch_router_catalog("tok")

    assert catalog.status is do.RouterDiscovery.OK
    assert catalog.names == ("primary", "fallback")
    assert calls[0][0] == do.DIGITALOCEAN_ROUTERS_URL
    assert calls[0][1]["Authorization"] == "Bearer tok"


@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_names"),
    [
        ({"model_routers": []}, "EMPTY", ()),
        (
            {"model_routers": [{"name": ""}, {"missing_name": True}, "invalid"]},
            "MALFORMED",
            (),
        ),
        (
            {"model_routers": [{"name": "primary"}, {"missing_name": True}]},
            "PARTIAL",
            ("primary",),
        ),
        (
            {"model_routers": [{"name": "   "}]},
            "MALFORMED",
            (),
        ),
        (
            {"model_routers": [{"name": "primary"}, {"name": "   "}]},
            "PARTIAL",
            ("primary",),
        ),
        (
            {"model_routers": [{"name": "primary"}, {"name": "fallback"}]},
            "OK",
            ("primary", "fallback"),
        ),
        (
            {"model_routers": [{"name": "  primary router  "}]},
            "OK",
            ("  primary router  ",),
        ),
    ],
    ids=(
        "empty",
        "all-invalid",
        "mixed",
        "all-whitespace",
        "mixed-whitespace",
        "all-valid",
        "valid-name-preserved",
    ),
)
def test_parse_router_names_distinguishes_entry_validity(
    payload: object,
    expected_status: str,
    expected_names: tuple[str, ...],
) -> None:
    from pythinker_code.auth import digitalocean as do

    catalog = do._parse_router_names(payload)

    assert catalog.status.name == expected_status
    assert catalog.names == expected_names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_RouterResponse(200, {"model_routers": []}), "EMPTY"),
        (_RouterResponse(200, {"model_routers": [{"no_name": 1}]}), "MALFORMED"),
        (_RouterResponse(401, {"id": "unauthorized"}), "UNAUTHORIZED"),
        (_RouterResponse(403, {"id": "forbidden"}), "UNAUTHORIZED"),
        (_RouterResponse(500, {"id": "server_error"}), "UNAVAILABLE"),
        (_RouterResponse(200, ["not", "a", "dict"]), "MALFORMED"),
        (_RouterResponse(200, {"model_routers": "nope"}), "MALFORMED"),
        (_RouterResponse(200, ValueError("bad json")), "MALFORMED"),
    ],
)
async def test_fetch_router_catalog_distinguishes_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    response: _RouterResponse,
    expected: str,
) -> None:
    from pythinker_code.auth import digitalocean as do

    monkeypatch.setattr(do, "new_client_session", lambda: _RouterSession(response, []))

    catalog = await do._fetch_router_catalog("tok")

    assert catalog.status.name == expected
    assert catalog.names == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [aiohttp.ClientError(), TimeoutError(), OSError()])
async def test_fetch_router_catalog_treats_transport_errors_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
) -> None:
    from pythinker_code.auth import digitalocean as do

    monkeypatch.setattr(do, "new_client_session", lambda: _RaisingRouterSession(exc))

    catalog = await do._fetch_router_catalog("tok")

    assert catalog.status is do.RouterDiscovery.UNAVAILABLE


@pytest.mark.parametrize(
    ("status_name", "expected_message"),
    [
        (
            "PARTIAL",
            "DigitalOcean returned some malformed inference-router entries; sign-in saved "
            "with valid routers configured and malformed entries ignored.",
        ),
        (
            "MALFORMED",
            "DigitalOcean returned entirely malformed inference-router data; sign-in saved "
            "with no models configured.",
        ),
    ],
)
def test_router_status_message_distinguishes_partial_and_malformed_data(
    status_name: str,
    expected_message: str,
) -> None:
    from pythinker_code.auth import digitalocean as do

    status = do.RouterDiscovery[status_name]

    assert do._router_status_message(status) == expected_message


@pytest.mark.asyncio
async def test_login_digitalocean_saves_discovered_routers_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth import digitalocean as do
    from pythinker_code.auth.oauth_flows import ImplicitAuthorization

    access_token = "secret-digitalocean-token"
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_implicit_flow(**kwargs: Any) -> ImplicitAuthorization:
        return ImplicitAuthorization(access_token, None, "state")

    response = _RouterResponse(200, {"model_routers": [{"name": "primary"}, {"name": "fallback"}]})
    monkeypatch.setattr(do, "run_loopback_implicit_flow", fake_implicit_flow)
    monkeypatch.setattr(do, "new_client_session", lambda: _RouterSession(response, []))

    events = [event async for event in do.login_digitalocean(config, open_browser=False)]

    assert [event.type for event in events] == ["waiting", "success"]
    assert config.default_model == "digitalocean/router:primary"
    provider = config.providers[do.DIGITALOCEAN_PROVIDER_KEY]
    assert provider.api_key.get_secret_value() == access_token
    assert access_token not in "\n".join(f"{event!r}\n{event.json}" for event in events)
    assert (tmp_path / "config.toml").exists()


@pytest.mark.asyncio
async def test_login_digitalocean_rejects_blank_callback_token_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth import digitalocean as do
    from pythinker_code.auth.oauth_flows import (
        ImplicitAuthorization,
        _handle_implicit_loopback_callback,
    )

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    catalog_tokens: list[str] = []

    async def blank_implicit_flow(**_kwargs: Any) -> ImplicitAuthorization:
        result: asyncio.Future[ImplicitAuthorization] = asyncio.get_running_loop().create_future()
        payload = bytes(
            json.dumps({"access_token": "   ", "state": "expected-state"}),
            encoding="utf-8",
        )
        request = bytes(
            f"POST /auth/token HTTP/1.1\r\nContent-Length: {len(payload)}\r\n\r\n",
            encoding="utf-8",
        )
        reader = asyncio.StreamReader()
        reader.feed_data(request + payload)
        reader.feed_eof()
        await _handle_implicit_loopback_callback(
            reader,
            cast("asyncio.StreamWriter", _CallbackWriter()),
            callback_path="/auth/callback",
            token_path="/auth/token",
            expected_state="expected-state",
            result=result,
        )
        return await result

    async def track_catalog(access_token: str) -> do.RouterCatalog:
        catalog_tokens.append(access_token)
        return do.RouterCatalog(do.RouterDiscovery.EMPTY, ())

    monkeypatch.setattr(do, "run_loopback_implicit_flow", blank_implicit_flow)
    monkeypatch.setattr(do, "_fetch_router_catalog", track_catalog)

    events = [event async for event in do.login_digitalocean(config)]

    assert [event.type for event in events] == ["waiting", "error"]
    assert events[-1].message == (
        "DigitalOcean browser login failed: OAuth callback did not include an access token."
    )
    assert catalog_tokens == []
    assert do.DIGITALOCEAN_PROVIDER_KEY not in config.providers
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("router_payload", "expected_info", "expected_models", "expected_success"),
    [
        (
            {
                "model_routers": [
                    {"name": ""},
                    {"unexpected": "malformed-entry-secret"},
                ]
            },
            "DigitalOcean returned entirely malformed inference-router data; sign-in saved "
            "with no models configured.",
            set(),
            "DigitalOcean credentials saved; no inference routers are configured.",
        ),
        (
            {
                "model_routers": [
                    {"name": "primary"},
                    {"unexpected": "malformed-entry-secret"},
                ]
            },
            "DigitalOcean returned some malformed inference-router entries; sign-in saved "
            "with valid routers configured and malformed entries ignored.",
            {"digitalocean/router:primary"},
            "DigitalOcean configured with model digitalocean/router:primary.",
        ),
        (
            {"model_routers": [{"name": "   "}]},
            "DigitalOcean returned entirely malformed inference-router data; sign-in saved "
            "with no models configured.",
            set(),
            "DigitalOcean credentials saved; no inference routers are configured.",
        ),
        (
            {"model_routers": [{"name": "primary"}, {"name": "   "}]},
            "DigitalOcean returned some malformed inference-router entries; sign-in saved "
            "with valid routers configured and malformed entries ignored.",
            {"digitalocean/router:primary"},
            "DigitalOcean configured with model digitalocean/router:primary.",
        ),
    ],
    ids=("all-invalid", "mixed", "all-whitespace", "mixed-whitespace"),
)
async def test_login_digitalocean_reports_malformed_router_entries_safely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    router_payload: object,
    expected_info: str,
    expected_models: set[str],
    expected_success: str,
) -> None:
    from pythinker_code.auth import digitalocean as do
    from pythinker_code.auth.oauth_flows import ImplicitAuthorization

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_implicit_flow(**_kwargs: Any) -> ImplicitAuthorization:
        return ImplicitAuthorization("access-token", None, "state")

    response = _RouterResponse(200, router_payload)
    monkeypatch.setattr(do, "run_loopback_implicit_flow", fake_implicit_flow)
    monkeypatch.setattr(do, "new_client_session", lambda: _RouterSession(response, []))

    events = [event async for event in do.login_digitalocean(config)]

    assert [event.type for event in events] == ["waiting", "info", "success"]
    assert events[1].message == expected_info
    assert events[-1].message == expected_success
    configured_models = {
        alias
        for alias, model in config.models.items()
        if model.provider == do.DIGITALOCEAN_PROVIDER_KEY
    }
    assert configured_models == expected_models
    assert "malformed-entry-secret" not in "\n".join(event.json for event in events)


@pytest.mark.asyncio
async def test_digitalocean_persistence_errors_hide_internal_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.auth import digitalocean as do
    from pythinker_code.auth.oauth_flows import ImplicitAuthorization

    diagnostic = "permission denied at /private/config.toml"
    config = Config(is_from_default_location=True)

    async def fake_implicit_flow(**_kwargs: Any) -> ImplicitAuthorization:
        return ImplicitAuthorization("access-token", None, "state")

    async def fake_catalog(_access_token: str) -> do.RouterCatalog:
        return do.RouterCatalog(do.RouterDiscovery.OK, ("primary",))

    async def fail_persistence(*_args: object, **_kwargs: object) -> None:
        raise OSError(diagnostic)

    monkeypatch.setattr(do, "run_loopback_implicit_flow", fake_implicit_flow)
    monkeypatch.setattr(do, "_fetch_router_catalog", fake_catalog)
    monkeypatch.setattr(do, "persist_config_change", fail_persistence)

    login_events = [event async for event in do.login_digitalocean(config)]
    logout_events = [event async for event in do.logout_digitalocean(config)]

    assert login_events[-1].message == "Failed to save DigitalOcean login."
    assert logout_events[-1].message == "Failed to log out of DigitalOcean."
    assert diagnostic not in login_events[-1].json
    assert diagnostic not in logout_events[-1].json


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _RouterResponse(200, {"model_routers": []}),  # empty
        _RouterResponse(401, {"id": "unauthorized"}),  # unauthorized
        _RouterResponse(500, {"id": "server_error"}),  # outage
    ],
)
async def test_login_digitalocean_reports_router_discovery_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    response: _RouterResponse,
) -> None:
    from pythinker_code.auth import digitalocean as do
    from pythinker_code.auth.oauth_flows import ImplicitAuthorization

    access_token = "secret-empty-router-token"
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_implicit_flow(**kwargs: Any) -> ImplicitAuthorization:
        return ImplicitAuthorization(access_token, None, "state")

    monkeypatch.setattr(do, "run_loopback_implicit_flow", fake_implicit_flow)
    monkeypatch.setattr(do, "new_client_session", lambda: _RouterSession(response, []))

    events = [event async for event in do.login_digitalocean(config)]

    # Sign-in is saved but the degraded discovery is surfaced as an info event
    # before success, never silently swallowed.
    assert [event.type for event in events] == ["waiting", "info", "success"]
    assert do.DIGITALOCEAN_PROVIDER_KEY in config.providers
    assert not any(
        model.provider == do.DIGITALOCEAN_PROVIDER_KEY for model in config.models.values()
    )
    assert access_token not in "\n".join(f"{event!r}\n{event.json}" for event in events)
    assert "configured with model" not in events[-1].message
    assert events[-1].message == (
        "DigitalOcean credentials saved; no inference routers are configured."
    )
