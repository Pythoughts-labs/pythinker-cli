from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import aiohttp
from pydantic import SecretStr

from pythinker_code.auth import OPENCODE_GO_PLATFORM_ID
from pythinker_code.auth.models_dev import (
    ModelsDevCatalog,
    ModelsDevProvider,
    get_models_dev_catalog,
    get_provider_models,
    parse_models_dev_catalog,
)
from pythinker_code.auth.oauth import OAuthEvent
from pythinker_code.config import Config, LLMModel, LLMProvider, save_config
from pythinker_code.llm import ModelCapability
from pythinker_code.thinking import apply_login_thinking_defaults
from pythinker_code.utils.aiohttp import new_client_session

# OpenAI-compatible base: the OpenAI SDK appends "/chat/completions" (and the
# "/models" discovery path), so it includes the "/v1" segment.
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
# Anthropic-compatible base: the Anthropic SDK appends "/v1/messages" itself, so
# this base must NOT include "/v1" or requests 404 at ".../go/v1/v1/messages".
OPENCODE_GO_ANTHROPIC_BASE_URL = "https://opencode.ai/zen/go"
OPENCODE_GO_OPENAI_PROVIDER_KEY = "managed:opencode-go-openai"
OPENCODE_GO_ANTHROPIC_PROVIDER_KEY = "managed:opencode-go-anthropic"
OPENCODE_GO_PROVIDER_KEYS = (
    OPENCODE_GO_OPENAI_PROVIDER_KEY,
    OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
)
OPENCODE_GO_DEFAULT_MODEL_ALIAS = "opencode-go/kimi-k2.6"
OPENCODE_GO_DEFAULT_CONTEXT = 262_000

MODELS_DEV_PROVIDER_ID = "opencode-go"


@dataclass(frozen=True, slots=True)
class OpenCodeGoModel:
    model_id: str
    display_name: str
    provider_key: str
    max_context_size: int = 262_000

    @property
    def alias(self) -> str:
        return f"{OPENCODE_GO_PLATFORM_ID}/{self.model_id}"


OPENCODE_GO_MODELS: tuple[OpenCodeGoModel, ...] = (
    OpenCodeGoModel("glm-5", "GLM-5", OPENCODE_GO_OPENAI_PROVIDER_KEY),
    OpenCodeGoModel("glm-5.1", "GLM-5.1", OPENCODE_GO_OPENAI_PROVIDER_KEY),
    OpenCodeGoModel("kimi-k2.5", "Kimi K2.5", OPENCODE_GO_OPENAI_PROVIDER_KEY),
    OpenCodeGoModel("kimi-k2.6", "Kimi K2.6", OPENCODE_GO_OPENAI_PROVIDER_KEY),
    OpenCodeGoModel("deepseek-v4-pro", "DeepSeek V4 Pro", OPENCODE_GO_OPENAI_PROVIDER_KEY),
    OpenCodeGoModel("deepseek-v4-flash", "DeepSeek V4 Flash", OPENCODE_GO_OPENAI_PROVIDER_KEY),
    OpenCodeGoModel("mimo-v2-pro", "MiMo-V2-Pro", OPENCODE_GO_OPENAI_PROVIDER_KEY),
    OpenCodeGoModel("mimo-v2-omni", "MiMo-V2-Omni", OPENCODE_GO_OPENAI_PROVIDER_KEY),
    OpenCodeGoModel("mimo-v2.5-pro", "MiMo-V2.5-Pro", OPENCODE_GO_OPENAI_PROVIDER_KEY, 1_000_000),
    OpenCodeGoModel("mimo-v2.5", "MiMo-V2.5", OPENCODE_GO_OPENAI_PROVIDER_KEY, 1_000_000),
    # Qwen models speak the Anthropic-shaped endpoint (models.dev routes them
    # via @ai-sdk/anthropic); the OpenAI-shaped endpoint rejects them with
    # "not supported for format oa-compat".
    OpenCodeGoModel("qwen3.5-plus", "Qwen3.5 Plus", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 262_000),
    OpenCodeGoModel("qwen3.6-plus", "Qwen3.6 Plus", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 262_000),
    OpenCodeGoModel("qwen3.7-max", "Qwen3.7 Max", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 1_000_000),
    OpenCodeGoModel("minimax-m2.5", "MiniMax M2.5", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 205_000),
    OpenCodeGoModel("minimax-m2.7", "MiniMax M2.7", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 205_000),
)


