import pytest

from pythinker_core.chat_provider import APIStreamProtocolError
from pythinker_core.message import TextPart, ToolCall, ToolCallPart
from pythinker_core.stream_message_assembler import StreamMessageAssembler


def test_interleaved_indexed_tool_calls_assemble_independently() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(
            id="call_a",
            function=ToolCall.FunctionBody(name="first", arguments=""),
            stream_index=0,
        )
    )
    assembler.add(
        ToolCall(
            id="call_b",
            function=ToolCall.FunctionBody(name="second", arguments=""),
            stream_index=1,
        )
    )
    assembler.add(ToolCallPart(arguments_part='{"x":', stream_index=0))
    assembler.add(ToolCallPart(arguments_part='{"y":', stream_index=1))
    assembler.add(ToolCallPart(arguments_part="1}", stream_index=0))
    assembler.add(ToolCallPart(arguments_part="2}", stream_index=1))

    message = assembler.finish(response_id="resp_1", finish_reason="tool_calls")

    assert message.tool_calls == [
        ToolCall(
            id="call_a",
            function=ToolCall.FunctionBody(name="first", arguments='{"x":1}'),
        ),
        ToolCall(
            id="call_b",
            function=ToolCall.FunctionBody(name="second", arguments='{"y":2}'),
        ),
    ]


def test_indexed_calls_return_in_ascending_index_order() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(
            id="call_2",
            function=ToolCall.FunctionBody(name="second", arguments="{}"),
            stream_index=2,
        )
    )
    assembler.add(
        ToolCall(
            id="call_0",
            function=ToolCall.FunctionBody(name="first", arguments="{}"),
            stream_index=0,
        )
    )

    message = assembler.finish(response_id=None, finish_reason="stop")

    assert [call.id for call in message.tool_calls or []] == ["call_0", "call_2"]


def test_id_only_calls_preserve_first_seen_order() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(id="second", function=ToolCall.FunctionBody(name="second", arguments="{}"))
    )
    assembler.add(
        ToolCall(id="first", function=ToolCall.FunctionBody(name="first", arguments="{}"))
    )

    message = assembler.finish(response_id=None, finish_reason="tool_calls")

    assert [call.id for call in message.tool_calls or []] == ["second", "first"]


def test_indexed_fragment_before_start_is_buffered() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(ToolCallPart(arguments_part='{"value":', stream_index=4))
    assembler.add(
        ToolCall(
            id="late",
            function=ToolCall.FunctionBody(name="late_start", arguments="1}"),
            stream_index=4,
        )
    )

    message = assembler.finish(response_id=None, finish_reason="completed")

    assert message.tool_calls == [
        ToolCall(
            id="late",
            function=ToolCall.FunctionBody(name="late_start", arguments='{"value":1}'),
        )
    ]


def test_repeated_matching_identity_and_complete_name_are_accepted() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(
            id="same",
            function=ToolCall.FunctionBody(name="lookup", arguments=""),
            stream_index=0,
        )
    )
    assembler.add(
        ToolCall(
            id="same",
            function=ToolCall.FunctionBody(name="lookup", arguments="{}"),
            stream_index=0,
        )
    )

    message = assembler.finish(response_id=None, finish_reason="function_call")

    assert message.tool_calls == [
        ToolCall(id="same", function=ToolCall.FunctionBody(name="lookup", arguments="{}"))
    ]


def test_correlated_name_fragments_assemble_in_order() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(ToolCallPart(name_part="look", stream_index=0))
    assembler.add(
        ToolCall(id="call_1", function=ToolCall.FunctionBody(name="", arguments=""), stream_index=0)
    )
    assembler.add(ToolCallPart(name_part="up", arguments_part="{}", stream_index=0))

    message = assembler.finish(response_id=None, finish_reason="tool_use")

    assert message.tool_calls == [
        ToolCall(id="call_1", function=ToolCall.FunctionBody(name="lookup", arguments="{}"))
    ]


def test_complete_and_fragmented_name_must_agree() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(
            id="call_1",
            function=ToolCall.FunctionBody(name="lookup", arguments=""),
            stream_index=0,
        )
    )
    assembler.add(ToolCallPart(name_part="look", stream_index=0))
    assembler.add(ToolCallPart(name_part="up", arguments_part="{}", stream_index=0))

    message = assembler.finish(response_id=None, finish_reason="end_turn")

    assert message.tool_calls == [
        ToolCall(id="call_1", function=ToolCall.FunctionBody(name="lookup", arguments="{}"))
    ]


def test_missing_arguments_normalize_to_empty_object() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(id="call_1", function=ToolCall.FunctionBody(name="lookup", arguments=None))
    )

    message = assembler.finish(response_id=None, finish_reason=None)

    assert message.tool_calls == [
        ToolCall(id="call_1", function=ToolCall.FunctionBody(name="lookup", arguments="{}"))
    ]


def test_missing_provider_id_gets_deterministic_message_local_id() -> None:
    first = StreamMessageAssembler()
    second = StreamMessageAssembler()
    for assembler in (first, second):
        assembler.add(
            ToolCall(id="", function=ToolCall.FunctionBody(name="lookup", arguments="{}"))
        )

    first_message = first.finish(response_id="response", finish_reason="stop_sequence")
    second_message = second.finish(response_id="response", finish_reason="stop_sequence")

    assert first_message.tool_calls == second_message.tool_calls
    assert first_message.tool_calls == [
        ToolCall(
            id="call_8cbfc88f6a403717b878",
            function=ToolCall.FunctionBody(name="lookup", arguments="{}"),
        )
    ]


