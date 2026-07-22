from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Literal, Self, cast

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
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionToolCall,
    ResponseOutputItem,
    ResponseOutputItemAddedEvent,
    ResponseReasoningItem,
    ResponseStreamEvent,
)
from openai.types.responses.response import IncompleteDetails

from pythinker_core import generate
from pythinker_core.chat_provider import (
    APIStreamProtocolError,
    StreamedMessage,
    StreamedMessagePart,
    ThinkingEffort,
)
from pythinker_core.chat_provider.pythinker import PythinkerStreamedMessage
from pythinker_core.contrib.chat_provider.anthropic import AnthropicStreamedMessage
from pythinker_core.contrib.chat_provider.openai_legacy import OpenAILegacyStreamedMessage
from pythinker_core.contrib.chat_provider.openai_responses import OpenAIResponsesStreamedMessage
from pythinker_core.message import Message, ThinkPart, ToolCall, ToolCallPart
from pythinker_core.stream_message_assembler import StreamMessageAssembler
from pythinker_core.tooling import Tool


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
    output: list[ResponseOutputItem] | None = None,
) -> Response:
    return Response(
        id=response_id,
        created_at=1,
        model="gpt-5",
        object="response",
        output=[] if output is None else output,
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


async def _collect_parts(stream: object) -> list[StreamedMessagePart]:
    return [part async for part in cast(AsyncIterator[StreamedMessagePart], stream)]


class _StaticStreamProvider:
    name = "static-stream"

    def __init__(self, stream: StreamedMessage) -> None:
        self._stream = stream

    @property
    def model_name(self) -> str:
        return "static-stream"

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


@pytest.mark.parametrize("adapter", ["openai_legacy", "pythinker"])
async def test_openai_shaped_missing_id_and_late_name_finalize_deterministically(
    adapter: str,
) -> None:
    chunks = _async_events(
        _chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": None,
                    "type": "function",
                    "function": {"name": None, "arguments": None},
                }
            ]
        ),
        _chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": None,
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"a.py"}'},
                }
            ]
        ),
    )
    if adapter == "openai_legacy":
        stream: StreamedMessage = OpenAILegacyStreamedMessage(
            cast(AsyncStream[ChatCompletionChunk], chunks), reasoning_key=None
        )
    else:
        stream = PythinkerStreamedMessage(cast(AsyncStream[ChatCompletionChunk], chunks))
    raw_parts: list[ToolCall | ToolCallPart] = []

    async def on_part(part: StreamedMessagePart) -> None:
        if isinstance(part, (ToolCall, ToolCallPart)):
            raw_parts.append(part)

    result = await generate(
        _StaticStreamProvider(stream),
        "",
        [],
        [],
        on_message_part=on_part,
    )

    assert raw_parts == [
        ToolCall(
            id="",
            function=ToolCall.FunctionBody(name="", arguments=None),
            stream_index=0,
        ),
        ToolCallPart(
            arguments_part='{"path":"a.py"}',
            name_part="read",
            stream_index=0,
            stream_call_id=None,
        ),
    ]
    assert result.message.tool_calls == [
        ToolCall(
            id="call_fada958acfb8ed05ed05",
            function=ToolCall.FunctionBody(name="read", arguments='{"path":"a.py"}'),
        )
    ]


@pytest.mark.parametrize("adapter", ["openai_legacy", "pythinker"])
async def test_openai_shaped_late_id_without_function_content_is_preserved(
    adapter: str,
) -> None:
    chunks = _async_events(
        _chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": None,
                    "type": "function",
                    "function": {"name": "read", "arguments": ""},
                }
            ]
        ),
        _chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "late_call",
                    "type": "function",
                    "function": None,
                }
            ]
        ),
        _chat_chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": None,
                    "type": "function",
                    "function": {"name": None, "arguments": "{}"},
                }
            ]
        ),
    )
    if adapter == "openai_legacy":
        stream: StreamedMessage = OpenAILegacyStreamedMessage(
            cast(AsyncStream[ChatCompletionChunk], chunks), reasoning_key=None
        )
    else:
        stream = PythinkerStreamedMessage(cast(AsyncStream[ChatCompletionChunk], chunks))

    result = await generate(_StaticStreamProvider(stream), "", [], [])

    assert result.message.tool_calls == [
        ToolCall(
            id="late_call",
            function=ToolCall.FunctionBody(name="read", arguments="{}"),
        )
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


async def test_openai_responses_preserves_reasoning_summary_order_and_indices() -> None:
    events = _async_events(
        cast(
            ResponseStreamEvent,
            SimpleNamespace(type="response.reasoning_summary_part.added", summary_index=0),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                delta="Plan",
                summary_index=0,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(type="response.reasoning_summary_part.added", summary_index=2),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                delta="Check",
                summary_index=2,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                delta="Evaluate",
                summary_index=1,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                delta="Finish",
                summary_index=2,
            ),
        ),
    )
    stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))

    parts = [part for part in await _collect_parts(stream) if isinstance(part, ThinkPart)]

    assert [(part.think, part.summary_index) for part in parts] == [
        ("", 0),
        ("Plan", 0),
        ("", 2),
        ("Check", 2),
        ("Evaluate", 1),
        ("Finish", 2),
    ]


