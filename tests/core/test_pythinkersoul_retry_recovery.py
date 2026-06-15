from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from pythinker_core.chat_provider import (
    APIConnectionError,
    APIStatusError,
    StreamedMessagePart,
    ThinkingEffort,
    TokenUsage,
)
from pythinker_core.message import Message, TextPart, ThinkPart
from pythinker_core.tooling import Tool
from pythinker_core.tooling.simple import SimpleToolset

from pythinker_code.config import LLMModel, LLMProvider, OAuthRef
from pythinker_code.llm import LLM
from pythinker_code.soul import run_soul
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.utils.aioqueue import QueueShutDown
from pythinker_code.wire import Wire
from pythinker_code.wire.types import ContextOverflowRecovered, StepBegin, StepRetry


class StaticStreamedMessage:
    def __init__(self, parts: Sequence[StreamedMessagePart]) -> None:
        self._iter = self._to_stream(parts)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _to_stream(
        self, parts: Sequence[StreamedMessagePart]
    ) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part

    @property
    def id(self) -> str | None:
        return "recovering"

    @property
    def usage(self) -> TokenUsage | None:
        return None

    @property
    def finish_reason(self) -> str | None:
        return None


class RecoveringSequenceProvider:
    name = "recovering-sequence"

    def __init__(self) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0

    @property
    def model_name(self) -> str:
        return "recovering-sequence"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            raise APIConnectionError("Connection error.")
        return StaticStreamedMessage([TextPart(text="recovered")])

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class AlwaysConnectionErrorProvider:
    name = "always-connection-error"

    def __init__(self) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0

    @property
    def model_name(self) -> str:
        return "always-connection-error"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        raise APIConnectionError("Connection error.")

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class StatusErrorThenSuccessProvider:
    name = "status-error-then-success"

    def __init__(self, status_code: int = 503) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0
        self._status_code = status_code

    @property
    def model_name(self) -> str:
        return "status-error-then-success"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts < 3:
            raise APIStatusError(self._status_code, f"Status {self._status_code}")
        return StaticStreamedMessage([TextPart(text="status recovered")])

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class ContextOverflowThenSuccessProvider:
    name = "context-overflow-then-success"

    def __init__(self) -> None:
        self.generate_attempts = 0

    @property
    def model_name(self) -> str:
        return "context-overflow-then-success"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            raise APIStatusError(413, "context length exceeded")
        return StaticStreamedMessage([TextPart(text="recovered after overflow")])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class AlwaysContextOverflowProvider:
    name = "always-context-overflow"

    def __init__(self) -> None:
        self.generate_attempts = 0

    @property
    def model_name(self) -> str:
        return "always-context-overflow"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        raise APIStatusError(413, "context length exceeded")

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class PartialStreamThenStatusErrorProvider:
    name = "partial-stream-then-status-error"

    def __init__(self, status_code: int = 429) -> None:
        self.generate_attempts = 0
        self._status_code = status_code

    @property
    def model_name(self) -> str:
        return "partial-stream-then-status-error"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage | PartialThenErrorStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            return PartialThenErrorStreamedMessage(
                [ThinkPart(think="old attempt")],
                APIStatusError(self._status_code, f"Status {self._status_code}"),
            )
        return StaticStreamedMessage([ThinkPart(think="new attempt"), TextPart(text="done")])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class PartialThenErrorStreamedMessage:
    def __init__(self, parts: Sequence[StreamedMessagePart], error: BaseException) -> None:
        self._iter = self._to_stream(parts, error)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _to_stream(
        self, parts: Sequence[StreamedMessagePart], error: BaseException
    ) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part
        raise error

    @property
    def id(self) -> str | None:
        return "partial-error"

    @property
    def usage(self) -> TokenUsage | None:
        return None

    @property
    def finish_reason(self) -> str | None:
        return None


