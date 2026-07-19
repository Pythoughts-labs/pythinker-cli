from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import aiohttp
from pydantic import SecretStr

from pythinker_code.auth import DIGITALOCEAN_PLATFORM_ID
from pythinker_code.auth.oauth import OAuthError, OAuthEvent
from pythinker_code.auth.oauth_flows import run_loopback_implicit_flow
from pythinker_code.auth.platforms import managed_model_key, managed_provider_key
from pythinker_code.config import Config, LLMModel, LLMProvider, save_config
from pythinker_code.thinking import apply_login_thinking_defaults
from pythinker_code.utils.aiohttp import new_client_session

DIGITALOCEAN_CLIENT_ID = "b1a6c5158156caac821fd1b30253ca8acb52454a48fa744420e41889cb589f82"
DIGITALOCEAN_AUTHORIZE_URL = "https://cloud.digitalocean.com/v1/oauth/authorize"
DIGITALOCEAN_SCOPE = "genai:read inference:query"
DIGITALOCEAN_BASE_URL = "https://inference.do-ai.run/v1"
DIGITALOCEAN_ROUTERS_URL = "https://api.digitalocean.com/v2/gen-ai/models/routers"
DIGITALOCEAN_REDIRECT_PORT = 1456
DIGITALOCEAN_CALLBACK_PATH = "/auth/callback"
DIGITALOCEAN_TOKEN_PATH = "/auth/token"
DIGITALOCEAN_PROVIDER_KEY = managed_provider_key(DIGITALOCEAN_PLATFORM_ID)


def _skip_browser_open(_url: str) -> None:
    return None


def _apply_digitalocean_config(
    config: Config,
    api_key: SecretStr,
    router_names: tuple[str, ...],
) -> None:
    config.providers[DIGITALOCEAN_PROVIDER_KEY] = LLMProvider(
        type="openai_legacy",
        base_url=DIGITALOCEAN_BASE_URL,
        api_key=api_key,
    )

    for alias, model in list(config.models.items()):
        if model.provider == DIGITALOCEAN_PROVIDER_KEY:
            del config.models[alias]

    for name in router_names:
        model_id = f"router:{name}"
        alias = managed_model_key(DIGITALOCEAN_PLATFORM_ID, model_id)
        config.models[alias] = LLMModel(
            provider=DIGITALOCEAN_PROVIDER_KEY,
            model=model_id,
            max_context_size=128_000,
            display_name=name,
        )

    if router_names:
        config.default_model = managed_model_key(
            DIGITALOCEAN_PLATFORM_ID,
            f"router:{router_names[0]}",
        )
    elif config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
    apply_login_thinking_defaults(config, thinking=False, effort="off")


async def _fetch_router_names(access_token: str) -> tuple[str, ...]:
    try:
        async with (
            new_client_session() as session,
            session.get(
                DIGITALOCEAN_ROUTERS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                raise_for_status=True,
            ) as response,
        ):
            payload: Any = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
        return ()

    if not isinstance(payload, dict):
        return ()
    payload = cast(dict[str, Any], payload)
    raw = payload.get("model_routers")
    if not isinstance(raw, list):
        return ()

    result: list[str] = []
    for item in cast(list[Any], raw):
        if isinstance(item, dict):
            name = cast(dict[str, Any], item).get("name")
            if isinstance(name, str) and name:
                result.append(name)
    return tuple(result)


async def login_digitalocean(
    config: Config, *, open_browser: bool = True
) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Login requires the default config file; restart without --config/--config-file.",
        )
        return

    yield OAuthEvent("waiting", "Waiting for DigitalOcean browser authorization...")
    try:
        auth = await run_loopback_implicit_flow(
            authorize_endpoint=DIGITALOCEAN_AUTHORIZE_URL,
            client_id=DIGITALOCEAN_CLIENT_ID,
            scope=DIGITALOCEAN_SCOPE,
            callback_path=DIGITALOCEAN_CALLBACK_PATH,
            token_path=DIGITALOCEAN_TOKEN_PATH,
            port=DIGITALOCEAN_REDIRECT_PORT,
            browser_open=None if open_browser else _skip_browser_open,
        )
    except OAuthError as exc:
        yield OAuthEvent("error", f"DigitalOcean browser login failed: {exc}")
        return

    router_names = await _fetch_router_names(auth.access_token)
    _apply_digitalocean_config(config, SecretStr(auth.access_token), router_names)
    save_config(config)
    if not router_names:
        yield OAuthEvent(
            "info",
            "DigitalOcean Inference Routers unavailable; sign-in saved. "
            "Re-run login to load routers.",
        )
    yield OAuthEvent("success", f"DigitalOcean configured with model {config.default_model}.")


async def logout_digitalocean(config: Config) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Logout requires the default config file; restart without --config/--config-file.",
        )
        return

    provider_keys = {DIGITALOCEAN_PROVIDER_KEY}
    config.providers.pop(DIGITALOCEAN_PROVIDER_KEY, None)
    for alias, model in list(config.models.items()):
        if model.provider in provider_keys:
            del config.models[alias]

    if config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
    save_config(config)
    yield OAuthEvent("success", "Logged out of DigitalOcean successfully.")
