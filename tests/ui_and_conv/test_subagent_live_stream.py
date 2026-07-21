"""Integration tests for subagent ToolOutputPart and ToolExecutionStarted wiring."""

from __future__ import annotations

import pytest
from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolOk
from rich.console import Console, RenderableType

from pythinker_code.ui.shell.visualize import _LiveView
from pythinker_code.wire.types import (
    StatusUpdate,
    SubagentEvent,
    ToolCallPart,
    ToolExecutionStarted,
    ToolOutputPart,
    ToolResult,
    TurnBegin,
)
from pythinker_code.wire.types import (
    ToolCall as WireToolCall,
)


def _render(view: _LiveView, *, width: int = 100) -> str:
    console = Console(width=width, record=True, highlight=False, color_system=None)
    console.print(view.compose())
    return console.export_text()


def _agent_call(call_id: str = "agent-1") -> WireToolCall:
    return WireToolCall(
        id=call_id,
        function=WireToolCall.FunctionBody(
            name="Agent",
            arguments='{"description":"security scan","subagent_type":"security-reviewer","prompt":"check it"}',
        ),
    )


def _sub_tool_call(sub_id: str, name: str, args: str) -> ToolCall:
    return ToolCall(
        id=sub_id,
        function=ToolCall.FunctionBody(name=name, arguments=args),
    )


def test_subagent_tool_output_part_appears_in_live_view():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_agent_call())

    sub_call = _sub_tool_call("sub-1", "Bash", '{"command":"grep -r TODO ."}')
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=sub_call,
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolOutputPart(tool_call_id="sub-1", text="src/app.py:42: # TODO\n"),
        )
    )

    output = _render(view)
    assert "src/app.py:42" in output


def test_subagent_tool_execution_started_tracked():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_agent_call())

    sub_call = _sub_tool_call("sub-1", "Read", '{"file_path":"src/app.py"}')
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=sub_call,
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolExecutionStarted(tool_call_id="sub-1"),
        )
    )

    block = view._tool_call_blocks["agent-1"]
    assert "sub-1" in block._subagent_execution_started


def test_subagent_tool_call_and_args_request_live_refresh():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_agent_call())

    view._need_recompose = False
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=_sub_tool_call("sub-1", "Read", '{"file_path":"src/'),
        )
    )
    assert view._need_recompose is True

    view._need_recompose = False
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolCallPart(arguments_part='app.py"}'),
        )
    )
    assert view._need_recompose is True
    assert "src/app.py" in _render(view)


def test_output_part_for_unknown_parent_renders_fallback_without_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    emitted: list[RenderableType] = []
    from pythinker_code.ui.shell.visualize import _live_view as live_view_module

    monkeypatch.setattr(
        live_view_module,
        "emit_scrollback_block",
        lambda _console, renderable: emitted.append(renderable),
    )
    # No agent tool call dispatched — parent_tool_call_id won't resolve
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="nonexistent-agent",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolOutputPart(tool_call_id="sub-1", text="should be ignored\n"),
        )
    )
    # Must not raise or expose the nested payload; a muted fallback is emitted.
    output = _render(view)
    assert "should be ignored" not in output
    assert len(emitted) == 1
    console = Console(width=100, record=True, highlight=False, color_system=None)
    console.print(emitted[0])
    fallback = console.export_text()
    assert "Nested subagent activity unavailable" in fallback
    assert "should be ignored" not in fallback


def test_output_cleared_after_sub_tool_call_finishes():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_agent_call())

    sub_call = _sub_tool_call("sub-1", "Bash", '{"command":"ls"}')
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=sub_call,
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolOutputPart(tool_call_id="sub-1", text="SHOULD_DISAPPEAR\n"),
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolResult(tool_call_id="sub-1", return_value=ToolOk(output="")),
        )
    )

    output = _render(view)
    assert "SHOULD_DISAPPEAR" not in output
