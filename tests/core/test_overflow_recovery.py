"""Reactive context-overflow recovery in the agent loop.

The proactive prune/compact thresholds run on heuristic token counts and
can undercount (e.g. large pending tool output), so the provider may
still reject a step with a context-length 400. The loop recovers once
per turn — prune, force-compact, retry the step — instead of killing
the turn.
"""

from __future__ import annotations

from contextvars import Token
from unittest.mock import AsyncMock

import pytest
from pythinker_core.tooling.simple import SimpleToolset

from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.wire import Wire


def _wire_context() -> Token[Wire | None]:
    import pythinker_code.soul as soul_module

    wire = Wire()
    return soul_module._current_wire.set(wire)


def _reset_wire_context(token: Token[Wire | None]) -> None:
    import pythinker_code.soul as soul_module

    soul_module._current_wire.reset(token)


def _make_soul(runtime: Runtime, tmp_path) -> PythinkerSoul:
    agent = Agent(name="Overflow", system_prompt="sys", toolset=SimpleToolset(), runtime=runtime)
    context = Context(file_backend=tmp_path / "history.jsonl")
    return PythinkerSoul(agent, context=context)


class TestClassifierLocation:
    def test_classifier_importable_from_api_errors_and_soul(self) -> None:
        from pythinker_code.soul.api_errors import classify_api_error as from_module
        from pythinker_code.soul.pythinkersoul import classify_api_error as from_soul

        assert from_module is from_soul


class TestRecoverFromContextOverflow:
    @pytest.mark.asyncio
    async def test_prunes_compacts_and_reports_recovered(self, runtime, tmp_path) -> None:
        soul = _make_soul(runtime, tmp_path)
        soul.prune_context = AsyncMock(return_value=True)  # type: ignore[method-assign]
        soul.compact_context = AsyncMock()  # type: ignore[method-assign]
        wire_token = _wire_context()

        try:
            recovered = await soul._recover_from_context_overflow(step_no=3)
        finally:
            _reset_wire_context(wire_token)

        assert recovered is True
        soul.prune_context.assert_awaited_once()
        soul.compact_context.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prune_failure_does_not_block_compaction(self, runtime, tmp_path) -> None:
        soul = _make_soul(runtime, tmp_path)
        soul.prune_context = AsyncMock(side_effect=RuntimeError("prune broke"))  # type: ignore[method-assign]
        soul.compact_context = AsyncMock()  # type: ignore[method-assign]
        wire_token = _wire_context()

        try:
            recovered = await soul._recover_from_context_overflow(step_no=3)
        finally:
            _reset_wire_context(wire_token)

        assert recovered is True
        soul.compact_context.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_compaction_failure_reports_not_recovered(self, runtime, tmp_path) -> None:
        soul = _make_soul(runtime, tmp_path)
        soul.prune_context = AsyncMock(return_value=False)  # type: ignore[method-assign]
        soul.compact_context = AsyncMock(side_effect=RuntimeError("compact broke"))  # type: ignore[method-assign]
        wire_token = _wire_context()

        try:
            recovered = await soul._recover_from_context_overflow(step_no=3)
        finally:
            _reset_wire_context(wire_token)

        assert recovered is False
