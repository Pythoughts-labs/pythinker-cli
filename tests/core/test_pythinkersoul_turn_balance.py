from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pythinker_core import StepResult
from pythinker_core.message import Message, ToolCall
from pythinker_core.tooling import ToolResult
from pythinker_core.tooling.empty import EmptyToolset

import pythinker_code.soul.pythinkersoul as pythinkersoul_module
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.approval import Approval
from pythinker_code.soul.context import Context
from pythinker_code.soul.dynamic_injection import DynamicInjection
from pythinker_code.soul.pythinkersoul import PythinkerSoul, TurnOutcome
from pythinker_code.wire.types import StepBegin, StepInterrupted, TextPart, TurnBegin, TurnEnd


@pytest.fixture
def approval() -> Approval:
    """Override global yolo=True fixture; these tests only need wire semantics."""
    return Approval(yolo=False)


def _make_soul(runtime: Runtime, tmp_path: Path) -> PythinkerSoul:
    agent = Agent(
        name="Turn Balance Agent",
        system_prompt="Test prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


@pytest.mark.asyncio
async def test_run_emits_turn_end_when_step_interrupts(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    sent: list[object] = []

    async def fake_checkpoint() -> None:
        return None

    async def fake_step():
        raise RuntimeError("boom")

    monkeypatch.setattr(soul, "_checkpoint", fake_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)
    monkeypatch.setattr(soul, "_step", fake_step)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda msg: sent.append(msg))

    with pytest.raises(RuntimeError, match="boom"):
        await soul.run("hello")

    assert [msg for msg in sent if isinstance(msg, TurnBegin)] == [TurnBegin(user_input="hello")]
    assert [msg for msg in sent if isinstance(msg, StepBegin)] == [StepBegin(n=1)]
    assert [msg for msg in sent if isinstance(msg, StepInterrupted)] == [StepInterrupted()]
    assert [msg for msg in sent if isinstance(msg, TurnEnd)] == [TurnEnd()]
    assert isinstance(sent[-1], TurnEnd)


@pytest.mark.asyncio
async def test_run_emits_turn_end_on_cancelled_error(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    sent: list[object] = []

    async def fake_checkpoint() -> None:
        return None

    async def fake_step():
        raise asyncio.CancelledError()

    monkeypatch.setattr(soul, "_checkpoint", fake_checkpoint)
    monkeypatch.setattr(soul._denwa_renji, "set_n_checkpoints", lambda _n: None)
    monkeypatch.setattr(soul, "_step", fake_step)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda msg: sent.append(msg))

    with pytest.raises(asyncio.CancelledError):
        await soul.run("hello")

    assert [msg for msg in sent if isinstance(msg, TurnBegin)] == [TurnBegin(user_input="hello")]
    assert [msg for msg in sent if isinstance(msg, StepBegin)] == [StepBegin(n=1)]
    assert [msg for msg in sent if isinstance(msg, StepInterrupted)] == []
    assert [msg for msg in sent if isinstance(msg, TurnEnd)] == [TurnEnd()]
    assert isinstance(sent[-1], TurnEnd)


@pytest.mark.asyncio
async def test_run_does_not_duplicate_turn_end_for_blocked_prompt(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    sent: list[object] = []

    async def fake_trigger(*args, **kwargs):
        return [SimpleNamespace(action="block", reason="blocked by hook")]

    monkeypatch.setattr(soul._hook_engine, "trigger", fake_trigger)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda msg: sent.append(msg))

    await soul.run("hello")

    assert sent == [
        TurnBegin(user_input="hello"),
        TextPart(text="blocked by hook"),
        TurnEnd(),
    ]


@pytest.mark.asyncio
async def test_turn_appends_budget_nudge_after_crossing_ratio(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.config.loop_control.max_session_cost_usd = 1.0
    runtime.config.loop_control.budget_nudge_ratio = 0.75
    soul = _make_soul(runtime, tmp_path)
    soul._session_cost_usd = 0.70
    calls = 0

    async def fake_agent_loop() -> TurnOutcome:
        nonlocal calls
        calls += 1
        soul._session_cost_usd = 0.80
        final_message = Message(role="assistant", content=[TextPart(text="done")])
        await soul.context.append_message(final_message)
        return TurnOutcome(stop_reason="no_tool_calls", final_message=final_message, step_count=1)

    monkeypatch.setattr(soul, "_agent_loop", fake_agent_loop)
    monkeypatch.setattr(soul, "_checkpoint", AsyncMock())

    await soul.turn(Message(role="user", content=[TextPart(text="go")]))

    assert calls == 1  # nudge is context-only; it must not auto-continue the turn
    nudge = soul.context.history[-1]
    assert nudge.role == "user"
    text = nudge.extract_text(" ")
    assert "system-reminder" in text
    assert "75%" in text
    assert "spend ceiling" in text
    assert len(text) <= 500


@pytest.mark.asyncio
async def test_turn_does_not_append_budget_nudge_when_budget_exhausted(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.config.loop_control.max_session_cost_usd = 1.0
    runtime.config.loop_control.budget_nudge_ratio = 0.75
    soul = _make_soul(runtime, tmp_path)
    soul._session_cost_usd = 0.99

    async def fake_agent_loop() -> TurnOutcome:
        soul._session_cost_usd = 1.00
        final_message = Message(role="assistant", content=[TextPart(text="budget hit")])
        await soul.context.append_message(final_message)
        return TurnOutcome(
            stop_reason="budget_exhausted", final_message=final_message, step_count=1
        )

    monkeypatch.setattr(soul, "_agent_loop", fake_agent_loop)
    monkeypatch.setattr(soul, "_checkpoint", AsyncMock())

    await soul.turn(Message(role="user", content=[TextPart(text="go")]))

    assert "budget hit" in soul.context.history[-1].extract_text(" ")
    assert "system-reminder" not in soul.context.history[-1].extract_text(" ")


@pytest.mark.asyncio
async def test_turn_does_not_append_budget_nudge_when_goal_auto_continue_enabled(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.config.loop_control.max_session_cost_usd = 1.0
    runtime.config.loop_control.budget_nudge_ratio = 0.75
    runtime.config.goal.auto_continue = True
    soul = _make_soul(runtime, tmp_path)
    soul._session_cost_usd = 0.70

    async def fake_agent_loop() -> TurnOutcome:
        soul._session_cost_usd = 0.80
        final_message = Message(role="assistant", content=[TextPart(text="goal continues")])
        await soul.context.append_message(final_message)
        return TurnOutcome(stop_reason="no_tool_calls", final_message=final_message, step_count=1)

    monkeypatch.setattr(soul, "_agent_loop", fake_agent_loop)
    monkeypatch.setattr(soul, "_checkpoint", AsyncMock())

    await soul.turn(Message(role="user", content=[TextPart(text="go")]))

    assert "goal continues" in soul.context.history[-1].extract_text(" ")
    assert "system-reminder" not in soul.context.history[-1].extract_text(" ")


@pytest.mark.asyncio
async def test_step_persists_assistant_message_when_tool_results_cancelled(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_step must persist the assistant message and a synthetic tool-result message when
    tool_results() is cancelled mid-await, so the next turn does not see unanswered
    tool calls (which providers reject)."""
    soul = _make_soul(runtime, tmp_path)

    tool_call = ToolCall(
        id="call-cancel-1",
        function=ToolCall.FunctionBody(name="Noop", arguments="{}"),
    )
    pending_future: asyncio.Future[ToolResult] = asyncio.get_event_loop().create_future()

    async def fake_pythinker_core_step(chat_provider, system_prompt, toolset, history, **kwargs):
        return StepResult(
            id="step-cancel-1",
            message=Message(role="assistant", content=[TextPart(text="I'll use a tool.")]),
            usage=None,
            tool_calls=[tool_call],
            _tool_result_futures={"call-cancel-1": pending_future},
        )

    async def fake_collect_injections() -> list[DynamicInjection]:
        return []

    monkeypatch.setattr(soul, "_collect_injections", fake_collect_injections)
    monkeypatch.setattr(pythinkersoul_module.pythinker_core, "step", fake_pythinker_core_step)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _msg: None)

    # Run _step in a task and cancel it while it is blocked in tool_results()
    step_task = asyncio.create_task(soul._step())
    # Yield enough times for the task to reach `await result.tool_results()` which
    # then blocks on the pending_future.
    for _ in range(10):
        await asyncio.sleep(0)
    step_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await step_task

    # Allow any asyncio.shield()-wrapped grow_context_task to complete.
    for _ in range(10):
        await asyncio.sleep(0)

    history = list(soul.context.history)
    roles = [m.role for m in history]
    assert "assistant" in roles, f"assistant message not persisted; history={history}"
    tool_messages = [m for m in history if m.role == "tool"]
    assert tool_messages, f"no synthetic tool result message persisted; history={history}"
    assert tool_messages[0].tool_call_id == tool_call.id, (
        f"tool message has wrong tool_call_id; "
        f"expected={tool_call.id}, got={tool_messages[0].tool_call_id}"
    )


async def test_step_persists_markers_when_cancelled_twice(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second interrupt while the shielded marker write is in flight must not
    orphan the write: by the time the cancellation propagates out of _step, the
    assistant message and synthetic tool results are already persisted (the old
    single-shot shield let the write task detach and race the next turn)."""
    soul = _make_soul(runtime, tmp_path)

    tool_call = ToolCall(
        id="call-cancel-2",
        function=ToolCall.FunctionBody(name="Noop", arguments="{}"),
    )
    pending_future: asyncio.Future[ToolResult] = asyncio.get_event_loop().create_future()

    async def fake_pythinker_core_step(chat_provider, system_prompt, toolset, history, **kwargs):
        return StepResult(
            id="step-cancel-2",
            message=Message(role="assistant", content=[TextPart(text="I'll use a tool.")]),
            usage=None,
            tool_calls=[tool_call],
            _tool_result_futures={"call-cancel-2": pending_future},
        )

    async def fake_collect_injections() -> list[DynamicInjection]:
        return []

    real_grow = soul._grow_context
    write_started = asyncio.Event()

    async def slow_grow(result, results):
        write_started.set()
        for _ in range(5):
            await asyncio.sleep(0)
        await real_grow(result, results)

    monkeypatch.setattr(soul, "_collect_injections", fake_collect_injections)
    monkeypatch.setattr(soul, "_grow_context", slow_grow)
    monkeypatch.setattr(pythinkersoul_module.pythinker_core, "step", fake_pythinker_core_step)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _msg: None)

    step_task = asyncio.create_task(soul._step())
    for _ in range(10):
        await asyncio.sleep(0)
    step_task.cancel()
    await write_started.wait()
    step_task.cancel()  # second interrupt lands mid marker-write

    with pytest.raises(asyncio.CancelledError):
        await step_task

    # No further loop yields: the settle loop must have completed the write
    # BEFORE the cancellation propagated.
    history = list(soul.context.history)
    tool_messages = [m for m in history if m.role == "tool"]
    assert tool_messages, f"marker write was orphaned; history={history}"
    assert tool_messages[0].tool_call_id == tool_call.id


@pytest.mark.asyncio
async def test_token_budget_nudge_does_not_fire_when_under_threshold(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.config.loop_control.max_session_cost_usd = 10.0
    runtime.config.goal.auto_continue = False
    soul = _make_soul(runtime, tmp_path)
    soul._session_cost_usd = 0.5

    async def fake_agent_loop() -> TurnOutcome:
        return TurnOutcome(
            stop_reason="no_tool_calls",
            final_message=Message(role="assistant", content=[TextPart(text="done")]),
            step_count=1,
        )

    async def fake_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_agent_loop", fake_agent_loop)
    monkeypatch.setattr(soul, "_checkpoint", fake_checkpoint)

    before = len(soul.context.history)
    await soul.turn(Message(role="user", content=[TextPart(text="hello")]))
    assert len(soul.context.history) == before + 1
    assert not any("spend ceiling" in message.extract_text(" ") for message in soul.context.history)
