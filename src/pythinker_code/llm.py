from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

from pythinker_core.chat_provider import ChatProvider, ThinkingEffort

from pythinker_code.constant import USER_AGENT
from pythinker_code.thinking import (
    DEFAULT_THINKING_EFFORT,
    bool_to_thinking_effort,
    normalize_thinking_effort,
    thinking_effort_enabled,
)
from pythinker_code.utils.logging import logger

if TYPE_CHECKING:
    from pythinker_code.auth.oauth import OAuthManager
    from pythinker_code.config import Config, LLMModel, LLMProvider

type ProviderType = Literal[
    "pythinker",
    "openai_legacy",
    "openai_responses",
    "openai_codex",
    "anthropic",
    "google_genai",  # for backward-compatibility, equals to `gemini`
    "gemini",
    "vertexai",
    "_echo",
    "_scripted_echo",
    "_chaos",
]

type ModelCapability = Literal["image_in", "video_in", "thinking", "always_thinking"]
ALL_MODEL_CAPABILITIES: set[ModelCapability] = set(get_args(ModelCapability.__value__))


@dataclass(slots=True)
class LLM:
    chat_provider: ChatProvider
    max_context_size: int
    capabilities: set[ModelCapability]
    model_config: LLMModel | None = None
    provider_config: LLMProvider | None = None
    thinking: bool | None = None
    thinking_effort: ThinkingEffort | None = None

    @property
    def model_name(self) -> str:
        return self.chat_provider.model_name


def model_display_name(model_name: str | None, model: LLMModel | None = None) -> str:
    if model is not None and model.display_name:
        return model.display_name
    if not model_name:
        return ""
    if model_name in ("pythinker-for-coding", "pythinker-code"):
        return "pythinker-for-coding"
    return model_name


def augment_provider_with_env_vars(
    provider: LLMProvider,
    model: LLMModel,
    *,
    provider_key: str | None = None,
) -> dict[str, str]:
    """Override provider/model settings from environment variables.

    Returns:
        Mapping of environment variables that were applied.
    """
    applied: dict[str, str] = {}

    if provider_key == "managed:lm-studio":
        if base_url := os.getenv("LM_STUDIO_BASE_URL"):
            provider.base_url = base_url
            applied["LM_STUDIO_BASE_URL"] = base_url
        return applied

    if provider_key == "managed:ollama":
        if base_url := os.getenv("OLLAMA_BASE_URL"):
            provider.base_url = base_url
            applied["OLLAMA_BASE_URL"] = base_url
        return applied

    match provider.type:
        case "pythinker":
            if base_url := os.getenv("PYTHINKER_BASE_URL"):
                provider.base_url = base_url
                applied["PYTHINKER_BASE_URL"] = base_url
            if model_name := os.getenv("PYTHINKER_MODEL_NAME"):
                model.model = model_name
                applied["PYTHINKER_MODEL_NAME"] = model_name
            if max_context_size := os.getenv("PYTHINKER_MODEL_MAX_CONTEXT_SIZE"):
                model.max_context_size = int(max_context_size)
                applied["PYTHINKER_MODEL_MAX_CONTEXT_SIZE"] = max_context_size
            if capabilities := os.getenv("PYTHINKER_MODEL_CAPABILITIES"):
                caps_lower = (cap.strip().lower() for cap in capabilities.split(",") if cap.strip())
                model.capabilities = set(
                    cast(ModelCapability, cap)
                    for cap in caps_lower
                    if cap in get_args(ModelCapability.__value__)
                )
                applied["PYTHINKER_MODEL_CAPABILITIES"] = capabilities
        case "openai_legacy" | "openai_responses" | "openai_codex":
            # OPENAI_* non-secret overrides are for OpenAI-compatible user
            # configs and OpenAI managed providers only. Runtime auth must come
            # from saved config/OAuth created by `pythinker login` or `/login`,
            # never from ambient shell API-key environment variables.
            allow_openai_env = provider_key is None or provider_key in {
                "openai",
                "managed:openai",
                "managed:openai-chatgpt",
            }
            if allow_openai_env and (base_url := os.getenv("OPENAI_BASE_URL")):
                provider.base_url = base_url
        case _:
            pass

    return applied


