from __future__ import annotations

import os
from typing import Any, NamedTuple, cast

import aiohttp
from pydantic import BaseModel

from pythinker_code.auth import (
    DIGITALOCEAN_PLATFORM_ID,
    GITHUB_COPILOT_PLATFORM_ID,
    LM_STUDIO_PLATFORM_ID,
    OLLAMA_PLATFORM_ID,
    OPENAI_API_PLATFORM_ID,
    OPENAI_CHATGPT_PLATFORM_ID,
    PYTHINKER_CODE_PLATFORM_ID,
    XAI_PLATFORM_ID,
)
from pythinker_code.config import Config, LLMModel, load_config, save_config
from pythinker_code.llm import ModelCapability
from pythinker_code.utils.aiohttp import new_client_session
from pythinker_code.utils.logging import logger

LOCAL_API_KEY_PLACEHOLDER = "local"


def bearer_headers(api_key: str) -> dict[str, str]:
    if not api_key or api_key == LOCAL_API_KEY_PLACEHOLDER:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


class ModelInfo(BaseModel):
    """Model information returned from the API."""

    id: str
    context_length: int
    supports_reasoning: bool
    supports_image_in: bool
    supports_video_in: bool
    display_name: str | None = None

    @property
    def capabilities(self) -> set[ModelCapability]:
        """Derive capabilities from model info."""
        caps: set[ModelCapability] = set()
        if self.supports_reasoning:
            caps.add("thinking")
        # Models with "thinking" in name are always-thinking
        if "thinking" in self.id.lower():
            caps.update(("thinking", "always_thinking"))
        if self.supports_image_in:
            caps.add("image_in")
        if self.supports_video_in:
            caps.add("video_in")
        if self.id.lower().startswith("pythinker-ai"):
            caps.update(("thinking", "image_in", "video_in"))
        return caps


class Platform(NamedTuple):
    id: str
    name: str
    base_url: str
    search_url: str | None = None
    fetch_url: str | None = None
    allowed_prefixes: list[str] | None = None


def _pythinker_code_base_url() -> str:
    if base_url := os.getenv("PYTHINKER_CODE_BASE_URL"):
        return base_url
    return "https://api.pythinker.com/coding/v1"


def _lm_studio_base_url() -> str:
    return os.getenv("LM_STUDIO_BASE_URL") or "http://localhost:1234/v1"


def _ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434/v1"


PLATFORMS: list[Platform] = [
    Platform(
        id=PYTHINKER_CODE_PLATFORM_ID,
        name="Pythinker",
        base_url=_pythinker_code_base_url(),
        search_url=f"{_pythinker_code_base_url()}/search",
        fetch_url=f"{_pythinker_code_base_url()}/fetch",
    ),
    Platform(
        id=OPENAI_API_PLATFORM_ID,
        name="OpenAI API",
        base_url="https://api.openai.com/v1",
    ),
    Platform(
        id=OPENAI_CHATGPT_PLATFORM_ID,
        name="OpenAI ChatGPT Codex",
        base_url="https://chatgpt.com/backend-api/codex",
    ),
    Platform(
        id=GITHUB_COPILOT_PLATFORM_ID,
        name="GitHub Copilot",
        base_url="https://api.githubcopilot.com",
    ),
    Platform(
        id=DIGITALOCEAN_PLATFORM_ID,
        name="DigitalOcean",
        base_url="https://inference.do-ai.run/v1",
    ),
    Platform(
        id=XAI_PLATFORM_ID,
        name="xAI Grok",
        base_url="https://api.x.ai/v1",
    ),
    Platform(
        id="pythinker_ai-cn",
        name="Pythinker AI Open Platform (pythinker-ai.cn)",
        base_url="https://api.pythinker-ai.cn/v1",
        allowed_prefixes=["pythinker-k"],
    ),
    Platform(
        id="pythinker-ai",
        name="Pythinker AI Open Platform (pythinker-ai.ai)",
        base_url="https://api.pythinker-ai.ai/v1",
        allowed_prefixes=["pythinker-k"],
    ),
    Platform(
        id=LM_STUDIO_PLATFORM_ID,
        name="LM Studio",
        base_url=_lm_studio_base_url(),
    ),
    Platform(
        id=OLLAMA_PLATFORM_ID,
        name="Ollama",
        base_url=_ollama_base_url(),
    ),
]

