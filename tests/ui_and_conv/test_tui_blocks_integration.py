"""Integration tests for the Pythinker _ToolCallBlock wiring.

Exercises the flag fallback contract: when ``style != "card"`` OR no renderer
is registered for the tool, the legacy worklog rendering must be used
unchanged.
"""

from __future__ import annotations

import pytest
from pythinker_core.message import ToolCall
from pythinker_core.tooling import BriefDisplayBlock, ToolReturnValue
from rich.console import RenderableType
from rich.text import Text

from pythinker_code.ui.shell.components import render_plain
from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ASSISTANT_MARKER
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
    clear_tool_renderers,
    register_tool_renderer,
)
from pythinker_code.ui.shell.visualize._blocks import _ToolCallBlock


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_tool_renderers()
    yield
    clear_tool_renderers()


@pytest.fixture
def _force_pythinker_style(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")


@pytest.fixture
def _force_card_style(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")


def _make_tool_call(name: str = "ReadFile", args: str | None = '{"path":"src/x.py"}') -> ToolCall:
    return ToolCall(
        id="t1",
        function=ToolCall.FunctionBody(name=name, arguments=args),
    )


def _ok_result(brief: str = "✓ 12 lines") -> ToolReturnValue:
    return ToolReturnValue(
        is_error=False,
        output="",
        message="",
        display=[BriefDisplayBlock(text=brief)],
    )


def _err_result(brief: str = "ENOENT") -> ToolReturnValue:
    return ToolReturnValue(
        is_error=True,
        output="",
        message=brief,
        display=[BriefDisplayBlock(text=brief)],
    )


def _register_read_renderer():
    def render_call(ctx: ToolRenderContext) -> RenderableType:
        path = ctx.args.get("path", "?")
        line = Text("read ", style="bold")
        line.append(str(path), style="grey70")
        return line

    def render_result(_ctx: ToolRenderContext, r: ToolResultPayload) -> RenderableType | None:
        if not r.text:
            return None
        return Text(r.text, style="grey50")

    register_tool_renderer(
        ToolRenderDefinition(
            name="ReadFile",
            label="Read",
            render_call=render_call,
            render_result=render_result,
        )
    )


# ---------------------------------------------------------------------------
# Default style: legacy worklog rendering unchanged
# ---------------------------------------------------------------------------


def test_default_style_uses_legacy_rendering(_force_pythinker_style):
    _register_read_renderer()
    block = _ToolCallBlock(_make_tool_call())
    block.finish(_ok_result("✓ 12 lines"))
    rendered = render_plain(block.compose(), width=80)
    # Legacy path includes the worklog state token "completed" in plain text.
    assert "completed" in rendered
    # card uses "read " (lowercase) as the call header. Legacy uses "Read".
    assert "Read" in rendered


def test_card_style_with_registered_renderer_uses_card(_force_card_style):
    _register_read_renderer()
    block = _ToolCallBlock(_make_tool_call())
    block.finish(_ok_result("✓ 12 lines"))
    rendered = render_plain(block.compose(), width=80)
    # card output: lowercase "read" header from the registered renderer.
    assert "read src/x.py" in rendered
    # And the brief shows up as the result text.
    assert "12 lines" in rendered
    # Legacy worklog "completed" token must NOT appear on the card path.
    assert "completed" not in rendered


def test_card_style_without_specific_renderer_uses_generic(_force_card_style):
    """Under card style, tools without a specific renderer fall back to the
    generic card (not to the legacy worklog rendering)."""
    block = _ToolCallBlock(_make_tool_call(name="UnregisteredTool"))
    block.finish(_ok_result("done"))
    rendered = render_plain(block.compose(), width=80)
    # Generic renderer header includes the tool name + the brief result.
    assert "UnregisteredTool" in rendered
    assert "done" in rendered
    # And the legacy worklog "completed" token must not appear.
    assert "completed" not in rendered


def test_card_style_streaming_args_then_result(_force_card_style):
    _register_read_renderer()
    # Start with no args.
    tc = ToolCall(id="t1", function=ToolCall.FunctionBody(name="ReadFile", arguments=None))
    block = _ToolCallBlock(tc)

    # Stream the JSON in pieces.
    block.append_args_part('{"path":')
    block.append_args_part('"src/streamed.py"}')
    block.finish(_ok_result("✓ 5 lines"))

    rendered = render_plain(block.compose(), width=80)
    assert "read src/streamed.py" in rendered
    assert "5 lines" in rendered


def test_card_style_streamed_shell_output_renders_before_result(_force_card_style):
    from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers

    register_builtin_renderers()
    block = _ToolCallBlock(_make_tool_call(name="Shell", args='{"command":"printf hi"}'))

    block.mark_execution_started()
    block.append_output_part("hi", stream="stdout")

    rendered = render_plain(block.compose(), width=80)
    assert "Bash" in rendered
    assert "hi" in rendered
    assert "esc to cancel" in rendered


def test_card_style_error_result(_force_card_style):
    _register_read_renderer()
    block = _ToolCallBlock(_make_tool_call())
    block.finish(_err_result("permission denied"))
    rendered = render_plain(block.compose(), width=80)
    assert "permission denied" in rendered


def test_card_style_running_subagent_uses_solid_circle(_force_card_style, monkeypatch):
    from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers

    register_builtin_renderers()
    monkeypatch.setattr("pythinker_code.ui.shell.motion.time.monotonic", lambda: 0.0)
    block = _ToolCallBlock(
        _make_tool_call(name="Agent", args='{"description":"Audit UI","prompt":"check"}')
    )
    block.mark_execution_started()
    rendered = render_plain(block.compose(), width=80)
    spinner_frames = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

    assert "Agent(" in rendered
    assert "Audit UI" in rendered
    assert TRANSCRIPT_ASSISTANT_MARKER in rendered
    assert not any(frame in rendered for frame in spinner_frames)

    block.finish(_ok_result("done"))
    finished = render_plain(block.compose(), width=80)
    assert not any(frame in finished for frame in spinner_frames)


def test_card_style_finished_subagent_shows_compact_result(_force_card_style, monkeypatch):
    from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers

    register_builtin_renderers()
    monkeypatch.setattr(
        "pythinker_code.ui.shell.components.tool_execution.time.monotonic", lambda: 0.0
    )
    block = _ToolCallBlock(
        _make_tool_call(name="Agent", args='{"description":"Audit UI","prompt":"check"}')
    )

    monkeypatch.setattr(
        "pythinker_code.ui.shell.components.tool_execution.time.monotonic", lambda: 36.6
    )
    block.finish(_ok_result("done"))
    rendered = render_plain(block.compose(), width=80)

    assert f"{TRANSCRIPT_ASSISTANT_MARKER} Agent(coder · Audit UI)" in rendered
    assert "⎿  done" in rendered
    assert "Agent finished" not in rendered


def test_card_style_completed_agent_shows_result_not_tools_rollup(_force_card_style):
    import json

    from pythinker_core.tooling import ToolOk

    from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers
    from pythinker_code.wire.types import ToolResult

    register_builtin_renderers()
    block = _ToolCallBlock(
        _make_tool_call(name="Agent", args='{"description":"Audit UI","prompt":"check"}')
    )
    long_result = "\n".join(f"detail line {index}" for index in range(40))
    block.finish(_ok_result(long_result))
    for index in range(3):
        call = ToolCall(
            id=f"sub-{index}",
            function=ToolCall.FunctionBody(
                name="Grep",
                arguments=json.dumps({"pattern": f"term{index}"}),
            ),
        )
        block.append_sub_tool_call(call)
        block.finish_sub_tool_call(ToolResult(tool_call_id=call.id, return_value=ToolOk(output="")))

    rendered = render_plain(block.compose(), width=120)
    lines = [line.rstrip() for line in rendered.splitlines()]
    # Blessed supersession: a completed single Agent renders its result body with an
    # expand affordance; the legacy per-tool rollup is gone and raw sub-tool payloads
    # never leak into the collapsed card.
    assert any("detail line 0" in line for line in lines)
    assert any("expand" in line.lower() and "ctrl" in line.lower() for line in lines)
    assert not any("tools:" in line for line in lines)
    for leaked in ("Grep", "term0", "term1", "term2"):
        assert leaked not in rendered


def test_card_style_running_task_output_uses_solid_circle(_force_card_style, monkeypatch):
    from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers

    register_builtin_renderers()
    monkeypatch.setattr("pythinker_code.ui.shell.motion.time.monotonic", lambda: 0.0)
    block = _ToolCallBlock(
        _make_tool_call(
            name="TaskOutput",
            args='{"task_id":"agent-123","block":true,"timeout":300}',
        )
    )
    block.mark_execution_started()
    rendered = render_plain(block.compose(), width=80)
    spinner_frames = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

    assert "TaskOutput(" in rendered
    assert "agent-123" in rendered
    assert TRANSCRIPT_ASSISTANT_MARKER in rendered
    assert not any(frame in rendered for frame in spinner_frames)


def test_card_style_running_subagent_marker_pulses(_force_card_style, monkeypatch):
    from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers

    register_builtin_renderers()
    monkeypatch.setattr("pythinker_code.ui.shell.motion.time.monotonic", lambda: 0.0)
    block = _ToolCallBlock(
        _make_tool_call(name="Agent", args='{"description":"Audit UI","prompt":"check"}')
    )
    block.mark_execution_started()

    first = render_plain(block.compose(), width=80)
    monkeypatch.setattr("pythinker_code.ui.shell.motion.time.monotonic", lambda: 0.9)
    second = render_plain(block.compose(), width=80)

    assert first != second
    assert "Agent(" in first
    assert TRANSCRIPT_ASSISTANT_MARKER in first
    assert "Agent(" in second
    assert TRANSCRIPT_ASSISTANT_MARKER not in second


def test_card_style_background_subagent_result_keeps_solid_circle(_force_card_style, monkeypatch):
    from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers

    register_builtin_renderers()
    monkeypatch.setattr("pythinker_code.ui.shell.motion.time.monotonic", lambda: 0.0)
    block = _ToolCallBlock(
        _make_tool_call(
            name="Agent",
            args=('{"description":"background audit","prompt":"check","run_in_background":true}'),
        )
    )
    block.finish(
        _ok_result(
            "task_id: agent-123\n"
            "kind: agent\n"
            "status: running\n"
            "description: background audit\n"
            "agent_id: a123\n"
        )
    )
    rendered = render_plain(block.compose(), width=80)
    spinner_frames = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

    assert "background subagent working" in rendered
    assert "background audit" in rendered
    assert "status: running" in rendered
    assert TRANSCRIPT_ASSISTANT_MARKER in rendered
    assert not any(frame in rendered for frame in spinner_frames)


def test_card_style_background_subagent_marker_pulses(_force_card_style, monkeypatch):
    from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers

    register_builtin_renderers()
    monkeypatch.setattr("pythinker_code.ui.shell.motion.time.monotonic", lambda: 0.0)
    block = _ToolCallBlock(
        _make_tool_call(
            name="Agent",
            args=('{"description":"background audit","prompt":"check","run_in_background":true}'),
        )
    )
    block.finish(
        _ok_result(
            "task_id: agent-123\n"
            "kind: agent\n"
            "status: running\n"
            "description: background audit\n"
            "agent_id: a123\n"
        )
    )

    first = render_plain(block.compose(), width=80)
    monkeypatch.setattr("pythinker_code.ui.shell.motion.time.monotonic", lambda: 0.9)
    second = render_plain(block.compose(), width=80)

    assert first != second
    assert "background subagent working" in first
    assert f"⎿  {TRANSCRIPT_ASSISTANT_MARKER} background subagent working" in first
    assert "background subagent working" in second
    assert "⎿    background subagent working" in second


def test_card_style_lifecycle_marks_execution_started(_force_card_style):
    """ToolExecutionStarted should mark the card running before the result arrives."""
    seen = {"execution_started": False, "args_complete": False}

    def render_call(ctx: ToolRenderContext):
        seen["execution_started"] = ctx.execution_started
        seen["args_complete"] = ctx.args_complete
        return Text("ok")

    register_tool_renderer(
        ToolRenderDefinition(
            name="ReadFile",
            label="Read",
            render_call=render_call,
        )
    )
    block = _ToolCallBlock(_make_tool_call())
    render_plain(block.compose(), width=40)
    assert seen["execution_started"] is False
    assert seen["args_complete"] is False

    block.mark_execution_started()
    render_plain(block.compose(), width=40)
    assert seen["execution_started"] is True
    assert seen["args_complete"] is True

    # After the result lands, args_complete should remain set.
    block.finish(_ok_result("done"))
    render_plain(block.compose(), width=40)
    assert seen["args_complete"] is True


def test_card_style_renderer_crash_does_not_break_block(_force_card_style):
    def render_call(_ctx: ToolRenderContext) -> RenderableType:
        raise RuntimeError("boom")

    register_tool_renderer(
        ToolRenderDefinition(
            name="ReadFile",
            label="ReadFile",
            render_call=render_call,
        )
    )
    block = _ToolCallBlock(_make_tool_call())
    # Should not raise — the component falls back to a plain label.
    rendered = render_plain(block.compose(), width=80)
    assert "ReadFile" in rendered
