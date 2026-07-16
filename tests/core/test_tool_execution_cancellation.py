from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel
from pythinker_core import step
from pythinker_core.chat_provider.mock import MockChatProvider
from pythinker_core.tooling import (
    CallableTool2,
    ToolBatchContext,
    ToolCancellationTimeoutError,
    ToolOk,
    ToolReturnValue,
)

from pythinker_code.soul import tool_execution
from pythinker_code.soul.toolset import MCPServerInfo, PythinkerToolset
from pythinker_code.wire.types import ToolCall, ToolResult


class NoParams(BaseModel):
    pass


class CancellationIgnoringTool(CallableTool2[NoParams]):
    name: str = "Stubborn"
    description: str = "Wait until released, including after cancellation"
    params: type[NoParams] = NoParams
    supports_parallel: ClassVar[bool] = True

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.invocations = 0

    async def __call__(self, params: NoParams) -> ToolReturnValue:
        del params
        self.invocations += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release.wait()
        finally:
            self.finished.set()
        return ToolOk(output="late")


class CompleteAndCancelCallerTool(CallableTool2[NoParams]):
    name: str = "CompleteAndCancel"
    description: str = "Complete while cancelling the result collector"
    params: type[NoParams] = NoParams
    supports_parallel: ClassVar[bool] = True

    def __init__(self) -> None:
        super().__init__()
        self.cancel_target: Callable[[], bool] | None = None

    async def __call__(self, params: NoParams) -> ToolReturnValue:
        del params
        assert self.cancel_target is not None
        asyncio.get_running_loop().call_soon(self.cancel_target)
        return ToolOk(output="completed before cancellation")


class ImmediateTool(CallableTool2[NoParams]):
    name: str = "Immediate"
    description: str = "Complete immediately"
    params: type[NoParams] = NoParams
    supports_parallel: ClassVar[bool] = True

    def __init__(self) -> None:
        super().__init__()
        self.invocations = 0

    async def __call__(self, params: NoParams) -> ToolReturnValue:
        del params
        self.invocations += 1
        return ToolOk(output="done")


class RecordingClient:
    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def close(self) -> None:
        self.closed.set()


def _call(call_id: str, name: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name=name, arguments="{}"),
    )


async def _wait_until_recovered(toolset: PythinkerToolset) -> None:
    for _ in range(100):
        if not toolset._execution.poisoned:  # pyright: ignore[reportPrivateUsage]
            return
        await asyncio.sleep(0.001)
    raise AssertionError("execution engine did not recover after late task settlement")


def test_cancellation_timeout_default_is_five_seconds() -> None:
    assert tool_execution.TOOL_CANCELLATION_TIMEOUT_SECONDS == 5.0


@pytest.mark.parametrize("timeout", [-1.0, float("nan"), float("inf")])
async def test_engine_cleanup_rejects_invalid_timeout(timeout: float) -> None:
    toolset = PythinkerToolset()

    with pytest.raises(ValueError, match="finite and non-negative"):
        await toolset._execution.cleanup(timeout=timeout)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("timeout", [-1.0, float("nan"), float("inf")])
async def test_invalid_cancellation_timeout_does_not_cancel_work(timeout: float) -> None:
    stubborn = CancellationIgnoringTool()
    toolset = PythinkerToolset()
    toolset.add(stubborn)
    batch = toolset.handle_batch([_call("stubborn", "Stubborn")], ToolBatchContext())
    await stubborn.started.wait()

    with pytest.raises(ValueError, match="finite and non-negative"):
        await batch.cancel_and_settle(timeout=timeout)

    assert not stubborn.cancel_seen.is_set()
    stubborn.release.set()
    assert [result.tool_call_id for result in await batch.results()] == ["stubborn"]


async def test_zero_timeout_accepts_already_completed_batch() -> None:
    immediate = ImmediateTool()
    toolset = PythinkerToolset()
    toolset.add(immediate)
    batch = toolset.handle_batch([_call("done", "Immediate")], ToolBatchContext())
    assert [result.tool_call_id for result in await batch.results()] == ["done"]

    await batch.cancel_and_settle(timeout=0)

    assert toolset._execution.poisoned is False  # pyright: ignore[reportPrivateUsage]


