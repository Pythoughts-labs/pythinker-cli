from collections.abc import AsyncIterator
from typing import Literal, cast

import pytest
from anthropic import AsyncStream as AnthropicAsyncStream
from anthropic.types import (
    InputJSONDelta,
    MessageDeltaEvent,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawMessageStreamEvent,
    ToolUseBlock,
)
from anthropic.types.raw_message_delta_event import Delta
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk
from openai.types.responses import (
    Response,
    ResponseCreatedEvent,
    ResponseFailedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionToolCall,
    ResponseOutputItemAddedEvent,
    ResponseStreamEvent,
)
from openai.types.responses.response import IncompleteDetails

from pythinker_core.chat_provider.pythinker import PythinkerStreamedMessage
from pythinker_core.contrib.chat_provider.anthropic import AnthropicStreamedMessage
from pythinker_core.contrib.chat_provider.openai_legacy import OpenAILegacyStreamedMessage
from pythinker_core.contrib.chat_provider.openai_responses import OpenAIResponsesStreamedMessage
from pythinker_core.message import ToolCall, ToolCallPart


async def _async_events[T](*events: T) -> AsyncIterator[T]:
    for event in events:
        yield event


def _chat_chunk(*, tool_calls: list[dict[str, object]]) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate(
        {
            "id": "response_1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "provider-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": tool_calls},
                    "finish_reason": None,
                }
            ],
        }
    )


def _response(
    *,
    response_id: str = "response_1",
    status: Literal["completed", "failed", "cancelled", "incomplete"] = "completed",
    incomplete_reason: Literal["max_output_tokens", "content_filter"] | None = None,
) -> Response:
    return Response(
        id=response_id,
        created_at=1,
        model="gpt-5",
        object="response",
        output=[],
        parallel_tool_calls=True,
        tool_choice="auto",
        tools=[],
        status=status,
        incomplete_details=(
            IncompleteDetails(reason=incomplete_reason) if incomplete_reason is not None else None
        ),
    )


async def _collect(stream: object) -> list[ToolCall | ToolCallPart]:
    return [part async for part in cast(AsyncIterator[ToolCall | ToolCallPart], stream)]


async def test_openai_legacy_preserves_tool_call_index_and_id() -> None:
    chunks = _async_events(
        _chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": ""},
                }
            ]
        ),
        _chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": None, "arguments": '{"path":"a.py"}'},
                }
            ]
        ),
    )
    stream = OpenAILegacyStreamedMessage(
        cast(AsyncStream[ChatCompletionChunk], chunks), reasoning_key=None
    )

    assert await _collect(stream) == [
        ToolCall(
            id="call_1",
            function=ToolCall.FunctionBody(name="read", arguments=""),
            stream_index=0,
        ),
        ToolCallPart(
            arguments_part='{"path":"a.py"}',
            stream_index=0,
            stream_call_id="call_1",
        ),
    ]


async def test_pythinker_preserves_tool_call_index_and_id() -> None:
    chunks = _async_events(
        _chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": ""},
                }
            ]
        ),
        _chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": None, "arguments": '{"path":"a.py"}'},
                }
            ]
        ),
    )
    stream = PythinkerStreamedMessage(cast(AsyncStream[ChatCompletionChunk], chunks))

    assert await _collect(stream) == [
        ToolCall(
            id="call_1",
            function=ToolCall.FunctionBody(name="read", arguments=""),
            stream_index=0,
        ),
        ToolCallPart(
            arguments_part='{"path":"a.py"}',
            stream_index=0,
            stream_call_id="call_1",
        ),
    ]


async def test_openai_responses_uses_output_index_and_semantic_call_id() -> None:
    item = ResponseFunctionToolCall(
        arguments="",
        call_id="call_1",
        id="item_1",
        name="read",
        status="in_progress",
        type="function_call",
    )
    events = _async_events(
        ResponseOutputItemAddedEvent(
            item=item,
            output_index=3,
            sequence_number=1,
            type="response.output_item.added",
        ),
        ResponseFunctionCallArgumentsDeltaEvent(
            delta='{"path":"a.py"}',
            item_id="item_1",
            output_index=3,
            sequence_number=2,
            type="response.function_call_arguments.delta",
        ),
    )
    stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))

    assert await _collect(stream) == [
        ToolCall(
            id="call_1",
            function=ToolCall.FunctionBody(name="read", arguments=""),
            stream_index=3,
        ),
        ToolCallPart(
            arguments_part='{"path":"a.py"}',
            stream_index=3,
            stream_call_id=None,
        ),
    ]