async def test_openai_responses_streamed_reasoning_done_encrypts_last_summary() -> None:
    events = _async_events(
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_part.added",
                item_id="reasoning_1",
                output_index=0,
                summary_index=0,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                item_id="reasoning_1",
                output_index=0,
                delta="Plan",
                summary_index=0,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_part.added",
                item_id="reasoning_1",
                output_index=0,
                summary_index=1,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                item_id="reasoning_1",
                output_index=0,
                delta="Check",
                summary_index=1,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.output_item.done",
                output_index=0,
                item=SimpleNamespace(
                    type="reasoning",
                    id="reasoning_1",
                    encrypted_content="enc_last",
                ),
            ),
        ),
    )
    stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))
    assembler = StreamMessageAssembler()

    for part in await _collect_parts(stream):
        assembler.add(part)
    message = assembler.finish(response_id=None, finish_reason="completed")

    assert message.content == [
        ThinkPart(think="Plan", summary_index=0),
        ThinkPart(think="Check", encrypted="enc_last", summary_index=1),
    ]


async def test_openai_responses_done_without_summary_emits_encrypted_part() -> None:
    events = _async_events(
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.output_item.done",
                output_index=4,
                item=SimpleNamespace(
                    type="reasoning",
                    id="reasoning_4",
                    encrypted_content="enc_orphan",
                ),
            ),
        ),
    )
    stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))

    parts = [part for part in await _collect_parts(stream) if isinstance(part, ThinkPart)]

    assert parts == [ThinkPart(think="", encrypted="enc_orphan", summary_index=None)]


async def test_openai_responses_streamed_reasoning_done_indices_do_not_leak_between_items() -> None:
    events = _async_events(
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                item_id="reasoning_1",
                output_index=0,
                delta="First",
                summary_index=0,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                item_id="reasoning_2",
                output_index=1,
                delta="Second",
                summary_index=2,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.output_item.done",
                output_index=0,
                item=SimpleNamespace(
                    type="reasoning",
                    id="reasoning_1",
                    encrypted_content="enc_first",
                ),
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.output_item.done",
                output_index="bad",
                item=SimpleNamespace(
                    type="reasoning",
                    id=None,
                    encrypted_content="enc_untracked",
                ),
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.output_item.done",
                output_index=1,
                item=SimpleNamespace(
                    type="reasoning",
                    id="reasoning_2",
                    encrypted_content="enc_second",
                ),
            ),
        ),
    )
    stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))

    parts = [part for part in await _collect_parts(stream) if isinstance(part, ThinkPart)]

    assert parts == [
        ThinkPart(think="First", summary_index=0),
        ThinkPart(think="Second", summary_index=2),
        ThinkPart(think="", encrypted="enc_first", summary_index=0),
        ThinkPart(think="", encrypted="enc_untracked"),
        ThinkPart(think="", encrypted="enc_second", summary_index=2),
    ]


async def test_openai_responses_reasoning_summary_invalid_indices_fallback_to_none() -> None:
    events = _async_events(
        cast(
            ResponseStreamEvent,
            SimpleNamespace(type="response.reasoning_summary_part.added", summary_index=True),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                delta="bool",
                summary_index=True,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                delta="negative",
                summary_index=-1,
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                delta="string",
                summary_index="3",
            ),
        ),
        cast(
            ResponseStreamEvent,
            SimpleNamespace(type="response.reasoning_summary_text.delta", delta="missing"),
        ),
    )
    stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))

    parts = [part for part in await _collect_parts(stream) if isinstance(part, ThinkPart)]

    assert [(part.think, part.summary_index) for part in parts] == [
        ("", None),
        ("bool", None),
        ("negative", None),
        ("string", None),
        ("missing", None),
    ]


