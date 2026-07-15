from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import cast

import pytest
from pydantic import BaseModel
from pythinker_core.tooling import (
    BatchToolset,
    CallableTool2,
    ToolBatchContext,
    ToolBatchHandle,
    ToolOk,
    ToolResult,
    ToolReturnValue,
)

from pythinker_code.soul.tool_execution import ToolExecutionEngine
from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.wire.types import ToolCall


class DelayParams(BaseModel):
    label: str
    delay: float = 0


class DelayTool(CallableTool2[DelayParams]):
    name: str = "Delay"
    description: str = "Return a label after an optional delay"
    params: type[DelayParams] = DelayParams
    supports_parallel = True

    def __init__(self, invocations: list[str]) -> None:
        super().__init__()
        self._invocations = invocations

    async def __call__(self, params: DelayParams) -> ToolReturnValue:
        self._invocations.append(params.label)
        await asyncio.sleep(params.delay)
        return ToolOk(output=params.label)


def _call(call_id: str, *, label: str, delay: float = 0) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(
            name="Delay",
            arguments=f'{{"label":"{label}","delay":{delay}}}',
        ),
    )


def test_facade_retains_registry_while_engine_owns_execution_state() -> None:
    toolset = PythinkerToolset()
    tool = DelayTool([])
    toolset.add(tool)

    assert isinstance(toolset._execution, ToolExecutionEngine)  # pyright: ignore[reportPrivateUsage]
    assert toolset.find("Delay") is tool
    assert "_tool_dict" in vars(toolset)
    assert "_tool_dict" not in vars(toolset._execution)  # pyright: ignore[reportPrivateUsage]
    assert "_current_step_tasks" not in vars(toolset)
    assert "_current_step_tasks" in vars(
        toolset._execution  # pyright: ignore[reportPrivateUsage]
    )


def test_facade_handle_and_step_state_delegate_to_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    toolset = PythinkerToolset()
    call = _call("call-1", label="one")
    expected = ToolResult(tool_call_id=call.id, return_value=ToolOk(output="delegated"))
    handled: list[ToolCall] = []

    def fake_handle(received: ToolCall) -> ToolResult:
        handled.append(received)
        return expected

    monkeypatch.setattr(toolset._execution, "handle", fake_handle)  # pyright: ignore[reportPrivateUsage]

    assert toolset.handle(call) == expected
    assert handled == [call]

    toolset.begin_step([("Delay", '{"label":"before","delay":0}')], step_no=3, turn_id="t")
    assert toolset.end_step() == []
    assert toolset.dedup_triggered is False
    assert toolset.consecutive_repeat_count == 1


async def test_batch_handle_reports_completion_order_and_returns_model_order() -> None:
    invocations: list[str] = []
    callbacks: list[str] = []
    toolset = PythinkerToolset()
    toolset.add(DelayTool(invocations))
    calls = [
        _call("slow", label="slow", delay=0.02),
        _call("fast", label="fast", delay=0),
    ]

    batch = toolset.handle_batch(
        calls,
        ToolBatchContext(turn_id="turn", step_no=1),
        on_tool_result=lambda result: callbacks.append(result.tool_call_id),
    )
    results = await batch.results()

    assert isinstance(toolset, BatchToolset)
    assert isinstance(batch, ToolBatchHandle)
    assert callbacks == ["fast", "slow"]
    assert [result.tool_call_id for result in results] == ["slow", "fast"]
    assert set(batch.completed_results) == {"slow", "fast"}
    assert batch.summary.current_call_fingerprints == (
        ("Delay", '{"delay":0.02,"label":"slow"}'),
        ("Delay", '{"delay":0,"label":"fast"}'),
    )
    assert batch.summary.finalized is True
    assert invocations == ["slow", "fast"]


async def test_batch_same_step_duplicate_runs_original_once() -> None:
    invocations: list[str] = []
    toolset = PythinkerToolset()
    toolset.add(DelayTool(invocations))
    calls = [_call("one", label="same"), _call("two", label="same")]

    results = await toolset.handle_batch(calls, ToolBatchContext()).results()

    assert invocations == ["same"]
    assert [result.tool_call_id for result in results] == ["one", "two"]
    assert [result.return_value.output for result in results] == ["same", "same"]


class _ExplodingFunction:
    @property
    def name(self) -> str:
        raise RuntimeError("batch preparation failed")

    arguments = "{}"


class _ExplodingCall:
    id = "explode"
    function = _ExplodingFunction()


async def test_batch_construction_failure_starts_no_tool_work() -> None:
    invocations: list[str] = []
    callbacks: list[ToolResult] = []
    toolset = PythinkerToolset()
    toolset.add(DelayTool(invocations))
    calls = cast(Sequence[ToolCall], [_call("valid", label="valid"), _ExplodingCall()])

    with pytest.raises(RuntimeError, match="batch preparation failed"):
        toolset.handle_batch(
            calls,
            ToolBatchContext(),
            on_tool_result=cast(Callable[[ToolResult], None], callbacks.append),
        )

    await asyncio.sleep(0)
    assert invocations == []
    assert callbacks == []


async def test_batch_context_seeds_cross_step_dedup_summary() -> None:
    invocations: list[str] = []
    toolset = PythinkerToolset()
    toolset.add(DelayTool(invocations))
    prior = (("Delay", '{"delay":0,"label":"same"}'),)

    batch = toolset.handle_batch(
        [_call("one", label="same")],
        ToolBatchContext(
            turn_id="turn",
            step_no=2,
            prior_call_fingerprints=prior,
        ),
    )
    await batch.results()

    assert batch.summary.dedup_triggered is True
    assert batch.summary.consecutive_identical_call_count == 2