class NonRetryableConnectionProvider:
    name = "non-retryable-connection"

    def __init__(self) -> None:
        self.generate_attempts = 0

    @property
    def model_name(self) -> str:
        return "non-retryable-connection"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            raise APIConnectionError("Connection error.")
        return StaticStreamedMessage([TextPart(text="non-retryable recovered")])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class ConnectionThen401ThenSuccessProvider:
    name = "connection-then-401-then-success"

    def __init__(self) -> None:
        self.generate_attempts = 0
        self.recovery_calls = 0

    @property
    def model_name(self) -> str:
        return "connection-then-401-then-success"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            raise APIConnectionError("Connection error.")
        if self.generate_attempts == 2:
            raise APIStatusError(401, "expired token")
        return StaticStreamedMessage([TextPart(text="auth recovered")])

    def on_retryable_error(self, error: BaseException) -> bool:
        self.recovery_calls += 1
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


def _runtime_with_llm(runtime: Runtime, llm: LLM) -> Runtime:
    return Runtime(
        config=runtime.config,
        llm=llm,
        session=runtime.session,
        builtin_args=runtime.builtin_args,
        denwa_renji=runtime.denwa_renji,
        approval=runtime.approval,
        labor_market=runtime.labor_market,
        environment=runtime.environment,
        notifications=runtime.notifications,
        background_tasks=runtime.background_tasks,
        skills=runtime.skills,
        oauth=runtime.oauth,
        additional_dirs=runtime.additional_dirs,
        skills_dirs=runtime.skills_dirs,
        role=runtime.role,
    )