def test_late_provider_id_replaces_missing_indexed_identity() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(id="", function=ToolCall.FunctionBody(name="lookup", arguments=""), stream_index=0)
    )
    assembler.add(ToolCallPart(arguments_part="{}", stream_index=0, stream_call_id="provider_id"))

    message = assembler.finish(response_id=None, finish_reason="tool_calls")

    assert message.tool_calls == [
        ToolCall(id="provider_id", function=ToolCall.FunctionBody(name="lookup", arguments="{}"))
    ]


def test_conflicting_late_provider_id_is_rejected() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(id="", function=ToolCall.FunctionBody(name="lookup", arguments=""), stream_index=0)
    )
    assembler.add(ToolCallPart(stream_index=0, stream_call_id="first"))

    with pytest.raises(APIStreamProtocolError) as caught:
        assembler.add(
            ToolCallPart(arguments_part="secret", stream_index=0, stream_call_id="second")
        )

    assert caught.value.category == "conflicting_identity"
    assert "secret" not in str(caught.value)


def test_unindexed_fragment_with_two_open_calls_is_ambiguous() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(ToolCall(id="a", function=ToolCall.FunctionBody(name="first", arguments="")))
    assembler.add(ToolCall(id="b", function=ToolCall.FunctionBody(name="second", arguments="")))

    with pytest.raises(APIStreamProtocolError) as caught:
        assembler.add(ToolCallPart(arguments_part="{}"))

    assert caught.value.category == "ambiguous_fragment"
    assert caught.value.call_id is None
    assert "{}" not in str(caught.value)


def test_conflicting_index_and_id_association_is_rejected() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(
            id="call_a",
            function=ToolCall.FunctionBody(name="first", arguments=""),
            stream_index=0,
        )
    )

    with pytest.raises(APIStreamProtocolError) as caught:
        assembler.add(
            ToolCallPart(arguments_part="private", stream_index=1, stream_call_id="call_a")
        )

    assert caught.value.category == "conflicting_identity"
    assert caught.value.stream_index == 1
    assert caught.value.call_id == "call_a"
    assert "private" not in str(caught.value)


def test_conflicting_complete_and_fragmented_name_is_rejected() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(
            id="call_1",
            function=ToolCall.FunctionBody(name="first", arguments="{}"),
            stream_index=0,
        )
    )
    assembler.add(ToolCallPart(name_part="second", stream_index=0))

    with pytest.raises(APIStreamProtocolError) as caught:
        assembler.finish(response_id=None, finish_reason="tool_calls")

    assert caught.value.category == "conflicting_name"


def test_conflicting_complete_name_is_rejected() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(
            id="call_1",
            function=ToolCall.FunctionBody(name="first", arguments=""),
            stream_index=0,
        )
    )

    with pytest.raises(APIStreamProtocolError) as caught:
        assembler.add(
            ToolCall(
                id="call_1",
                function=ToolCall.FunctionBody(name="second", arguments="private"),
                stream_index=0,
            )
        )

    assert caught.value.category == "conflicting_name"
    assert "private" not in str(caught.value)


def test_unresolved_keyed_fragment_is_rejected_at_finish() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(ToolCallPart(arguments_part="private", stream_index=7, stream_call_id="missing"))

    with pytest.raises(APIStreamProtocolError) as caught:
        assembler.finish(response_id="response", finish_reason="tool_calls")

    assert caught.value.category == "orphan_fragment"
    assert caught.value.response_id == "response"
    assert caught.value.stream_index == 7
    assert caught.value.call_id == "missing"
    assert "private" not in str(caught.value)


def test_mixed_indexed_and_unindexed_multi_call_starts_are_rejected() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(
            id="indexed",
            function=ToolCall.FunctionBody(name="first", arguments="{}"),
            stream_index=0,
        )
    )

    with pytest.raises(APIStreamProtocolError) as caught:
        assembler.add(
            ToolCall(id="legacy", function=ToolCall.FunctionBody(name="second", arguments="{}"))
        )

    assert caught.value.category == "mixed_correlation"


def test_empty_function_name_is_rejected() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(ToolCall(id="call_1", function=ToolCall.FunctionBody(name="", arguments="{}")))

    with pytest.raises(APIStreamProtocolError) as caught:
        assembler.finish(response_id=None, finish_reason="tool_calls")

    assert caught.value.category == "missing_name"


@pytest.mark.parametrize(
    ("finish_reason", "category"),
    [
        ("length", "truncated_tool_call"),
        ("content_filter", "terminal_failure"),
        ("incomplete", "terminal_failure"),
        ("failed", "terminal_failure"),
        ("cancelled", "terminal_failure"),
        ("network_error", "terminal_failure"),
        ("model_context_window_exceeded", "terminal_failure"),
        ("pause_turn", "terminal_failure"),
        ("refusal", "terminal_failure"),
        ("sensitive", "terminal_failure"),
        ("provider_new_reason", "terminal_failure"),
    ],
)
def test_terminal_failure_reasons_are_rejected(finish_reason: str, category: str) -> None:
    assembler = StreamMessageAssembler()
    assembler.add(
        ToolCall(id="call_1", function=ToolCall.FunctionBody(name="lookup", arguments="private"))
    )

    with pytest.raises(APIStreamProtocolError) as caught:
        assembler.finish(response_id="response", finish_reason=finish_reason)

    assert caught.value.category == category
    assert caught.value.output_published is False
    assert "private" not in str(caught.value)


def test_content_parts_assemble_independently_from_tool_calls() -> None:
    assembler = StreamMessageAssembler()
    assembler.add(TextPart(text="hello "))
    assembler.add(
        ToolCall(id="call_1", function=ToolCall.FunctionBody(name="lookup", arguments="{}"))
    )
    assembler.add(TextPart(text="world"))

    message = assembler.finish(response_id=None, finish_reason="completed")

    assert message.content == [TextPart(text="hello world")]
