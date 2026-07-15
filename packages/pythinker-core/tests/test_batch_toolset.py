from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence

import pytest

from pythinker_core import StepResult, step
from pythinker_core.chat_provider import StreamedMessagePart
from pythinker_core.chat_provider.mock import MockChatProvider
from pythinker_core.message import Message, TextPart, ToolCall
from pythinker_core.tooling import (
    BatchToolset,
    Tool,
    ToolBatchContext,
    ToolBatchHandle,
    ToolBatchSummary,
    ToolCancellationTimeoutError,
    ToolOk,
    ToolResult,
    ToolResultFuture,
    Toolset,
)
from pythinker_core.tooling.simple import SimpleToolset


def _tool_call(call_id: str, name: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name=name, arguments="{}"),
    )


def _tool_result(call: ToolCall) -> ToolResult:
    return ToolResult(tool_call_id=call.id, return_value=ToolOk(output=call.function.name))


def _message_parts(*parts: StreamedMessagePart) -> list[StreamedMessagePart]:
    return list(parts)


class CompletedBatch:
    def __init__(
        self,
        calls: list[ToolCall],
        results: list[ToolResult],
        on_tool_result: Callable[[ToolResult], None] | None,
    ) -> None:
        self._calls = calls
        self._results = results
        self._on_tool_result = on_tool_result
        self._notified = False
        self._completed = {result.tool_call_id: result for result in reversed(results)}
        self._summary = ToolBatchSummary(
            current_call_fingerprints=tuple(
                (call.function.name, call.function.arguments or "{}") for call in calls
            ),
            dedup_triggered=False,
            consecutive_identical_call_count=1 if calls else 0,
        )
        self.cancelled = False

    @property
    def tool_calls(self) -> list[ToolCall]:
        return list(self._calls)

    @property
    def completed_results(self) -> dict[str, ToolResult]:
        return dict(self._completed)

    @property
    def summary(self) -> ToolBatchSummary:
        return self._summary

    async def results(self) -> list[ToolResult]:
        if not self._notified and self._on_tool_result is not None:
            self._notified = True
            for result in reversed(self._results):
                self._on_tool_result(result)
        return list(self._results)

    async def cancel_and_settle(self, *, timeout: float | None = None) -> None:
        del timeout
        self.cancelled = True


class RecordingBatchToolset:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace
        self.batch_calls = 0
        self.received_context: ToolBatchContext | None = None
        self.handle_count = 0
        self.batch: CompletedBatch | None = None

    @property
    def tools(self) -> list[Tool]:
        return []

    def handle(self, tool_call: ToolCall) -> ToolResult:
        self.handle_count += 1
        raise AssertionError(f"batch-capable toolset used handle for {tool_call.id}")

    def handle_batch(
        self,
        tool_calls: Sequence[ToolCall],
        context: ToolBatchContext,
        *,
        on_tool_result: Callable[[ToolResult], None] | None = None,
    ) -> ToolBatchHandle:
        self.trace.append("batch")
        self.batch_calls += 1
        self.received_context = context
        calls = list(tool_calls)
        self.batch = CompletedBatch(calls, [_tool_result(call) for call in calls], on_tool_result)
        return self.batch


async def test_step_uses_batch_toolset_once_after_all_message_parts() -> None:
    trace: list[str] = []
    toolset = RecordingBatchToolset(trace)
    calls = [_tool_call("call-1", "First"), _tool_call("call-2", "Second")]

    result = await step(
        MockChatProvider([TextPart(text="ready"), *calls], finish_reason="tool_calls"),
        "",
        toolset,
        [],
        on_message_part=lambda part: trace.append(f"part:{type(part).__name__}"),
        tool_batch_context=ToolBatchContext(turn_id="turn-1", step_no=2),
    )

    assert trace == ["part:TextPart", "part:ToolCall", "part:ToolCall", "batch"]
    assert toolset.batch_calls == 1
    assert toolset.handle_count == 0
    assert toolset.received_context == ToolBatchContext(turn_id="turn-1", step_no=2)
    assert await result.tool_results() == [_tool_result(call) for call in calls]


