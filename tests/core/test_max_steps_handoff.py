"""Graceful max-steps handoff (sysprompt-2).

When a turn hits the step ceiling, human-facing surfaces produce a brief,
tools-disabled handoff summary (what was done / what's left / next step) instead
of only the static "max steps reached" line, so the human who resumes does not
have to reconstruct state. The summary reuses the side-question (tools-denied)
mechanism, so it cannot itself re-hit the step ceiling or mutate the workspace.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pythinker_core.message import Message, ToolCall
from pythinker_core.tooling import ToolError, ToolResult

from pythinker_code.soul.btw import generate_max_steps_handoff
from pythinker_code.wire.types import TextPart


@dataclass
class _FakeStepResult:
    message: Message
    tool_calls: list[ToolCall]
    _tool_results: list[ToolResult] = field(default_factory=list)

    async def tool_results(self) -> list[ToolResult]:
        return self._tool_results


def _text_result(text: str) -> _FakeStepResult:
    return _FakeStepResult(message=Message(role="assistant", content=text), tool_calls=[])


def _tool_call_result() -> _FakeStepResult:
    tc = ToolCall(id="tc", function=ToolCall.FunctionBody(name="Shell", arguments="{}"))
    err = ToolResult(tool_call_id=tc.id, return_value=ToolError(message="denied", brief="denied"))
    return _FakeStepResult(
        message=Message(role="assistant", content=[], tool_calls=[tc]),
        tool_calls=[tc],
        _tool_results=[err],
    )


def _make_soul() -> MagicMock:
    soul = MagicMock()
    soul._runtime.llm.chat_provider = MagicMock()
    soul._agent.system_prompt = "sys"
    soul._agent.toolset.tools = []
    soul.context.history = []
    return soul


def test_handoff_returns_summary_text() -> None:
    soul = _make_soul()

    async def fake_step(provider, sys_prompt, toolset, history, **kw):
        if kw.get("on_message_part"):
            kw["on_message_part"](TextPart(text="Did X. Remaining: Y. Next: Z."))
        return _text_result("Did X. Remaining: Y. Next: Z.")

    with patch("pythinker_code.soul.btw.pythinker_core.step", side_effect=fake_step):
        summary = asyncio.run(generate_max_steps_handoff(soul))

    assert summary == "Did X. Remaining: Y. Next: Z."


def test_handoff_uses_step_limit_framing_not_side_question() -> None:
    """The handoff must carry the step-limit framing, not the side-question one."""
    soul = _make_soul()
    captured: dict[str, object] = {}

    async def fake_step(provider, sys_prompt, toolset, history, **kw):
        captured["history"] = history
        if kw.get("on_message_part"):
            kw["on_message_part"](TextPart(text="summary"))
        return _text_result("summary")

    with patch("pythinker_code.soul.btw.pythinker_core.step", side_effect=fake_step):
        asyncio.run(generate_max_steps_handoff(soul))

    history = captured["history"]
    assert isinstance(history, list)
    side_message_text = history[-1].extract_text(" ").lower()
    assert "step limit" in side_message_text
    assert "side question" not in side_message_text


def test_handoff_returns_none_when_summary_cannot_be_produced() -> None:
    """If the model only ever tries (denied) tool calls, return None so the
    caller falls back to the static max-steps line."""
    soul = _make_soul()

    async def fake_step(provider, sys_prompt, toolset, history, **kw):
        return _tool_call_result()

    with patch("pythinker_code.soul.btw.pythinker_core.step", side_effect=fake_step):
        summary = asyncio.run(generate_max_steps_handoff(soul))

    assert summary is None


@pytest.mark.asyncio
async def test_wire_server_streams_handoff_on_max_steps(
    runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wire clients get a handoff event and result field when the step ceiling hits."""
    from pythinker_core.tooling.simple import SimpleToolset

    from pythinker_code.soul import MaxStepsReached
    from pythinker_code.soul.agent import Agent
    from pythinker_code.soul.context import Context
    from pythinker_code.soul.pythinkersoul import PythinkerSoul
    from pythinker_code.wire.jsonrpc import JSONRPCPromptMessage, JSONRPCSuccessResponse
    from pythinker_code.wire.server import WireServer

    agent = Agent(
        name="Max Steps Wire Test",
        system_prompt="sys",
        toolset=SimpleToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    server = WireServer(soul)

    async def fake_run_soul(*_args, **_kwargs) -> None:
        raise MaxStepsReached(3)

    monkeypatch.setattr("pythinker_code.wire.server.run_soul", fake_run_soul)

    sent_events: list[object] = []
    original_send = server._send_msg

    async def capture_send(msg):  # type: ignore[no-untyped-def]
        if getattr(msg, "method", None) == "event":
            sent_events.append(msg.params)
        return await original_send(msg)

    monkeypatch.setattr(server, "_send_msg", capture_send)

    with patch(
        "pythinker_code.soul.btw.generate_max_steps_handoff",
        new=AsyncMock(return_value="Resume with step Z."),
    ):
        response = await server._handle_prompt(
            JSONRPCPromptMessage(
                id="1",
                params=JSONRPCPromptMessage.Params(user_input="hello"),
            )
        )

    assert isinstance(response, JSONRPCSuccessResponse)
    assert response.result == {
        "status": "max_steps_reached",
        "steps": 3,
        "handoff": "Resume with step Z.",
    }
    assert any(getattr(event, "text", "").endswith("Resume with step Z.") for event in sent_events)
