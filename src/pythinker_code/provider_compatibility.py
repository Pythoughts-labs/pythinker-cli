from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from pythinker_core.chat_provider import ThinkingEffort
from pythinker_core.contrib.chat_provider.common import (
    ReasoningReplayMode,
    ToolMessageConversion,
)

from pythinker_code.thinking import (
    DEFAULT_THINKING_EFFORT,
    available_thinking_levels,
    clamp_thinking_effort,
)

if TYPE_CHECKING:
    from pythinker_code.config import LLMModel, LLMProvider


type ApiFamily = Literal["pythinker", "openai", "anthropic", "gemini", "vertex", "test"]
type ThinkingFormat = Literal[
    "native",
    "zai_tiered",
    "zai_binary",
    "kimi",
    "dashscope",
    "qwen_template",
    "none",
]
type ZaiThinkingMode = Literal["tiered", "binary"]


@dataclass(frozen=True, slots=True)
class ZaiModelPolicy:
    model_id: str
    context_tokens: int
    max_output_tokens: int
    thinking_mode: ZaiThinkingMode
    tool_stream: bool


@dataclass(slots=True)
class GenerationOverrides:
    native_effort: ThinkingEffort | None
    generation_kwargs: dict[str, object]
    extra_body: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProviderCompatibility:
    profile_id: str
    api_family: ApiFamily
    output_tokens_kwarg: str
    max_output_tokens: int | None
    tool_message_conversion: ToolMessageConversion | None
    deferred_tool_search: bool
    reasoning_key: str | None
    reasoning_replay_mode: ReasoningReplayMode
    auto_reasoning_effort: bool
    thinking_format: ThinkingFormat
    supported_thinking_levels: tuple[ThinkingEffort, ...] | None
    tool_stream: bool

    def effective_effort(
        self,
        requested: ThinkingEffort | None,
        capabilities: Collection[str] | None,
    ) -> ThinkingEffort | None:
        if capabilities and "always_thinking" in capabilities:
            if requested is not None and requested != "off":
                return requested
            return DEFAULT_THINKING_EFFORT
        if not capabilities or "thinking" not in capabilities:
            return "off" if requested is not None else None
        if requested is None:
            return None
        if self.thinking_format == "zai_binary":
            return "off" if requested in {"off", "minimal"} else "high"
        if self.thinking_format == "zai_tiered" and requested == "max":
            return "max"
        levels = self.supported_thinking_levels or available_thinking_levels(capabilities)
        return clamp_thinking_effort(requested, levels)

    def request_overrides(
        self,
        *,
        model_id: str,
        effort: ThinkingEffort | None,
    ) -> GenerationOverrides:
        del model_id
        generation_kwargs: dict[str, object] = {}
        extra_body: dict[str, object] = {}
        native_effort: ThinkingEffort | None = None

        if self.max_output_tokens is not None:
            generation_kwargs[self.output_tokens_kwarg] = self.max_output_tokens

        if self.thinking_format == "native":
            native_effort = effort
        elif self.thinking_format == "zai_tiered":
            if effort in {"off", "minimal"}:
                extra_body["thinking"] = {"type": "disabled"}
            else:
                extra_body["thinking"] = {
                    "type": "enabled",
                    "clear_thinking": False,
                }
                extra_body["reasoning_effort"] = "max" if effort in {"xhigh", "max"} else "high"
        elif self.thinking_format == "zai_binary":
            if effort in {"off", "minimal"}:
                extra_body["thinking"] = {"type": "disabled"}
            else:
                extra_body["thinking"] = {
                    "type": "enabled",
                    "clear_thinking": False,
                }
        elif self.thinking_format == "kimi":
            extra_body["thinking"] = {"type": "enabled" if _effort_enabled(effort) else "disabled"}
        elif self.thinking_format == "dashscope":
            extra_body["enable_thinking"] = _effort_enabled(effort)
        elif self.thinking_format == "qwen_template":
            extra_body["chat_template_kwargs"] = {"enable_thinking": _effort_enabled(effort)}

        return GenerationOverrides(
            native_effort=native_effort,
            generation_kwargs=generation_kwargs,
            extra_body=extra_body,
        )