async def test_batch_callbacks_can_complete_out_of_order_but_results_stay_model_ordered() -> None:
    toolset = RecordingBatchToolset([])
    calls = [_tool_call("call-1", "First"), _tool_call("call-2", "Second")]
    callbacks: list[str] = []

    result = await step(
        MockChatProvider(_message_parts(*calls), finish_reason="tool_calls"),
        "",
        toolset,
        [],
        on_tool_result=lambda item: callbacks.append(item.tool_call_id),
    )

    ordered = await result.tool_results()
    await asyncio.sleep(0)
    assert callbacks == ["call-2", "call-1"]
    assert [item.tool_call_id for item in ordered] == ["call-1", "call-2"]


async def test_step_exposes_batch_summary() -> None:
    toolset = RecordingBatchToolset([])
    calls = [_tool_call("call-1", "First"), _tool_call("call-2", "Second")]

    result = await step(
        MockChatProvider(_message_parts(*calls), finish_reason="tool_calls"),
        "",
        toolset,
        [],
        tool_batch_context=ToolBatchContext(
            turn_id="turn-1",
            step_no=1,
            prior_call_fingerprints=(("Earlier", "{}"),),
        ),
    )

    assert result.tool_execution_summary == ToolBatchSummary(
        current_call_fingerprints=(("First", "{}"), ("Second", "{}")),
        dedup_triggered=False,
        consecutive_identical_call_count=1,
    )


async def test_empty_tool_call_response_returns_empty_summary() -> None:
    toolset = RecordingBatchToolset([])

    result = await step(MockChatProvider([TextPart(text="done")]), "", toolset, [])

    assert toolset.batch_calls == 1
    assert result.tool_calls == []
    assert await result.tool_results() == []
    assert result.tool_execution_summary == ToolBatchSummary(consecutive_identical_call_count=0)


class WaitingBatch:
    def __init__(self, calls: Sequence[ToolCall]) -> None:
        self._calls = list(calls)
        self.results_started = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.settle_release = asyncio.Event()
        self.settle_finished = asyncio.Event()
        self._never = asyncio.Event()
        self.cancel_count = 0

    @property
    def tool_calls(self) -> Sequence[ToolCall]:
        return self._calls

    @property
    def completed_results(self) -> Mapping[str, ToolResult]:
        return {}

    @property
    def summary(self) -> ToolBatchSummary:
        return ToolBatchSummary()

    async def results(self) -> list[ToolResult]:
        self.results_started.set()
        await self._never.wait()
        return []

    async def cancel_and_settle(self, *, timeout: float | None = None) -> None:
        del timeout
        self.cancel_count += 1
        self.cancel_started.set()
        await self.settle_release.wait()
        self.settle_finished.set()


class WaitingBatchToolset(RecordingBatchToolset):
    def __init__(self) -> None:
        super().__init__([])
        self.waiting: WaitingBatch | None = None

    def handle_batch(
        self,
        tool_calls: Sequence[ToolCall],
        context: ToolBatchContext,
        *,
        on_tool_result: Callable[[ToolResult], None] | None = None,
    ) -> WaitingBatch:
        del context, on_tool_result
        self.batch_calls += 1
        self.waiting = WaitingBatch(tool_calls)
        return self.waiting


async def test_tool_results_cancellation_delegates_to_batch_handle() -> None:
    toolset = WaitingBatchToolset()
    result = await step(
        MockChatProvider([_tool_call("call-1", "First")], finish_reason="tool_calls"),
        "",
        toolset,
        [],
    )
    assert toolset.waiting is not None
    waiter = asyncio.create_task(result.tool_results())
    await toolset.waiting.results_started.wait()

    waiter.cancel()
    await toolset.waiting.cancel_started.wait()
    toolset.waiting.settle_release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert toolset.waiting.cancel_count == 1
    assert toolset.waiting.settle_finished.is_set()


async def test_cancel_tool_execution_owns_batch_before_tool_results_starts() -> None:
    toolset = WaitingBatchToolset()
    result = await step(
        MockChatProvider([_tool_call("call-1", "First")], finish_reason="tool_calls"),
        "",
        toolset,
        [],
    )
    assert toolset.waiting is not None
    cancel = asyncio.create_task(result.cancel_tool_execution())
    await toolset.waiting.cancel_started.wait()
    toolset.waiting.settle_release.set()
    await cancel

    assert not toolset.waiting.results_started.is_set()
    assert toolset.waiting.settle_finished.is_set()


