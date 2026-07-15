"""Gate for the deferred ToolSearch workflow.

Regression coverage for the GLM-5.2 ToolSearch loop: `ToolSearch` must only be
offered to models that genuinely support Anthropic's `tool_reference` /
`defer_loading` beta. Compat proxies that declare `type="anthropic"` but point
at their own endpoint (Kimi, MiniMax, custom bridges) must NOT see it.
See `pythinker_code.llm.supports_deferred_tool_search`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import SecretStr

from pythinker_code.config import LLMModel, LLMProvider
from pythinker_code.llm import LLM, supports_deferred_tool_search
from pythinker_code.provider_compatibility import (
    default_provider_compatibility,
    resolve_provider_compatibility,
)


def _llm(provider_type: str | None, base_url: str, model: str) -> LLM:
    provider = (
        None
        if provider_type is None
        else LLMProvider(type=cast("str", provider_type), base_url=base_url, api_key=SecretStr("x"))  # type: ignore[arg-type]
    )
    model_config = LLMModel(
        provider="test",
        model=model,
        max_context_size=200_000,
    )
    compatibility = (
        resolve_provider_compatibility("test", provider, model_config)
        if provider is not None
        else default_provider_compatibility()
    )
    return LLM(
        chat_provider=cast("object", SimpleNamespace(model_name=model)),  # type: ignore[arg-type]
        max_context_size=200_000,
        capabilities=set(),
        compatibility=compatibility,
        model_config=model_config,
        provider_config=provider,
    )


def test_genuine_anthropic_supports_tool_search() -> None:
    assert supports_deferred_tool_search(
        _llm("anthropic", "https://api.anthropic.com", "claude-opus-4-8")
    )


@pytest.mark.parametrize(
    ("base_url", "label"),
    [
        ("https://proxy.example/anthropic", "custom proxy"),
        ("https://api.moonshot.ai/anthropic", "kimi"),
        ("https://api.minimax.io/anthropic", "minimax"),
    ],
)
def test_anthropic_compat_proxies_are_excluded(base_url: str, label: str) -> None:
    # type="anthropic" but a non-genuine host -> no tool_reference beta -> hidden.
    assert not supports_deferred_tool_search(_llm("anthropic", base_url, "glm-5.2")), label


def test_non_anthropic_provider_excluded() -> None:
    assert not supports_deferred_tool_search(
        _llm("openai_responses", "https://api.openai.com", "gpt-5.5")
    )


def test_haiku_excluded_even_on_genuine_anthropic() -> None:
    assert not supports_deferred_tool_search(
        _llm("anthropic", "https://api.anthropic.com", "claude-haiku-4-5")
    )


def test_none_llm_or_provider_excluded() -> None:
    assert not supports_deferred_tool_search(None)
    assert not supports_deferred_tool_search(_llm(None, "", "x"))


def test_env_force_enable_overrides_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit opt-in: user asserts their proxy forwards the beta.
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    assert supports_deferred_tool_search(
        _llm("anthropic", "https://proxy.example/anthropic", "claude-compatible")
    )


def test_env_kill_switch_overrides_genuine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    assert not supports_deferred_tool_search(
        _llm("anthropic", "https://api.anthropic.com", "claude-opus-4-8")
    )
