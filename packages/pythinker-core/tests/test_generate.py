import asyncio
from copy import deepcopy

import pytest

from pythinker_core import generate
from pythinker_core.chat_provider import (
    APIEmptyResponseError,
    APIStreamProtocolError,
    StreamedMessagePart,
)
from pythinker_core.chat_provider.mock import MockChatProvider
from pythinker_core.message import ImageURLPart, TextPart, ThinkPart, ToolCall, ToolCallPart


def test_generate():
    chat_provider = MockChatProvider(
        message_parts=[
            TextPart(text="Hello, "),
            TextPart(text="world"),
            TextPart(text="!"),
            ImageURLPart(image_url=ImageURLPart.ImageURL(url="https://example.com/image.png")),
            TextPart(text="Another text."),
            TextPart(text=""),
            ToolCall(
                id="get_weather#123",
                function=ToolCall.FunctionBody(name="get_weather", arguments=None),
            ),
            ToolCallPart(arguments_part="{"),
            ToolCallPart(arguments_part='"city":'),
            ToolCallPart(arguments_part='"Beijing"'),
            ToolCallPart(arguments_part="}"),
            ToolCallPart(arguments_part=None),
        ]
    )
    message = asyncio.run(generate(chat_provider, system_prompt="", tools=[], history=[])).message
    assert message.content == [
        TextPart(text="Hello, world!"),
        ImageURLPart(image_url=ImageURLPart.ImageURL(url="https://example.com/image.png")),
        TextPart(text="Another text."),
    ]
    assert message.tool_calls == [
        ToolCall(
            id="get_weather#123",
            function=ToolCall.FunctionBody(name="get_weather", arguments='{"city":"Beijing"}'),
        ),
    ]


def test_generate_with_callbacks():
    input_parts: list[StreamedMessagePart] = [
        TextPart(text="Hello, "),
        TextPart(text="world"),
        TextPart(text="!"),
        ToolCall(
            id="get_weather#123",
            function=ToolCall.FunctionBody(name="get_weather", arguments=None),
        ),
        ToolCallPart(arguments_part="{"),
        ToolCallPart(arguments_part='"city":'),
        ToolCallPart(arguments_part='"Beijing"'),
        ToolCallPart(arguments_part="}"),
        ToolCall(
            id="get_time#123",
            function=ToolCall.FunctionBody(name="get_time", arguments=""),
        ),
    ]
    chat_provider = MockChatProvider(message_parts=deepcopy(input_parts))

    output_parts: list[StreamedMessagePart] = []
    output_tool_calls: list[ToolCall] = []

    async def on_message_part(part: StreamedMessagePart):
        output_parts.append(part)

    async def on_tool_call(tool_call: ToolCall):
        output_tool_calls.append(tool_call)

    message = asyncio.run(
        generate(
            chat_provider,
            system_prompt="",
            tools=[],
            history=[],
            on_message_part=on_message_part,
            on_tool_call=on_tool_call,
        )
    ).message
    assert output_parts == input_parts
    assert output_tool_calls == message.tool_calls


async def test_tool_callbacks_wait_for_successful_terminal_assembly() -> None:
    parts: list[StreamedMessagePart] = [
        TextPart(text="working"),
        ToolCall(
            id="call_b",
            function=ToolCall.FunctionBody(name="second", arguments=""),
            stream_index=1,
        ),
        ToolCall(
            id="call_a",
            function=ToolCall.FunctionBody(name="first", arguments=""),
            stream_index=0,
        ),
        ToolCallPart(arguments_part="{}", stream_index=1),
        ToolCallPart(arguments_part="{}", stream_index=0),
    ]
    events: list[str] = []

    async def on_part(part: StreamedMessagePart) -> None:
        events.append(f"part:{type(part).__name__}")

    async def on_call(call: ToolCall) -> None:
        events.append(f"call:{call.id}")

    result = await generate(
        MockChatProvider(parts, finish_reason="tool_calls"),
        "",
        [],
        [],
        on_message_part=on_part,
        on_tool_call=on_call,
    )

    assert events == [
        "part:TextPart",
        "part:ToolCall",
        "part:ToolCall",
        "part:ToolCallPart",
        "part:ToolCallPart",
        "call:call_a",
        "call:call_b",
    ]
    assert [call.id for call in result.message.tool_calls or []] == ["call_a", "call_b"]