async def test_cancelled_batch_does_not_commit_cross_step_dedup_state() -> None:
    stubborn = CancellationIgnoringTool()
    immediate = ImmediateTool()
    toolset = PythinkerToolset()
    toolset.add(stubborn)
    toolset.add(immediate)

    first = toolset.handle_batch(
        [_call("first", "Immediate")],
        ToolBatchContext(turn_id="turn", step_no=1),
    )
    await first.results()
    prior = first.summary.current_call_fingerprints

    cancelled = toolset.handle_batch(
        [_call("cancelled", "Stubborn")],
        ToolBatchContext(
            turn_id="turn",
            step_no=2,
            prior_call_fingerprints=prior,
        ),
    )
    await stubborn.started.wait()
    settlement = asyncio.create_task(cancelled.cancel_and_settle())
    await stubborn.cancel_seen.wait()
    stubborn.release.set()
    await settlement

    retry = toolset.handle_batch(
        [_call("retry", "Stubborn")],
        ToolBatchContext(
            turn_id="turn",
            step_no=2,
            prior_call_fingerprints=prior,
        ),
    )
    assert [result.tool_call_id for result in await retry.results()] == ["retry"]
    assert retry.summary.dedup_triggered is False
    assert retry.summary.consecutive_identical_call_count == 1
    assert stubborn.invocations == 2


async def test_timeout_poisons_new_batches_until_late_task_is_drained() -> None:
    stubborn = CancellationIgnoringTool()
    immediate = ImmediateTool()
    callbacks: list[str] = []
    toolset = PythinkerToolset()
    toolset.add(stubborn)
    toolset.add(immediate)
    batch = toolset.handle_batch(
        [_call("stubborn", "Stubborn")],
        ToolBatchContext(turn_id="turn", step_no=1),
        on_tool_result=lambda result: callbacks.append(result.tool_call_id),
    )
    await stubborn.started.wait()

    with pytest.raises(ToolCancellationTimeoutError, match="did not settle"):
        await batch.cancel_and_settle(timeout=0.01)

    assert toolset._execution.poisoned is True  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ToolCancellationTimeoutError, match="unavailable"):
        toolset.handle_batch(
            [_call("blocked", "Immediate")],
            ToolBatchContext(turn_id="turn", step_no=2),
        )
    assert immediate.invocations == 0
    assert callbacks == []

    stubborn.release.set()
    await stubborn.finished.wait()
    await _wait_until_recovered(toolset)

    recovery = toolset.handle_batch(
        [_call("recovered", "Immediate")],
        ToolBatchContext(turn_id="turn", step_no=2),
    )
    assert [result.tool_call_id for result in await recovery.results()] == ["recovered"]
    assert immediate.invocations == 1
    assert callbacks == []


async def test_timeout_keeps_previously_completed_snapshot_and_blocks_late_callback() -> None:
    stubborn = CancellationIgnoringTool()
    immediate = ImmediateTool()
    callbacks: list[str] = []
    toolset = PythinkerToolset()
    toolset.add(stubborn)
    toolset.add(immediate)
    batch = toolset.handle_batch(
        [_call("done", "Immediate"), _call("stubborn", "Stubborn")],
        ToolBatchContext(),
        on_tool_result=lambda result: callbacks.append(result.tool_call_id),
    )
    await stubborn.started.wait()
    for _ in range(100):
        if "done" in batch.completed_results:
            break
        await asyncio.sleep(0.001)

    with pytest.raises(ToolCancellationTimeoutError):
        await batch.cancel_and_settle(timeout=0.01)

    assert batch.completed_results == {
        "done": ToolResult(tool_call_id="done", return_value=ToolOk(output="done"))
    }
    assert callbacks == ["done"]
    stubborn.release.set()
    await stubborn.finished.wait()
    await _wait_until_recovered(toolset)
    await asyncio.sleep(0)
    assert callbacks == ["done"]