def get_opencode_go_api_key_from_env() -> str | None:
    for name in ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY", "OPENCODE_ZEN_API_KEY"):
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _apply_opencode_go_config(
    config: Config,
    api_key: SecretStr,
    models: tuple[OpenCodeGoModel, ...] = OPENCODE_GO_MODELS,
) -> None:
    config.providers[OPENCODE_GO_OPENAI_PROVIDER_KEY] = LLMProvider(
        type="openai_legacy",
        base_url=OPENCODE_GO_BASE_URL,
        api_key=api_key,
    )
    config.providers[OPENCODE_GO_ANTHROPIC_PROVIDER_KEY] = LLMProvider(
        type="anthropic",
        base_url=OPENCODE_GO_ANTHROPIC_BASE_URL,
        api_key=api_key,
    )

    provider_keys = {OPENCODE_GO_OPENAI_PROVIDER_KEY, OPENCODE_GO_ANTHROPIC_PROVIDER_KEY}
    for key, model in list(config.models.items()):
        if model.provider in provider_keys:
            del config.models[key]

    for model in models:
        config.models[model.alias] = LLMModel(
            provider=model.provider_key,
            model=model.model_id,
            max_context_size=model.max_context_size,
            capabilities=_native_thinking_capabilities(model.model_id),
            display_name=model.display_name,
        )

    if OPENCODE_GO_DEFAULT_MODEL_ALIAS in config.models:
        config.default_model = OPENCODE_GO_DEFAULT_MODEL_ALIAS
    else:
        fallback = next((model.alias for model in models), next(iter(config.models), ""))
        config.default_model = fallback
    apply_login_thinking_defaults(config, thinking=False, effort="off")


def _model_by_id() -> dict[str, OpenCodeGoModel]:
    return {model.model_id: model for model in OPENCODE_GO_MODELS}


@dataclass(frozen=True, slots=True)
class _ModelsDevMeta:
    """The slice of models.dev metadata we consume for a single model id.

    ``is_anthropic`` is the authoritative API-shape signal: models.dev marks
    Anthropic-shaped models with ``provider.npm == "@ai-sdk/anthropic"`` and
    leaves the rest on the provider default (``@ai-sdk/openai-compatible``).
    ``None`` means models.dev had no entry to derive a shape from.
    """

    display_name: str | None
    max_context: int | None
    is_anthropic: bool | None


MODELS_DEV_ANTHROPIC_NPM = "@ai-sdk/anthropic"


def _native_thinking_capabilities(model_id: str) -> set[ModelCapability] | None:
    """Model families whose reasoning is built in, not a user effort dial.

    GLM, MiniMax, and Qwen reason natively (``always_thinking``): reasoning is
    on by default with no user-facing off switch, so the effort dial and its
    top-border label are hidden. Reasoning still flows over the Anthropic
    ``thinking`` block on the @ai-sdk/anthropic route.
    """
    normalized = model_id.lower().replace("_", "-")
    if normalized.startswith(("glm-", "minimax-", "qwen")):
        return {"always_thinking"}
    return None


def _heuristic_provider_key(model_id: str) -> str:
    """Last-ditch shape guess when models.dev and the catalog are both silent.

    OpenAI-compatible is the safe default (most Go models use it); only the
    stable ``minimax-`` family is reliably Anthropic-shaped by name.
    """
    if model_id.startswith("minimax-"):
        return OPENCODE_GO_ANTHROPIC_PROVIDER_KEY
    return OPENCODE_GO_OPENAI_PROVIDER_KEY


def _resolve_provider_key(
    model_id: str, meta: _ModelsDevMeta | None, catalog: OpenCodeGoModel | None
) -> str:
    """Pick the provider (API shape) for a model.

    models.dev is authoritative; the curated catalog is the offline fallback;
    the name heuristic is the last resort. This is the fix for Qwen models
    being rejected as ``not supported for format oa-compat`` — they are
    Anthropic-shaped, which only models.dev (or the corrected catalog) knows.
    """
    if meta is not None and meta.is_anthropic is not None:
        return (
            OPENCODE_GO_ANTHROPIC_PROVIDER_KEY
            if meta.is_anthropic
            else (OPENCODE_GO_OPENAI_PROVIDER_KEY)
        )
    if catalog is not None:
        return catalog.provider_key
    return _heuristic_provider_key(model_id)


