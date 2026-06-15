from __future__ import annotations

import pytest
from pythinker_core.message import Message, TextPart
from pythinker_core.tooling.simple import SimpleToolset

from pythinker_code.config import LoopControl
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.compaction import (
    cap_stale_tool_result_bodies,
    estimate_text_tokens,
)
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul


def _tool(text: str, call_id: str) -> Message:
    return Message(role="tool", content=text, tool_call_id=call_id)


def _old_history(message: Message) -> list[Message]:
    return [message, *[Message(role="user", content=f"m{i}") for i in range(8)]]


def test_below_budget_is_unchanged() -> None:
    history = _old_history(_tool("x" * 80, "c1"))

    capped, freed = cap_stale_tool_result_bodies(history, protect_last=2, max_chars=100)

    assert freed == 0
    assert capped == history


def test_old_large_tool_output_is_capped_with_placeholder() -> None:
    body = "a" * 120 + "END"
    history = _old_history(_tool(body, "c1"))

    capped, freed = cap_stale_tool_result_bodies(history, protect_last=2, max_chars=80)

    tool_msg = capped[0]
    text = tool_msg.extract_text("")
    assert freed == len(body) - len(text)
    assert len(text) <= 80
    assert text.startswith("a")
    assert "tool output capped" in text
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "c1"
    assert len(capped) == len(history)


def test_recent_protected_messages_are_preserved() -> None:
    body = "recent" * 40
    history = [Message(role="user", content="old"), _tool(body, "recent")]

    capped, freed = cap_stale_tool_result_bodies(history, protect_last=2, max_chars=50)

    assert freed == 0
    assert capped == history


def test_non_tool_messages_are_untouched() -> None:
    assistant = Message(role="assistant", content=[TextPart(text="a" * 200)])
    history = _old_history(assistant)

    capped, freed = cap_stale_tool_result_bodies(history, protect_last=2, max_chars=50)

    assert freed == 0
    assert capped == history


def test_freed_token_accounting_is_sane() -> None:
    history = _old_history(_tool("b" * 400, "c1"))

    capped, freed = cap_stale_tool_result_bodies(history, protect_last=2, max_chars=100)

    assert freed > 0
    assert estimate_text_tokens(capped) < estimate_text_tokens(history)


def test_loop_control_disables_microcompact_by_default() -> None:
    assert LoopControl().prune_tool_result_max_chars == 0


def _make_soul(runtime: Runtime, tmp_path) -> tuple[Context, PythinkerSoul]:
    agent = Agent(
        name="Microcompact", system_prompt="sys", toolset=SimpleToolset(), runtime=runtime
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    return context, PythinkerSoul(agent, context=context)


@pytest.mark.asyncio
async def test_prune_context_applies_tool_result_budget(runtime, tmp_path) -> None:
    runtime.config.loop_control.prune_protect_last = 2
    runtime.config.loop_control.prune_min_chars = 10_000
    runtime.config.loop_control.prune_tool_result_max_chars = 100
    context, soul = _make_soul(runtime, tmp_path)
    await context.write_system_prompt("sys")
    await context.append_message(
        [
            Message(role="user", content="go"),
            Message(role="tool", content="x" * 500, tool_call_id="c1"),
            Message(role="user", content="latest"),
            Message(role="assistant", content=[TextPart(text="done")]),
        ]
    )
    before = soul.context.token_count

    did_prune = await soul.prune_context()

    tool_msg = next(m for m in soul.context.history if m.role == "tool")
    text = tool_msg.extract_text("")
    assert did_prune is True
    assert len(text) <= 100
    assert "tool output capped" in text
    assert tool_msg.tool_call_id == "c1"
    assert soul.context.history[-1].extract_text("") == "done"
    assert soul.context.token_count <= before