async def test_openai_responses_keeps_response_id_separate_from_item_id() -> None:
    events = _async_events(
        ResponseCreatedEvent(
            response=_response(response_id="response_created"),
            sequence_number=0,
            type="response.created",
        ),
        ResponseOutputItemAddedEvent(
            item=ResponseFunctionToolCall(
                arguments="",
                call_id="semantic_call",
                id="output_item",
                name="read",
                status="in_progress",
                type="function_call",
            ),
            output_index=0,
            sequence_number=1,
            type="response.output_item.added",
        ),
        ResponseFailedEvent(
            response=_response(response_id="response_terminal", status="failed"),
            sequence_number=2,
            type="response.failed",
        ),
    )
    stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))

    parts = await _collect(stream)

    assert isinstance(parts[0], ToolCall)
    assert parts[0].id == "semantic_call"
    assert stream.id == "response_terminal"


class _AnthropicEventStream:
    def __init__(self, *events: RawMessageStreamEvent):
        self._events = _async_events(*events)

    async def __aenter__(self) -> "_AnthropicEventStream":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[RawMessageStreamEvent]:
        return self._events


async def test_anthropic_preserves_content_block_index() -> None:
    start = RawContentBlockStartEvent(
        type="content_block_start",
        index=4,
        content_block=ToolUseBlock(
            type="tool_use",
            id="call_1",
            name="read",
            input={},
        ),
    )
    delta = RawContentBlockDeltaEvent(
        type="content_block_delta",
        index=4,
        delta=InputJSONDelta(
            type="input_json_delta",
            partial_json='{"path":"a.py"}',
        ),
    )
    manager = _AnthropicEventStream(start, delta)
    stream = AnthropicStreamedMessage(cast(AnthropicAsyncStream[RawMessageStreamEvent], manager))

    assert await _collect(stream) == [
        ToolCall(
            id="call_1",
            function=ToolCall.FunctionBody(name="read", arguments=""),
            stream_index=4,
        ),
        ToolCallPart(arguments_part='{"path":"a.py"}', stream_index=4),
    ]


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (_response(status="incomplete", incomplete_reason="max_output_tokens"), "length"),
        (_response(status="incomplete", incomplete_reason="content_filter"), "content_filter"),
        (_response(status="failed"), "failed"),
        (_response(status="cancelled"), "cancelled"),
    ],
)
async def test_responses_normalizes_incomplete_failed_and_cancelled_reasons(
    response: Response, expected_reason: str
) -> None:
    stream = OpenAIResponsesStreamedMessage(response)

    async for _ in stream:
        pass

    assert stream.finish_reason == expected_reason


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        (_response(status="failed", incomplete_reason="max_output_tokens"), "failed"),
        (_response(status="cancelled", incomplete_reason="content_filter"), "cancelled"),
    ],
)
async def test_responses_explicit_status_takes_precedence_over_incomplete_details(
    response: Response, expected_reason: str
) -> None:
    stream = OpenAIResponsesStreamedMessage(response)

    async for _ in stream:
        pass

    assert stream.finish_reason == expected_reason


@pytest.mark.parametrize(
    ("stop_reason", "expected_reason"),
    [("pause_turn", "pause_turn"), ("refusal", "refusal")],
)
async def test_anthropic_exposes_pause_and_refusal_terminal_reasons(
    stop_reason: Literal["pause_turn", "refusal"], expected_reason: str
) -> None:
    event = MessageDeltaEvent(
        type="message_delta",
        delta=Delta(stop_reason=stop_reason, stop_sequence=None),
        usage=MessageDeltaUsage(output_tokens=1),
    )
    manager = _AnthropicEventStream(event)
    stream = AnthropicStreamedMessage(cast(AnthropicAsyncStream[RawMessageStreamEvent], manager))

    async for _ in stream:
        pass

    assert stream.finish_reason == expected_reason