def _derive_display_name(model_id: str) -> str:
    return model_id.replace("-", " ").title()


def _extract_model_ids(data: object) -> list[str]:
    """Pull the ordered list of model ids from the /models payload."""
    if not isinstance(data, dict):
        return []
    raw_items = cast(dict[str, Any], data).get("data")
    if not isinstance(raw_items, list):
        return []
    ids: list[str] = []
    for item in cast(list[Any], raw_items):
        if not isinstance(item, dict):
            continue
        model_id = cast(dict[str, Any], item).get("id")
        if isinstance(model_id, str) and model_id:
            ids.append(model_id)
    return ids


def _metadata_from_catalog(catalog: ModelsDevCatalog) -> dict[str, _ModelsDevMeta]:
    result: dict[str, _ModelsDevMeta] = {}
    for model_id, model in get_provider_models(catalog, MODELS_DEV_PROVIDER_ID).items():
        is_anthropic = model.npm == MODELS_DEV_ANTHROPIC_NPM if model.npm is not None else None
        result[model_id] = _ModelsDevMeta(
            display_name=model.display_name,
            max_context=model.context_length,
            is_anthropic=is_anthropic,
        )
    return result


def _parse_models_dev_metadata(data: object) -> dict[str, _ModelsDevMeta]:
    """Compatibility wrapper over the shared models.dev catalog parser."""
    raw_catalog = cast(dict[object, object], data) if isinstance(data, dict) else None
    if raw_catalog is not None and all(
        isinstance(provider, ModelsDevProvider) for provider in raw_catalog.values()
    ):
        catalog = cast(ModelsDevCatalog, raw_catalog)
    else:
        catalog = parse_models_dev_catalog(cast(object, data))
    return _metadata_from_catalog(catalog)


def _build_models(
    model_ids: list[str],
    metadata: dict[str, _ModelsDevMeta],
) -> tuple[OpenCodeGoModel, ...]:
    """Turn discovered ids into models.

    The /models list is authoritative for *which* models exist. For each id,
    properties resolve in order: models.dev → curated catalog → default. This
    lets the live path self-correct shape/context even if the catalog drifts.
    """
    known = _model_by_id()
    result: list[OpenCodeGoModel] = []
    for model_id in model_ids:
        catalog = known.get(model_id)
        meta = metadata.get(model_id)
        display_name = (
            (meta.display_name if meta else None)
            or (catalog.display_name if catalog else None)
            or _derive_display_name(model_id)
        )
        max_context = (
            (meta.max_context if meta else None)
            or (catalog.max_context_size if catalog else None)
            or OPENCODE_GO_DEFAULT_CONTEXT
        )
        result.append(
            OpenCodeGoModel(
                model_id,
                display_name,
                _resolve_provider_key(model_id, meta, catalog),
                max_context,
            )
        )
    return tuple(result)


async def _fetch_models_dev_metadata() -> dict[str, _ModelsDevMeta]:
    """Load best-effort OpenCode metadata from the shared models.dev catalog."""
    result = await get_models_dev_catalog()
    return _parse_models_dev_metadata(result.catalog)


async def _discover_opencode_go_models(api_key: str) -> tuple[OpenCodeGoModel, ...]:
    async with (
        new_client_session() as session,
        session.get(
            f"{OPENCODE_GO_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            raise_for_status=True,
        ) as response,
    ):
        payload = await response.json(content_type=None)

    model_ids = _extract_model_ids(payload)
    if not model_ids:
        return ()

    # models.dev is the authority for API shape + context. Its shared loader
    # refreshes stale data on demand and falls back to disk when unavailable.
    metadata = await _fetch_models_dev_metadata()
    return _build_models(model_ids, metadata)


def _opencode_go_api_key(config: Config) -> str | None:
    """The saved OpenCode Go key (both providers share it), or None if absent."""
    for provider_key in OPENCODE_GO_PROVIDER_KEYS:
        provider = config.providers.get(provider_key)
        if provider is None:
            continue
        value = provider.api_key.get_secret_value().strip()
        if value:
            return value
    return None


