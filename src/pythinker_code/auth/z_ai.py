from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import aiohttp
from pydantic import SecretStr

from pythinker_code.auth import ZAI_API_PLATFORM_ID, ZAI_CODING_PLATFORM_ID
from pythinker_code.auth.oauth import OAuthEvent
from pythinker_code.auth.platforms import managed_model_key, managed_provider_key
from pythinker_code.config import Config, LLMModel, LLMProvider, save_config
from pythinker_code.provider_compatibility import (
    get_zai_model_policies,
    get_zai_model_policy,
)
from pythinker_code.thinking import apply_login_thinking_defaults
from pythinker_code.utils.aiohttp import new_client_session

ZAI_MODEL_DISCOVERY_TIMEOUT = aiohttp.ClientTimeout(total=15, sock_connect=8, sock_read=10)

type ZaiCatalogStatus = Literal["live", "degraded", "unauthorized", "unconfigured"]
type ZaiCatalogFailure = Literal[
    "empty",
    "http",
    "malformed",
    "timeout",
    "transport",
    "unauthorized",
    "unconfigured",
]


@dataclass(frozen=True, slots=True)
class ZaiRoute:
    platform_id: str
    display_name: str
    base_url: str
    api_key_env: str

    @property
    def provider_key(self) -> str:
        return managed_provider_key(self.platform_id)

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"


ZAI_CODING_ROUTE = ZaiRoute(
    platform_id=ZAI_CODING_PLATFORM_ID,
    display_name="Z.AI Coding Plan",
    base_url="https://api.z.ai/api/coding/paas/v4",
    api_key_env="ZAI_CODING_API_KEY",
)
ZAI_API_ROUTE = ZaiRoute(
    platform_id=ZAI_API_PLATFORM_ID,
    display_name="Z.AI API",
    base_url="https://api.z.ai/api/paas/v4",
    api_key_env="ZAI_API_KEY",
)
ZAI_ROUTES = (ZAI_CODING_ROUTE, ZAI_API_ROUTE)


@dataclass(frozen=True, slots=True)
class ZaiModel:
    model_id: str
    display_name: str
    max_context_size: int


@dataclass(frozen=True, slots=True)
class ZaiCatalogResult:
    status: ZaiCatalogStatus
    models: tuple[ZaiModel, ...] | None
    failure: ZaiCatalogFailure | None = None

    def __post_init__(self) -> None:
        if self.status == "live":
            if not self.models or self.failure is not None:
                raise ValueError("A live Z.AI catalog requires non-empty models and no failure")
        elif self.models is not None:
            raise ValueError("A non-live Z.AI catalog must not carry models")


def _display_name(model_id: str) -> str:
    return "-".join(
        part.upper() if part.lower() == "glm" else part.capitalize() for part in model_id.split("-")
    )


ZAI_MODELS: tuple[ZaiModel, ...] = tuple(
    ZaiModel(
        model_id=policy.model_id,
        display_name=_display_name(policy.model_id),
        max_context_size=policy.context_tokens,
    )
    for policy in get_zai_model_policies()
)
_PINNED_MODEL_IDS = frozenset({"glm-5.2"})


def get_z_ai_api_key_from_env(route: ZaiRoute) -> str | None:
    value = os.getenv(route.api_key_env)
    if value and value.strip():
        return value.strip()
    return None


def _to_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _context_size_from_item(item: Mapping[str, Any], fallback: int) -> int:
    for key in ("context_length", "max_context_length", "context_window"):
        if (parsed := _to_positive_int(item.get(key))) is not None:
            return parsed
    return fallback


