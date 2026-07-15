import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Self, override

import pytest

from pythinker_core import step
from pythinker_core.chat_provider import (
    APIConnectionError,
    APIStreamProtocolError,
    StreamedMessage,
    StreamedMessagePart,
    ThinkingEffort,
    TokenUsage,
)
from pythinker_core.chat_provider.mock import MockChatProvider
from pythinker_core.message import Message, TextPart, ToolCall, ToolCallPart
from pythinker_core.tooling import (
    CallableTool,
    ParametersType,
    Tool,
    ToolOk,
    ToolResult,
    ToolResultFuture,
    ToolReturnValue,
)
from pythinker_core.tooling.error import ToolParseError
from pythinker_core.tooling.simple import SimpleToolset


class _RecordingToolset:
    def __init__(self) -> None:
        self.handle_count = 0

    @property
    def tools(self) -> list[Tool]:
        return []

    def handle(self, tool_call: ToolCall) -> ToolResult:
        self.handle_count += 1
        return ToolResult(tool_call_id=tool_call.id, return_value=ToolOk(output="ok"))


class _ScriptedStream:
    def __init__(
        self,
        parts: list[StreamedMessagePart],
        *,
        finish_reason: str | None = None,
        terminal_error: BaseException | None = None,
        block_after_parts: bool = False,
    ) -> None:
        self._parts = parts
        self._finish_reason = finish_reason
        self._terminal_error = terminal_error
        self._block_after_parts = block_after_parts
        self.entered_block = asyncio.Event()
        self._never = asyncio.Event()
        self._iter = self._stream()

    def __aiter__(self) -> AsyncIterator[StreamedMessagePart]:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _stream(self) -> AsyncIterator[StreamedMessagePart]:
        for part in self._parts:
            yield part
        if self._block_after_parts:
            self.entered_block.set()
            await self._never.wait()
        if self._terminal_error is not None:
            raise self._terminal_error

    @property
    def id(self) -> str:
        return "scripted-response"

    @property
    def usage(self) -> TokenUsage | None:
        return None

    @property
    def finish_reason(self) -> str | None:
        return self._finish_reason


class _StaticProvider:
    name = "static"

    def __init__(self, stream: StreamedMessage) -> None:
        self._stream = stream

    @property
    def model_name(self) -> str:
        return "static"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> StreamedMessage:
        return self._stream

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


def _tool_call(call_id: str, *, index: int) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name="tool", arguments="{}"),
        stream_index=index,
    )


def test_step():
    class PlusTool(CallableTool):
        name: str = "plus"
        description: str = "This is a plus tool"
        parameters: ParametersType = {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
        }

        @override
        async def __call__(self, a: int, b: int) -> ToolReturnValue:
            return ToolOk(output=str(a + b))

    plus_tool_call = ToolCall(
        id="plus#123",
        function=ToolCall.FunctionBody(name="plus", arguments='{"a": 1, "b": 2}'),
    )
    input_parts: list[StreamedMessagePart] = [
        TextPart(text="Hello, world!"),
        plus_tool_call,
    ]
    chat_provider = MockChatProvider(message_parts=input_parts)
    toolset = SimpleToolset([PlusTool()])

    output_parts: list[StreamedMessagePart] = []
    collected_tool_results: list[ToolResult] = []

    def on_message_part(part: StreamedMessagePart):
        output_parts.append(part)

    def on_tool_result(result: ToolResult):
        collected_tool_results.append(result)

    async def run():
        step_result = await step(
            chat_provider,
            system_prompt="",
            toolset=toolset,
            history=[],
            on_message_part=on_message_part,
            on_tool_result=on_tool_result,
        )
        tool_results = await step_result.tool_results()
        return step_result, tool_results

    step_result, tool_results = asyncio.run(run())
    assert step_result.message.content == [TextPart(text="Hello, world!")]
    assert step_result.tool_calls == [plus_tool_call]
    assert output_parts == input_parts
    assert tool_results == [ToolResult(tool_call_id="plus#123", return_value=ToolOk(output="3"))]
    assert collected_tool_results == tool_results


async def test_structurally_complete_invalid_json_remains_tool_parse_error() -> None:
    class NeverRunsTool(CallableTool):
        name: str = "never_runs"
        description: str = "Must not run when arguments are malformed."
        parameters: ParametersType = {"type": "object", "properties": {}}

        @override
        async def __call__(self) -> ToolReturnValue:
            raise AssertionError("invalid JSON reached tool execution")

    provider = MockChatProvider(
        [
            ToolCall(
                id="call_1",
                function=ToolCall.FunctionBody(name="never_runs", arguments="{not-json"),
            )
        ],
        finish_reason="tool_calls",
    )

    result = await step(provider, "", SimpleToolset([NeverRunsTool()]), [])
    tool_results = await result.tool_results()

    assert len(tool_results) == 1
    assert isinstance(tool_results[0].return_value, ToolParseError)