async def test_repeated_cancellation_during_settlement_never_detaches_cleanup() -> None:
    toolset = WaitingBatchToolset()
    result = await step(
        MockChatProvider([_tool_call("call-1", "First")], finish_reason="tool_calls"),
        "",
        toolset,
        [],
    )
    assert toolset.waiting is not None
    cancel = asyncio.create_task(result.cancel_tool_execution())
    await toolset.waiting.cancel_started.wait()

    cancel.cancel()
    await asyncio.sleep(0)
    cancel.cancel()
    await asyncio.sleep(0)
    assert not toolset.waiting.settle_finished.is_set()
    toolset.waiting.settle_release.set()

    with pytest.raises(asyncio.CancelledError):
        await cancel
    assert toolset.waiting.settle_finished.is_set()
    assert toolset.waiting.cancel_count == 1


async def test_direct_step_result_constructor_keeps_future_map_contract() -> None:
    call = _tool_call("call-1", "First")
    expected = _tool_result(call)
    future: ToolResultFuture = asyncio.get_running_loop().create_future()
    future.set_result(expected)
    result = StepResult(
        None,
        Message(role="assistant", content=[]),
        None,
        [call],
        {call.id: future},
    )

    assert await result.tool_results() == [expected]
    assert result.tool_execution_summary == ToolBatchSummary()


async def test_step_completed_results_snapshot_covers_batch_and_legacy_modes() -> None:
    batch_toolset = RecordingBatchToolset([])
    batch_result = await step(
        MockChatProvider([_tool_call("call-1", "First")], finish_reason="tool_calls"),
        "",
        batch_toolset,
        [],
    )
    assert batch_result.completed_tool_results == {
        "call-1": _tool_result(_tool_call("call-1", "First"))
    }

    done_call = _tool_call("done", "Done")
    pending_call = _tool_call("pending", "Pending")
    done: ToolResultFuture = asyncio.get_running_loop().create_future()
    done.set_result(_tool_result(done_call))
    pending: ToolResultFuture = asyncio.get_running_loop().create_future()
    legacy = StepResult(
        None,
        Message(role="assistant", content=[]),
        None,
        [done_call, pending_call],
        {"done": done, "pending": pending},
    )
    assert legacy.completed_tool_results == {"done": _tool_result(done_call)}
    await legacy.cancel_tool_execution()


class InlineCallbackToolset(RecordingBatchToolset):
    def handle_batch(
        self,
        tool_calls: Sequence[ToolCall],
        context: ToolBatchContext,
        *,
        on_tool_result: Callable[[ToolResult], None] | None = None,
    ) -> ToolBatchHandle:
        batch = super().handle_batch(tool_calls, context, on_tool_result=None)
        if on_tool_result is not None:
            on_tool_result(_tool_result(list(tool_calls)[0]))
        return batch


async def test_batch_immediate_callback_exception_is_reported_but_nonfatal() -> None:
    reports: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    try:
        result = await step(
            MockChatProvider([_tool_call("call-1", "First")], finish_reason="tool_calls"),
            "",
            InlineCallbackToolset([]),
            [],
            on_tool_result=lambda _result: (_ for _ in ()).throw(
                RuntimeError("private callback detail")
            ),
        )
        assert await result.tool_results() == [_tool_result(_tool_call("call-1", "First"))]
    finally:
        loop.set_exception_handler(previous)

    assert reports == [
        {
            "message": "Tool result callback failed",
            "exception_type": "RuntimeError",
        }
    ]


async def test_tool_results_waits_for_owned_async_callback() -> None:
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()

    async def blocking_callback(_result: ToolResult) -> None:
        callback_started.set()
        await callback_release.wait()

    result = await step(
        MockChatProvider([_tool_call("call-1", "First")], finish_reason="tool_calls"),
        "",
        RecordingBatchToolset([]),
        [],
        on_tool_result=blocking_callback,
    )
    results_task = asyncio.create_task(result.tool_results())
    await callback_started.wait()
    assert not results_task.done()

    callback_release.set()
    assert await results_task == [_tool_result(_tool_call("call-1", "First"))]


async def test_tool_results_cancellation_settles_owned_async_callback() -> None:
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def blocking_callback(_result: ToolResult) -> None:
        callback_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            callback_cancelled.set()

    result = await step(
        MockChatProvider([_tool_call("call-1", "First")], finish_reason="tool_calls"),
        "",
        RecordingBatchToolset([]),
        [],
        on_tool_result=blocking_callback,
    )
    results_task = asyncio.create_task(result.tool_results())
    await callback_started.wait()
    results_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await results_task
    assert callback_cancelled.is_set()


