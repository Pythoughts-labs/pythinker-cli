"""Failure-threshold escalation (obs-eval-5).

When a model gets stuck in a degenerate loop where every tool call fails, the
agent loop should yield to the human after `max_consecutive_failures` consecutive
all-error steps — stopping with a `stuck` turn outcome and a handoff summary —
instead of burning steps until the blunt `max_steps_per_turn` cap.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from pythinker_core.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from pythinker_core.message import Message, TextPart, ToolCall
from pythinker_core.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pythinker_core.tooling.simple import SimpleToolset

from pythinker_code.llm import LLM
from pythinker_code.soul import run_soul
from pythinker_code.soul.agent import Agent, BuiltinSystemPromptArgs, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul, TurnStopReason
from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.utils.aioqueue import QueueShutDown
from pythinker_code.wire import Wire


class _StaticStreamedMessage:
    def __init__(
        self, parts: Sequence[StreamedMessagePart], finish_reason: str | None = None
    ) -> None:
        self._iter = self._to_stream(parts)
        self._finish_reason = finish_reason

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _to_stream(
        self, parts: Sequence[StreamedMessagePart]
    ) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part

    @property
    def id(self) -> str | None:
        return "stuck-loop"

    @property
    def usage(self) -> TokenUsage | None:
        return None

    @property
    def finish_reason(self) -> str | None:
        return self._finish_reason


class _ScriptedToolCallProvider:
    """Emits one tool call (or final text) per step from a fixed script.

    Each script entry is a tool name to call, or ``None`` to emit a tool-call-free
    text message (which ends the turn normally).
    """

    name = "scripted-tool-call"

    def __init__(self, script: Sequence[str | None], truncated_steps: Sequence[int] = ()) -> None:
        self._script = list(script)
        self._truncated_steps = set(truncated_steps)
        self.generate_attempts = 0

    @property
    def model_name(self) -> str:
        return "scripted-tool-call"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[object],
        history: Sequence[Message],
    ) -> _StaticStreamedMessage:
        index = self.generate_attempts
        self.generate_attempts += 1
        finish_reason = "length" if index in self._truncated_steps else None
        entry = self._script[index] if index < len(self._script) else None
        if entry is None:
            return _StaticStreamedMessage([TextPart(text="done")], finish_reason)
        return _StaticStreamedMessage(
            [ToolCall(id=f"c{index}", function=ToolCall.FunctionBody(name=entry, arguments="{}"))],
            finish_reason,
        )

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


class _NoParams(BaseModel):
    pass


class _BoomTool(CallableTool2[_NoParams]):
    name: str = "Boom"
    description: str = "Always fails."
    params: type[_NoParams] = _NoParams

    async def __call__(self, params: _NoParams) -> ToolReturnValue:
        return ToolError(message="boom", brief="boom")


class _OkTool(CallableTool2[_NoParams]):
    name: str = "Ok"
    description: str = "Always succeeds."
    params: type[_NoParams] = _NoParams

    async def __call__(self, params: _NoParams) -> ToolReturnValue:
        return ToolOk(output="ok", message="ok")


def _rebuild_runtime_with_llm(runtime: Runtime, llm: LLM) -> Runtime:
    return Runtime(
        config=runtime.config,
        llm=llm,
        session=runtime.session,
        builtin_args=runtime.builtin_args,
        denwa_renji=runtime.denwa_renji,
        approval=runtime.approval,
        labor_market=runtime.labor_market,
        environment=runtime.environment,
        notifications=runtime.notifications,
        background_tasks=runtime.background_tasks,
        skill_catalog=runtime.skill_catalog,
        skills=runtime.skills,
        oauth=runtime.oauth,
        additional_dirs=runtime.additional_dirs,
        skills_dirs=runtime.skills_dirs,
        role=runtime.role,
    )


def _make_soul(
    runtime: Runtime, provider: _ScriptedToolCallProvider, tmp_path: Path
) -> tuple[Context, PythinkerSoul]:
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    runtime = _rebuild_runtime_with_llm(runtime, llm)
    agent = Agent(
        name="Stuck Test Agent",
        system_prompt="Stuck test prompt.",
        toolset=SimpleToolset([_BoomTool(), _OkTool()]),
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    soul = PythinkerSoul(agent, context=context)
    return context, soul


def _make_soul_with_pythinker_toolset(
    runtime: Runtime, provider: _ScriptedToolCallProvider, tmp_path: Path
) -> tuple[Context, PythinkerSoul]:
    """Like `_make_soul`, but with a real `PythinkerToolset` — required to exercise the
    identical-call repeat backstop, which is tracked on `PythinkerToolset` specifically."""
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    runtime = _rebuild_runtime_with_llm(runtime, llm)
    toolset = PythinkerToolset()
    toolset.add(_BoomTool())
    toolset.add(_OkTool())
    agent = Agent(
        name="Stuck Test Agent",
        system_prompt="Stuck test prompt.",
        toolset=toolset,
        runtime=runtime,
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    soul = PythinkerSoul(agent, context=context)
    return context, soul


async def _drain_ui_messages(wire: Wire) -> None:
    wire_ui = wire.ui_side(merge=True)
    while True:
        try:
            await wire_ui.receive()
        except QueueShutDown:
            return


async def _collect_ui_messages(wire: Wire, seen: list[object]) -> None:
    wire_ui = wire.ui_side(merge=True)
    while True:
        try:
            seen.append(await wire_ui.receive())
        except QueueShutDown:
            return


@pytest.mark.asyncio
async def test_consecutive_failures_yield_stuck_outcome(runtime: Runtime, tmp_path: Path) -> None:
    """N consecutive all-error steps stop the turn with `stuck`, not max_steps."""
    runtime.config.loop_control.max_consecutive_failures = 3
    runtime.config.loop_control.max_steps_per_turn = 50
    provider = _ScriptedToolCallProvider(["Boom"] * 10)
    context, soul = _make_soul(runtime, provider, tmp_path)

    with patch("pythinker_code.telemetry.metrics.record_turn") as record_turn:
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    # Stopped at the failure threshold, well before max_steps_per_turn.
    assert provider.generate_attempts == 3
    assert record_turn.call_args.kwargs["stop_reason"] == "stuck"
    # The final assistant message is a handoff summary mentioning being stuck.
    assert "stuck" in context.history[-1].extract_text(" ").lower()


@pytest.mark.asyncio
async def test_a_successful_step_resets_the_failure_counter(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A productive (non-all-error) step resets the consecutive-failure counter."""
    runtime.config.loop_control.max_consecutive_failures = 3
    runtime.config.loop_control.max_steps_per_turn = 50
    # Boom, Boom, Ok (reset), Boom, Boom, then final text. Max run of failures is
    # 2 < 3, so the turn ends normally rather than `stuck`.
    provider = _ScriptedToolCallProvider(["Boom", "Boom", "Ok", "Boom", "Boom", None])
    context, soul = _make_soul(runtime, provider, tmp_path)

    with patch("pythinker_code.telemetry.metrics.record_turn") as record_turn:
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 6
    assert record_turn.call_args.kwargs["stop_reason"] == "no_tool_calls"


