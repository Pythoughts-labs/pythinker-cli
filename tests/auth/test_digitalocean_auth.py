from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from pythinker_code.config import Config, LLMModel, LLMProvider


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
async def test_login_digitalocean_saves_discovered_routers_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth.digitalocean import (
        DIGITALOCEAN_PROVIDER_KEY,
        login_digitalocean,
    )
    from pythinker_code.auth.oauth_flows import ImplicitAuthorization

    access_token = "secret-digitalocean-token"
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_implicit_flow(**kwargs: Any) -> ImplicitAuthorization:
        return ImplicitAuthorization(access_token, 2_592_000, "state")

    async def fake_fetch_router_names(token: str) -> tuple[str, ...]:
        assert token == access_token
        return ("primary", "fallback")

    monkeypatch.setattr(
        "pythinker_code.auth.digitalocean.run_loopback_implicit_flow",
        fake_implicit_flow,
    )
    monkeypatch.setattr(
        "pythinker_code.auth.digitalocean._fetch_router_names",
        fake_fetch_router_names,
    )

    events = [event async for event in login_digitalocean(config, open_browser=False)]

    assert [event.type for event in events] == ["waiting", "success"]
    assert config.default_model == "digitalocean/router:primary"
    assert config.providers[DIGITALOCEAN_PROVIDER_KEY].api_key.get_secret_value() == access_token
    assert access_token not in "\n".join(f"{event!r}\n{event.json}" for event in events)
    assert (tmp_path / "config.toml").exists()


@pytest.mark.asyncio
async def test_login_digitalocean_succeeds_when_router_catalog_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pythinker_code.auth.digitalocean import (
        DIGITALOCEAN_PROVIDER_KEY,
        login_digitalocean,
    )
    from pythinker_code.auth.oauth_flows import ImplicitAuthorization

    access_token = "secret-empty-router-token"
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_implicit_flow(**kwargs: Any) -> ImplicitAuthorization:
        return ImplicitAuthorization(access_token, 2_592_000, "state")

    async def fake_fetch_router_names(token: str) -> tuple[str, ...]:
        assert token == access_token
        return ()

    monkeypatch.setattr(
        "pythinker_code.auth.digitalocean.run_loopback_implicit_flow",
        fake_implicit_flow,
    )
    monkeypatch.setattr(
        "pythinker_code.auth.digitalocean._fetch_router_names",
        fake_fetch_router_names,
    )

    events = [event async for event in login_digitalocean(config)]

    assert [event.type for event in events] == ["waiting", "info", "success"]
    assert DIGITALOCEAN_PROVIDER_KEY in config.providers
    assert not any(model.provider == DIGITALOCEAN_PROVIDER_KEY for model in config.models.values())
    assert access_token not in "\n".join(f"{event!r}\n{event.json}" for event in events)