_ZAI_CODING_ENDPOINT = ("api.z.ai", "/api/coding/paas/v4")
_ZAI_API_ENDPOINT = ("api.z.ai", "/api/paas/v4")
_ZAI_PROVIDER_KEYS = frozenset({"managed:z-ai-coding", "managed:z-ai-api"})
_ZAI_MODEL_POLICIES = (
    ZaiModelPolicy("glm-5.2", 1_000_000, 131_072, "tiered", True),
    ZaiModelPolicy("glm-5.1", 204_800, 131_072, "binary", True),
    ZaiModelPolicy("glm-5", 204_800, 131_072, "binary", True),
    ZaiModelPolicy("glm-5-turbo", 204_800, 131_072, "binary", True),
    ZaiModelPolicy("glm-4.7", 204_800, 131_072, "binary", True),
    ZaiModelPolicy("glm-4.5-air", 131_072, 98_304, "binary", False),
)
_ZAI_MODEL_POLICIES_BY_ID = {policy.model_id: policy for policy in _ZAI_MODEL_POLICIES}
_GENUINE_ANTHROPIC_HOSTS = frozenset({"api.anthropic.com"})
_GENUINE_OPENAI_HOSTS = frozenset({"api.openai.com"})
_GPT5_REASONING_RE = re.compile(r"gpt-5(?:\.(\d+))?", re.IGNORECASE)


def get_zai_model_policy(model_id: str) -> ZaiModelPolicy | None:
    return _ZAI_MODEL_POLICIES_BY_ID.get(model_id.lower())


def default_provider_compatibility() -> ProviderCompatibility:
    return ProviderCompatibility(
        profile_id="conservative",
        api_family="test",
        output_tokens_kwarg="max_tokens",
        max_output_tokens=None,
        tool_message_conversion=None,
        deferred_tool_search=False,
        reasoning_key=None,
        reasoning_replay_mode="tool_calls",
        auto_reasoning_effort=True,
        thinking_format="native",
        supported_thinking_levels=None,
        tool_stream=False,
    )


def resolve_provider_compatibility(
    provider_key: str,
    provider: LLMProvider,
    model: LLMModel,
) -> ProviderCompatibility:
    api_family = _api_family(provider.type)
    endpoint = _normalize_endpoint(provider.base_url)
    zai_route = _zai_route(provider_key, api_family, endpoint)
    if zai_route is not None:
        return _zai_profile(zai_route, provider, model)

    output_kwarg = _output_tokens_kwarg(provider.type)
    reasoning_key = _reasoning_key(provider)
    supported_levels = openai_gpt_reasoning_levels(model.model)

    if api_family == "anthropic":
        native = endpoint[0] in _GENUINE_ANTHROPIC_HOSTS
        deferred = native and "haiku" not in model.model.lower()
        return ProviderCompatibility(
            profile_id="anthropic-native" if native else "anthropic-compatible",
            api_family=api_family,
            output_tokens_kwarg=output_kwarg,
            max_output_tokens=None,
            tool_message_conversion=None if native else "extract_text",
            deferred_tool_search=deferred,
            reasoning_key=None,
            reasoning_replay_mode="exact",
            auto_reasoning_effort=True,
            thinking_format="native",
            supported_thinking_levels=supported_levels,
            tool_stream=False,
        )

    if api_family == "openai":
        native = endpoint[0] in _GENUINE_OPENAI_HOSTS
        profile_id = "openai-native" if native else "openai-compatible"
        thinking_format: ThinkingFormat = "native"
        replay_mode: ReasoningReplayMode = "tool_calls"
        if provider.type == "openai_legacy" and _is_dashscope_endpoint(endpoint[0]):
            profile_id = "dashscope"
            thinking_format = "dashscope"
        elif provider.type == "openai_legacy" and _is_kimi_model(model.model):
            profile_id = "kimi"
            thinking_format = "kimi"
            replay_mode = "strict_synthetic"
        elif provider.type == "openai_legacy" and _is_qwen3_model(model.model):
            profile_id = "qwen-template"
            thinking_format = "qwen_template"
        elif provider.type == "openai_legacy" and _is_strict_replay_model(model.model):
            replay_mode = "strict_synthetic"
        return ProviderCompatibility(
            profile_id=profile_id,
            api_family=api_family,
            output_tokens_kwarg=output_kwarg,
            max_output_tokens=None,
            tool_message_conversion=None if native else "extract_text",
            deferred_tool_search=False,
            reasoning_key=reasoning_key,
            reasoning_replay_mode=replay_mode,
            auto_reasoning_effort=True,
            thinking_format=thinking_format,
            supported_thinking_levels=supported_levels,
            tool_stream=False,
        )

    return ProviderCompatibility(
        profile_id=api_family,
        api_family=api_family,
        output_tokens_kwarg=output_kwarg,
        max_output_tokens=None,
        tool_message_conversion=None,
        deferred_tool_search=False,
        reasoning_key=None,
        reasoning_replay_mode="exact",
        auto_reasoning_effort=True,
        thinking_format="native",
        supported_thinking_levels=None,
        tool_stream=False,
    )