def test_stuck_summary_handles_whitespace_only_brief() -> None:
    """A tool error with a whitespace-only brief must not crash the handoff summary.

    ``(brief or message or "error").strip().splitlines()[0]`` raised IndexError when
    brief was non-empty whitespace: it is truthy, ``.strip()`` yields ``""``, and
    ``"".splitlines()`` is ``[]`` — exactly the all-error path this backstop exists for.
    """
    from pythinker_core.tooling import ToolError, ToolResult

    from pythinker_code.soul.pythinkersoul import _stuck_summary_message

    call = ToolCall(id="c0", function=ToolCall.FunctionBody(name="Boom", arguments="{}"))
    result = ToolResult(tool_call_id="c0", return_value=ToolError(message="", brief="   "))

    msg = _stuck_summary_message(3, [call], [result])

    text = msg.extract_text(" ")
    assert "stuck" in text.lower()
    assert "Boom" in text  # tool name still surfaced despite the empty brief


@pytest.mark.parametrize(
    ("truncated", "has_tool_calls", "recoveries", "limit", "expected"),
    [
        (True, False, 0, 3, True),  # truncated text, under budget -> nudge to continue
        (False, False, 0, 3, False),  # not truncated
        (True, True, 0, 3, False),  # has tool calls -> the loop continues via results anyway
        (True, False, 3, 3, False),  # recovery budget exhausted
    ],
)
def test_should_nudge_truncation(
    truncated: bool, has_tool_calls: bool, recoveries: int, limit: int, expected: bool
) -> None:
    from pythinker_code.soul.pythinkersoul import _should_nudge_truncation

    assert _should_nudge_truncation(truncated, has_tool_calls, recoveries, limit) is expected


