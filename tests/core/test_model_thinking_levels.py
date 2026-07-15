"""Per-model reasoning-effort scoping (OpenAI GPT-5 family).

OpenAI's ``reasoning_effort`` set is model-dependent and drifted across the
GPT-5 line, so the provider-neutral ladder over-offers levels a given model
rejects (e.g. ``minimal`` on gpt-5.4/5.5). These tests pin the per-model rule
the selector and ``create_llm`` use so unsupported levels are never offered nor
sent.
"""

from __future__ import annotations

from pydantic import SecretStr

from pythinker_code.config import LLMModel, LLMProvider
from pythinker_code.llm import (
    available_model_thinking_levels,
    openai_gpt_reasoning_levels,
)
from pythinker_code.provider_compatibility import resolve_provider_compatibility
from pythinker_code.thinking import clamp_thinking_effort


def test_openai_gpt_reasoning_levels_per_version() -> None:
    # 5.0 generation keeps 'minimal' (its lowest tier), no 'xhigh'.
    assert openai_gpt_reasoning_levels("gpt-5") == ("off", "minimal", "low", "medium", "high")
    assert openai_gpt_reasoning_levels("gpt-5-codex") == (
        "off",
        "minimal",
        "low",
        "medium",
        "high",
    )
    # 5.1 replaced 'minimal' with 'none', still no 'xhigh'.
    assert openai_gpt_reasoning_levels("gpt-5.1") == ("off", "low", "medium", "high")
    # codex-max and 5.4+ add 'xhigh' and drop 'minimal'.
    assert openai_gpt_reasoning_levels("gpt-5.1-codex-max") == (
        "off",
        "low",
        "medium",
        "high",
        "xhigh",
    )
    assert openai_gpt_reasoning_levels("gpt-5.4-mini") == ("off", "low", "medium", "high", "xhigh")
    assert openai_gpt_reasoning_levels("gpt-5.5") == ("off", "low", "medium", "high", "xhigh")
    # A provider-prefixed id (e.g. OpenRouter) still matches.
    assert openai_gpt_reasoning_levels("openai/gpt-5.5") == (
        "off",
        "low",
        "medium",
        "high",
        "xhigh",
    )
    # Models without a known per-model rule opt out.
    assert openai_gpt_reasoning_levels("claude-opus-4") is None
    assert openai_gpt_reasoning_levels("glm-5.2") is None


def test_available_model_thinking_levels_scopes_gpt() -> None:
    caps = {"thinking"}
    gpt55 = LLMModel(provider="openai", model="gpt-5.5", max_context_size=400_000)
    levels = available_model_thinking_levels(gpt55, caps)
    assert "minimal" not in levels
    assert levels == ("off", "low", "medium", "high", "xhigh")

    gpt5 = LLMModel(provider="openai", model="gpt-5", max_context_size=400_000)
    assert available_model_thinking_levels(gpt5, caps) == (
        "off",
        "minimal",
        "low",
        "medium",
        "high",
    )


def test_available_model_thinking_levels_non_gpt_keeps_full_ladder() -> None:
    other = LLMModel(provider="anthropic", model="claude-opus-4", max_context_size=200_000)
    # No per-model rule -> full provider-neutral ladder preserved.
    assert available_model_thinking_levels(other, {"thinking"}) == (
        "off",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    )


def test_available_model_thinking_levels_prefers_profile_override() -> None:
    provider = LLMProvider(
        type="openai_legacy",
        base_url="https://api.z.ai/api/paas/v4",
        api_key=SecretStr("test-key"),
    )
    model = LLMModel(
        provider="managed:z-ai-api",
        model="glm-4.7",
        max_context_size=204_800,
        capabilities={"thinking"},
    )
    profile = resolve_provider_compatibility(model.provider, provider, model)

    assert available_model_thinking_levels(model, {"thinking"}, profile) == ("off", "high")


def test_unsupported_effort_clamps_up_to_supported() -> None:
    # The create_llm send-path clamps a persisted unsupported effort to the
    # nearest supported level, so 'minimal' is never sent to gpt-5.5.
    gpt55_levels = ("off", "low", "medium", "high", "xhigh")
    assert clamp_thinking_effort("minimal", gpt55_levels) == "low"