def _display_name_from_item(item: Mapping[str, Any], fallback: str) -> str:
    for key in ("display_name", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _parse_discovered_models(data: object) -> tuple[ZaiModel, ...] | None:
    if not isinstance(data, dict):
        return None
    raw_items = cast(dict[str, Any], data).get("data")
    if not isinstance(raw_items, list):
        return None

    result: list[ZaiModel] = []
    seen: set[str] = set()
    for raw_item in cast(list[Any], raw_items):
        if not isinstance(raw_item, Mapping):
            continue
        item = cast(Mapping[str, Any], raw_item)
        raw_model_id = item.get("id")
        if not isinstance(raw_model_id, str):
            continue
        model_id = raw_model_id.strip().lower()
        if not model_id.startswith("glm-") or model_id in seen:
            continue
        seen.add(model_id)
        policy = get_zai_model_policy(model_id)
        fallback_context = policy.context_tokens if policy is not None else 131_072
        fallback_display = _display_name(model_id)
        result.append(
            ZaiModel(
                model_id=model_id,
                display_name=_display_name_from_item(item, fallback_display),
                max_context_size=_context_size_from_item(item, fallback_context),
            )
        )
    return tuple(result)


def _with_pinned_models(models: tuple[ZaiModel, ...]) -> tuple[ZaiModel, ...]:
    present = {model.model_id for model in models}
    pins = tuple(
        model
        for model in ZAI_MODELS
        if model.model_id in _PINNED_MODEL_IDS and model.model_id not in present
    )
    return pins + models


async def _request_z_ai_models(route: ZaiRoute, api_key: str) -> object:
    async with (
        new_client_session(timeout=ZAI_MODEL_DISCOVERY_TIMEOUT) as session,
        session.get(
            route.models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            raise_for_status=False,
        ) as response,
    ):
        if response.status >= 400:
            raise aiohttp.ClientResponseError(
                response.request_info,
                response.history,
                status=response.status,
                message="Z.AI model catalog request failed",
                headers=response.headers,
            )
        return await response.json(content_type=None)


async def _discover_z_ai_catalog(route: ZaiRoute, api_key: str) -> ZaiCatalogResult:
    try:
        payload = await _request_z_ai_models(route, api_key)
    except aiohttp.ClientResponseError as exc:
        if exc.status in {401, 403}:
            return ZaiCatalogResult(
                status="unauthorized",
                models=None,
                failure="unauthorized",
            )
        return ZaiCatalogResult(status="degraded", models=None, failure="http")
    except TimeoutError:
        return ZaiCatalogResult(status="degraded", models=None, failure="timeout")
    except aiohttp.ClientError:
        return ZaiCatalogResult(status="degraded", models=None, failure="transport")
    except (TypeError, ValueError):
        return ZaiCatalogResult(status="degraded", models=None, failure="malformed")

    models = _parse_discovered_models(payload)
    if models is None:
        return ZaiCatalogResult(status="degraded", models=None, failure="malformed")
    if not models:
        return ZaiCatalogResult(status="degraded", models=None, failure="empty")
    return ZaiCatalogResult(status="live", models=_with_pinned_models(models))


def _model_alias(route: ZaiRoute, model_id: str) -> str:
    return managed_model_key(route.platform_id, model_id)


def _to_model_config(route: ZaiRoute, model: ZaiModel) -> LLMModel:
    policy = get_zai_model_policy(model.model_id)
    return LLMModel(
        provider=route.provider_key,
        model=model.model_id,
        max_context_size=model.max_context_size,
        capabilities={"thinking"} if policy is not None else None,
        display_name=model.display_name,
    )


def _apply_z_ai_config(
    config: Config,
    route: ZaiRoute,
    api_key: SecretStr,
    models: tuple[ZaiModel, ...] = ZAI_MODELS,
) -> None:
    config.providers[route.provider_key] = LLMProvider(
        type="openai_legacy",
        base_url=route.base_url,
        api_key=api_key,
    )
    for alias, model in list(config.models.items()):
        if model.provider == route.provider_key:
            del config.models[alias]

    models = _with_pinned_models(models)
    for model in models:
        config.models[_model_alias(route, model.model_id)] = _to_model_config(route, model)

    preferred = _model_alias(route, "glm-5.2")
    if preferred in config.models:
        config.default_model = preferred
    else:
        route_alias = next(
            (
                alias
                for alias, model in config.models.items()
                if model.provider == route.provider_key
            ),
            "",
        )
        config.default_model = route_alias or next(iter(config.models), "")
    apply_login_thinking_defaults(config, thinking=True, effort="high")


async def _login_z_ai_route(
    config: Config,
    route: ZaiRoute,
    api_key: str | None,
) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Login requires the default config file; restart without --config/--config-file.",
        )
        return

    resolved_key = (api_key or get_z_ai_api_key_from_env(route) or "").strip()
    if not resolved_key:
        yield OAuthEvent("error", f"{route.display_name} API key is required.")
        return

    catalog = await _discover_z_ai_catalog(route, resolved_key)
    if catalog.status == "unauthorized":
        yield OAuthEvent(
            "error",
            f"Invalid {route.display_name} API key; the key was not saved.",
        )
        return
    if catalog.status == "live":
        assert catalog.models is not None
        models = catalog.models
    elif catalog.status == "degraded":
        yield OAuthEvent(
            "info",
            f"{route.display_name} model listing is unavailable; using the built-in catalog.",
        )
        models = ZAI_MODELS
    else:
        yield OAuthEvent("error", f"{route.display_name} could not be configured.")
        return

    _apply_z_ai_config(config, route, SecretStr(resolved_key), models)
    save_config(config)
    yield OAuthEvent("success", f"{route.display_name} configured with {config.default_model}.")