def _pythinker_default_headers(provider: LLMProvider, oauth: OAuthManager | None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if oauth:
        headers.update(oauth.common_headers())
    if provider.custom_headers:
        headers.update(provider.custom_headers)
    return headers


def _build_recording_http_client(provider_key: str) -> object:
    """Build an httpx.AsyncClient that pipes response headers from each chat
    completion into the rate-limit cache.

    Returned as `object` so this module doesn't have to import httpx eagerly
    (chat-completion code paths are the only callers, and they're already
    importing httpx via openai/anthropic).
    """
    import httpx

    from pythinker_code.usage_ratelimit_cache import get_cache

    cache = get_cache()

    async def _on_response(response: httpx.Response) -> None:
        # Telemetry must never fail a chat request. The header dict is cheap
        # to copy and the cache `record` call is sync + non-blocking.
        with contextlib.suppress(Exception):
            cache.record(provider_key, dict(response.headers))

    return httpx.AsyncClient(event_hooks={"response": [_on_response]})


def create_llm(
    provider: LLMProvider,
    model: LLMModel,
    *,
    thinking: bool | None = None,
    thinking_effort: ThinkingEffort | None = None,
    session_id: str | None = None,
    oauth: OAuthManager | None = None,
) -> LLM | None:
    if provider.type not in {"_echo", "_scripted_echo"} and (
        not provider.base_url or not model.model
    ):
        logger.warning(
            "Cannot create LLM: missing base_url or model (provider_type={provider_type})",
            provider_type=provider.type,
        )
        return None

    resolved_api_key = (
        oauth.resolve_api_key(provider.api_key, provider.oauth)
        if oauth and provider.oauth
        else provider.api_key.get_secret_value()
    )

    # Capture rate-limit headers from every chat-completion HTTP response so
    # /usage can render a "live rate limits" fallback panel for providers
    # without a dedicated usage adapter (or whose adapter returned no data).
    rl_http_client = (
        _build_recording_http_client(model.provider)
        if provider.type
        not in {"_echo", "_scripted_echo", "_chaos", "google_genai", "gemini", "vertexai"}
        else None
    )

    match provider.type:
        case "pythinker":
            from pythinker_core.chat_provider.pythinker import Pythinker

            chat_provider = Pythinker(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_headers=_pythinker_default_headers(provider, oauth),
                http_client=rl_http_client,
            )

            gen_kwargs: Pythinker.GenerationKwargs = {}
            if session_id:
                gen_kwargs["prompt_cache_key"] = session_id
            if temperature := os.getenv("PYTHINKER_MODEL_TEMPERATURE"):
                gen_kwargs["temperature"] = float(temperature)
            if top_p := os.getenv("PYTHINKER_MODEL_TOP_P"):
                gen_kwargs["top_p"] = float(top_p)
            if max_tokens := os.getenv("PYTHINKER_MODEL_MAX_TOKENS"):
                gen_kwargs["max_tokens"] = int(max_tokens)

            if gen_kwargs:
                chat_provider = chat_provider.with_generation_kwargs(**gen_kwargs)
        case "openai_legacy":
            from pythinker_core.contrib.chat_provider.openai_legacy import OpenAILegacy

            reasoning_key = (
                provider.reasoning_key
                if provider.reasoning_key is not None
                else "reasoning_content"
            )
            stream = not (
                _is_alibaba_workspace_endpoint(provider.base_url)
                and model.model.lower().replace("_", "-") == "deepseek-v3.2"
            )
            chat_provider = OpenAILegacy(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                stream=stream,
                reasoning_key=reasoning_key,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
                http_client=rl_http_client,
            )
        case "openai_responses":
            from pythinker_core.contrib.chat_provider.openai_responses import OpenAIResponses

            chat_provider = OpenAIResponses(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
                http_client=rl_http_client,
            )
        case "openai_codex":
            from pythinker_core.contrib.chat_provider.openai_responses import OpenAIResponses

            from pythinker_code.auth.openai import build_chatgpt_codex_headers

            default_headers = build_chatgpt_codex_headers(
                account_id=oauth.get_chatgpt_account_id(provider.oauth) if oauth else None
            )
            if provider.custom_headers:
                default_headers.update(provider.custom_headers)
            chat_provider = OpenAIResponses(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                system_prompt_as_instructions=True,
                default_headers=default_headers,
                http_client=rl_http_client,
            )
        case "anthropic":
            from pythinker_core.contrib.chat_provider.anthropic import Anthropic

            chat_provider = Anthropic(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_max_tokens=50000,
                metadata={"user_id": session_id} if session_id else None,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
                http_client=rl_http_client,
            )
        case "google_genai" | "gemini":
            from pythinker_core.contrib.chat_provider.google_genai import GoogleGenAI

            chat_provider = GoogleGenAI(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "vertexai":
            from pythinker_core.contrib.chat_provider.google_genai import GoogleGenAI

            os.environ.update(provider.env or {})
            chat_provider = GoogleGenAI(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                vertexai=True,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "_echo":
            from pythinker_core.chat_provider.echo import EchoChatProvider

            chat_provider = EchoChatProvider()
        case "_scripted_echo":
            from pythinker_core.chat_provider.echo import ScriptedEchoChatProvider

            if provider.env:
                os.environ.update(provider.env)
            scripts = _load_scripted_echo_scripts()
            trace_value = os.getenv("PYTHINKER_SCRIPTED_ECHO_TRACE", "")
            trace = trace_value.strip().lower() in {"1", "true", "yes", "on"}
            chat_provider = ScriptedEchoChatProvider(scripts, trace=trace)
        case "_chaos":
            from pythinker_core.chat_provider.chaos import ChaosChatProvider, ChaosConfig
            from pythinker_core.chat_provider.pythinker import Pythinker

            chat_provider = ChaosChatProvider(
                provider=Pythinker(
                    model=model.model,
                    base_url=provider.base_url,
                    api_key=resolved_api_key,
                    default_headers=_pythinker_default_headers(provider, oauth),
                ),
                chaos_config=ChaosConfig(
                    error_probability=0.8,
                    error_types=[429, 500, 503],
                ),
            )

    capabilities = derive_model_capabilities(model)

    requested_effort = (
        normalize_thinking_effort(thinking_effort)
        if thinking_effort is not None
        else bool_to_thinking_effort(thinking)
    )
    if thinking_effort is not None and requested_effort is None:
        raise ValueError(f"Invalid thinking effort: {thinking_effort!r}")

    supports_thinking = "thinking" in capabilities
    if "always_thinking" in capabilities:
        # Always-thinking models cannot be disabled. Preserve an explicit
        # non-off effort; otherwise keep the legacy high-effort default.
        effective_effort = (
            requested_effort
            if requested_effort is not None and requested_effort != "off"
            else DEFAULT_THINKING_EFFORT
        )
    elif supports_thinking:
        effective_effort = requested_effort
    else:
        # Clamp to the model's supported levels: non-reasoning models have
        # only the off level, so explicit non-off requests become off instead of
        # being recorded as active but ignored by the provider.
        effective_effort = "off" if requested_effort is not None else None

    thinking_on = thinking_effort_enabled(effective_effort)
    # DashScope's routing layer rejects reasoning_effort entirely; use
    # model-specific body fields instead.
    is_dashscope_legacy = provider.type == "openai_legacy" and _is_dashscope_endpoint(
        provider.base_url or ""
    )
    # Moonshot K2.x models use the provider-specific thinking.type field on Moonshot-style
    # endpoints, but Alibaba's DashScope-compatible routes use enable_thinking.
    is_kimi_openai_legacy = (
        provider.type == "openai_legacy"
        and _is_kimi_k2_model(model.model)
        and not is_dashscope_legacy
    )
    is_glm_openai_legacy = provider.type == "openai_legacy" and _is_glm_model(model.model)
    if (
        effective_effort is not None
        and supports_thinking
        and not is_kimi_openai_legacy
        and not is_glm_openai_legacy
        and not is_dashscope_legacy
    ):
        # Only explicitly send thinking controls for models that advertise
        # reasoning. Some OpenAI-compatible non-reasoning models reject even a
        # null reasoning_effort field.
        chat_provider = chat_provider.with_thinking(effective_effort)

    # Moonshot K2.x and GLM use thinking.type on Moonshot-style endpoints.
    if (is_kimi_openai_legacy or is_glm_openai_legacy) and effective_effort is not None:
        thinking_body: dict[str, object] = {"type": "enabled" if thinking_on else "disabled"}
        if is_glm_openai_legacy and thinking_on:
            # Z.ai documents Preserved Thinking for coding/agent scenarios as
            # `clear_thinking: false`; OpenAILegacy already replays ThinkPart as
            # `reasoning_content`, which is the required history field.
            thinking_body["clear_thinking"] = False
        chat_provider = cast(Any, chat_provider).with_generation_kwargs(
            extra_body={"thinking": thinking_body}
        )

    # DashScope-compatible models use enable_thinking unless handled by a
    # provider-specific format above.
    if (
        is_dashscope_legacy
        and not is_kimi_openai_legacy
        and not is_glm_openai_legacy
        and effective_effort is not None
    ):
        chat_provider = cast(Any, chat_provider).with_generation_kwargs(
            extra_body={"enable_thinking": thinking_on}
        )

    # Apply Pythinker AI-specific ``thinking.keep`` (preserved thinking) only when
    # the model is actually in thinking mode; otherwise the API would see a
    # ``thinking.keep`` without an accompanying ``thinking.type`` it honors.
    if thinking_on and provider.type == "pythinker":
        from pythinker_core.chat_provider.pythinker import Pythinker

        if isinstance(chat_provider, Pythinker) and (
            thinking_keep := os.getenv("PYTHINKER_MODEL_THINKING_KEEP")
        ):
            chat_provider = chat_provider.with_extra_body({"thinking": {"keep": thinking_keep}})

    return LLM(
        chat_provider=chat_provider,
        max_context_size=model.max_context_size,
        capabilities=capabilities,
        model_config=model,
        provider_config=provider,
        thinking=thinking_effort_enabled(effective_effort)
        if effective_effort is not None
        else thinking,
        thinking_effort=effective_effort,
    )


def clone_llm_with_model_alias(
    llm: LLM | None,
    config: Config,
    model_alias: str | None,
    *,
    session_id: str,
    oauth: OAuthManager | None,
    thinking: bool | None = None,
    thinking_effort: ThinkingEffort | None = None,
) -> LLM | None:
    if model_alias is None:
        return llm
    if model_alias not in config.models:
        raise KeyError(f"Unknown model alias: {model_alias}")
    model = config.models[model_alias]
    provider = config.providers[model.provider]
    if thinking_effort is None and thinking is None and llm is not None:
        thinking_effort = llm.thinking_effort
    if thinking_effort is None and thinking is None and llm is not None:
        effort = getattr(llm.chat_provider, "thinking_effort", None)
        if effort is not None:
            thinking_effort = effort
    if thinking_effort is None and thinking is None and llm is not None:
        thinking = llm.thinking
    return create_llm(
        provider,
        model,
        thinking=thinking,
        thinking_effort=thinking_effort,
        session_id=session_id,
        oauth=oauth,
    )


def derive_model_capabilities(model: LLMModel) -> set[ModelCapability]:
    capabilities = set(model.capabilities or ())
    model_name = model.model.lower()
    # Moonshot K2.5/K2.6 support thinking, but it can be disabled via
    # `thinking.type`. Keep them out of always_thinking so --no-thinking and the
    # default_thinking=false config path can send the provider-specific disable
    # switch in create_llm().
    if _is_kimi_k2_model(model.model):
        capabilities.add("thinking")
        # Moonshot's thinking-only K2 variant (its model name contains
        # "thinking"); unlike the hybrid K2.5/K2.6 it cannot be switched off.
        if "thinking" in model_name:
            capabilities.add("always_thinking")
    # Models with "thinking" in their name are always-thinking models
    elif "thinking" in model_name or "reason" in model_name:
        capabilities.update(("thinking", "always_thinking"))
    # These models support thinking but can be toggled on/off
    elif model.model in {"pythinker-for-coding", "pythinker-code"}:
        capabilities.update(("thinking", "image_in", "video_in"))
    return capabilities


def _is_kimi_k2_model(model_name: str) -> bool:
    return "kimi-k2" in model_name.lower().replace("_", "-")


def _is_glm_model(model_name: str) -> bool:
    return model_name.lower().replace("_", "-").startswith("glm-")


def _is_dashscope_endpoint(base_url: str) -> bool:
    """True for any Alibaba DashScope endpoint (standard, intl, workspace)."""
    from urllib.parse import urlparse

    host = urlparse(base_url).hostname or ""
    return host == "aliyuncs.com" or host.endswith(".aliyuncs.com")


def _is_alibaba_workspace_endpoint(base_url: str) -> bool:
    from urllib.parse import urlparse

    host = urlparse(base_url).hostname or ""
    return host.startswith("ws-") and host.endswith(".maas.aliyuncs.com")


def _load_scripted_echo_scripts() -> list[str]:
    script_path = os.getenv("PYTHINKER_SCRIPTED_ECHO_SCRIPTS")
    if not script_path:
        raise ValueError("PYTHINKER_SCRIPTED_ECHO_SCRIPTS is required for _scripted_echo.")
    path = Path(script_path).expanduser()
    if not path.exists():
        raise ValueError(f"Scripted echo file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError:
        scripts = [chunk.strip() for chunk in text.split("\n---\n") if chunk.strip()]
        if scripts:
            return scripts
        raise ValueError(
            "Scripted echo file must be a JSON array of strings or a text file "
            "split by '\\n---\\n'."
        ) from None
    if isinstance(data, list):
        data_list = cast(list[object], data)
        if all(isinstance(item, str) for item in data_list):
            return cast(list[str], data_list)
    raise ValueError("Scripted echo JSON must be an array of strings.")
