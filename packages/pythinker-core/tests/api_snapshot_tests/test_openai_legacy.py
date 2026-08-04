"""Snapshot tests for OpenAI Legacy (Chat Completions API) chat provider."""

import json

import pytest
import respx
from common import COMMON_CASES, Case, make_chat_completion_response, run_test_cases
from httpx import Response
from inline_snapshot import snapshot

from pythinker_core.contrib.chat_provider.common import ReasoningReplayMode
from pythinker_core.contrib.chat_provider.openai_legacy import OpenAILegacy
from pythinker_core.message import Message, TextPart, ThinkPart, ToolCall
from pythinker_core.tooling import Tool

TEST_CASES: dict[str, Case] = {**COMMON_CASES}


async def test_openai_legacy_message_conversion():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response("gpt-4.1"))
        )
        provider = OpenAILegacy(model="gpt-4.1", api_key="test-key", stream=False)
        results = await run_test_cases(mock, provider, TEST_CASES, ("messages", "tools"))

        assert results == snapshot(
            {
                "simple_user_message": {
                    "messages": [
                        {"role": "system", "content": "You are helpful."},
                        {"role": "user", "content": "Hello!"},
                    ],
                    "tools": [],
                },
                "multi_turn_conversation": {
                    "messages": [
                        {"role": "user", "content": "What is 2+2?"},
                        {"role": "assistant", "content": "2+2 equals 4."},
                        {"role": "user", "content": "And 3+3?"},
                    ],
                    "tools": [],
                },
                "multi_turn_with_system": {
                    "messages": [
                        {"role": "system", "content": "You are a math tutor."},
                        {"role": "user", "content": "What is 2+2?"},
                        {"role": "assistant", "content": "2+2 equals 4."},
                        {"role": "user", "content": "And 3+3?"},
                    ],
                    "tools": [],
                },
                "image_url": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "What's in this image?"},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "https://example.com/image.png",
                                        "id": None,
                                    },
                                },
                            ],
                        }
                    ],
                    "tools": [],
                },
                "tool_definition": {
                    "messages": [{"role": "user", "content": "Add 2 and 3"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "add",
                                "description": "Add two integers.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "a": {
                                            "type": "integer",
                                            "description": "First number",
                                        },
                                        "b": {
                                            "type": "integer",
                                            "description": "Second number",
                                        },
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                        },
                        {
                            "type": "function",
                            "function": {
                                "name": "multiply",
                                "description": "Multiply two integers.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "a": {"type": "integer", "description": "First number"},
                                        "b": {"type": "integer", "description": "Second number"},
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                        },
                    ],
                },
                "tool_call_with_image": {
                    "messages": [
                        {"role": "user", "content": "Add 2 and 3"},
                        {
                            "role": "assistant",
                            "content": "I'll add those numbers for you.",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": "call_abc123",
                                    "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "content": [
                                {"type": "text", "text": "5"},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "https://example.com/image.png",
                                        "id": None,
                                    },
                                },
                            ],
                            "tool_call_id": "call_abc123",
                        },
                    ],
                    "tools": [],
                },
                "tool_call": {
                    "messages": [
                        {"role": "user", "content": "Add 2 and 3"},
                        {
                            "role": "assistant",
                            "content": "I'll add those numbers for you.",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": "call_abc123",
                                    "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                                }
                            ],
                        },
                        {"role": "tool", "content": "5", "tool_call_id": "call_abc123"},
                    ],
                    "tools": [],
                },
                "parallel_tool_calls": {
                    "messages": [
                        {"role": "user", "content": "Calculate 2+3 and 4*5"},
                        {
                            "role": "assistant",
                            "content": "I'll calculate both.",
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "id": "call_add",
                                    "function": {
                                        "name": "add",
                                        "arguments": '{"a": 2, "b": 3}',
                                    },
                                },
                                {
                                    "type": "function",
                                    "id": "call_mul",
                                    "function": {
                                        "name": "multiply",
                                        "arguments": '{"a": 4, "b": 5}',
                                    },
                                },
                            ],
                        },
                        {
                            "role": "tool",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "<system-reminder>This is a system reminder"
                                    "</system-reminder>",
                                },
                                {"type": "text", "text": "5"},
                            ],
                            "tool_call_id": "call_add",
                        },
                        {
                            "role": "tool",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "<system-reminder>This is a system reminder"
                                    "</system-reminder>",
                                },
                                {"type": "text", "text": "20"},
                            ],
                            "tool_call_id": "call_mul",
                        },
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "add",
                                "description": "Add two integers.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "a": {"type": "integer", "description": "First number"},
                                        "b": {"type": "integer", "description": "Second number"},
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                        },
                        {
                            "type": "function",
                            "function": {
                                "name": "multiply",
                                "description": "Multiply two integers.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "a": {"type": "integer", "description": "First number"},
                                        "b": {"type": "integer", "description": "Second number"},
                                    },
                                    "required": ["a", "b"],
                                },
                            },
                        },
                    ],
                },
            }
        )