async def test_toolset_cleanup_waits_for_timed_out_engine_work() -> None:
    stubborn = CancellationIgnoringTool()
    toolset = PythinkerToolset()
    toolset.add(stubborn)
    batch = toolset.handle_batch([_call("stubborn", "Stubborn")], ToolBatchContext())
    await stubborn.started.wait()
    with pytest.raises(ToolCancellationTimeoutError):
        await batch.cancel_and_settle(timeout=0.01)

    cleanup_task = asyncio.create_task(toolset.cleanup())
    try:
        await asyncio.sleep(0)
        assert not cleanup_task.done()

        stubborn.release.set()
        await asyncio.wait_for(cleanup_task, timeout=1)
        assert stubborn.finished.is_set()
        assert toolset._execution.poisoned is False  # pyright: ignore[reportPrivateUsage]
    finally:
        stubborn.release.set()
        await asyncio.wait_for(stubborn.finished.wait(), timeout=1)
        await asyncio.gather(cleanup_task, return_exceptions=True)


async def test_toolset_cleanup_closes_mcp_before_reporting_engine_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_execution, "TOOL_CANCELLATION_TIMEOUT_SECONDS", 0.01)
    stubborn = CancellationIgnoringTool()
    client = RecordingClient()
    toolset = PythinkerToolset()
    toolset.add(stubborn)
    toolset._mcp_servers["recording"] = MCPServerInfo(  # pyright: ignore[reportPrivateUsage]
        status="connected",
        client=cast(Any, client),
        tools=[],
        resources=[],
        prompts=[],
    )
    batch = toolset.handle_batch([_call("stubborn", "Stubborn")], ToolBatchContext())
    await stubborn.started.wait()
    with pytest.raises(ToolCancellationTimeoutError):
        await batch.cancel_and_settle(timeout=0.01)

    try:
        with pytest.raises(ToolCancellationTimeoutError, match="cleanup did not settle"):
            await toolset.cleanup()
        assert client.closed.is_set()
    finally:
        stubborn.release.set()
        await asyncio.wait_for(stubborn.finished.wait(), timeout=1)
        await _wait_until_recovered(toolset)


async def test_completion_racing_cancellation_remains_in_snapshot() -> None:
    tool = CompleteAndCancelCallerTool()
    toolset = PythinkerToolset()
    toolset.add(tool)
    result = await step(
        MockChatProvider(
            [_call("completed", "CompleteAndCancel")],
            finish_reason="tool_calls",
        ),
        "",
        toolset,
        [],
    )
    waiter = asyncio.create_task(result.tool_results())
    tool.cancel_target = waiter.cancel

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert result.completed_tool_results == {
        "completed": ToolResult(
            tool_call_id="completed",
            return_value=ToolOk(output="completed before cancellation"),
        )
    }


async def test_repeated_caller_cancellation_cannot_detach_core_settlement() -> None:
    stubborn = CancellationIgnoringTool()
    toolset = PythinkerToolset()
    toolset.add(stubborn)
    result = await step(
        MockChatProvider([_call("stubborn", "Stubborn")], finish_reason="tool_calls"),
        "",
        toolset,
        [],
    )
    waiter = asyncio.create_task(result.tool_results())
    await stubborn.started.wait()

    waiter.cancel()
    await stubborn.cancel_seen.wait()
    waiter.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()

    stubborn.release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert stubborn.finished.is_set()
    assert toolset._execution.poisoned is False  # pyright: ignore[reportPrivateUsage]


async def test_core_surfaces_timeout_then_engine_recovers_without_task_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pythinker_core._STEP_CANCELLATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(tool_execution, "TOOL_CANCELLATION_TIMEOUT_SECONDS", 0.01)
    reports: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    stubborn = CancellationIgnoringTool()
    toolset = PythinkerToolset()
    toolset.add(stubborn)
    try:
        result = await step(
            MockChatProvider([_call("stubborn", "Stubborn")], finish_reason="tool_calls"),
            "",
            toolset,
            [],
        )
        waiter = asyncio.create_task(result.tool_results())
        await stubborn.started.wait()
        waiter.cancel()

        with pytest.raises(ToolCancellationTimeoutError):
            await waiter
        assert toolset._execution.poisoned is True  # pyright: ignore[reportPrivateUsage]

        stubborn.release.set()
        await stubborn.finished.wait()
        await _wait_until_recovered(toolset)
        await asyncio.sleep(0)
    finally:
        stubborn.release.set()
        loop.set_exception_handler(previous_handler)

    assert reports == []
