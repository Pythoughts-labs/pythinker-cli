from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import SecretStr

from pythinker_code.config import LLMModel, LLMProvider
from pythinker_code.provider_compatibility import (
    GenerationOverrides,
    get_zai_model_policy,
    resolve_provider_compatibility,
)


def _provider(provider_type: str, base_url: str) -> LLMProvider:
    return LLMProvider(
        type=provider_type,  # type: ignore[arg-type]
        base_url=base_url,
        api_key=SecretStr("test-key"),
    )


def _model(provider_key: str, model_id: str, *, thinking: bool = True) -> LLMModel:
    return LLMModel(
        provider=provider_key,
        model=model_id,
        max_context_size=200_000,
        capabilities={"thinking"} if thinking else None,
    )


@pytest.mark.parametrize(
    (
        "provider_key",
        "provider_type",
        "base_url",
        "model_id",
        "profile_id",
        "api_family",
        "thinking_format",
        "tool_conversion",
        "deferred_tool_search",
    ),
    [
        (
            "anthropic",
            "anthropic",
            "https://api.anthropic.com/v1",
            "claude-opus-4-8",
            "anthropic-native",
            "anthropic",
            "native",
            None,
            True,
        ),
        (
            "proxy",
            "anthropic",
            "https://proxy.example/anthropic",
            "claude-opus-4-8",
            "anthropic-compatible",
            "anthropic",
            "native",
            "extract_text",
            False,
        ),
        (
            "openai",
            "openai_legacy",
            "https://api.openai.com/v1",
            "gpt-5.2",
            "openai-native",
            "openai",
            "native",
            None,
            False,
        ),
        (
            "proxy",
            "openai_legacy",
            "https://proxy.example/v1",
            "some-model",
            "openai-compatible",
            "openai",
            "native",
            "extract_text",
            False,
        ),
        (
            "managed:alibaba",
            "openai_legacy",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "qwen3-max",
            "dashscope",
            "openai",
            "dashscope",
            "extract_text",
            False,
        ),
        (
            "moonshot",
            "openai_legacy",
            "https://api.moonshot.ai/v1",
            "kimi-k2.6",
            "kimi",
            "openai",
            "kimi",
            "extract_text",
            False,
        ),
        (
            "local",
            "openai_legacy",
            "http://localhost:8080/v1",
            "Qwen3.6-35B-A3B",
            "qwen-template",
            "openai",
            "qwen_template",
            "extract_text",
            False,
        ),
    ],
)
def test_resolver_preserves_existing_provider_matrix(
    provider_key: str,
    provider_type: str,
    base_url: str,
    model_id: str,
    profile_id: str,
    api_family: str,
    thinking_format: str,
    tool_conversion: str | None,
    deferred_tool_search: bool,
) -> None:
    profile = resolve_provider_compatibility(
        provider_key,
        _provider(provider_type, base_url),
        _model(provider_key, model_id),
    )

    assert profile.profile_id == profile_id
    assert profile.api_family == api_family
    assert profile.thinking_format == thinking_format
    assert profile.tool_message_conversion == tool_conversion
    assert profile.deferred_tool_search is deferred_tool_search


@pytest.mark.parametrize(
    ("provider_key", "base_url", "profile_id"),
    [
        (
            "managed:z-ai-coding",
            "https://unrelated.example/v1",
            "z-ai-coding",
        ),
        (
            "managed:z-ai-api",
            "https://api.z.ai/api/coding/paas/v4",
            "z-ai-api",
        ),
        (
            "custom",
            "https://API.Z.AI/api/coding/paas/v4/",
            "z-ai-coding",
        ),
        (
            "custom",
            "https://api.z.ai/api/paas/v4/",
            "z-ai-api",
        ),
    ],
)
def test_zai_identity_precedence_and_normalized_endpoint_fallback(
    provider_key: str, base_url: str, profile_id: str
) -> None:
    profile = resolve_provider_compatibility(
        provider_key,
        _provider("openai_legacy", base_url),
        _model(provider_key, "glm-5.2"),
    )

    assert profile.profile_id == profile_id
    assert profile.api_family == "openai"
    assert profile.thinking_format == "zai_tiered"
    assert profile.reasoning_replay_mode == "exact"
    assert profile.auto_reasoning_effort is False
    assert profile.tool_stream is True


def test_local_glm_name_does_not_activate_zai_policy() -> None:
    profile = resolve_provider_compatibility(
        "local",
        _provider("openai_legacy", "http://localhost:8080/v1"),
        _model("local", "glm-5.2"),
    )

    assert profile.profile_id == "openai-compatible"
    assert not profile.thinking_format.startswith("zai_")
    assert profile.tool_stream is False
    assert profile.max_output_tokens is None


def test_zai_model_policy_matrix_is_literal_and_immutable() -> None:
    expected = {
        "glm-5.2": (1_000_000, 131_072, "tiered", True),
        "glm-5.1": (204_800, 131_072, "binary", True),
        "glm-5": (204_800, 131_072, "binary", True),
        "glm-5-turbo": (204_800, 131_072, "binary", True),
        "glm-4.7": (204_800, 131_072, "binary", True),
        "glm-4.5-air": (131_072, 98_304, "binary", False),
    }

    for model_id, values in expected.items():
        policy = get_zai_model_policy(model_id)
        assert policy is not None
        assert (
            policy.context_tokens,
            policy.max_output_tokens,
            policy.thinking_mode,
            policy.tool_stream,
        ) == values
        with pytest.raises(FrozenInstanceError):
            policy.max_output_tokens = 1  # pyright: ignore[reportAttributeAccessIssue]

    assert get_zai_model_policy("glm-future") is None
    assert get_zai_model_policy("glm-5.2[1m]") is None


