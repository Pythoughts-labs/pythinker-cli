"""Snapshot tests for OpenAI Responses API chat provider."""

import json
from typing import Any

import pytest
import respx
from common import COMMON_CASES, Case, capture_request, run_test_cases
from httpx import Response
from inline_snapshot import snapshot

from pythinker_core.contrib.chat_provider.openai_responses import OpenAIResponses
from pythinker_core.message import Message, TextPart, ThinkPart


def make_response() -> dict[str, Any]:
    return {
        "id": "resp_test123",
        "object": "response",
        "created_at": 1234567890,
        "status": "completed",
        "model": "gpt-4.1",
        "output": [
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello", "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


TEST_CASES: dict[str, Case] = {
    **COMMON_CASES,
    "assistant_with_reasoning": {
        "history": [
            Message(role="user", content="What is 2+2?"),
            Message(
                role="assistant",
                content=[
                    ThinkPart(think="Thinking...", encrypted="enc_abc"),
                    TextPart(text="4."),
                ],
            ),
            Message(role="user", content="Thanks!"),
        ],
    },
}


async def test_openai_responses_message_conversion():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(model="gpt-4.1", api_key="test-key", stream=False)
        results = await run_test_cases(mock, provider, TEST_CASES, ("input", "tools"))

        assert results == snapshot(
            {
                "simple_user_message": {
                    "input": [
                        {"role": "developer", "content": "You are helpful."},
                        {
                            "content": [{"type": "input_text", "text": "Hello!"}],
                            "role": "user",
                            "type": "message",
                        },
                    ],
                    "tools": [],
                },
                "multi_turn_conversation": {
                    "input": [
                        {
                            "content": [{"type": "input_text", "text": "What is 2+2?"}],
                            "role": "user",
                            "type": "message",
                        },
                        {
                            "content": [
                                {"type": "output_text", "text": "2+2 equals 4.", "annotations": []}
                            ],
                            "role": "assistant",
                            "type": "message",
                        },
                        {
                            "content": [{"type": "input_text", "text": "And 3+3?"}],
                            "role": "user",
                            "type": "message",
                        },
                    ],
                    "tools": [],
                },
                "multi_turn_with_system": {
                    "input": [
                        {"role": "developer", "content": "You are a math tutor."},
                        {
                            "content": [{"type": "input_text", "text": "What is 2+2?"}],
                            "role": "user",
                            "type": "message",
                        },
                        {
                            "content": [
                                {"type": "output_text", "text": "2+2 equals 4.", "annotations": []}
                            ],
                            "role": "assistant",
                            "type": "message",
                        },
                        {
                            "content": [{"type": "input_text", "text": "And 3+3?"}],
                            "role": "user",
                            "type": "message",
                        },
                    ],
                    "tools": [],
                },
                "image_url": {
                    "input": [
                        {
                            "content": [
                                {"type": "input_text", "text": "What's in this image?"},
                                {
                                    "type": "input_image",
                                    "detail": "auto",
                                    "image_url": "https://example.com/image.png",
                                },
                            ],
                            "role": "user",
                            "type": "message",
                        }
                    ],
                    "tools": [],
                },
                "tool_definition": {
                    "input": [
                        {
                            "content": [{"type": "input_text", "text": "Add 2 and 3"}],
                            "role": "user",
                            "type": "message",
                        }
                    ],
                    "tools": [
                        {
                            "type": "function",
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
                            "strict": False,
                        },
                        {
                            "type": "function",
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
                            "strict": False,
                        },
                    ],
                },
                "tool_call_with_image": {
                    "input": [
                        {
                            "content": [{"type": "input_text", "text": "Add 2 and 3"}],
                            "role": "user",
                            "type": "message",
                        },
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "I'll add those numbers for you.",
                                    "annotations": [],
                                }
                            ],
                            "role": "assistant",
                            "type": "message",
                        },
                        {
                            "arguments": '{"a": 2, "b": 3}',
                            "call_id": "call_abc123",
                            "name": "add",
                            "type": "function_call",
                        },
                        {
                            "call_id": "call_abc123",
                            "output": [
                                {"type": "input_text", "text": "5"},
                                {
                                    "type": "input_image",
                                    "image_url": "https://example.com/image.png",
                                },
                            ],
                            "type": "function_call_output",
                        },
                    ],
                    "tools": [],
                },
                "tool_call": {
                    "input": [
                        {
                            "content": [{"type": "input_text", "text": "Add 2 and 3"}],
                            "role": "user",
                            "type": "message",
                        },
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "I'll add those numbers for you.",
                                    "annotations": [],
                                }
                            ],
                            "role": "assistant",
                            "type": "message",
                        },
                        {
                            "arguments": '{"a": 2, "b": 3}',
                            "call_id": "call_abc123",
                            "name": "add",
                            "type": "function_call",
                        },
                        {
                            "call_id": "call_abc123",
                            "output": [{"type": "input_text", "text": "5"}],
                            "type": "function_call_output",
                        },
                    ],
                    "tools": [],
                },
                "parallel_tool_calls": {
                    "input": [
                        {
                            "content": [{"type": "input_text", "text": "Calculate 2+3 and 4*5"}],
                            "role": "user",
                            "type": "message",
                        },
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "I'll calculate both.",
                                    "annotations": [],
                                }
                            ],
                            "role": "assistant",
                            "type": "message",
                        },
                        {
                            "arguments": '{"a": 2, "b": 3}',
                            "call_id": "call_add",
                            "name": "add",
                            "type": "function_call",
                        },
                        {
                            "arguments": '{"a": 4, "b": 5}',
                            "call_id": "call_mul",
                            "name": "multiply",
                            "type": "function_call",
                        },
                        {
                            "call_id": "call_add",
                            "output": [
                                {
                                    "type": "input_text",
                                    "text": "<system-reminder>This is a system reminder"
                                    "</system-reminder>",
                                },
                                {"type": "input_text", "text": "5"},
                            ],
                            "type": "function_call_output",
                        },
                        {
                            "call_id": "call_mul",
                            "output": [
                                {
                                    "type": "input_text",
                                    "text": "<system-reminder>This is a system reminder"
                                    "</system-reminder>",
                                },
                                {"type": "input_text", "text": "20"},
                            ],
                            "type": "function_call_output",
                        },
                    ],
                    "tools": [
                        {
                            "type": "function",
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
                            "strict": False,
                        },
                        {
                            "type": "function",
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
                            "strict": False,
                        },
                    ],
                },
                "assistant_with_reasoning": {
                    "input": [
                        {
                            "content": [{"type": "input_text", "text": "What is 2+2?"}],
                            "role": "user",
                            "type": "message",
                        },
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "4.",
                                    "annotations": [],
                                }
                            ],
                            "role": "assistant",
                            "type": "message",
                        },
                        {
                            "content": [{"type": "input_text", "text": "Thanks!"}],
                            "role": "user",
                            "type": "message",
                        },
                    ],
                    "tools": [],
                },
            }
        )