@pytest.mark.parametrize(
    "finish_reason",
    [
        "length",
        "content_filter",
        "incomplete",
        "failed",
        "cancelled",
        "network_error",
        "model_context_window_exceeded",
        "pause_turn",
        "refusal",
        "sensitive",
    ],
)
async def test_terminal_tool_failure_starts_no_tools(finish_reason: str) -> None:
    toolset = _RecordingToolset()
    callbacks: list[ToolResult] = []
    provider = MockChatProvider([_tool_call("call_1", index=0)], finish_reason=finish_reason)

    with pytest.raises(APIStreamProtocolError):
        await step(provider, "", toolset, [], on_tool_result=callbacks.append)

    assert toolset.handle_count == 0
    assert callbacks == []


async def test_malformed_correlation_starts_no_tools_and_does_not_leak_state() -> None:
    toolset = _RecordingToolset()
    callbacks: list[ToolResult] = []
    malformed = MockChatProvider(
        [
            _tool_call("call_1", index=0),
            ToolCallPart(
                arguments_part="private",
                stream_index=1,
                stream_call_id="call_1",
            ),
        ],
        finish_reason="tool_calls",
    )

    with pytest.raises(APIStreamProtocolError):
        await step(malformed, "", toolset, [], on_tool_result=callbacks.append)

    assert toolset.handle_count == 0
    assert callbacks == []

    recovered = await step(
        MockChatProvider([_tool_call("call_2", index=0)], finish_reason="tool_calls"),
        "",
        toolset,
        [],
    )
    assert await recovered.tool_results() == [
        ToolResult(tool_call_id="call_2", return_value=ToolOk(output="ok"))
    ]
    assert toolset.handle_count == 1


async def test_transport_error_after_partial_call_starts_no_tools() -> None:
    toolset = _RecordingToolset()
    callbacks: list[ToolResult] = []
    error = APIConnectionError("connection ended")
    stream = _ScriptedStream([_tool_call("call_1", index=0)], terminal_error=error)

    with pytest.raises(APIConnectionError) as caught:
        await step(
            _StaticProvider(stream),
            "",
            toolset,
            [],
            on_tool_result=callbacks.append,
        )

    assert caught.value is error
    assert toolset.handle_count == 0
    assert callbacks == []


async def test_cancellation_after_partial_call_starts_no_tools() -> None:
    toolset = _RecordingToolset()
    callbacks: list[ToolResult] = []
    stream = _ScriptedStream([_tool_call("call_1", index=0)], block_after_parts=True)
    running = asyncio.create_task(
        step(
            _StaticProvider(stream),
            "",
            toolset,
            [],
            on_tool_result=callbacks.append,
        )
    )
    await asyncio.wait_for(stream.entered_block.wait(), timeout=1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert toolset.handle_count == 0
    assert callbacks == []


class _PendingThenFailingToolset:
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


async def test_later_terminal_tool_dispatch_failure_cancels_earlier_future() -> None:
    toolset = _PendingThenFailingToolset()
    callbacks: list[ToolResult] = []
    provider = MockChatProvider(
        [_tool_call("call_1", index=0), _tool_call("call_2", index=1)],
        finish_reason="tool_calls",
    )

    with pytest.raises(RuntimeError, match="second dispatch failed"):
        await step(provider, "", toolset, [], on_tool_result=callbacks.append)

    assert toolset.first_future is not None
    assert toolset.first_future.cancelled()
    await asyncio.sleep(0)
    assert callbacks == []


class _ImmediateThenCancelledToolset:
    def __init__(self) -> None:
        self.handle_count = 0

    @property
    def tools(self) -> list[Tool]:
        return []

    def handle(self, tool_call: ToolCall) -> ToolResult:
        self.handle_count += 1
        if self.handle_count == 1:
            return ToolResult(tool_call_id=tool_call.id, return_value=ToolOk(output="done"))
        raise asyncio.CancelledError


async def test_dispatch_rollback_suppresses_queued_completed_callback() -> None:
    toolset = _ImmediateThenCancelledToolset()
    callbacks: list[ToolResult] = []
    provider = MockChatProvider(
        [_tool_call("call_1", index=0), _tool_call("call_2", index=1)],
        finish_reason="tool_calls",
    )

    with pytest.raises(asyncio.CancelledError):
        await step(provider, "", toolset, [], on_tool_result=callbacks.append)

    await asyncio.sleep(0)
    assert callbacks == []
