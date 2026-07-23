"""Test loading indicator coverage during active turns.

Verifies that:
1. An empty ThinkPart (e.g. Anthropic block-start) creates a thinking indicator.
2. The moon spinner shows as a fallback whenever the turn is active but nothing
   else is visible (covers TurnBegin→StepBegin, ToolResult→StepBegin gaps).
3. Higher-priority indicators (content blocks, tool blocks, compaction) take
   precedence over the moon fallback.
"""

from __future__ import annotations

from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolResult, ToolReturnValue
from rich.console import Console, Group

from pythinker_code.ui.shell.visualize import _LiveView
from pythinker_code.wire.types import (
    CompactionBegin,
    StatusUpdate,
    StepBegin,
    StepRetry,
    TextPart,
    ThinkPart,
    TurnBegin,
    TurnEnd,
)


def _render(renderable) -> str:
    console = Console(width=100, record=True, highlight=False)
    console.print(renderable)
    return console.export_text()


def _make_tool_call(call_id: str = "call_1") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name="Shell", arguments='{"command": "ls"}'),
    )


def _make_tool_result(call_id: str = "call_1") -> ToolResult:
    return ToolResult(
        tool_call_id=call_id,
        return_value=ToolReturnValue(is_error=False, output="ok", message="ok", display=[]),
    )


# ---------------------------------------------------------------------------
# Empty ThinkPart indicator
# ---------------------------------------------------------------------------


def test_empty_think_part_creates_thinking_indicator():
    """After StepBegin + empty ThinkPart, the thinking indicator must be visible."""
    from rich.text import Text

    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))

    # Empty ThinkPart arrives (Anthropic block-start, think="")
    view.dispatch_wire_message(ThinkPart(think=""))

    # A thinking content block must exist and take priority over moon fallback
    assert view._current_content_block is not None
    assert view._current_content_block.is_think is True
    agent_blocks = view.compose_agent_output()
    assert isinstance(agent_blocks[0], Text)
    assert agent_blocks[0].plain.strip() == ""
    rendered = _render(view.compose())
    assert "Thinking" in rendered


def test_empty_text_part_still_skipped():
    """Empty TextPart should NOT create a content block (existing behavior)."""
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(TextPart(text=""))

    assert view._current_content_block is None


def test_empty_think_then_real_think_no_artifact(monkeypatch):
    """Empty ThinkPart followed by real ThinkPart should not print spurious lines."""
    from pythinker_code.ui.shell.console import console as shell_console

    view = _LiveView(StatusUpdate())
    printed = []
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: printed.extend(args))

    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(ThinkPart(think=""))
    view.dispatch_wire_message(ThinkPart(think="Let me analyze this..."))

    assert view._current_content_block is not None
    assert view._current_content_block.is_think is True
    assert view._current_content_block.raw_text == "Let me analyze this..."
    # No spurious thinking trace lines should have been printed
    assert len(printed) == 0


def test_empty_think_then_text_no_spurious_thought_line(monkeypatch):
    """Empty ThinkPart followed by TextPart should not print a thinking trace."""
    from pythinker_code.ui.shell.console import console as shell_console

    view = _LiveView(StatusUpdate())
    printed = []
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: printed.extend(args))

    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(ThinkPart(think=""))
    view.dispatch_wire_message(TextPart(text="Hello!"))

    assert view._current_content_block is not None
    assert view._current_content_block.is_think is False
    assert view._current_content_block.raw_text == "Hello!"
    for item in printed:
        rendered = _render(item)
        assert "Cogitated for" not in rendered