_PLATFORM_BY_ID = {platform.id: platform for platform in PLATFORMS}
_PLATFORM_BY_NAME = {platform.name: platform for platform in PLATFORMS}


def get_platform_by_id(platform_id: str) -> Platform | None:
    return _PLATFORM_BY_ID.get(platform_id)


def get_platform_by_name(name: str) -> Platform | None:
    return _PLATFORM_BY_NAME.get(name)


MANAGED_PROVIDER_PREFIX = "managed:"


def managed_provider_key(platform_id: str) -> str:
    return f"{MANAGED_PROVIDER_PREFIX}{platform_id}"


def managed_model_key(platform_id: str, model_id: str) -> str:
    return f"{platform_id}/{model_id}"


def parse_managed_provider_key(provider_key: str) -> str | None:
    if not provider_key.startswith(MANAGED_PROVIDER_PREFIX):
        return None
    return provider_key.removeprefix(MANAGED_PROVIDER_PREFIX)


def is_managed_provider_key(provider_key: str) -> bool:
    return provider_key.startswith(MANAGED_PROVIDER_PREFIX)


def get_platform_name_for_provider(provider_key: str) -> str | None:
    platform_id = parse_managed_provider_key(provider_key)
    if not platform_id:
        return None
    platform = get_platform_by_id(platform_id)
    return platform.name if platform else None


def _select_retry_api_keys(
    *,
    attempted_api_key: str,
    resolved_api_key: str,
    fallback_api_key: str,
) -> list[str]:
    result: list[str] = []
    for candidate in (resolved_api_key, fallback_api_key):
        if not candidate or candidate == attempted_api_key or candidate in result:
            continue
        result.append(candidate)
    return result


def _openai_fallback_models(platform_id: str) -> list[ModelInfo] | None:
    # ChatGPT model availability is subscription/account-specific. Do not
    # replace the user's live catalog with a static fallback; stale fallback
    # slugs surface as 400 "model is not supported with a ChatGPT account".
    if platform_id == OPENAI_CHATGPT_PLATFORM_ID:
        return None
    if platform_id == OPENAI_API_PLATFORM_ID:
        from pythinker_code.auth.openai import OPENAI_API_FALLBACK_MODELS

        return list(OPENAI_API_FALLBACK_MODELS)
    return None


def _fallback_or_log(
    *,
    platform_id: str,
    error: Exception,
) -> list[ModelInfo] | None:
    models = _openai_fallback_models(platform_id)
    if models is None:
        logger.error(
            "Failed to refresh models for {platform}: {error}",
            platform=platform_id,
            error=error,
        )
        return None
    logger.warning(
        "Using fallback models for {platform} after model refresh failed: {error}",
        platform=platform_id,
        error=error,
    )
    return models


async def _list_models_for_managed_platform(
    *,
    platform_id: str,
    platform: Platform,
    api_key: str,
    account_id: str | None = None,
) -> list[ModelInfo]:
    if platform_id == OPENAI_CHATGPT_PLATFORM_ID:
        from pythinker_code.auth.openai import discover_chatgpt_models

        return await discover_chatgpt_models(
            api_key,
            account_id=account_id,
            base_url=platform.base_url,
        )
    return await list_models(platform, api_key)