def test_user_message_with_hook_context() -> None:
    """Non-block additional_context from UserPromptSubmit hooks is appended to the user
    turn as a system reminder; block results and empty context contribute nothing."""
    from pythinker_code.hooks.runner import HookResult
    from pythinker_code.soul.pythinkersoul import _user_message_with_hook_context

    plain = _user_message_with_hook_context("review the diff", [])
    assert "review the diff" in plain.extract_text(" ")
    assert "system-reminder" not in plain.extract_text(" ")

    enriched = _user_message_with_hook_context(
        "review the diff", [HookResult(additional_context="The repo uses pnpm, not npm.")]
    )
    text = enriched.extract_text(" ")
    assert "review the diff" in text
    assert "pnpm" in text
    # Hook stdout is external/untrusted: it must be wrapped in the untrusted-data envelope
    # (not left as bare trusted text), matching fetch/search/shell/grep ingress.
    assert "<untrusted_data" in text

    # The invisible-char smuggling vector (here a zero-width space) is stripped by the wrapper.
    zero_width_space = chr(0x200B)  # U+200B ZERO WIDTH SPACE
    smuggled = _user_message_with_hook_context(
        "review the diff",
        [HookResult(additional_context=f"benign{zero_width_space}text")],
    )
    assert zero_width_space not in smuggled.extract_text(" ")

    # A blocking result does not contribute context (block is handled separately).
    blocked = _user_message_with_hook_context(
        "review the diff", [HookResult(action="block", additional_context="should be ignored")]
    )
    assert "should be ignored" not in blocked.extract_text(" ")


def test_with_agents_md_preamble_prepends_authoritative_reminder(
    builtin_args: BuiltinSystemPromptArgs,
) -> None:
    """The merged AGENTS.md is prepended as a leading user-role <system-reminder>, ahead of
    the conversation, WITHOUT mutating context history — assembled fresh each step so it
    survives compaction (never persisted) and the injection budget (not a dynamic injection)."""
    from pythinker_code.soul.message import is_system_reminder_message
    from pythinker_code.soul.pythinkersoul import _with_agents_md_preamble

    history = [Message(role="user", content=[TextPart(text="hello")])]
    result = _with_agents_md_preamble(history, builtin_args)

    # A leading reminder is prepended; the original history follows it, by identity.
    assert len(result) == 2
    assert is_system_reminder_message(result[0])
    reminder_part = result[0].content[0]
    assert isinstance(reminder_part, TextPart)
    assert "Test agents content" in reminder_part.text
    assert result[1] is history[0]
    # The input list is never mutated (the preamble must not leak into context.history).
    assert history == [Message(role="user", content=[TextPart(text="hello")])]


def test_with_agents_md_preamble_absent_returns_history_unchanged(
    builtin_args: BuiltinSystemPromptArgs,
) -> None:
    """No AGENTS.md → history passes through unchanged (no empty preamble is injected)."""
    from dataclasses import replace

    from pythinker_code.soul.pythinkersoul import _with_agents_md_preamble

    empty = replace(builtin_args, PYTHINKER_AGENTS_MD="")
    history = [Message(role="user", content=[TextPart(text="hi")])]
    result = _with_agents_md_preamble(history, empty)
    assert result == history


def test_with_agents_md_preamble_normalizes_to_lead_the_first_user_turn(
    builtin_args: BuiltinSystemPromptArgs,
) -> None:
    """After history normalization the AGENTS.md reminder leads the first user message —
    a stable position-0 prefix (good for prompt-cache keying), not a stray extra turn."""
    from pythinker_code.soul.dynamic_injection import normalize_history
    from pythinker_code.soul.pythinkersoul import _with_agents_md_preamble

    history = [Message(role="user", content=[TextPart(text="first prompt")])]
    normalized = normalize_history(_with_agents_md_preamble(history, builtin_args))

    assert len(normalized) == 1
    text = "".join(p.text for p in normalized[0].content if isinstance(p, TextPart))
    assert text.index("Test agents content") < text.index("first prompt")