def test_profiles_are_frozen_and_request_override_dicts_are_fresh() -> None:
    profile = resolve_provider_compatibility(
        "managed:z-ai-coding",
        _provider("openai_legacy", "https://api.z.ai/api/coding/paas/v4"),
        _model("managed:z-ai-coding", "glm-5.2"),
    )

    with pytest.raises(FrozenInstanceError):
        profile.profile_id = "changed"  # pyright: ignore[reportAttributeAccessIssue]

    first = profile.request_overrides(model_id="glm-5.2", effort="high")
    second = profile.request_overrides(model_id="glm-5.2", effort="high")
    assert first == GenerationOverrides(
        native_effort=None,
        generation_kwargs={"max_tokens": 131_072},
        extra_body={
            "thinking": {"type": "enabled", "clear_thinking": False},
            "reasoning_effort": "high",
        },
    )
    first.extra_body["mutated"] = True
    assert "mutated" not in second.extra_body


@pytest.mark.parametrize(
    ("effort", "expected_thinking", "expected_reasoning_effort"),
    [
        ("off", {"type": "disabled"}, None),
        ("minimal", {"type": "disabled"}, None),
        ("low", {"type": "enabled", "clear_thinking": False}, "high"),
        ("medium", {"type": "enabled", "clear_thinking": False}, "high"),
        ("high", {"type": "enabled", "clear_thinking": False}, "high"),
        ("xhigh", {"type": "enabled", "clear_thinking": False}, "max"),
        ("max", {"type": "enabled", "clear_thinking": False}, "max"),
    ],
)
def test_glm52_request_overrides(
    effort: str, expected_thinking: dict[str, object], expected_reasoning_effort: str | None
) -> None:
    profile = resolve_provider_compatibility(
        "managed:z-ai-api",
        _provider("openai_legacy", "https://api.z.ai/api/paas/v4"),
        _model("managed:z-ai-api", "glm-5.2"),
    )

    overrides = profile.request_overrides(model_id="glm-5.2", effort=effort)  # type: ignore[arg-type]

    assert overrides.native_effort is None
    assert overrides.generation_kwargs == {"max_tokens": 131_072}
    assert overrides.extra_body["thinking"] == expected_thinking
    assert overrides.extra_body.get("reasoning_effort") == expected_reasoning_effort


@pytest.mark.parametrize(
    ("effort", "expected_effort", "expected_thinking"),
    [
        ("off", "off", {"type": "disabled"}),
        ("minimal", "off", {"type": "disabled"}),
        ("low", "high", {"type": "enabled", "clear_thinking": False}),
        ("xhigh", "high", {"type": "enabled", "clear_thinking": False}),
    ],
)
def test_binary_zai_effort_maps_before_request_assembly(
    effort: str, expected_effort: str, expected_thinking: dict[str, object]
) -> None:
    profile = resolve_provider_compatibility(
        "managed:z-ai-api",
        _provider("openai_legacy", "https://api.z.ai/api/paas/v4"),
        _model("managed:z-ai-api", "glm-5.1"),
    )

    effective = profile.effective_effort(effort, {"thinking"})  # type: ignore[arg-type]
    overrides = profile.request_overrides(model_id="glm-5.1", effort=effective)

    assert effective == expected_effort
    assert overrides.native_effort is None
    assert overrides.generation_kwargs == {"max_tokens": 131_072}
    assert overrides.extra_body == {"thinking": expected_thinking}


def test_zai_thinking_levels_are_model_specific() -> None:
    tiered = resolve_provider_compatibility(
        "managed:z-ai-coding",
        _provider("openai_legacy", "https://api.z.ai/api/coding/paas/v4"),
        _model("managed:z-ai-coding", "glm-5.2"),
    )
    binary = resolve_provider_compatibility(
        "managed:z-ai-coding",
        _provider("openai_legacy", "https://api.z.ai/api/coding/paas/v4"),
        _model("managed:z-ai-coding", "glm-4.7"),
    )

    assert tiered.supported_thinking_levels == (
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    )
    assert binary.supported_thinking_levels == ("off", "high")


def test_unknown_zai_model_is_conservative() -> None:
    profile = resolve_provider_compatibility(
        "managed:z-ai-api",
        _provider("openai_legacy", "https://api.z.ai/api/paas/v4"),
        _model("managed:z-ai-api", "glm-future", thinking=False),
    )

    assert profile.profile_id == "z-ai-api"
    assert profile.max_output_tokens is None
    assert profile.thinking_format == "none"
    assert profile.supported_thinking_levels is None
    assert profile.tool_stream is False
    assert profile.effective_effort(None, None) is None
    assert profile.request_overrides(model_id="glm-future", effort=None) == GenerationOverrides(
        native_effort=None,
        generation_kwargs={},
        extra_body={},
    )


def test_generic_profile_preserves_unconfigured_effort() -> None:
    profile = resolve_provider_compatibility(
        "proxy",
        _provider("openai_legacy", "https://proxy.example/v1"),
        _model("proxy", "plain-model", thinking=False),
    )

    assert profile.effective_effort(None, None) is None
    assert profile.request_overrides(model_id="plain-model", effort=None) == GenerationOverrides(
        native_effort=None,
        generation_kwargs={},
        extra_body={},
    )
