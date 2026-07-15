"""SimpleCompaction shrink-on-overflow fallback.

Compaction sends the whole to-compact slice to the provider, so a turn
that already overflowed the context window can overflow the compaction
request too. On a context-length rejection the compactor drops the
oldest half of the slice and retries; when nothing summarizable fits it
falls back to preserving only the tail with an explicit dropped-context
note instead of failing the turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
import pythinker_core
from pydantic import SecretStr
from pythinker_core.chat_provider import APIStatusError
from pythinker_core.message import Message

from pythinker_code.config import LLMModel, LLMProvider
from pythinker_code.llm import LLM, capped_chat_provider
from pythinker_code.provider_compatibility import (
    default_provider_compatibility,
    resolve_provider_compatibility,
)
from pythinker_code.soul.compaction import SimpleCompaction
from pythinker_code.wire.types import TextPart


def _history(n_pairs: int = 4) -> list[Message]:
    messages: list[Message] = []
    for index in range(n_pairs):
        messages.append(Message(role="user", content=[TextPart(text=f"request {index}")]))
        messages.append(Message(role="assistant", content=[TextPart(text=f"reply {index}")]))
    return messages


class _FakeChatProvider:
    """Minimal provider double recording `with_generation_kwargs` calls.

    Returns a fresh instance rather than mutating in place, matching the real
    providers' copy-on-write contract — so tests must assert on the returned
    provider, not the original, to actually exercise that contract.
    """

    def __init__(self, generation_kwargs: dict[str, object] | None = None) -> None:
        self.generation_kwargs: dict[str, object] = generation_kwargs or {}

    def with_generation_kwargs(self, **kwargs: object) -> _FakeChatProvider:
        return _FakeChatProvider(kwargs)


def _fake_llm() -> LLM:
    return cast(
        LLM,
        SimpleNamespace(
            chat_provider=_FakeChatProvider(),
            provider_config=None,
            compatibility=default_provider_compatibility(),
        ),
    )


def _fake_llm_with_provider_type(provider_type: str) -> LLM:
    provider = LLMProvider(
        type=cast(Any, provider_type),
        base_url="https://api.example/v1",
        api_key=SecretStr("test-key"),
    )
    model = LLMModel(
        provider="test",
        model="test-model",
        max_context_size=100_000,
    )
    return cast(
        LLM,
        SimpleNamespace(
            chat_provider=_FakeChatProvider(),
            provider_config=provider,
            compatibility=resolve_provider_compatibility("test", provider, model),
        ),
    )


@pytest.mark.parametrize(
    ("provider_type", "expected_kwarg"),
    [
        ("openai_legacy", "max_tokens"),
        ("anthropic", "max_tokens"),
        ("pythinker", "max_tokens"),
        ("openai_responses", "max_output_tokens"),
        # ChatGPT/Codex sessions build the same OpenAIResponses provider as
        # "openai_responses" (see create_llm's "openai_codex" case), so they
        # take the same max_output_tokens kwarg, not the max_tokens default.
        ("openai_codex", "max_output_tokens"),
        ("google_genai", "max_output_tokens"),
        ("gemini", "max_output_tokens"),
        ("vertexai", "max_output_tokens"),
    ],
)
def test_capped_chat_provider_picks_kwarg_by_provider_type(
    provider_type: str, expected_kwarg: str
) -> None:
    llm = _fake_llm_with_provider_type(provider_type)

    capped_provider = cast(_FakeChatProvider, capped_chat_provider(llm, 4000))

    assert capped_provider.generation_kwargs == {expected_kwarg: 4000}


def _overflow_error() -> APIStatusError:
    return APIStatusError(400, "This model's maximum context length is exceeded")


def _summary_result() -> SimpleNamespace:
    return SimpleNamespace(
        message=Message(role="assistant", content=[TextPart(text="the summary")]),
        usage=None,
    )


class _FakeStep:
    def __init__(self, failures_before_success: int) -> None:
        self.failures_before_success = failures_before_success
        self.histories: list[list[Message]] = []
        self.chat_providers: list[object] = []

    async def __call__(self, *, chat_provider, system_prompt, toolset, history):
        self.histories.append(list(history))
        self.chat_providers.append(chat_provider)
        if len(self.histories) <= self.failures_before_success:
            raise _overflow_error()
        return _summary_result()


def _section_count(message: Message) -> int:
    return message.extract_text(" ").count("## Message")


@pytest.mark.asyncio
async def test_overflow_retries_with_smaller_slice(monkeypatch) -> None:
    fake_step = _FakeStep(failures_before_success=1)
    monkeypatch.setattr(pythinker_core, "step", fake_step)

    result = await SimpleCompaction(max_preserved_messages=2).compact(_history(), llm=_fake_llm())

    assert len(fake_step.histories) == 2
    first, second = (h[0] for h in fake_step.histories)
    assert _section_count(second) < _section_count(first)
    assert "the summary" in result.messages[0].extract_text(" ")


@pytest.mark.asyncio
async def test_exhausted_retries_fall_back_to_tail_with_note(monkeypatch) -> None:
    fake_step = _FakeStep(failures_before_success=99)
    monkeypatch.setattr(pythinker_core, "step", fake_step)

    history = _history()
    result = await SimpleCompaction(max_preserved_messages=2).compact(history, llm=_fake_llm())

    joined = " ".join(m.extract_text(" ") for m in result.messages)
    assert "dropped" in joined.lower()
    # The preserved tail survives.
    assert "reply 3" in joined


@pytest.mark.asyncio
async def test_compaction_caps_output_tokens(monkeypatch) -> None:
    fake_step = _FakeStep(failures_before_success=0)
    monkeypatch.setattr(pythinker_core, "step", fake_step)

    llm = _fake_llm()
    await SimpleCompaction(max_preserved_messages=2).compact(_history(), llm=llm)

    used_provider = cast(_FakeChatProvider, fake_step.chat_providers[0])
    assert used_provider.generation_kwargs == {"max_tokens": 4000}


@pytest.mark.asyncio
async def test_non_overflow_error_propagates(monkeypatch) -> None:
    async def _step_raises(**kwargs):
        raise APIStatusError(400, "invalid request: bad tool schema")

    monkeypatch.setattr(pythinker_core, "step", _step_raises)

    with pytest.raises(APIStatusError):
        await SimpleCompaction(max_preserved_messages=2).compact(_history(), llm=_fake_llm())
