from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

import aiohttp
from pydantic import SecretStr

from pythinker_code.auth import DIGITALOCEAN_PLATFORM_ID
from pythinker_code.auth.oauth import OAuthError, OAuthEvent, persist_config_change
from pythinker_code.auth.oauth_flows import run_loopback_implicit_flow
from pythinker_code.auth.platforms import managed_model_key, managed_provider_key
from pythinker_code.config import Config, LLMModel, LLMProvider
from pythinker_code.thinking import apply_login_thinking_defaults
from pythinker_code.utils.aiohttp import new_client_session
from pythinker_code.utils.logging import logger

DIGITALOCEAN_CLIENT_ID = "b1a6c5158156caac821fd1b30253ca8acb52454a48fa744420e41889cb589f82"
DIGITALOCEAN_AUTHORIZE_URL = "https://cloud.digitalocean.com/v1/oauth/authorize"
DIGITALOCEAN_SCOPE = "genai:read inference:query"
DIGITALOCEAN_BASE_URL = "https://inference.do-ai.run/v1"
DIGITALOCEAN_ROUTERS_URL = "https://api.digitalocean.com/v2/gen-ai/models/routers"
DIGITALOCEAN_REDIRECT_PORT = 1456
DIGITALOCEAN_CALLBACK_PATH = "/auth/callback"
DIGITALOCEAN_TOKEN_PATH = "/auth/token"
DIGITALOCEAN_PROVIDER_KEY = managed_provider_key(DIGITALOCEAN_PLATFORM_ID)
DIGITALOCEAN_DEFAULT_CONTEXT = 128_000


class RouterDiscovery(str, Enum):
    """Outcome of a DigitalOcean inference-router discovery call."""

    OK = "ok"  # routers were discovered
    PARTIAL = "partial"  # valid routers were discovered alongside malformed entries
    EMPTY = "empty"  # the account has no routers (valid, but empty)
    UNAUTHORIZED = "unauthorized"  # the token could not list routers
    UNAVAILABLE = "unavailable"  # timeout / outage / non-2xx response
    MALFORMED = "malformed"  # the response body was not the expected shape


@dataclass(frozen=True, slots=True)
class RouterCatalog:
    """Discovered routers paired with the outcome that produced them."""

    status: RouterDiscovery
    names: tuple[str, ...]


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
            max_context_size=DIGITALOCEAN_DEFAULT_CONTEXT,
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


def _parse_router_names(payload: object) -> RouterCatalog:
    if not isinstance(payload, dict):
        return RouterCatalog(RouterDiscovery.MALFORMED, ())
    raw = cast(dict[str, Any], payload).get("model_routers")
    if not isinstance(raw, list):
        return RouterCatalog(RouterDiscovery.MALFORMED, ())
    if not raw:
        return RouterCatalog(RouterDiscovery.EMPTY, ())

    names: list[str] = []
    has_malformed_entry = False
    for item in cast(list[Any], raw):
        if isinstance(item, dict):
            name = cast(dict[str, Any], item).get("name")
            if isinstance(name, str) and name.strip():
                names.append(name)
                continue
        has_malformed_entry = True
    if not names:
        return RouterCatalog(RouterDiscovery.MALFORMED, ())
    if has_malformed_entry:
        return RouterCatalog(RouterDiscovery.PARTIAL, tuple(names))
    return RouterCatalog(RouterDiscovery.OK, tuple(names))


async def _fetch_router_catalog(access_token: str) -> RouterCatalog:
    """Discover the account's inference routers, preserving distinct outcomes.

    Authentication failures, dependency outages, malformed responses, and a
    valid-but-empty catalog are each reported separately so the login flow can
    surface an accurate message instead of collapsing everything into "no
    routers".
    """
    try:
        async with (
            new_client_session() as session,
            session.get(
                DIGITALOCEAN_ROUTERS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            ) as response,
        ):
            status = response.status
            if status in (401, 403):
                return RouterCatalog(RouterDiscovery.UNAUTHORIZED, ())
            if not 200 <= status < 300:
                return RouterCatalog(RouterDiscovery.UNAVAILABLE, ())
            try:
                payload: Any = await response.json(content_type=None)
            except (ValueError, aiohttp.ClientError):
                return RouterCatalog(RouterDiscovery.MALFORMED, ())
    except (TimeoutError, aiohttp.ClientError, OSError):
        return RouterCatalog(RouterDiscovery.UNAVAILABLE, ())

    return _parse_router_names(payload)


def _router_status_message(status: RouterDiscovery) -> str | None:
    if status is RouterDiscovery.OK:
        return None
    if status is RouterDiscovery.PARTIAL:
        return (
            "DigitalOcean returned some malformed inference-router entries; sign-in saved "
            "with valid routers configured and malformed entries ignored."
        )
    if status is RouterDiscovery.EMPTY:
        return "DigitalOcean returned no inference routers; sign-in saved with no models."
    if status is RouterDiscovery.MALFORMED:
        return (
            "DigitalOcean returned entirely malformed inference-router data; sign-in saved "
            "with no models configured."
        )
    if status is RouterDiscovery.UNAUTHORIZED:
        return (
            "DigitalOcean did not authorize inference-router discovery; sign-in saved. "
            "Re-run login once the account has inference access."
        )
    return (
        "DigitalOcean Inference Routers were unavailable; sign-in saved. "
        "Re-run login to load routers."
    )


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

    catalog = await _fetch_router_catalog(auth.access_token)
    try:
        await persist_config_change(
            config,
            lambda cfg: _apply_digitalocean_config(
                cfg, SecretStr(auth.access_token), catalog.names
            ),
        )
    except Exception as exc:
        logger.warning("Failed to persist DigitalOcean login: {exc}", exc=exc)
        yield OAuthEvent("error", "Failed to save DigitalOcean login.")
        return

    message = _router_status_message(catalog.status)
    if message:
        yield OAuthEvent("info", message)
    if catalog.names:
        success_message = f"DigitalOcean configured with model {config.default_model}."
    else:
        success_message = "DigitalOcean credentials saved; no inference routers are configured."
    yield OAuthEvent("success", success_message)


async def logout_digitalocean(config: Config) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Logout requires the default config file; restart without --config/--config-file.",
        )
        return

    def _remove(cfg: Config) -> None:
        cfg.providers.pop(DIGITALOCEAN_PROVIDER_KEY, None)
        for alias, model in list(cfg.models.items()):
            if model.provider == DIGITALOCEAN_PROVIDER_KEY:
                del cfg.models[alias]
        if cfg.default_model not in cfg.models:
            cfg.default_model = next(iter(cfg.models), "")

    try:
        await persist_config_change(config, _remove)
    except Exception as exc:
        logger.warning("Failed to persist DigitalOcean logout: {exc}", exc=exc)
        yield OAuthEvent("error", "Failed to log out of DigitalOcean.")
        return
    yield OAuthEvent("success", "Logged out of DigitalOcean successfully.")
