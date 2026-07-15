from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import SecretStr
from pythinker_core.chat_provider import ChatProvider

from pythinker_code.config import Config, LLMModel, LLMProvider
from pythinker_code.llm import LLM
from pythinker_code.provider_compatibility import (
    ProviderCompatibility,
    resolve_provider_compatibility,
)
from pythinker_code.soul.pythinkersoul import PythinkerSoul


def _zai_config(model_id: str = "glm-5.1") -> Config:
    provider_key = "managed:z-ai-coding"
    return Config(
        is_from_default_location=True,
        default_model="z-ai-coding/model",
        providers={
            provider_key: LLMProvider(
                type="openai_legacy",
                base_url="https://api.z.ai/api/coding/paas/v4",
                api_key=SecretStr("test-key"),
            )
        },
        models={
            "z-ai-coding/model": LLMModel(
                provider=provider_key,
                model=model_id,
                max_context_size=204_800,
                capabilities={"thinking"},
            )
        },
    )


def test_soul_available_thinking_efforts_reads_runtime_profile() -> None:
    config = _zai_config()
    model = config.models[config.default_model]
    provider = config.providers[model.provider]
    profile = resolve_provider_compatibility(model.provider, provider, model)
    llm = LLM(
        chat_provider=cast(ChatProvider, SimpleNamespace(model_name=model.model)),
        max_context_size=model.max_context_size,
        capabilities={"thinking"},
        model_config=model,
        provider_config=provider,
        compatibility=profile,
    )
    soul = object.__new__(PythinkerSoul)
    soul._runtime = SimpleNamespace(llm=llm)  # pyright: ignore[reportAttributeAccessIssue]

    assert soul.available_thinking_efforts() == ("off", "high")


async def test_model_selector_resolves_selected_models_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code import llm as llm_module
    from pythinker_code.ui.shell import model_picker
    from pythinker_code.ui.shell import slash as shell_slash
    from pythinker_code.ui.shell.selectors import thinking as thinking_selector

    config = _zai_config("glm-5.2")
    model = config.models[config.default_model]
    provider = config.providers[model.provider]
    current_llm = SimpleNamespace(model_config=model)
    soul = SimpleNamespace(
        runtime=SimpleNamespace(config=config, llm=current_llm),
        thinking_effort="off",
        thinking=False,
    )

    class _ModelPicker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run(self) -> str:
            return config.default_model

    resolutions: list[tuple[str, LLMProvider, LLMModel]] = []
    original_resolver = llm_module.resolve_provider_compatibility

    def recording_resolver(
        provider_key: str,
        selected_provider: LLMProvider,
        selected_model: LLMModel,
    ) -> ProviderCompatibility:
        resolutions.append((provider_key, selected_provider, selected_model))
        return original_resolver(provider_key, selected_provider, selected_model)

    captured_levels: list[str] = []

    async def capture_selector(*, current_level: str, available_levels: list[str]) -> None:
        del current_level
        captured_levels.extend(available_levels)

    async def no_refresh(_config: Config) -> None:
        return None

    monkeypatch.setattr(shell_slash, "ensure_pythinker_soul", lambda _app: soul)
    monkeypatch.setattr(shell_slash, "refresh_managed_models", no_refresh)
    monkeypatch.setattr(model_picker, "ModelPickerApp", _ModelPicker)
    monkeypatch.setattr(llm_module, "resolve_provider_compatibility", recording_resolver)
    monkeypatch.setattr(thinking_selector, "run_thinking_selector", capture_selector)

    await cast(Any, shell_slash.model)(cast(Any, SimpleNamespace()), "")

    assert resolutions == [(model.provider, provider, model)]
    assert captured_levels == ["off", "minimal", "low", "medium", "high", "xhigh"]
