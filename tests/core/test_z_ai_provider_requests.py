from __future__ import annotations

import json
from typing import cast

import pytest
import respx
from httpx import Response
from pydantic import SecretStr
from pythinker_core.chat_provider import ThinkingEffort
from pythinker_core.message import Message, TextPart, ThinkPart, ToolCall
from pythinker_core.tooling import Tool

from pythinker_code.auth.z_ai import ZAI_ROUTES, ZaiRoute
from pythinker_code.config import LLMModel, LLMProvider
from pythinker_code.llm import ModelCapability, create_llm


def _completion_response(model_id: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


async def _captured_body(
    *,
    route: ZaiRoute | None,
    provider_key: str,
    base_url: str,
    model_id: str,
    max_context_size: int,
    capabilities: set[ModelCapability] | None,
    effort: ThinkingEffort,
    history: list[Message],
    tools: list[Tool],
) -> dict[str, object]:
    provider = LLMProvider(
        type="openai_legacy",
        base_url=base_url,
        api_key=SecretStr("test-key"),
    )
    model = LLMModel(
        provider=provider_key,
        model=model_id,
        max_context_size=max_context_size,
        capabilities=capabilities,
    )
    llm = create_llm(provider, model, thinking_effort=effort)
    assert llm is not None
    llm.chat_provider.stream = False  # type: ignore[attr-defined]

    with respx.mock(base_url=base_url) as mock:
        endpoint = mock.post("/chat/completions").mock(
            return_value=Response(200, json=_completion_response(model_id))
        )
        try:
            stream = await llm.chat_provider.generate("", tools, history)
            async for _part in stream:
                pass
        finally:
            await llm.chat_provider.client.close()  # type: ignore[attr-defined]

    assert endpoint.called
    body = json.loads(endpoint.calls.last.request.content.decode())
    assert body["model"] == model_id
    if route is not None:
        assert endpoint.calls.last.request.url == f"{route.base_url}/chat/completions"
    return body


@pytest.mark.parametrize("route", ZAI_ROUTES)
@pytest.mark.parametrize(
    ("effort", "thinking", "reasoning_effort"),
    [
        ("off", {"type": "disabled"}, None),
        ("minimal", {"type": "disabled"}, None),
        ("low", {"type": "enabled", "clear_thinking": False}, "high"),
        ("medium", {"type": "enabled", "clear_thinking": False}, "high"),
        ("high", {"type": "enabled", "clear_thinking": False}, "high"),
        ("xhigh", {"type": "enabled", "clear_thinking": False}, "max"),
        ("max", {"type": "enabled", "clear_thinking": False}, "max"),
    ],
)
async def test_glm52_request_effort_mapping(
    route: ZaiRoute,
    effort: ThinkingEffort,
    thinking: dict[str, object],
    reasoning_effort: str | None,
) -> None:
    body = await _captured_body(
        route=route,
        provider_key=route.provider_key,
        base_url=route.base_url,
        model_id="glm-5.2",
        max_context_size=1_000_000,
        capabilities={"thinking"},
        effort=effort,
        history=[Message(role="user", content=[TextPart(text="Use no tools")])],
        tools=[],
    )

    assert body["thinking"] == thinking
    assert body.get("reasoning_effort") == reasoning_effort
    assert body["max_tokens"] == 131_072
    assert "tool_stream" not in body


@pytest.mark.parametrize("route", ZAI_ROUTES)
@pytest.mark.parametrize(
    ("model_id", "context_tokens", "max_tokens", "tool_stream"),
    [
        ("glm-5.1", 204_800, 131_072, True),
        ("glm-5", 204_800, 131_072, True),
        ("glm-5-turbo", 204_800, 131_072, True),
        ("glm-4.7", 204_800, 131_072, True),
        ("glm-4.5-air", 131_072, 98_304, False),
    ],
)
@pytest.mark.parametrize(
    ("effort", "thinking"),
    [
        ("off", {"type": "disabled"}),
        ("minimal", {"type": "disabled"}),
        ("high", {"type": "enabled", "clear_thinking": False}),
    ],
)
async def test_binary_curated_model_request_matrix(
    route: ZaiRoute,
    model_id: str,
    context_tokens: int,
    max_tokens: int,
    tool_stream: bool,
    effort: ThinkingEffort,
    thinking: dict[str, object],
) -> None:
    tool = Tool(
        name="read",
        description="Read a file",
        parameters={"type": "object", "properties": {}},
    )
    body = await _captured_body(
        route=route,
        provider_key=route.provider_key,
        base_url=route.base_url,
        model_id=model_id,
        max_context_size=context_tokens,
        capabilities={"thinking"},
        effort=effort,
        history=[Message(role="user", content="Read a file")],
        tools=[tool],
    )

    assert body["thinking"] == thinking
    assert "reasoning_effort" not in body
    assert body["max_tokens"] == max_tokens
    assert body.get("tool_stream") is (True if tool_stream else None)


@pytest.mark.parametrize("route", ZAI_ROUTES)
async def test_glm52_tool_stream_and_exact_reasoning_replay(route: ZaiRoute) -> None:
    tool_call = ToolCall(
        id="call_1",
        function=ToolCall.FunctionBody(name="read", arguments='{"path":"a.py"}'),
    )
    history = [
        Message(role="user", content="Read a.py"),
        Message(
            role="assistant",
            content=[
                ThinkPart(think="first\n"),
                ThinkPart(think="second"),
            ],
            tool_calls=[tool_call],
        ),
        Message(role="tool", tool_call_id="call_1", content="contents"),
    ]
    tool = Tool(
        name="read",
        description="Read a file",
        parameters={"type": "object", "properties": {}},
    )

    body = await _captured_body(
        route=route,
        provider_key=route.provider_key,
        base_url=route.base_url,
        model_id="glm-5.2",
        max_context_size=1_000_000,
        capabilities={"thinking"},
        effort="high",
        history=history,
        tools=[tool],
    )

    messages = cast(list[dict[str, object]], body["messages"])
    assistant = messages[1]
    assert assistant["reasoning_content"] == "first\nsecond"
    assert "[reasoning unavailable]" not in str(assistant)
    assert body["reasoning_effort"] == "high"
    assert body["tool_stream"] is True


@pytest.mark.parametrize("route", ZAI_ROUTES)
async def test_glm52_exact_replay_does_not_synthesize_missing_reasoning(
    route: ZaiRoute,
) -> None:
    history = [
        Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call_1",
                    function=ToolCall.FunctionBody(name="read", arguments="{}"),
                )
            ],
        )
    ]

    body = await _captured_body(
        route=route,
        provider_key=route.provider_key,
        base_url=route.base_url,
        model_id="glm-5.2",
        max_context_size=1_000_000,
        capabilities={"thinking"},
        effort="high",
        history=history,
        tools=[],
    )

    messages = cast(list[dict[str, object]], body["messages"])
    assistant = messages[0]
    assert "reasoning_content" not in assistant
    assert "[reasoning unavailable]" not in str(assistant)
    assert body["reasoning_effort"] != "medium"