async def test_message_part_callback_receives_defensive_copy() -> None:
    source = ToolCall(
        id="call_1",
        function=ToolCall.FunctionBody(name="read", arguments="{}"),
        stream_index=0,
    )

    async def mutate_callback(part: StreamedMessagePart) -> None:
        if isinstance(part, ToolCall):
            part.function.name = "mutated"
            part.stream_index = 99

    result = await generate(
        MockChatProvider([source], finish_reason="tool_calls"),
        "",
        [],
        [],
        on_message_part=mutate_callback,
    )

    assert result.message.tool_calls == [
        ToolCall(id="call_1", function=ToolCall.FunctionBody(name="read", arguments="{}"))
    ]


async def test_protocol_error_without_callback_marks_output_unpublished() -> None:
    provider = MockChatProvider(
        [ToolCall(id="call_1", function=ToolCall.FunctionBody(name="read", arguments=""))],
        finish_reason="length",
    )

    with pytest.raises(APIStreamProtocolError) as caught:
        await generate(provider, "", [], [])

    assert caught.value.output_published is False
    assert caught.value.category == "truncated_tool_call"


async def test_protocol_error_after_successful_callback_marks_output_published() -> None:
    provider = MockChatProvider(
        [ToolCall(id="call_1", function=ToolCall.FunctionBody(name="read", arguments=""))],
        finish_reason="length",
    )
    published: list[StreamedMessagePart] = []

    async def on_part(part: StreamedMessagePart) -> None:
        published.append(part)

    with pytest.raises(APIStreamProtocolError) as caught:
        await generate(provider, "", [], [], on_message_part=on_part)

    assert published
    assert caught.value.output_published is True
    assert caught.value.category == "truncated_tool_call"


async def test_callback_exception_does_not_become_protocol_error() -> None:
    provider = MockChatProvider([TextPart(text="visible")], finish_reason="stop")

    async def fail_callback(_part: StreamedMessagePart) -> None:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        await generate(provider, "", [], [], on_message_part=fail_callback)


async def test_add_time_protocol_error_gets_available_response_id() -> None:
    provider = MockChatProvider(
        [
            ToolCall(
                id="call_1",
                function=ToolCall.FunctionBody(name="read", arguments=""),
                stream_index=0,
            ),
            ToolCallPart(
                arguments_part="private arguments",
                stream_index=1,
                stream_call_id="call_1",
            ),
        ],
        finish_reason="tool_calls",
    )

    with pytest.raises(APIStreamProtocolError) as caught:
        await generate(provider, "", [], [])

    assert caught.value.category == "conflicting_identity"
    assert caught.value.response_id == "mock"
    assert caught.value.output_published is False
    assert "private arguments" not in str(caught.value)
    assert "reasoning" not in str(caught.value).lower()


def test_text_only_length_still_returns_truncated_result():
    """A response cut off by the output-token limit (finish_reason 'length') sets
    GenerateResult.truncated so the agent loop can detect and recover from truncation."""
    chat_provider = MockChatProvider(
        message_parts=[TextPart(text="a partial answer that got cut off")],
        finish_reason="length",
    )
    result = asyncio.run(generate(chat_provider, system_prompt="", tools=[], history=[]))
    assert result.truncated is True


def test_generate_not_truncated_by_default():
    """A normal completion is not marked truncated."""
    chat_provider = MockChatProvider(message_parts=[TextPart(text="a complete answer")])
    result = asyncio.run(generate(chat_provider, system_prompt="", tools=[], history=[]))
    assert result.truncated is False


def test_generate_not_truncated_on_explicit_stop():
    """An explicit clean finish_reason='stop' is not truncated — this pins the negative side
    of the contract so the suite can't pass only because the default happens to be falsy."""
    chat_provider = MockChatProvider(
        message_parts=[TextPart(text="a complete answer")],
        finish_reason="stop",
    )
    result = asyncio.run(generate(chat_provider, system_prompt="", tools=[], history=[]))
    assert result.truncated is False


