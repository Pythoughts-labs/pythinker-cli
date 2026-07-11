"""Tests for dynamic-injection provider hook handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pythinker_core.message import Message
from pythinker_core.tooling.empty import EmptyToolset

import pythinker_code.soul.context as context_module
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.compaction import CompactionResult
from pythinker_code.soul.context import Context, ContextGenerationConflictError, ContextReplacement
from pythinker_code.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider
from pythinker_code.soul.pythinkersoul import PythinkerSoul


class _BoomProvider(DynamicInjectionProvider):
    """Buggy provider that raises from both hooks."""

    def __init__(self) -> None:
        self.on_context_compacted_calls = 0

    async def get_injections(self, history, soul) -> list[DynamicInjection]:  # noqa: ARG002
        raise RuntimeError("boom")

    async def on_context_compacted(self) -> None:
        self.on_context_compacted_calls += 1
        raise RuntimeError("boom-compact")


class _RecordingProvider(DynamicInjectionProvider):
    """Stub provider that records whether its hooks were awaited."""

    def __init__(self) -> None:
        self.get_injections_calls: int = 0
        self.on_context_compacted_calls: int = 0

    async def get_injections(self, history, soul) -> list[DynamicInjection]:  # noqa: ARG002
        self.get_injections_calls += 1
        return []

    async def on_context_compacted(self) -> None:
        self.on_context_compacted_calls += 1


class _BlockingProvider(_RecordingProvider):
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self._entered = entered
        self._release = release

    async def on_context_compacted(self) -> None:
        self.on_context_compacted_calls += 1
        self._entered.set()
        await self._release.wait()


class _SelfCancellingProvider(_RecordingProvider):
    async def on_context_compacted(self) -> None:
        self.on_context_compacted_calls += 1
        raise asyncio.CancelledError()


async def test_compacted_hook_isolates_provider_failures(runtime: Runtime, tmp_path: Path) -> None:
    """A buggy provider must not abort compaction notification of later providers."""
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))

    recorder = _RecordingProvider()
    soul._injection_providers = [_BoomProvider(), recorder]  # pyright: ignore[reportPrivateUsage]

    await soul.notify_history_rebuilt()

    assert recorder.on_context_compacted_calls == 1


async def test_compacted_hook_isolates_provider_originated_cancellation(
    runtime: Runtime, tmp_path: Path
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    cancelling = _SelfCancellingProvider()
    recorder = _RecordingProvider()
    soul._injection_providers = [cancelling, recorder]  # pyright: ignore[reportPrivateUsage]

    await soul.notify_history_rebuilt()

    assert cancelling.on_context_compacted_calls == 1
    assert recorder.on_context_compacted_calls == 1


async def test_stale_operation_does_not_claim_concurrent_replacement_commit(
    runtime: Runtime, tmp_path: Path
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "concurrent.jsonl")
    soul = PythinkerSoul(agent, context=context)
    recorder = _RecordingProvider()
    soul._injection_providers = [recorder]  # pyright: ignore[reportPrivateUsage]
    expected_generation = context.mutation_generation
    stale_entered = asyncio.Event()
    release_stale = asyncio.Event()

    async def stale_replacement():
        stale_entered.set()
        await release_stale.wait()
        return await context.replace_history(
            ContextReplacement(None, (Message(role="user", content="stale"),), 1, False),
            expected_generation=expected_generation,
        )

    stale = asyncio.create_task(soul._complete_history_replacement(stale_replacement()))  # pyright: ignore[reportPrivateUsage]
    try:
        await asyncio.wait_for(stale_entered.wait(), timeout=5.0)
        await soul._complete_history_replacement(  # pyright: ignore[reportPrivateUsage]
            context.replace_history(
                ContextReplacement(None, (Message(role="user", content="winner"),), 1, False),
                expected_generation=expected_generation,
            )
        )
    finally:
        release_stale.set()

    with pytest.raises(ContextGenerationConflictError):
        await asyncio.wait_for(stale, timeout=5.0)
    assert recorder.on_context_compacted_calls == 1
    assert soul._request_lifecycle.history_generation == 1  # pyright: ignore[reportPrivateUsage]


async def test_revert_visible_durability_error_rearms_all_providers_before_propagating(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    soul = PythinkerSoul(agent, context=context)
    boom = _BoomProvider()
    recorder = _RecordingProvider()
    soul._injection_providers = [boom, recorder]  # pyright: ignore[reportPrivateUsage]
    await context.append_message(Message(role="user", content="before"))
    await context.checkpoint(add_user_message=False)
    await context.append_message(Message(role="assistant", content="after"))
    lifecycle_generation = soul._request_lifecycle.history_generation  # pyright: ignore[reportPrivateUsage]

    def fail_directory_sync(_path: Path) -> bool:
        raise OSError("fsync failed")

    monkeypatch.setattr(
        context_module,
        "_sync_parent_directory",
        fail_directory_sync,
    )

    with pytest.raises(
        context_module.ContextPersistenceError,
        match="power-loss durability is uncertain",
    ):
        await soul._revert_context_to(0)  # pyright: ignore[reportPrivateUsage]

    assert [message.extract_text("") for message in context.history] == ["before"]
    assert soul._request_lifecycle.history_generation == lifecycle_generation + 1  # pyright: ignore[reportPrivateUsage]
    assert boom.on_context_compacted_calls == 1
    assert recorder.on_context_compacted_calls == 1


async def _make_compactable_soul(runtime: Runtime, tmp_path: Path) -> PythinkerSoul:
    """Build a real soul through its constructor and mock only the compaction LLM boundary.

    Exercising the production constructor (rather than ``object.__new__`` with hand-assigned
    private fields) keeps the test honest against constructor drift: a field the compaction
    path starts reading is wired by ``__init__`` here instead of silently defaulting to a mock.
    """
    runtime.session.state.active_skills = []
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    await context.append_message(Message(role="user", content="compact me"))
    soul = PythinkerSoul(agent, context=context)
    result = CompactionResult(
        messages=[Message(role="user", content="compacted")],
        usage=None,
    )
    soul._run_with_connection_recovery = AsyncMock(return_value=result)  # pyright: ignore[reportPrivateUsage]
    return soul


async def test_compact_context_notifies_injection_providers(
    runtime: Runtime, tmp_path: Path
) -> None:
    """compact_context() must await on_context_compacted on every registered provider."""
    soul = await _make_compactable_soul(runtime, tmp_path)
    provider_a = _RecordingProvider()
    provider_b = _RecordingProvider()
    soul.add_injection_provider(provider_a)
    soul.add_injection_provider(provider_b)

    with patch("pythinker_code.soul.pythinkersoul.wire_send"):
        await soul.compact_context()

    assert provider_a.on_context_compacted_calls == 1
    assert provider_b.on_context_compacted_calls == 1


async def test_compact_context_notifies_surviving_providers_after_failure(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A provider raising in its hook must not prevent later providers from being notified."""
    soul = await _make_compactable_soul(runtime, tmp_path)
    boom = _BoomProvider()
    recorder = _RecordingProvider()
    soul.add_injection_provider(boom)
    soul.add_injection_provider(recorder)

    with patch("pythinker_code.soul.pythinkersoul.wire_send"):
        await soul.compact_context()

    assert recorder.on_context_compacted_calls == 1