async def test_openai_responses_can_send_system_prompt_as_instructions():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(
            model="gpt-5.5",
            api_key="test-key",
            stream=False,
            system_prompt_as_instructions=True,
        )

        body = await capture_request(
            mock,
            provider,
            "You are helpful.",
            [],
            [Message(role="user", content="Hi")],
        )

        assert body["instructions"] == "You are helpful."
        assert body["input"] == [
            {
                "content": [{"type": "input_text", "text": "Hi"}],
                "role": "user",
                "type": "message",
            }
        ]


async def test_openai_responses_generation_kwargs():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(
            model="gpt-4.1", api_key="test-key", stream=False
        ).with_generation_kwargs(temperature=0.7, max_output_tokens=2048)
        stream = await provider.generate("", [], [Message(role="user", content="Hi")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert (body["temperature"], body["max_output_tokens"]) == snapshot((0.7, 2048))


async def test_openai_responses_omits_reasoning_by_default():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(model="gpt-4.1", api_key="test-key", stream=False)
        stream = await provider.generate("", [], [Message(role="user", content="Hi")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert "reasoning" not in body
        assert "include" not in body


async def test_openai_responses_with_thinking_off_omits_reasoning():
    """with_thinking("off") should also omit reasoning from the request,
    since thinking_effort_to_reasoning_effort("off") returns None."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(model="gpt-4.1", api_key="test-key", stream=False).with_thinking(
            "off"
        )
        stream = await provider.generate("", [], [Message(role="user", content="Hi")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert "reasoning" not in body
        assert "include" not in body


async def test_openai_responses_with_thinking_low():
    """with_thinking("low") should send reasoning with effort="low"."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(model="gpt-4.1", api_key="test-key", stream=False).with_thinking(
            "low"
        )
        stream = await provider.generate("", [], [Message(role="user", content="Think")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["reasoning"] == snapshot({"effort": "low", "summary": "auto"})
        assert body["include"] == snapshot(["reasoning.encrypted_content"])


async def test_openai_responses_does_not_replay_stored_encrypted_reasoning():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(model="gpt-5.5", api_key="test-key", stream=False)
        stream = await provider.generate(
            "",
            [],
            [
                Message(role="user", content="Hi"),
                Message(
                    role="assistant",
                    content=[
                        ThinkPart(think="private reasoning", encrypted="stale-encrypted-content"),
                        TextPart(text="Hello"),
                    ],
                ),
                Message(role="user", content="Again"),
            ],
        )
        async for _ in stream:
            pass

        body = json.loads(mock.calls.last.request.content.decode())
        assert body["input"] == snapshot(
            [
                {
                    "content": [{"type": "input_text", "text": "Hi"}],
                    "role": "user",
                    "type": "message",
                },
                {
                    "content": [{"type": "output_text", "text": "Hello", "annotations": []}],
                    "role": "assistant",
                    "type": "message",
                },
                {
                    "content": [{"type": "input_text", "text": "Again"}],
                    "role": "user",
                    "type": "message",
                },
            ]
        )


async def test_openai_responses_with_thinking():
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(model="gpt-4.1", api_key="test-key", stream=False).with_thinking(
            "high"
        )
        stream = await provider.generate("", [], [Message(role="user", content="Think")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["reasoning"] == snapshot({"effort": "high", "summary": "auto"})


async def test_openai_responses_with_thinking_xhigh():
    """with_thinking("xhigh") must pass xhigh through — OpenAI supports it
    natively for models after gpt-5.1-codex-max. We use a model name from
    that supported set so the test aligns with its own rationale."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(
            model="gpt-5.1-codex-max", api_key="test-key", stream=False
        ).with_thinking("xhigh")
        stream = await provider.generate("", [], [Message(role="user", content="Think")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["reasoning"] == snapshot({"effort": "xhigh", "summary": "auto"})


async def test_openai_responses_with_thinking_max_clamps_to_xhigh():
    """Pythinker Core's "max" is Anthropic-specific; for OpenAI it clamps to xhigh
    (their highest level) rather than falling back to high."""
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(
            model="gpt-5.1-codex-max", api_key="test-key", stream=False
        ).with_thinking("max")
        stream = await provider.generate("", [], [Message(role="user", content="Think")])
        async for _ in stream:
            pass
        body = json.loads(mock.calls.last.request.content.decode())
        assert body["reasoning"] == snapshot({"effort": "xhigh", "summary": "auto"})


# Regression: local system-prompt roles must become developer messages regardless
# of model name. A model switch to gpt-5.5 produced
# `Error code: 400 - {'detail': 'System messages are not allowed'}` while the old
# conversion gate was tied to the openai SDK's lagging ResponsesModel literal.
@pytest.mark.parametrize(
    "model_name",
    [
        "gpt-5.5",  # in Pythinker catalog but missing from openai SDK ResponsesModel
        "gpt-5.3-codex",  # same — in catalog, missing from SDK
        "ft:gpt-5.5:my-org:custom:id",  # user-defined fine-tune, never in any SDK set
    ],
)
async def test_openai_responses_system_prompt_uses_developer_role(model_name: str):
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(model=model_name, api_key="test-key", stream=False)
        body = await capture_request(
            mock,
            provider,
            "You are a helpful assistant.",
            [],
            [Message(role="user", content="Hi")],
        )

    assert body["input"][0] == {
        "role": "developer",
        "content": "You are a helpful assistant.",
    }
    assert all(item.get("role") != "system" for item in body["input"])


async def test_openai_responses_history_system_message_becomes_developer():
    """Mid-session model switch via `_carry_context_to_session` seeds a
    role='system' summary into the new session's history. With an OpenAI Responses
    target (e.g. gpt-5.5), that history item must be re-mapped to role='developer'
    on the wire — otherwise the first request after the switch is rejected.
    """
    carried_summary = Message(
        role="system",
        content="Summary carried from the previous model session: user asked about X.",
    )
    with respx.mock(base_url="https://api.openai.com") as mock:
        mock.post("/v1/responses").mock(return_value=Response(200, json=make_response()))
        provider = OpenAIResponses(model="gpt-5.5", api_key="test-key", stream=False)
        body = await capture_request(
            mock,
            provider,
            "You are a helpful assistant.",
            [],
            [carried_summary, Message(role="user", content="continue")],
        )

    roles = [item.get("role") for item in body["input"]]
    assert "system" not in roles, body["input"]
    assert roles[0] == "developer"  # system_prompt
    assert roles[1] == "developer"  # carried-over summary
    assert roles[2] == "user"