async def test_length_with_tool_state_raises_protocol_error() -> None:
    provider = MockChatProvider(
        [ToolCall(id="call_1", function=ToolCall.FunctionBody(name="read", arguments="{}"))],
        finish_reason="length",
    )

    with pytest.raises(APIStreamProtocolError) as caught:
        await generate(provider, "", [], [])

    assert caught.value.category == "truncated_tool_call"


@pytest.mark.parametrize(
    "finish_reason",
    [
        "content_filter",
        "incomplete",
        "failed",
        "cancelled",
        "network_error",
        "model_context_window_exceeded",
        "pause_turn",
        "refusal",
        "sensitive",
        "provider_future_failure",
    ],
)
async def test_each_supported_terminal_failure_reason_rejects_tool_state(
    finish_reason: str,
) -> None:
    calls: list[ToolCall] = []

    async def on_call(call: ToolCall) -> None:
        calls.append(call)

    provider = MockChatProvider(
        [ToolCall(id="call_1", function=ToolCall.FunctionBody(name="read", arguments="{}"))],
        finish_reason=finish_reason,
    )

    with pytest.raises(APIStreamProtocolError) as caught:
        await generate(provider, "", [], [], on_tool_call=on_call)

    assert caught.value.category == "terminal_failure"
    assert calls == []


@pytest.mark.parametrize(
    "finish_reason",
    [
        None,
        "stop",
        "tool_calls",
        "function_call",
        "tool_use",
        "end_turn",
        "stop_sequence",
        "completed",
    ],
)
async def test_known_success_and_none_terminal_reasons_allow_complete_calls(
    finish_reason: str | None,
) -> None:
    provider = MockChatProvider(
        [ToolCall(id="call_1", function=ToolCall.FunctionBody(name="read", arguments="{}"))],
        finish_reason=finish_reason,
    )

    result = await generate(provider, "", [], [])

    assert [call.id for call in result.message.tool_calls or []] == ["call_1"]
    assert result.truncated is False


def test_generate_think_only_raises_error():
    """Think-only response (no text, no tool calls) should raise APIEmptyResponseError."""
    chat_provider = MockChatProvider(
        message_parts=[
            ThinkPart(think="Deep thinking about the problem..."),
        ]
    )
    with pytest.raises(APIEmptyResponseError, match="only thinking content"):
        asyncio.run(generate(chat_provider, system_prompt="", tools=[], history=[]))


def test_generate_think_with_empty_text_raises_error():
    """ThinkPart + empty/whitespace TextPart should also raise APIEmptyResponseError."""
    chat_provider = MockChatProvider(
        message_parts=[
            ThinkPart(think="Thinking..."),
            TextPart(text="  \n  "),
        ]
    )
    with pytest.raises(APIEmptyResponseError, match="only thinking content"):
        asyncio.run(generate(chat_provider, system_prompt="", tools=[], history=[]))


def test_generate_think_with_text_succeeds():
    """ThinkPart + real TextPart should succeed normally."""
    chat_provider = MockChatProvider(
        message_parts=[
            ThinkPart(think="Let me think..."),
            TextPart(text="Here is the answer."),
        ]
    )
    result = asyncio.run(generate(chat_provider, system_prompt="", tools=[], history=[]))
    assert any(isinstance(p, ThinkPart) for p in result.message.content)
    assert any(isinstance(p, TextPart) for p in result.message.content)


def test_generate_think_with_tool_calls_succeeds():
    """ThinkPart + tool calls (no text) should succeed — tools are valid output."""
    chat_provider = MockChatProvider(
        message_parts=[
            ThinkPart(think="I should call a tool..."),
            ToolCall(
                id="tool#1",
                function=ToolCall.FunctionBody(name="read_file", arguments='{"path": "/tmp"}'),
            ),
        ]
    )
    result = asyncio.run(generate(chat_provider, system_prompt="", tools=[], history=[]))
    assert any(isinstance(p, ThinkPart) for p in result.message.content)
    assert result.message.tool_calls