async def test_batch_async_callback_exception_is_reported_but_nonfatal() -> None:
    reports: list[dict[str, object]] = []
    reported = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def capture_report(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        reports.append(context)
        reported.set()

    loop.set_exception_handler(capture_report)

    async def failing_callback(_result: ToolResult) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("private async callback detail")

    try:
        result = await step(
            MockChatProvider([_tool_call("call-1", "First")], finish_reason="tool_calls"),
            "",
            RecordingBatchToolset([]),
            [],
            on_tool_result=failing_callback,
        )
        assert await result.tool_results() == [_tool_result(_tool_call("call-1", "First"))]
        await asyncio.wait_for(reported.wait(), timeout=1)
    finally:
        loop.set_exception_handler(previous)

    assert reports == [
        {
            "message": "Tool result callback failed",
            "exception_type": "RuntimeError",
        }
    ]


class ConstructionFailureToolset(RecordingBatchToolset):
    def __init__(self) -> None:
        super().__init__([])

    def handle_batch(
        self,
        tool_calls: Sequence[ToolCall],
        context: ToolBatchContext,
        *,
        on_tool_result: Callable[[ToolResult], None] | None = None,
    ) -> ToolBatchHandle:
        del tool_calls, context, on_tool_result
        raise RuntimeError("construction failed")


async def test_batch_construction_failure_propagates_without_callbacks() -> None:
    toolset = ConstructionFailureToolset()
    callbacks: list[ToolResult] = []

    with pytest.raises(RuntimeError, match="construction failed"):
        await step(
            MockChatProvider([_tool_call("call-1", "First")], finish_reason="tool_calls"),
            "",
            toolset,
            [],
            on_tool_result=callbacks.append,
        )

    assert callbacks == []


class PendingThenFailingToolset:
    def __init__(self) -> None:
        self.handle_count = 0
        self.first_future: ToolResultFuture | None = None

    @property
    def tools(self) -> list[Tool]:
        return []

    def handle(self, tool_call: ToolCall) -> ToolResult | ToolResultFuture:
        self.handle_count += 1
        if self.handle_count == 1:
            self.first_future = asyncio.get_running_loop().create_future()
            return self.first_future
        raise RuntimeError("second dispatch failed")


async def test_legacy_second_handle_cancellation_preserves_pr1_rollback() -> None:
    toolset = PendingThenFailingToolset()
    callbacks: list[ToolResult] = []

    with pytest.raises(RuntimeError, match="second dispatch failed"):
        await step(
            MockChatProvider(
                [_tool_call("call-1", "First"), _tool_call("call-2", "Second")],
                finish_reason="tool_calls",
            ),
            "",
            toolset,
            [],
            on_tool_result=callbacks.append,
        )

    assert toolset.first_future is not None
    assert toolset.first_future.cancelled()
    await asyncio.sleep(0)
    assert callbacks == []


class ImmediateThenCancelledToolset:
    def __init__(self) -> None:
        self.handle_count = 0

    @property
    def tools(self) -> list[Tool]:
        return []

    def handle(self, tool_call: ToolCall) -> ToolResult:
        self.handle_count += 1
        if self.handle_count == 1:
            return _tool_result(tool_call)
        raise asyncio.CancelledError()


async def test_legacy_dispatch_abort_preserves_pr1_queued_callback_guard() -> None:
    callbacks: list[ToolResult] = []
    with pytest.raises(asyncio.CancelledError):
        await step(
            MockChatProvider(
                [_tool_call("call-1", "First"), _tool_call("call-2", "Second")],
                finish_reason="tool_calls",
            ),
            "",
            ImmediateThenCancelledToolset(),
            [],
            on_tool_result=callbacks.append,
        )

    await asyncio.sleep(0)
    assert callbacks == []


def test_batch_protocol_is_optional_and_runtime_checkable() -> None:
    batch = RecordingBatchToolset([])
    simple = SimpleToolset()

    assert isinstance(batch, BatchToolset)
    assert isinstance(batch, Toolset)
    assert isinstance(simple, Toolset)
    assert not isinstance(simple, BatchToolset)
    assert isinstance(
        CompletedBatch([], [], None),
        ToolBatchHandle,
    )
    assert issubclass(ToolCancellationTimeoutError, RuntimeError)