def apply_opencode_go_models(config: Config, models: tuple[OpenCodeGoModel, ...]) -> bool:
    """Upsert discovered OpenCode Go models and prune ones no longer offered,
    across both shape providers.

    Unlike ``_apply_opencode_go_config`` (the login path), this preserves the
    user's ``default_model`` and ``default_thinking`` — it only reassigns the
    default when the currently selected model was pruned. This mirrors
    ``platforms._apply_models`` for the two-provider OpenCode Go split, so the
    background refresh can keep the catalog current without resetting choices.

    Returns True if ``config.models`` (or the default) changed.
    """
    changed = False
    aliases: list[str] = []
    for model in models:
        alias = model.alias
        aliases.append(alias)
        existing = config.models.get(alias)
        if existing is None:
            config.models[alias] = LLMModel(
                provider=model.provider_key,
                model=model.model_id,
                max_context_size=model.max_context_size,
                capabilities=_native_thinking_capabilities(model.model_id),
                display_name=model.display_name,
            )
            changed = True
            continue
        if existing.provider != model.provider_key:
            existing.provider = model.provider_key
            changed = True
        if existing.model != model.model_id:
            existing.model = model.model_id
            changed = True
        if existing.max_context_size != model.max_context_size:
            existing.max_context_size = model.max_context_size
            changed = True
        capabilities = _native_thinking_capabilities(model.model_id)
        if existing.capabilities != capabilities:
            existing.capabilities = capabilities
            changed = True
        if existing.display_name != model.display_name:
            existing.display_name = model.display_name
            changed = True

    alias_set = set(aliases)
    removed_default = False
    for alias, model_cfg in list(config.models.items()):
        if model_cfg.provider not in OPENCODE_GO_PROVIDER_KEYS:
            continue
        if alias in alias_set:
            continue
        del config.models[alias]
        if config.default_model == alias:
            removed_default = True
        changed = True

    if removed_default:
        config.default_model = aliases[0] if aliases else next(iter(config.models), "")
        changed = True
    elif config.default_model and config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
        changed = True
    return changed


async def refresh_opencode_go_models(config: Config) -> tuple[OpenCodeGoModel, ...] | None:
    """Re-discover the live OpenCode Go model list for the startup managed-model
    refresh, reusing the saved API key.

    Returns the discovered models to apply, or None when OpenCode Go isn't
    configured or discovery yields nothing (so callers leave the saved list
    untouched). Network/HTTP errors propagate to the caller.
    """
    api_key = _opencode_go_api_key(config)
    if api_key is None:
        return None
    discovered = await _discover_opencode_go_models(api_key)
    return discovered or None


async def login_opencode_go_api_key(
    config: Config, api_key: str | None = None
) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Login requires the default config file; restart without --config/--config-file.",
        )
        return

    resolved_key = (api_key or get_opencode_go_api_key_from_env() or "").strip()
    if not resolved_key:
        yield OAuthEvent("error", "OpenCode Go API key is required.")
        return

    models = OPENCODE_GO_MODELS
    try:
        discovered = await _discover_opencode_go_models(resolved_key)
        if discovered:
            models = discovered
    except aiohttp.ClientResponseError as exc:
        if exc.status in {401, 403}:
            yield OAuthEvent("error", "Invalid OpenCode Go API key; the key was not saved.")
            return
        yield OAuthEvent(
            "info",
            "OpenCode Go model listing is unavailable; using the built-in model list.",
        )
    except (TimeoutError, aiohttp.ClientError, ValueError):
        yield OAuthEvent(
            "info",
            "OpenCode Go model listing is unavailable; using the built-in model list.",
        )

    _apply_opencode_go_config(config, SecretStr(resolved_key), models=models)
    save_config(config)
    yield OAuthEvent("success", f"OpenCode Go configured with model {config.default_model}.")


async def logout_opencode_go(config: Config) -> AsyncIterator[OAuthEvent]:
    if not config.is_from_default_location:
        yield OAuthEvent(
            "error",
            "Logout requires the default config file; restart without --config/--config-file.",
        )
        return

    provider_keys = {OPENCODE_GO_OPENAI_PROVIDER_KEY, OPENCODE_GO_ANTHROPIC_PROVIDER_KEY}
    for provider_key in provider_keys:
        config.providers.pop(provider_key, None)
    for key, model in list(config.models.items()):
        if model.provider in provider_keys:
            del config.models[key]

    if config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
    save_config(config)
    yield OAuthEvent("success", "Logged out of OpenCode Go successfully.")