@pytest.mark.asyncio
async def test_agents_md_reaches_llm_but_is_never_persisted(
    runtime: Runtime, tmp_path: Path
) -> None:
    """End-to-end: the AGENTS.md preamble is delivered to the model on a step, yet is never
    written to context.history — the exact property that makes it immune to compaction (the
    compactor only ever rewrites persisted history) and to the dynamic-injection budget."""
    captured: list[Message] = []

    class _CapturingProvider(_ScriptedToolCallProvider):
        async def generate(
            self, system_prompt: str, tools: Sequence[object], history: Sequence[Message]
        ) -> _StaticStreamedMessage:
            captured.extend(history)
            return await super().generate(system_prompt, tools, history)

    # The conftest runtime carries PYTHINKER_AGENTS_MD="Test agents content".
    assert "Test agents content" in runtime.builtin_args.PYTHINKER_AGENTS_MD
    provider = _CapturingProvider([None])  # one step: emit final text, no tool calls
    context, soul = _make_soul(runtime, provider, tmp_path)

    await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    # The model saw the AGENTS.md preamble on this step...
    assert any("Test agents content" in m.extract_text(" ") for m in captured)
    # ...but it is absent from the persisted conversation, so nothing can summarize or
    # truncate it: it is re-derived from runtime state on every step instead.
    assert all("Test agents content" not in m.extract_text(" ") for m in context.history)


@pytest.mark.parametrize(
    ("stop_reason", "text", "expected"),
    [
        ("no_tool_calls", "Here is the result.", True),  # substantive answer
        ("no_tool_calls", "   ", False),  # empty/whitespace final message
        ("no_tool_calls", None, False),  # no final message
        ("stuck", "I appear to be stuck — handing back.", False),  # forced handoff
        ("budget_exhausted", "Stopping: spend ceiling reached.", False),  # forced handoff
        ("tool_rejected", None, False),  # rejected tool call, no answer
    ],
)
def test_turn_outcome_produced_answer(
    stop_reason: TurnStopReason, text: str | None, expected: bool
) -> None:
    """`produced_answer` is True only for a turn that ended with a substantive assistant
    answer — degenerate stops (stuck / budget / rejection / empty) are not completions."""
    from pythinker_code.soul.pythinkersoul import TurnOutcome

    message = Message(role="assistant", content=[TextPart(text=text)]) if text is not None else None
    outcome = TurnOutcome(stop_reason=stop_reason, final_message=message, step_count=1)
    assert outcome.produced_answer is expected


@pytest.mark.parametrize(
    ("cost", "ceiling", "expected"),
    [
        (0.0, None, False),  # no ceiling configured
        (5.0, None, False),
        (5.0, 0.0, False),  # non-positive ceiling is disabled
        (5.0, -1.0, False),
        (0.0, 1.0, False),  # unpriced model (cost 0.0) never blocks — fail open
        (0.99, 1.0, False),  # under the ceiling
        (1.0, 1.0, True),  # exactly at the ceiling
        (2.5, 1.0, True),  # over the ceiling
    ],
)
def test_is_over_cost_ceiling(cost: float, ceiling: float | None, expected: bool) -> None:
    from pythinker_code.soul.pythinkersoul import _is_over_cost_ceiling

    assert _is_over_cost_ceiling(cost, ceiling) is expected


@pytest.mark.asyncio
async def test_session_cost_ceiling_stops_turn(runtime: Runtime, tmp_path: Path) -> None:
    """Once accumulated session cost reaches the configured ceiling, the next turn
    stops with `budget_exhausted` before making another (paid) model call."""
    runtime.config.loop_control.max_session_cost_usd = 1.0
    runtime.config.loop_control.max_steps_per_turn = 50
    provider = _ScriptedToolCallProvider(["Ok"] * 10)
    context, soul = _make_soul(runtime, provider, tmp_path)
    soul._session_cost_usd = 5.0  # already over the ceiling from prior turns

    with patch("pythinker_code.telemetry.metrics.record_turn") as record_turn:
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    # Stopped at the ceiling, before any model call this turn.
    assert provider.generate_attempts == 0
    assert record_turn.call_args.kwargs["stop_reason"] == "budget_exhausted"
    assert "ceiling" in context.history[-1].extract_text(" ").lower()