def _make_soul(runtime: Runtime, llm: LLM, tmp_path: Path) -> tuple[PythinkerSoul, Context]:
    agent = Agent(
        name="Retry Test Agent",
        system_prompt="Retry test prompt.",
        toolset=SimpleToolset(),
        runtime=_runtime_with_llm(runtime, llm),
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    return PythinkerSoul(agent, context=context), context


async def _drain_ui_messages(wire: Wire) -> None:
    wire_ui = wire.ui_side(merge=True)
    while True:
        try:
            await wire_ui.receive()
        except QueueShutDown:
            return


async def _collect_ui_messages(wire: Wire, seen: list[object]) -> None:
    wire_ui = wire.ui_side(merge=True)
    while True:
        try:
            seen.append(await wire_ui.receive())
        except QueueShutDown:
            return


@pytest.mark.asyncio
async def test_step_retry_recovers_retryable_provider(runtime: Runtime, tmp_path: Path) -> None:
    runtime.config.loop_control.max_retries_per_step = 2
    provider = RecoveringSequenceProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)

    await run_soul(soul, "trigger recovery", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 2
    assert provider.recovery_calls == 1
    assert context.history[-1].extract_text(" ").strip() == "recovered"


@pytest.mark.asyncio
async def test_step_connection_error_recovery_only_retries_once(
    runtime: Runtime, tmp_path: Path
) -> None:
    runtime.config.loop_control.max_retries_per_step = 5
    provider = AlwaysConnectionErrorProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, _ = _make_soul(runtime, llm, tmp_path)

    with pytest.raises(APIConnectionError):
        await run_soul(soul, "trigger connection failure", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 2
    assert provider.recovery_calls == 1


@pytest.mark.asyncio
async def test_step_connection_error_records_failed_llm_metrics(
    runtime: Runtime, tmp_path: Path
) -> None:
    runtime.config.loop_control.max_retries_per_step = 5
    provider = AlwaysConnectionErrorProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, _ = _make_soul(runtime, llm, tmp_path)

    with (
        patch("pythinker_code.telemetry.metrics.record_llm_call") as record_llm_call,
        patch("pythinker_code.telemetry.metrics.record_error") as record_error,
        pytest.raises(APIConnectionError),
    ):
        await run_soul(soul, "trigger connection failure", _drain_ui_messages, asyncio.Event())

    failed_llm_calls = [
        call for call in record_llm_call.call_args_list if call.kwargs.get("success") is False
    ]
    assert len(failed_llm_calls) == 2
    for call in failed_llm_calls:
        assert call.kwargs["system"] == "alwaysconnectionerrorprovider"
        assert call.kwargs["model"] == "always-connection-error"
        assert call.kwargs["duration_seconds"] >= 0

    assert [call.kwargs for call in record_error.call_args_list] == [
        {"kind": "api_error", "error_type": "network"},
        {"kind": "api_error", "error_type": "network"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [503, 504], ids=["503", "504"])
async def test_step_status_error_still_uses_tenacity_retries(
    runtime: Runtime, tmp_path: Path, status_code: int
) -> None:
    runtime.config.loop_control.max_retries_per_step = 3
    provider = StatusErrorThenSuccessProvider(status_code=status_code)
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)

    await run_soul(soul, "trigger status retry", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 3
    assert provider.recovery_calls == 0
    assert context.history[-1].extract_text(" ").strip() == "status recovered"


@pytest.mark.asyncio
async def test_context_overflow_recovery_emits_recovered_event(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.config.loop_control.max_retries_per_step = 1
    provider = ContextOverflowThenSuccessProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)
    compact_calls = 0

    async def fake_prune_context() -> bool:
        return False

    async def fake_compact_context() -> None:
        nonlocal compact_calls
        compact_calls += 1

    monkeypatch.setattr(soul, "prune_context", fake_prune_context)
    monkeypatch.setattr(soul, "compact_context", fake_compact_context)
    seen: list[object] = []

    await run_soul(
        soul,
        "trigger context overflow",
        lambda wire: _collect_ui_messages(wire, seen),
        asyncio.Event(),
    )

    assert provider.generate_attempts == 2
    assert compact_calls == 1
    assert context.history[-1].extract_text(" ").strip() == "recovered after overflow"
    assert [msg for msg in seen if isinstance(msg, ContextOverflowRecovered)] == [
        ContextOverflowRecovered(outcome="recovered", trigger_step=1)
    ]


@pytest.mark.asyncio
async def test_context_overflow_recovery_emits_failed_event(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.config.loop_control.max_retries_per_step = 1
    provider = AlwaysContextOverflowProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, _ = _make_soul(runtime, llm, tmp_path)

    async def fake_compact_context() -> None:
        raise RuntimeError("compact failed")

    monkeypatch.setattr(soul, "compact_context", fake_compact_context)
    seen: list[object] = []

    with pytest.raises(APIStatusError):
        await run_soul(
            soul,
            "trigger context overflow",
            lambda wire: _collect_ui_messages(wire, seen),
            asyncio.Event(),
        )

    assert provider.generate_attempts == 1
    assert [msg for msg in seen if isinstance(msg, ContextOverflowRecovered)] == [
        ContextOverflowRecovered(outcome="failed", trigger_step=1)
    ]


@pytest.mark.asyncio
async def test_context_overflow_recovery_is_one_shot_per_turn(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.config.loop_control.max_retries_per_step = 1
    provider = AlwaysContextOverflowProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, _ = _make_soul(runtime, llm, tmp_path)
    compact_calls = 0

    async def fake_prune_context() -> bool:
        return False

    async def fake_compact_context() -> None:
        nonlocal compact_calls
        compact_calls += 1

    monkeypatch.setattr(soul, "prune_context", fake_prune_context)
    monkeypatch.setattr(soul, "compact_context", fake_compact_context)
    seen: list[object] = []

    with pytest.raises(APIStatusError):
        await run_soul(
            soul,
            "trigger context overflow twice",
            lambda wire: _collect_ui_messages(wire, seen),
            asyncio.Event(),
        )

    assert provider.generate_attempts == 2
    assert compact_calls == 1
    assert [msg for msg in seen if isinstance(msg, ContextOverflowRecovered)] == [
        ContextOverflowRecovered(outcome="recovered", trigger_step=1)
    ]


@pytest.mark.asyncio
async def test_step_retry_event_after_partial_stream(runtime: Runtime, tmp_path: Path) -> None:
    runtime.config.loop_control.max_retries_per_step = 2
    provider = PartialStreamThenStatusErrorProvider(status_code=429)
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)
    seen: list[object] = []

    await run_soul(
        soul,
        "trigger streamed status retry",
        lambda wire: _collect_ui_messages(wire, seen),
        asyncio.Event(),
    )

    assert provider.generate_attempts == 2
    assert [type(msg) for msg in seen if isinstance(msg, StepBegin)] == [StepBegin]
    retry = next(msg for msg in seen if isinstance(msg, StepRetry))
    assert retry.n == 1
    assert retry.next_attempt == 2
    assert retry.max_attempts == 2
    assert retry.error_type == "APIStatusError"
    assert retry.status_code == 429
    parts = [msg for msg in seen if isinstance(msg, ThinkPart | TextPart)]
    assert parts == [
        ThinkPart(think="old attempt"),
        ThinkPart(think="new attempt"),
        TextPart(text="done"),
    ]
    assert context.history[-1].extract_text(" ").strip() == "done"


@pytest.mark.asyncio
async def test_step_non_retryable_provider_keeps_tenacity_connection_retries(
    runtime: Runtime, tmp_path: Path
) -> None:
    runtime.config.loop_control.max_retries_per_step = 2
    provider = NonRetryableConnectionProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
    )
    soul, context = _make_soul(runtime, llm, tmp_path)

    await run_soul(
        soul, "trigger non-retryable connection retry", _drain_ui_messages, asyncio.Event()
    )

    assert provider.generate_attempts == 2
    assert context.history[-1].extract_text(" ").strip() == "non-retryable recovered"


@pytest.mark.asyncio
async def test_step_connection_recovery_then_401_triggers_oauth_refresh(
    runtime: Runtime, tmp_path: Path
) -> None:
    oauth_provider = LLMProvider(
        type="pythinker",
        base_url="https://api.test/v1",
        api_key=SecretStr(""),
        oauth=OAuthRef(storage="file", key="oauth/pythinker-code"),
    )
    oauth_model = LLMModel(
        provider="managed:pythinker-code",
        model="pythinker-for-coding",
        max_context_size=100_000,
    )
    runtime.config.providers[oauth_model.provider] = oauth_provider
    runtime.config.models["pythinker-code/pythinker-for-coding"] = oauth_model

    provider = ConnectionThen401ThenSuccessProvider()
    llm = LLM(
        chat_provider=provider,
        max_context_size=100_000,
        capabilities=set(),
        model_config=oauth_model,
        provider_config=oauth_provider,
    )
    soul, context = _make_soul(runtime, llm, tmp_path)

    refresh_mock = AsyncMock()
    runtime.oauth.ensure_fresh = refresh_mock

    await run_soul(soul, "trigger mixed recovery", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 3
    assert provider.recovery_calls == 1
    assert context.history[-1].extract_text(" ").strip() == "auth recovered"
    assert len(refresh_mock.await_args_list) == 2
    assert any(call.kwargs.get("force") is True for call in refresh_mock.await_args_list)


class OverflowThenOkProvider:
    name = "overflow-then-ok"

    def __init__(self) -> None:
        self.generate_attempts = 0

    @property
    def model_name(self) -> str:
        return "overflow-then-ok"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StaticStreamedMessage:
        self.generate_attempts += 1
        if self.generate_attempts == 1:
            raise APIStatusError(400, "context length exceeded")
        return StaticStreamedMessage([TextPart(text="recovered after reactive compact")])

    def on_retryable_error(self, error: BaseException) -> bool:
        _ = error
        return False

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class TestReactiveOverflowRecovery:
    @pytest.mark.asyncio
    async def test_context_overflow_triggers_one_shot_reactive_recovery(
        self, runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = OverflowThenOkProvider()
        llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
        soul, context = _make_soul(runtime, llm, tmp_path)
        seen: list[object] = []
        compact_calls = 0

        async def _count_compact() -> None:
            nonlocal compact_calls
            compact_calls += 1

        monkeypatch.setattr(soul, "compact_context", _count_compact)

        await run_soul(
            soul,
            "overflow then recover",
            lambda wire: _collect_ui_messages(wire, seen),
            asyncio.Event(),
        )

        assert provider.generate_attempts == 2
        assert compact_calls == 1
        assert context.history[-1].extract_text(" ").strip() == "recovered after reactive compact"
        recovered = [msg for msg in seen if isinstance(msg, ContextOverflowRecovered)]
        assert len(recovered) == 1
        assert recovered[0].outcome == "recovered"
        assert recovered[0].trigger_step == 1