async def refresh_managed_models(config: Config) -> bool:
    if not config.is_from_default_location:
        return False

    from pythinker_code.auth.kimi import (
        KIMI_PROVIDER_KEY,
        KimiModel,
        apply_kimi_models,
        refresh_kimi_models,
    )
    from pythinker_code.auth.minimax import (
        MINIMAX_ANTHROPIC_PROVIDER_KEY,
        MiniMaxModel,
        apply_minimax_models,
        refresh_minimax_models,
    )
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_PROVIDER_KEYS,
        OpenCodeGoModel,
        apply_opencode_go_models,
        refresh_opencode_go_models,
    )
    from pythinker_code.auth.z_ai import (
        ZAI_ROUTES,
        ZaiCatalogResult,
        ZaiRoute,
        apply_z_ai_models,
        refresh_z_ai_models,
    )

    managed_providers = {
        key: provider for key, provider in config.providers.items() if is_managed_provider_key(key)
    }
    if not managed_providers:
        return False

    changed = False
    updates: list[tuple[str, str, list[ModelInfo]]] = []
    z_ai_provider_keys = {route.provider_key for route in ZAI_ROUTES}
    oauth_manager = None
    for provider_key, provider in managed_providers.items():
        # OpenCode Go and MiniMax own provider-specific model discovery. The
        # generic `managed:<platform>` path can't express OpenCode Go's
        # two-provider split, and MiniMax's provider key intentionally includes
        # the wire-shape suffix (`managed:minimax-anthropic`).
        if (
            provider_key in OPENCODE_GO_PROVIDER_KEYS
            or provider_key
            in {
                managed_provider_key(DIGITALOCEAN_PLATFORM_ID),
                managed_provider_key(GITHUB_COPILOT_PLATFORM_ID),
                managed_provider_key(XAI_PLATFORM_ID),
                MINIMAX_ANTHROPIC_PROVIDER_KEY,
                KIMI_PROVIDER_KEY,
            }
            or provider_key in z_ai_provider_keys
        ):
            continue
        platform_id = parse_managed_provider_key(provider_key)
        if not platform_id:
            continue
        # Local providers (LM Studio, Ollama) own their own discovery via
        # native endpoints (`/api/v0/models`, `/api/tags` + `/api/show`) which
        # provide context windows and filter embedding models. The OpenAI-compat
        # `/v1/models` path used here returns sparse data and would overwrite
        # the saved config with `max_context_size=0` and embedding entries.
        # Skip them here; users re-run `pythinker login --<provider>` to refresh.
        if platform_id in ("lm-studio", "ollama"):
            continue
        platform = get_platform_by_id(platform_id)
        if platform is None:
            logger.warning("Managed platform not found: {platform}", platform=platform_id)
            continue

        fallback_api_key = provider.api_key.get_secret_value()
        api_key = fallback_api_key
        if provider.oauth:
            if oauth_manager is None:
                from pythinker_code.auth.oauth import OAuthManager

                oauth_manager = OAuthManager(config)
            try:
                await oauth_manager.ensure_fresh()
            except Exception as exc:
                from pythinker_code.telemetry.errors import report_handled_error

                report_handled_error(exc, site="auth.platforms.refresh.pre_sync")
                logger.warning(
                    "Failed to refresh OAuth token before model sync for {platform}: {error}",
                    platform=platform_id,
                    error=exc,
                )
            api_key = oauth_manager.resolve_api_key(provider.api_key, provider.oauth)
        if not api_key:
            logger.warning(
                "Missing API key for managed provider: {provider}",
                provider=provider_key,
            )
            continue
        effective_platform = platform._replace(base_url=provider.base_url)
        account_id = (
            oauth_manager.get_chatgpt_account_id(provider.oauth)
            if platform_id == OPENAI_CHATGPT_PLATFORM_ID
            and provider.oauth is not None
            and oauth_manager is not None
            else None
        )
        try:
            models = await _list_models_for_managed_platform(
                platform_id=platform_id,
                platform=effective_platform,
                api_key=api_key,
                account_id=account_id,
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status != 401 or provider.oauth is None or oauth_manager is None:
                fallback_models = _fallback_or_log(platform_id=platform_id, error=exc)
                if fallback_models is None:
                    continue
                models = fallback_models
                updates.append((provider_key, platform_id, models))
                if _apply_models(config, provider_key, platform_id, models):
                    changed = True
                continue
            logger.warning(
                "Received 401 while refreshing models for {platform}; attempting token refresh",
                platform=platform_id,
            )
            refresh_exc: Exception | None = None
            try:
                await oauth_manager.ensure_fresh(force=True)
            except Exception as exc2:
                from pythinker_code.telemetry.errors import report_handled_error

                report_handled_error(exc2, site="auth.platforms.refresh.after_401")
                refresh_exc = exc2
                logger.warning(
                    "Failed to refresh OAuth token after 401 for {platform}: {error}",
                    platform=platform_id,
                    error=exc2,
                )

            retry_api_keys = _select_retry_api_keys(
                attempted_api_key=api_key,
                resolved_api_key=oauth_manager.resolve_api_key(provider.api_key, provider.oauth),
                fallback_api_key=fallback_api_key,
            )
            if platform_id == OPENAI_CHATGPT_PLATFORM_ID:
                account_id = oauth_manager.get_chatgpt_account_id(provider.oauth)
            if not retry_api_keys:
                fallback_models = _fallback_or_log(
                    platform_id=platform_id,
                    error=refresh_exc or exc,
                )
                if fallback_models is not None:
                    models = fallback_models
                    updates.append((provider_key, platform_id, models))
                    if _apply_models(config, provider_key, platform_id, models):
                        changed = True
                continue
            retry_exc: Exception | None = None
            for retry_api_key in retry_api_keys:
                try:
                    models = await _list_models_for_managed_platform(
                        platform_id=platform_id,
                        platform=effective_platform,
                        api_key=retry_api_key,
                        account_id=account_id,
                    )
                    break
                except Exception as exc3:
                    retry_exc = exc3
            else:
                fallback_models = _fallback_or_log(
                    platform_id=platform_id,
                    error=retry_exc or refresh_exc or exc,
                )
                if fallback_models is not None:
                    models = fallback_models
                    updates.append((provider_key, platform_id, models))
                    if _apply_models(config, provider_key, platform_id, models):
                        changed = True
                continue
        except Exception as exc:
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(exc, site="auth.platforms.sync")
            fallback_models = _fallback_or_log(platform_id=platform_id, error=exc)
            if fallback_models is None:
                continue
            models = fallback_models

        updates.append((provider_key, platform_id, models))
        if _apply_models(config, provider_key, platform_id, models):
            changed = True

    opencode_go_models: tuple[OpenCodeGoModel, ...] | None = None
    try:
        opencode_go_models = await refresh_opencode_go_models(config)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning("Failed to refresh OpenCode Go models: {error}", error=exc)
    if opencode_go_models and apply_opencode_go_models(config, opencode_go_models):
        changed = True

    minimax_models: tuple[MiniMaxModel, ...] | None = None
    try:
        minimax_models = await refresh_minimax_models(config)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning("Failed to refresh MiniMax models: {error}", error=exc)
    if minimax_models is not None and apply_minimax_models(config, minimax_models):
        changed = True

    z_ai_results: dict[ZaiRoute, ZaiCatalogResult] = {}
    for route in ZAI_ROUTES:
        try:
            result = await refresh_z_ai_models(config, route)
        except Exception as exc:
            logger.warning(
                "Unexpected Z.AI catalog refresh failure for {route}: {error_type}",
                route=route.platform_id,
                error_type=type(exc).__name__,
            )
            continue
        z_ai_results[route] = result
        if result.status == "live":
            assert result.models is not None
            if apply_z_ai_models(config, route, result.models):
                changed = True
        elif result.status != "unconfigured":
            logger.warning(
                "Z.AI catalog refresh did not update {route}: status={status}, failure={failure}",
                route=route.platform_id,
                status=result.status,
                failure=result.failure,
            )

    kimi_models: tuple[KimiModel, ...] | None = None
    try:
        kimi_models = await refresh_kimi_models(config)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning("Failed to refresh Kimi models: {error}", error=exc)
    if kimi_models is not None and apply_kimi_models(config, kimi_models):
        changed = True

    if changed:
        config_for_save = load_config()
        save_changed = False
        for provider_key, platform_id, models in updates:
            if _apply_models(config_for_save, provider_key, platform_id, models):
                save_changed = True
        if opencode_go_models and apply_opencode_go_models(config_for_save, opencode_go_models):
            save_changed = True
        if minimax_models is not None and apply_minimax_models(config_for_save, minimax_models):
            save_changed = True
        for route, result in z_ai_results.items():
            if result.status != "live":
                continue
            assert result.models is not None
            if apply_z_ai_models(config_for_save, route, result.models):
                save_changed = True
        if kimi_models is not None and apply_kimi_models(config_for_save, kimi_models):
            save_changed = True
        if save_changed:
            save_config(config_for_save)
    return changed


async def list_models(platform: Platform, api_key: str) -> list[ModelInfo]:
    async with new_client_session() as session:
        models = await _list_models(
            session,
            base_url=platform.base_url,
            api_key=api_key,
        )
    if platform.allowed_prefixes is None:
        return models
    prefixes = tuple(platform.allowed_prefixes)
    return [model for model in models if model.id.startswith(prefixes)]


async def _list_models(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    api_key: str,
) -> list[ModelInfo]:
    models_url = f"{base_url.rstrip('/')}/models"
    try:
        async with session.get(
            models_url,
            headers=bearer_headers(api_key),
            raise_for_status=True,
        ) as response:
            resp_json = await response.json()
    except aiohttp.ClientError:
        raise

    data = resp_json.get("data")
    if not isinstance(data, list):
        raise ValueError(f"Unexpected models response for {base_url}")

    result: list[ModelInfo] = []
    for item in cast(list[dict[str, Any]], data):
        model_id = item.get("id")
        if not model_id:
            continue
        raw_display_name = item.get("display_name")
        display_name = str(raw_display_name) if raw_display_name else None
        result.append(
            ModelInfo(
                id=str(model_id),
                context_length=int(item.get("context_length") or 0),
                supports_reasoning=bool(item.get("supports_reasoning")),
                supports_image_in=bool(item.get("supports_image_in")),
                supports_video_in=bool(item.get("supports_video_in")),
                display_name=display_name,
            )
        )
    return result


def _apply_models(
    config: Config,
    provider_key: str,
    platform_id: str,
    models: list[ModelInfo],
) -> bool:
    changed = False
    model_keys: list[str] = []

    for model in models:
        model_key = managed_model_key(platform_id, model.id)
        model_keys.append(model_key)

        existing = config.models.get(model_key)
        capabilities = model.capabilities or None  # empty set -> None

        if existing is None:
            config.models[model_key] = LLMModel(
                provider=provider_key,
                model=model.id,
                max_context_size=model.context_length,
                capabilities=capabilities,
                display_name=model.display_name,
            )
            changed = True
            continue

        if existing.provider != provider_key:
            existing.provider = provider_key
            changed = True
        if existing.model != model.id:
            existing.model = model.id
            changed = True
        if existing.max_context_size != model.context_length:
            existing.max_context_size = model.context_length
            changed = True
        if existing.capabilities != capabilities:
            existing.capabilities = capabilities
            changed = True
        if existing.display_name != model.display_name:
            existing.display_name = model.display_name
            changed = True

    removed_default = False
    model_keys_set = set(model_keys)
    for key, model in list(config.models.items()):
        if model.provider != provider_key:
            continue
        if key in model_keys_set:
            continue
        del config.models[key]
        if config.default_model == key:
            removed_default = True
        changed = True

    if removed_default:
        if model_keys:
            config.default_model = model_keys[0]
        else:
            config.default_model = next(iter(config.models), "")
        changed = True

    if config.default_model and config.default_model not in config.models:
        config.default_model = next(iter(config.models), "")
        changed = True

    return changed