async def test_openai_legacy_reasoning_content():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="deepseek-reasoner",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="What is 2+2?"),
            Message(
                role="assistant",
                content=[ThinkPart(think="Thinking..."), TextPart(text="4.")],
            ),
            Message(role="user", content="Thanks!"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["messages"] == snapshot(
            [
                {"role": "user", "content": "What is 2+2?"},
                {
                    "role": "assistant",
                    "content": "4.",
                    "reasoning_content": "Thinking...",
                },
                {"role": "user", "content": "Thanks!"},
            ]
        )


async def test_openai_legacy_generation_kwargs():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="gpt-4.1", api_key="test-key", stream=False
        ).with_generation_kwargs(temperature=0.7, max_tokens=2048)
        stream = await provider.generate("", [], [Message(role="user", content="Hi")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert (body["temperature"], body["max_tokens"]) == snapshot((0.7, 2048))


async def test_openai_legacy_with_thinking():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(model="gpt-4.1", api_key="test-key", stream=False).with_thinking(
            "high"
        )
        stream = await provider.generate("", [], [Message(role="user", content="Think")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["reasoning_effort"] == snapshot("high")


async def test_openai_legacy_auto_reasoning_effort_when_history_has_think_part():
    """When reasoning_effort is not set but history contains ThinkPart and reasoning_key is
    configured, reasoning_effort should be auto-set to avoid server validation errors.

    Reproduces: https://github.com/Pythoughts-labs/pythinker-cli/issues/1616
    """
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        # Provider with reasoning_key but NO explicit reasoning_effort
        provider = OpenAILegacy(
            model="pythinker-ai",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="Hello"),
            Message(
                role="assistant",
                content=[ThinkPart(think="Let me think..."), TextPart(text="Hi!")],
            ),
            Message(role="user", content="How are you?"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        # reasoning_effort should be auto-set because history contains ThinkPart
        assert body["reasoning_effort"] == "medium"
        # reasoning_content should still be present in the message
        assert body["messages"][1]["reasoning_content"] == "Let me think..."


async def test_openai_legacy_no_auto_reasoning_effort_without_think_part():
    """When history has no ThinkPart, reasoning_effort should remain unset."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="pythinker-ai",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="Hello"),
            Message(role="assistant", content="Hi!"),
            Message(role="user", content="How are you?"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert "reasoning_effort" not in body


async def test_openai_legacy_no_auto_reasoning_effort_without_reasoning_key():
    """When reasoning_key is not configured, reasoning_effort should not be auto-set
    even if history has ThinkPart (ThinkPart would be silently dropped)."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        # No reasoning_key configured
        provider = OpenAILegacy(
            model="some-model",
            api_key="test-key",
            stream=False,
        )
        history = [
            Message(role="user", content="Hello"),
            Message(
                role="assistant",
                content=[ThinkPart(think="Thinking..."), TextPart(text="Hi!")],
            ),
            Message(role="user", content="How are you?"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert "reasoning_effort" not in body


async def test_openai_legacy_reasoning_content_forced_on_assistant_tool_calls():
    """Assistant messages carrying tool calls must include `reasoning_content` (even when
    empty) for Kimi-style providers that enforce it on history replay.

    Reproduces: "thinking is enabled but reasoning_content is missing in assistant tool
    call message at index N" errors when replaying tool-call history.
    """
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="kimi-k2.6",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="List files"),
            # Assistant turn with tool calls but NO ThinkPart — the failure mode.
            Message(
                role="assistant",
                content=[],
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        function=ToolCall.FunctionBody(name="ls", arguments="{}"),
                    ),
                ],
            ),
            Message(role="tool", tool_call_id="call_1", content="file1.txt"),
            Message(role="user", content="thanks"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assistant_msg = body["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg.get("tool_calls"), "tool_calls should be on the assistant msg"
        # The fix: reasoning_content is present and non-empty so compatibility
        # bridges that strip empty strings still forward valid replay metadata.
        assert "reasoning_content" in assistant_msg
        assert assistant_msg["reasoning_content"] == "[reasoning unavailable]"


async def test_openai_legacy_reasoning_content_not_forced_on_plain_assistant():
    """Generic OpenAI-compatible providers must not receive empty reasoning fields on
    plain assistant text turns. The stricter replay invariant is scoped to known
    interleaved-thinking providers like Kimi/DeepSeek."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="plain-model",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
            Message(role="user", content="bye"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert "reasoning_content" not in body["messages"][1]


@pytest.mark.parametrize(
    ("replay_mode", "expected_reasoning"),
    [
        ("exact", None),
        ("tool_calls", ""),
        ("strict_synthetic", "[reasoning unavailable]"),
    ],
)
async def test_openai_legacy_explicit_reasoning_replay_modes_override_model_inference(
    replay_mode: ReasoningReplayMode,
    expected_reasoning: str | None,
) -> None:
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="kimi-k2.6",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
            reasoning_replay_mode=replay_mode,
        )
        history = [
            Message(role="user", content="List files"),
            Message(
                role="assistant",
                content=[],
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        function=ToolCall.FunctionBody(name="ls", arguments="{}"),
                    )
                ],
            ),
        ]

        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass

        body = json.loads(mock.calls.last.request.content.decode())
        assistant = body["messages"][1]
        if expected_reasoning is None:
            assert "reasoning_content" not in assistant
        else:
            assert assistant["reasoning_content"] == expected_reasoning


async def test_openai_legacy_omitted_replay_mode_preserves_generic_tool_call_behavior() -> None:
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="plain-model",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(
                role="assistant",
                content=[],
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        function=ToolCall.FunctionBody(name="ls", arguments="{}"),
                    )
                ],
            )
        ]

        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass

        body = json.loads(mock.calls.last.request.content.decode())
        assert body["messages"][0]["reasoning_content"] == ""


async def test_openai_legacy_exact_replay_preserves_multiple_think_parts_without_auto_effort():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="glm-5.2",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
            reasoning_replay_mode="exact",
            auto_reasoning_effort=False,
        )
        history = [
            Message(
                role="assistant",
                content=[
                    ThinkPart(think="first\n"),
                    ThinkPart(think="second"),
                    TextPart(text="visible"),
                ],
            )
        ]

        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass

        body = json.loads(mock.calls.last.request.content.decode())
        assert body["messages"][0]["reasoning_content"] == "first\nsecond"
        assert "reasoning_effort" not in body


@pytest.mark.parametrize("with_tools", [False, True])
async def test_openai_legacy_tool_stream_merges_with_existing_extra_body(
    with_tools: bool,
) -> None:
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="glm-5.2",
            api_key="test-key",
            stream=False,
            tool_stream=True,
        ).with_generation_kwargs(
            extra_body={"thinking": {"type": "enabled", "clear_thinking": False}}
        )
        tools = (
            [
                Tool(
                    name="read",
                    description="Read a file",
                    parameters={"type": "object", "properties": {}},
                )
            ]
            if with_tools
            else []
        )

        stream = await provider.generate("", tools, [Message(role="user", content="hello")])
        async for _ in stream:
            pass

        body = json.loads(mock.calls.last.request.content.decode())
        assert body["thinking"] == {"type": "enabled", "clear_thinking": False}
        assert body.get("tool_stream") is (True if with_tools else None)
        assert provider._generation_kwargs == {  # pyright: ignore[reportPrivateUsage]
            "extra_body": {"thinking": {"type": "enabled", "clear_thinking": False}}
        }


async def test_openai_legacy_reasoning_content_forced_on_known_interleaved_plain_assistant():
    """Kimi/DeepSeek-style interleaved-thinking providers need consistent assistant
    replay metadata across turns, not only on turns where visible reasoning text exists."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/chat/completions").mock(
            return_value=Response(200, json=make_chat_completion_response())
        )
        provider = OpenAILegacy(
            model="kimi-k2.6",
            api_key="test-key",
            stream=False,
            reasoning_key="reasoning_content",
        )
        history = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
            Message(role="user", content="next"),
        ]
        stream = await provider.generate("", [], history)
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["messages"][1]["reasoning_content"] == "hello"