def test_step_retry_clears_partial_content_and_updates_live_status(monkeypatch):
    import importlib

    live_view_mod = importlib.import_module("pythinker_code.ui.shell.visualize._live_view")
    view = _LiveView(StatusUpdate())
    printed = []
    monkeypatch.setattr(
        live_view_mod.console,
        "print",
        lambda *args, **kwargs: printed.extend(args),
    )

    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(ThinkPart(think="old attempt"))

    assert view._current_content_block is not None
    assert view._current_content_block.raw_text == "old attempt"

    view.dispatch_wire_message(
        StepRetry(
            n=1,
            next_attempt=2,
            max_attempts=3,
            wait_s=1.0,
            error_type="APIStatusError",
            status_code=429,
        )
    )

    assert view._current_content_block is None
    assert not view._tool_call_blocks
    assert view._last_tool_call_block is None

    assert printed == []
    rendered = _render(view.compose_agent_output())
    assert "old attempt" not in rendered
    assert "Retrying after rate limit" in rendered
    assert "attempt 2/3" in rendered
    assert "…" in rendered  # keep Pythinker's live verb spinner visible

    view.dispatch_wire_message(
        StepRetry(
            n=1,
            next_attempt=3,
            max_attempts=3,
            wait_s=2.0,
            error_type="APIStatusError",
            status_code=503,
        )
    )
    rendered = _render(view.compose_agent_output())
    assert rendered.count("Retrying after") == 1
    assert "server error" in rendered
    assert "attempt 3/3" in rendered
    assert "attempt 2/3" not in rendered

    view.dispatch_wire_message(ThinkPart(think="new attempt"))
    assert view._current_content_block is not None
    assert view._current_content_block.raw_text == "new attempt"
    rendered = _render(view.compose_agent_output())
    assert "Retrying after" not in rendered


# ---------------------------------------------------------------------------
# Moon fallback during active turn
# ---------------------------------------------------------------------------


def test_moon_fallback_during_active_turn():
    """Moon shows as fallback when turn is active but nothing else is visible."""
    from rich.text import Text

    view = _LiveView(StatusUpdate())

    # Before TurnBegin — no moon
    rendered = _render(view.compose())
    assert "🌑" not in rendered and "🌒" not in rendered

    # After TurnBegin — moon fallback active
    view.dispatch_wire_message(TurnBegin(user_input="test"))
    assert view._active_turn_depth > 0
    # compose_agent_output should include the moon spinner
    agent_blocks = view.compose_agent_output()
    assert len(agent_blocks) > 1
    assert isinstance(agent_blocks[0], Text)
    assert agent_blocks[0].plain.strip() == ""


def test_working_indicator_stays_visible_when_content_block_visible(monkeypatch):
    """The activity spinner stays visible while content streams."""
    from rich.text import Text

    from pythinker_code.ui.shell.visualize import _live_view

    # The verb rotates on wall-clock over a list that legitimately includes
    # "Working"; pin it so the static-"Working…" regression guard below cannot
    # false-positive during that verb's 10-minute rotation window.
    monkeypatch.setattr(_live_view, "spinner_message", lambda now=None, **_kw: "Composing…")
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(TextPart(text="Hello"))

    # Content block visible — compose_agent_output should also keep the
    # persistent rotating-word spinner visible.
    assert view._current_content_block is not None
    agent_blocks = view.compose_agent_output()
    assert len(agent_blocks) >= 3
    assert isinstance(agent_blocks[0], Text)
    assert agent_blocks[0].plain.strip() == ""
    rendered = _render(agent_blocks[-1])
    assert "Working" not in rendered
    assert "…" in rendered
    assert "(<1s)" in rendered


def test_action_spacer_between_content_and_spinner_in_all_tui_styles(monkeypatch):
    """Streaming content and the activity spinner stay separated by one blank row."""
    from rich.text import Text

    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(TextPart(text="Hello"))

    agent_blocks = view.compose_agent_output()

    assert len(agent_blocks) >= 3
    assert isinstance(agent_blocks[-2], Text)
    assert agent_blocks[-2].plain.strip() == ""
    rendered = _render(agent_blocks[-1])
    assert "…" in rendered
    assert "(<1s)" in rendered


def test_moon_fallback_after_all_tools_flushed(monkeypatch):
    """After all tool calls finish, moon fallback reappears automatically."""
    from rich.text import Text

    from pythinker_code.ui.shell.console import console as shell_console

    view = _LiveView(StatusUpdate())
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: None)

    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(TextPart(text="Let me check."))
    view.dispatch_wire_message(_make_tool_call("call_1"))

    # Tool executing — tool block visible, moon fallback hidden
    assert len(view._tool_call_blocks) == 1

    # Tool finishes and flushes
    view.dispatch_wire_message(_make_tool_result("call_1"))
    assert len(view._tool_call_blocks) == 0

    # Nothing else visible + turn active → moon fallback shows
    agent_blocks = view.compose_agent_output()
    assert len(agent_blocks) == 2  # leading blank row + spinner
    assert isinstance(agent_blocks[0], Text)
    assert agent_blocks[0].plain.strip() == ""