def openai_gpt_reasoning_levels(model_id: str) -> tuple[ThinkingEffort, ...] | None:
    match = _GPT5_REASONING_RE.search(model_id)
    if match is None:
        return None
    minor = int(match.group(1)) if match.group(1) else 0
    if minor == 0:
        return ("off", "minimal", "low", "medium", "high")
    if minor >= 4 or "codex-max" in model_id.lower():
        return ("off", "low", "medium", "high", "xhigh")
    return ("off", "low", "medium", "high")


def _zai_profile(
    route: Literal["z-ai-coding", "z-ai-api"],
    provider: LLMProvider,
    model: LLMModel,
) -> ProviderCompatibility:
    policy = get_zai_model_policy(model.model)
    if policy is None:
        thinking_format: ThinkingFormat = "none"
        supported_levels = None
        max_output_tokens = None
        tool_stream = False
    else:
        thinking_format = "zai_tiered" if policy.thinking_mode == "tiered" else "zai_binary"
        supported_levels = (
            ("off", "minimal", "low", "medium", "high", "xhigh")
            if policy.thinking_mode == "tiered"
            else ("off", "high")
        )
        max_output_tokens = policy.max_output_tokens
        tool_stream = policy.tool_stream
    return ProviderCompatibility(
        profile_id=route,
        api_family="openai",
        output_tokens_kwarg="max_tokens",
        max_output_tokens=max_output_tokens,
        tool_message_conversion="extract_text",
        deferred_tool_search=False,
        reasoning_key=_reasoning_key(provider),
        reasoning_replay_mode="exact",
        auto_reasoning_effort=False,
        thinking_format=thinking_format,
        supported_thinking_levels=supported_levels,
        tool_stream=tool_stream,
    )


def _api_family(provider_type: str) -> ApiFamily:
    if provider_type == "pythinker":
        return "pythinker"
    if provider_type in {"openai_legacy", "openai_responses", "openai_codex"}:
        return "openai"
    if provider_type == "anthropic":
        return "anthropic"
    if provider_type in {"google_genai", "gemini"}:
        return "gemini"
    if provider_type == "vertexai":
        return "vertex"
    return "test"


def _output_tokens_kwarg(provider_type: str) -> str:
    if provider_type in {"openai_responses", "openai_codex", "google_genai", "gemini", "vertexai"}:
        return "max_output_tokens"
    return "max_tokens"


def _reasoning_key(provider: LLMProvider) -> str | None:
    if provider.type != "openai_legacy":
        return None
    return provider.reasoning_key if provider.reasoning_key is not None else "reasoning_content"


def _normalize_endpoint(base_url: str | None) -> tuple[str, str]:
    if not base_url:
        return ("", "/")
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    path = f"/{parsed.path.lstrip('/')}".rstrip("/") or "/"
    return (host, path)


def _zai_route(
    provider_key: str,
    api_family: ApiFamily,
    endpoint: tuple[str, str],
) -> Literal["z-ai-coding", "z-ai-api"] | None:
    if provider_key in _ZAI_PROVIDER_KEYS:
        return "z-ai-coding" if provider_key == "managed:z-ai-coding" else "z-ai-api"
    if api_family != "openai":
        return None
    if endpoint == _ZAI_CODING_ENDPOINT:
        return "z-ai-coding"
    if endpoint == _ZAI_API_ENDPOINT:
        return "z-ai-api"
    return None


def _is_dashscope_endpoint(host: str) -> bool:
    return host == "aliyuncs.com" or host.endswith(".aliyuncs.com")


def _is_kimi_model(model_id: str) -> bool:
    return "kimi-k2" in model_id.lower().replace("_", "-")


def _is_qwen3_model(model_id: str) -> bool:
    normalized = model_id.lower().replace("_", "-")
    return "qwen3" in normalized or "qwen-3" in normalized


def _is_strict_replay_model(model_id: str) -> bool:
    normalized = model_id.lower()
    return "deepseek" in normalized or _is_kimi_model(normalized)


def _effort_enabled(effort: ThinkingEffort | None) -> bool:
    return effort is not None and effort != "off"