@pytest.mark.asyncio
async def test_compaction_overspend_stops_before_next_step(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Proactive compaction makes its own billable LLM call. If that call pushes the
    session over the spend ceiling, the turn must stop *before* the following (paid)
    model step — not pay for compaction and then run a full step before the
    top-of-loop guard re-fires next iteration.

    The load-bearing assertion is ``generate_attempts == 0``: no model step ran after
    the billed compaction. On the old code (single ceiling check at the loop top) the
    extra ``_step()`` would run first (``generate_attempts == 1``), then the *next*
    iteration's guard would still stop with ``budget_exhausted`` — so the stop_reason
    and history-text checks below pass on both old and new code and only corroborate.
    """
    runtime.config.loop_control.max_session_cost_usd = 1.0
    runtime.config.loop_control.max_steps_per_turn = 50
    # Under the ceiling on entry (and > 0, so the entry guard passes for the right
    # reason — _is_over_cost_ceiling fails open at 0.0 for unpriced models).
    provider = _ScriptedToolCallProvider(["Ok"] * 10)
    context, soul = _make_soul(runtime, provider, tmp_path)
    soul._session_cost_usd = 0.5

    async def _fake_compact(*_args: object, **_kwargs: object) -> None:
        # The compaction LLM call's spend folds into the cumulative total, pushing
        # the session strictly over the ceiling.
        soul._session_cost_usd = 5.0

    with (
        patch("pythinker_code.soul.pythinkersoul.should_auto_compact", return_value=True),
        patch.object(soul, "compact_context", _fake_compact),
        patch("pythinker_code.telemetry.metrics.record_turn") as record_turn,
    ):
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    # No model step ran after the billed compaction (the fix; old code -> 1).
    assert provider.generate_attempts == 0
    assert record_turn.call_args.kwargs["stop_reason"] == "budget_exhausted"
    assert "ceiling" in context.history[-1].extract_text(" ").lower()


@pytest.mark.asyncio
async def test_no_cost_ceiling_does_not_stop(runtime: Runtime, tmp_path: Path) -> None:
    """With no ceiling configured (default None), accumulated cost never stops the turn."""
    runtime.config.loop_control.max_session_cost_usd = None
    runtime.config.loop_control.max_steps_per_turn = 50
    provider = _ScriptedToolCallProvider([None])  # ends normally on the first text step
    context, soul = _make_soul(runtime, provider, tmp_path)
    soul._session_cost_usd = 999.0

    with patch("pythinker_code.telemetry.metrics.record_turn") as record_turn:
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 1
    assert record_turn.call_args.kwargs["stop_reason"] == "no_tool_calls"


@pytest.mark.asyncio
async def test_max_consecutive_failures_zero_disables_backstop(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A threshold of 0 disables the backstop entirely."""
    runtime.config.loop_control.max_consecutive_failures = 0
    runtime.config.loop_control.max_steps_per_turn = 50
    provider = _ScriptedToolCallProvider(["Boom", "Boom", "Boom", "Boom", None])
    context, soul = _make_soul(runtime, provider, tmp_path)

    with patch("pythinker_code.telemetry.metrics.record_turn") as record_turn:
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    # Never escalated despite 4 consecutive failures; ran to the final text step.
    assert provider.generate_attempts == 5
    assert record_turn.call_args.kwargs["stop_reason"] == "no_tool_calls"


@pytest.mark.asyncio
async def test_consecutive_identical_calls_yield_stuck_outcome(
    runtime: Runtime, tmp_path: Path
) -> None:
    """N consecutive identical-argument tool calls stop the turn with `stuck`, even
    though every call reports success — this backstop is independent of the
    all-error check above, so it still catches a tool that falsely reports success
    on a call that made no progress."""
    runtime.config.loop_control.max_consecutive_identical_calls = 3
    runtime.config.loop_control.max_steps_per_turn = 50
    provider = _ScriptedToolCallProvider(["Ok"] * 10)
    context, soul = _make_soul_with_pythinker_toolset(runtime, provider, tmp_path)

    with patch("pythinker_code.telemetry.metrics.record_turn") as record_turn:
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 3
    assert record_turn.call_args.kwargs["stop_reason"] == "stuck"
    assert "identical" in context.history[-1].extract_text(" ").lower()


@pytest.mark.asyncio
async def test_max_consecutive_identical_calls_zero_disables_backstop(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A threshold of 0 disables the identical-call backstop entirely."""
    runtime.config.loop_control.max_consecutive_identical_calls = 0
    runtime.config.loop_control.max_steps_per_turn = 50
    provider = _ScriptedToolCallProvider(["Ok", "Ok", "Ok", "Ok", None])
    context, soul = _make_soul_with_pythinker_toolset(runtime, provider, tmp_path)

    with patch("pythinker_code.telemetry.metrics.record_turn") as record_turn:
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 5
    assert record_turn.call_args.kwargs["stop_reason"] == "no_tool_calls"


@pytest.mark.asyncio
async def test_truncated_response_nudges_continuation(runtime: Runtime, tmp_path: Path) -> None:
    """A response cut off by the output-token limit (no tool calls) nudges the model to
    continue instead of ending the turn with a half-finished answer."""
    runtime.config.loop_control.max_truncation_recoveries = 3
    runtime.config.loop_control.max_steps_per_turn = 10
    # Step 0: a truncated text response. Step 1: a normal text response that ends the turn.
    provider = _ScriptedToolCallProvider([None, None], truncated_steps=[0])
    context, soul = _make_soul(runtime, provider, tmp_path)

    with patch("pythinker_code.telemetry.metrics.record_turn") as record_turn:
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    # Truncation triggered a second model call (the continuation nudge); then it ended.
    assert provider.generate_attempts == 2
    assert record_turn.call_args.kwargs["stop_reason"] == "no_tool_calls"
    history_text = " ".join(m.extract_text(" ") for m in context.history)
    assert "Output token limit hit" in history_text


@pytest.mark.asyncio
async def test_truncation_recovery_disabled_at_zero(runtime: Runtime, tmp_path: Path) -> None:
    """max_truncation_recoveries=0 disables the nudge — a truncated text response ends the turn."""
    runtime.config.loop_control.max_truncation_recoveries = 0
    runtime.config.loop_control.max_steps_per_turn = 10
    provider = _ScriptedToolCallProvider([None], truncated_steps=[0])
    context, soul = _make_soul(runtime, provider, tmp_path)

    with patch("pythinker_code.telemetry.metrics.record_turn") as record_turn:
        await run_soul(soul, "go", _drain_ui_messages, asyncio.Event())

    assert provider.generate_attempts == 1  # no continuation nudge
    assert record_turn.call_args.kwargs["stop_reason"] == "no_tool_calls"


@pytest.mark.asyncio
async def test_stuck_loop_sends_summary_to_wire(runtime: Runtime, tmp_path: Path) -> None:
    """The stuck-loop handoff summary must be sent to the wire so the interactive
    shell displays it — not only appended to context."""
    runtime.config.loop_control.max_consecutive_failures = 3
    runtime.config.loop_control.max_steps_per_turn = 50
    provider = _ScriptedToolCallProvider(["Boom"] * 10)
    context, soul = _make_soul(runtime, provider, tmp_path)

    seen: list[object] = []
    with patch("pythinker_code.telemetry.metrics.record_turn"):
        await run_soul(soul, "go", lambda wire: _collect_ui_messages(wire, seen), asyncio.Event())

    text_parts = [msg for msg in seen if isinstance(msg, TextPart)]
    assert any("stuck" in tp.text.lower() for tp in text_parts), (
        "Expected a TextPart wire event containing 'stuck' but none was found"
    )


@pytest.mark.asyncio
async def test_budget_exhausted_sends_message_to_wire(runtime: Runtime, tmp_path: Path) -> None:
    """The budget-exhausted handoff message must be sent to the wire so the interactive
    shell displays it — not only appended to context."""
    runtime.config.loop_control.max_session_cost_usd = 1.0
    runtime.config.loop_control.max_steps_per_turn = 50
    provider = _ScriptedToolCallProvider(["Ok"] * 10)
    context, soul = _make_soul(runtime, provider, tmp_path)
    soul._session_cost_usd = 5.0  # already over the ceiling

    seen: list[object] = []
    with patch("pythinker_code.telemetry.metrics.record_turn"):
        await run_soul(soul, "go", lambda wire: _collect_ui_messages(wire, seen), asyncio.Event())

    text_parts = [msg for msg in seen if isinstance(msg, TextPart)]
    assert any("ceiling" in tp.text.lower() or "budget" in tp.text.lower() for tp in text_parts), (
        "Expected a TextPart wire event containing 'ceiling' or 'budget' but none was found"
    )