async def login_z_ai_coding_api_key(
    config: Config,
    api_key: str | None = None,
) -> AsyncIterator[OAuthEvent]:
    async for event in _login_z_ai_route(config, ZAI_CODING_ROUTE, api_key):
        yield event


async def login_z_ai_api_key(
    config: Config,
    api_key: str | None = None,
) -> AsyncIterator[OAuthEvent]:
    async for event in _login_z_ai_route(config, ZAI_API_ROUTE, api_key):
        yield event


async def _logout_z_ai_route(
    config: Config,
    route: ZaiRoute,
) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Logout requires the default config file; restart without --config/--config-file.",
        )
        return

    config.providers.pop(route.provider_key, None)
    for alias, model in list(config.models.items()):
        if model.provider == route.provider_key:
            del config.models[alias]
    if config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
    save_config(config)
    yield OAuthEvent("success", f"Logged out of {route.display_name} successfully.")


async def logout_z_ai_coding(config: Config) -> AsyncIterator[OAuthEvent]:
    async for event in _logout_z_ai_route(config, ZAI_CODING_ROUTE):
        yield event


async def logout_z_ai_api(config: Config) -> AsyncIterator[OAuthEvent]:
    async for event in _logout_z_ai_route(config, ZAI_API_ROUTE):
        yield event


def apply_z_ai_models(
    config: Config,
    route: ZaiRoute,
    models: tuple[ZaiModel, ...],
) -> bool:
    models = _with_pinned_models(models)
    aliases = [_model_alias(route, model.model_id) for model in models]
    changed = False
    for alias, model in zip(aliases, models, strict=True):
        updated = _to_model_config(route, model)
        if config.models.get(alias) != updated:
            config.models[alias] = updated
            changed = True

    alias_set = set(aliases)
    removed_default = False
    for alias, model in list(config.models.items()):
        if model.provider != route.provider_key or alias in alias_set:
            continue
        del config.models[alias]
        removed_default = removed_default or config.default_model == alias
        changed = True

    if removed_default:
        config.default_model = aliases[0] if aliases else next(iter(config.models), "")
        changed = True
    elif config.default_model and config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
        changed = True
    return changed


def _z_ai_api_key(config: Config, route: ZaiRoute) -> str | None:
    provider = config.providers.get(route.provider_key)
    if provider is None:
        return None
    value = provider.api_key.get_secret_value().strip()
    return value or None


async def refresh_z_ai_models(config: Config, route: ZaiRoute) -> ZaiCatalogResult:
    api_key = _z_ai_api_key(config, route)
    if api_key is None:
        return ZaiCatalogResult(
            status="unconfigured",
            models=None,
            failure="unconfigured",
        )
    return await _discover_z_ai_catalog(route, api_key)