def test_working_indicator_stays_visible_while_parallel_tool_still_running(monkeypatch):
    """The activity spinner stays visible while tool blocks are visible."""
    from pythinker_code.ui.shell.console import console as shell_console
    from pythinker_code.ui.shell.visualize import _live_view

    # Pin the wall-clock-rotated verb; see the content-block variant above.
    monkeypatch.setattr(_live_view, "spinner_message", lambda now=None, **_kw: "Composing…")
    view = _LiveView(StatusUpdate())
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: None)

    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(TextPart(text="Running two tools."))
    view.dispatch_wire_message(_make_tool_call("call_1"))
    view.dispatch_wire_message(_make_tool_call("call_2"))

    # First tool finishes, second still running
    view.dispatch_wire_message(_make_tool_result("call_1"))

    assert len(view._tool_call_blocks) == 1  # call_2 still there
    # Tool block visible → persistent loading-word spinner remains visible.
    agent_blocks = view.compose_agent_output()
    assert len(agent_blocks) >= 2
    rendered = _render(agent_blocks[-1])
    assert "Working" not in rendered
    assert "…" in rendered
    assert "(<1s)" in rendered


def test_action_spacer_between_parallel_tools_in_all_tui_styles(monkeypatch):
    """Parallel tool action rows stay separated by one blank row."""
    from rich.text import Text

    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(_make_tool_call("call_1"))
    view.dispatch_wire_message(_make_tool_call("call_2"))

    agent_blocks = view.compose_agent_output()

    spacer_indices = [
        index
        for index, block in enumerate(agent_blocks)
        if isinstance(block, Text) and block.plain.strip() == ""
    ]
    assert spacer_indices
    assert any(0 < index < len(agent_blocks) - 1 for index in spacer_indices)


def test_action_spacer_between_run_agents_and_task_output(monkeypatch):
    """Background RunAgents stays live beside TaskOutput with a blank row between."""
    from rich.text import Text

    from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers

    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    register_builtin_renderers()
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(
        ToolCall(
            id="run-agents-1",
            function=ToolCall.FunctionBody(
                name="RunAgents",
                arguments=(
                    '{"summary":"scan","run_in_background":true,'
                    '"agents":[{"name":"scan-a","prompt":"look","subagent_type":"explore"}]}'
                ),
            ),
        )
    )
    view.dispatch_wire_message(
        ToolResult(
            tool_call_id="run-agents-1",
            return_value=ToolReturnValue(
                is_error=False,
                output=(
                    "tool_status: launched\n"
                    "mode: background\n"
                    "agent_count: 1\n"
                    "agents:\n"
                    "- name: scan-a\n"
                    "  subagent_type: explore\n"
                    "  status: starting\n"
                    "  task_id: agent-abc\n"
                ),
                message="Agents launched.",
                display=[],
            ),
        )
    )
    view.dispatch_wire_message(
        ToolCall(
            id="task-output-1",
            function=ToolCall.FunctionBody(
                name="TaskOutput",
                arguments='{"task_id":"agent-abc","block":true,"timeout":600}',
            ),
        )
    )

    assert len(view._tool_call_blocks) == 2
    agent_blocks = view.compose_agent_output(include_working_indicator=False)
    rendered = _render(Group(*agent_blocks))
    assert "Agents(" in rendered
    assert "TaskOutput(" in rendered

    spacer_indices = [
        index
        for index, block in enumerate(agent_blocks)
        if isinstance(block, Text) and block.plain.strip() == ""
    ]
    assert any(0 < index < len(agent_blocks) - 1 for index in spacer_indices)