async def test_openai_responses_completed_reasoning_summaries_keep_order_and_encryption() -> None:
    response = _response(
        output=[
            ResponseReasoningItem.model_validate(
                {
                    "type": "reasoning",
                    "id": "reasoning_1",
                    "summary": [
                        {"type": "summary_text", "text": "Plan"},
                        {"type": "summary_text", "text": "Check"},
                    ],
                    "encrypted_content": "enc_abc",
                }
            )
        ]
    )
    stream = OpenAIResponsesStreamedMessage(response)

    parts = [part for part in await _collect_parts(stream) if isinstance(part, ThinkPart)]

    assert parts == [
        ThinkPart(think="Plan", encrypted="enc_abc", summary_index=0),
        ThinkPart(think="Check", encrypted="enc_abc", summary_index=1),
    ]


async def test_openai_responses_empty_streamed_call_id_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_random_id() -> None:
        raise AssertionError("streamed Responses call used random ID fallback")

    monkeypatch.setattr(
        "pythinker_core.contrib.chat_provider.openai_responses.uuid.uuid4",
        reject_random_id,
    )

    async def generate_once() -> ToolCall:
        events = _async_events(
            ResponseCreatedEvent(
                response=_response(response_id="response_1"),
                sequence_number=0,
                type="response.created",
            ),
            ResponseOutputItemAddedEvent(
                item=ResponseFunctionToolCall(
                    arguments="{}",
                    call_id="",
                    id="output_item",
                    name="read",
                    status="completed",
                    type="function_call",
                ),
                output_index=3,
                sequence_number=1,
                type="response.output_item.added",
            ),
            ResponseCompletedEvent(
                response=_response(response_id="response_1"),
                sequence_number=2,
                type="response.completed",
            ),
        )
        stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))
        result = await generate(_StaticStreamProvider(stream), "", [], [])
        assert result.message.tool_calls is not None
        return result.message.tool_calls[0]

    first = await generate_once()
    second = await generate_once()

    assert first == second
    assert first.id.startswith("call_")
    assert first.function == ToolCall.FunctionBody(name="read", arguments="{}")


async def test_openai_responses_error_event_blocks_tool_callback() -> None:
    events = _async_events(
        ResponseCreatedEvent(
            response=_response(response_id="response_created"),
            sequence_number=0,
            type="response.created",
        ),
        ResponseOutputItemAddedEvent(
            item=ResponseFunctionToolCall(
                arguments="{}",
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
        ResponseErrorEvent(
            code="server_error",
            message="provider-private detail",
            param=None,
            sequence_number=2,
            type="error",
        ),
        ResponseCompletedEvent(
            response=_response(response_id="response_trailing", status="completed"),
            sequence_number=3,
            type="response.completed",
        ),
    )
    stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))
    callbacks: list[ToolCall] = []

    with pytest.raises(APIStreamProtocolError) as caught:
        await generate(
            _StaticStreamProvider(stream),
            "",
            [],
            [],
            on_tool_call=callbacks.append,
        )

    assert caught.value.category == "terminal_failure"
    assert "provider-private detail" not in str(caught.value)
    assert callbacks == []


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


async def test_openai_responses_generate_preserves_response_and_semantic_call_ids() -> None:
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
        ResponseFunctionCallArgumentsDeltaEvent(
            delta="{}",
            item_id="output_item",
            output_index=0,
            sequence_number=2,
            type="response.function_call_arguments.delta",
        ),
        ResponseCompletedEvent(
            response=_response(response_id="response_terminal", status="completed"),
            sequence_number=3,
            type="response.completed",
        ),
    )
    stream = OpenAIResponsesStreamedMessage(cast(AsyncStream[ResponseStreamEvent], events))

    result = await generate(_StaticStreamProvider(stream), "", [], [])

    assert result.id == "response_terminal"
    assert result.message.tool_calls == [
        ToolCall(
            id="semantic_call",
            function=ToolCall.FunctionBody(name="read", arguments="{}"),
        )
    ]


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