@pytest.mark.parametrize(
    ("effort", "enabled"),
    [("off", False), ("high", True)],
)
async def test_self_hosted_qwen_uses_chat_template_thinking_toggle(
    effort: ThinkingEffort,
    enabled: bool,
) -> None:
    body = await _captured_body(
        route=None,
        provider_key="local",
        base_url="http://localhost:8080/v1",
        model_id="Qwen3.6-35B-A3B",
        max_context_size=262_144,
        capabilities={"thinking"},
        effort=effort,
        history=[Message(role="user", content="hello")],
        tools=[],
    )

    assert body["chat_template_kwargs"] == {"enable_thinking": enabled}
    assert "reasoning_effort" not in body


async def test_local_glm_name_has_no_zai_request_policy() -> None:
    body = await _captured_body(
        route=None,
        provider_key="local",
        base_url="http://localhost:8080/v1",
        model_id="glm-5.2",
        max_context_size=1_000_000,
        capabilities={"thinking"},
        effort="high",
        history=[Message(role="user", content="hello")],
        tools=[],
    )

    assert "thinking" not in body
    assert "reasoning_effort" not in body
    assert "tool_stream" not in body
    assert "max_tokens" not in body


@pytest.mark.parametrize("route", ZAI_ROUTES)
async def test_unknown_zai_model_has_conservative_request(route: ZaiRoute) -> None:
    body = await _captured_body(
        route=route,
        provider_key=route.provider_key,
        base_url=route.base_url,
        model_id="glm-future",
        max_context_size=131_072,
        capabilities=None,
        effort="high",
        history=[Message(role="user", content="hello")],
        tools=[],
    )

    assert "thinking" not in body
    assert "reasoning_effort" not in body
    assert "tool_stream" not in body
    assert "max_tokens" not in body