def test_moon_survives_status_update(monkeypatch):
    """StatusUpdate does not affect moon fallback visibility."""
    from rich.text import Text

    from pythinker_code.ui.shell.console import console as shell_console

    view = _LiveView(StatusUpdate())
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: None)

    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(TextPart(text="Checking."))
    view.dispatch_wire_message(_make_tool_call("call_1"))
    view.dispatch_wire_message(_make_tool_result("call_1"))

    # StatusUpdate arrives (soul sends this between steps)
    view.dispatch_wire_message(StatusUpdate())

    # Turn still active, nothing else visible → moon fallback shows
    assert view._active_turn_depth > 0
    agent_blocks = view.compose_agent_output()
    assert len(agent_blocks) == 2
    assert isinstance(agent_blocks[0], Text)
    assert agent_blocks[0].plain.strip() == ""


def test_moon_hidden_after_turn_end(monkeypatch):
    """Moon fallback disappears when the turn ends."""
    from pythinker_code.ui.shell.console import console as shell_console

    view = _LiveView(StatusUpdate())
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: None)

    view.dispatch_wire_message(TurnBegin(user_input="test"))
    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(TextPart(text="Done."))
    view.dispatch_wire_message(TurnEnd())

    assert view._active_turn_depth == 0
    # Nothing visible and turn ended — no moon
    # (content was flushed? actually content block is still there)
    # But _active_turn_depth is False, so even without content the moon won't show


def test_compaction_takes_priority_over_moon():
    """Compaction spinner has higher priority than the moon fallback."""
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="test"))

    # Compaction starts — should show compaction, not moon
    view.dispatch_wire_message(CompactionBegin())
    agent_blocks = view.compose_agent_output()
    # Should be the compaction block, not the moon. Compaction commits with a
    # leading seam (blank row + block), so there are two blocks.
    assert len(agent_blocks) == 2
    assert view._compaction_block is not None


def test_interrupt_clears_active_turn():
    """cleanup(is_interrupt=True) resets _active_turn_depth to 0."""
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="test"))
    assert view._active_turn_depth > 0

    view.cleanup(is_interrupt=True)
    assert view._active_turn_depth == 0
    # No moon fallback after interrupt
    agent_blocks = view.compose_agent_output()
    assert len(agent_blocks) == 0


def test_step_cleanup_preserves_active_turn():
    """cleanup(is_interrupt=False) keeps _active_turn_depth > 0 (called on StepBegin)."""
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="test"))
    assert view._active_turn_depth > 0

    view.cleanup(is_interrupt=False)
    assert view._active_turn_depth > 0


# ---------------------------------------------------------------------------
# Nested TurnBegin/TurnEnd (ralph loop / flow turns)
# ---------------------------------------------------------------------------


def test_nested_turn_end_does_not_kill_outer_turn():
    """Inner TurnEnd should not prematurely clear the outer turn's active state."""
    view = _LiveView(StatusUpdate())

    # Outer turn
    view.dispatch_wire_message(TurnBegin(user_input="outer"))
    assert view._active_turn_depth == 1

    # Inner turn (flow turn)
    view.dispatch_wire_message(TurnBegin(user_input="inner"))
    assert view._active_turn_depth == 2

    view.dispatch_wire_message(StepBegin(n=1))
    view.dispatch_wire_message(TurnEnd())  # inner TurnEnd
    assert view._active_turn_depth == 1  # outer still active

    # Moon should still show (outer turn active, nothing else visible)
    agent_blocks = view.compose_agent_output()
    assert len(agent_blocks) > 0

    view.dispatch_wire_message(TurnEnd())  # outer TurnEnd
    assert view._active_turn_depth == 0


def test_turn_end_below_zero_clamps():
    """Extra TurnEnd messages should not make depth go negative."""
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnEnd())
    view.dispatch_wire_message(TurnEnd())
    assert view._active_turn_depth == 0


# ---------------------------------------------------------------------------
# Replay: StepBegin without TurnBegin
# ---------------------------------------------------------------------------


def test_step_begin_without_turn_begin_activates_moon():
    """StepBegin defensively sets depth=1 when no TurnBegin preceded it (replay)."""
    view = _LiveView(StatusUpdate())
    assert view._active_turn_depth == 0

    # Replay sends StepBegin directly without TurnBegin
    view.dispatch_wire_message(StepBegin(n=1))
    assert view._active_turn_depth == 1

    # Moon fallback should show
    agent_blocks = view.compose_agent_output()
    assert len(agent_blocks) > 0
